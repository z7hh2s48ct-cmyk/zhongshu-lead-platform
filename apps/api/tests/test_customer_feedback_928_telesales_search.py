"""2026-09-28 客户反馈第 2 条（电销 H5 搜索与来源）：

- 客户姓名模糊搜索与手机号后四位等值搜索共用 keyword 入口
  （4 位纯数字 → phone_tail4，其余 → 姓名模糊）；
- 按来源渠道（source_channel，如"直播"）筛选，任务卡可展示来源与脱敏号；
- phone_tail4 由服务层写入路径生成、由迁移对存量回填；
- 电销角色本人限定不被搜索/筛选覆盖。
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from apps.api.src.core.models import Lead, User, VerificationTask
from apps.api.src.core.security import encrypt_text, hash_phone, phone_tail4
from apps.api.src.core.v12_enums import LeadV12Status
from apps.api.src.services.lead_supply_v12 import _apply_editable_values
from apps.api.tests.test_customer_feedback_922_batch3_telesales_filter import (
    API,
    _task_ids,
)
from apps.api.tests.test_pre_dispatch_verification_http import _login

API = API  # 复用 922 批次的任务列表路径常量


def _make_task(
    db,
    *,
    customer: str,
    phone: str,
    source_channel: str | None = None,
    assignee_id: str | None = None,
    status: str = "PENDING",
) -> VerificationTask:
    now = datetime.now(timezone.utc)
    lead = Lead(
        source_type="PLATFORM_MANUAL",
        source_kind="PLATFORM_MANUAL",
        customer_name=customer,
        phone_encrypted=encrypt_text(phone),
        phone_hash=hash_phone(phone),
        phone_tail4=phone_tail4(phone),
        source_channel=source_channel,
        city="上海市",
        region_code="310101",
        category_code="OLD_RENOVATION",
        need_summary="928 电销搜索测试客资",
        consent_confirmed=True,
        status=LeadV12Status.PENDING_TELESALES_VERIFY.value,
        review_status="APPROVED",
        duplicate_status="CLEAR",
        imported_at=now,
        raw_payload={},
    )
    db.add(lead)
    db.flush()
    task = VerificationTask(
        lead_id=lead.id,
        task_type="PRE_DISPATCH_VERIFY",
        status=status,
        assignee_user_id=assignee_id,
        assigned_at=now,
        due_at=now + timedelta(days=2),
    )
    db.add(task)
    db.commit()
    return task


def _telesales_id(db) -> str:
    telesales = db.scalar(select(User).where(User.username == "telesales"))
    assert telesales is not None
    return telesales.id


def test_apply_editable_values_regenerates_phone_tail4(db) -> None:
    """服务层电话更正路径同步刷新 phone_tail4。"""

    now = datetime.now(timezone.utc)
    lead = Lead(
        source_type="PLATFORM_MANUAL",
        source_kind="PLATFORM_MANUAL",
        customer_name="尾号刷新客资",
        phone_encrypted=encrypt_text("13800001111"),
        phone_hash=hash_phone("13800001111"),
        phone_tail4="1111",
        status=LeadV12Status.DRAFT.value,
        review_status="DRAFT",
        raw_payload={},
        imported_at=now,
    )
    db.add(lead)
    db.commit()

    _apply_editable_values(db, lead, {"phone": "13999992222"})
    assert lead.phone_tail4 == "2222"


def test_phone_tail4_backfill_migration(db, monkeypatch) -> None:
    """迁移对存量客资回填后四位；解密失败（坏密文）保持 NULL。"""

    now = datetime.now(timezone.utc)
    good = Lead(
        source_type="PLATFORM_MANUAL",
        source_kind="PLATFORM_MANUAL",
        customer_name="存量可回填",
        phone_encrypted=encrypt_text("13866667777"),
        phone_hash=hash_phone("13866667777"),
        status=LeadV12Status.DRAFT.value,
        review_status="DRAFT",
        raw_payload={},
        imported_at=now,
    )
    bad = Lead(
        source_type="PLATFORM_MANUAL",
        source_kind="PLATFORM_MANUAL",
        customer_name="存量坏密文",
        phone_encrypted="not-a-fernet-token",
        phone_hash=hash_phone("13800000000"),
        status=LeadV12Status.DRAFT.value,
        review_status="DRAFT",
        raw_payload={},
        imported_at=now,
    )
    db.add_all([good, bad])
    db.commit()
    good_id, bad_id = good.id, bad.id

    path = (
        Path(__file__).resolve().parents[3]
        / "migrations/versions/0027_lead_phone_tail4.py"
    )
    spec = importlib.util.spec_from_file_location("lead_phone_tail4_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    monkeypatch.setattr(
        module, "op", Operations(MigrationContext.configure(db.connection()))
    )
    module.upgrade()
    db.expire_all()

    refreshed_good = db.get(Lead, good_id)
    refreshed_bad = db.get(Lead, bad_id)
    assert refreshed_good.phone_tail4 == "7777"
    assert refreshed_bad.phone_tail4 is None


def test_task_list_keyword_supports_tail4_and_name(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        assignee_id = _telesales_id(db)
        hit = _make_task(db, customer="张直播", phone="13911112222", assignee_id=assignee_id)
        other = _make_task(db, customer="李其他", phone="13933334444", assignee_id=assignee_id)
        hit_id, other_id = hit.id, other.id

    _login(client, "telesales", "Telesales123!")
    # 4 位纯数字 → 后四位等值匹配。
    assert _task_ids(client, {}, keyword="2222") == {hit_id}
    # 非纯数字/非 4 位 → 姓名模糊匹配照常工作。
    assert _task_ids(client, {}, keyword="张直播") == {hit_id}
    assert _task_ids(client, {}, keyword="李其他") == {other_id}
    # 无命中的后四位。
    assert _task_ids(client, {}, keyword="9999") == set()


def test_task_list_source_channel_filter_and_dto(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        assignee_id = _telesales_id(db)
        live = _make_task(
            db, customer="张直播", phone="13911112222", source_channel="LIVE", assignee_id=assignee_id
        )
        douyin = _make_task(
            db, customer="李抖音", phone="13933334444", source_channel="DOUYIN", assignee_id=assignee_id
        )
        live_id, douyin_id = live.id, douyin.id

    _login(client, "telesales", "Telesales123!")
    assert _task_ids(client, {}, source_channel="LIVE") == {live_id}
    assert _task_ids(client, {}, source_channel="live") == {live_id}
    assert _task_ids(client, {}, source_channel="DOUYIN") == {douyin_id}

    response = client.get(API, params={"source_channel": "LIVE"})
    assert response.status_code == 200, response.text
    item = response.json()["data"]["items"][0]
    assert item["lead"]["source_channel"] == "LIVE"
    # 默认来源字典中 LIVE → 直播。
    assert item["lead"]["source_channel_label"] == "直播"
    # 脱敏号保留后四位，供任务卡展示与后四位核对。
    assert item["lead"]["phone_masked"] == "139****2222"
    assert item["lead"]["phone"] is None  # 任务列表不泄露完整号码


def test_telesales_search_stays_within_own_tasks(api_client) -> None:
    """搜索与筛选不突破电销角色的本人任务限定。"""

    client, factory = api_client
    with factory() as db:
        mine_id = _telesales_id(db)
        other = db.scalar(select(User).where(User.username == "franchise_demo"))
        assert other is not None
        mine = _make_task(db, customer="共有姓名张某", phone="13911112222", assignee_id=mine_id)
        theirs = _make_task(db, customer="共有姓名张某", phone="13933334444", assignee_id=other.id)
        mine_id_task, theirs_id_task = mine.id, theirs.id

    _login(client, "telesales", "Telesales123!")
    # 两人任务同名同后四位也不跨人（此处后四位不同、姓名相同）。
    by_name = _task_ids(client, {}, keyword="共有姓名张某")
    assert by_name == {mine_id_task}
    assert theirs_id_task not in by_name
