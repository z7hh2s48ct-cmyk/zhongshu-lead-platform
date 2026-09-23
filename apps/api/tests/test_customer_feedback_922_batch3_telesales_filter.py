"""2026-09-22/23 客户反馈第三批之二（H1，对应 9.23 第 3 条）：

电销页搜索与筛选：客户姓名关键词、手机号精确搜索（需 lead.phone.export，
电销角色不带）、任务状态、电销人员、事实结论、核验参考时间区间、来源；
电销角色的本人限定不被筛选覆盖。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from apps.api.src.core.models import Lead, User, VerificationTask
from apps.api.src.core.security import encrypt_text, hash_phone
from apps.api.src.core.v12_enums import LeadV12Status
from apps.api.tests.test_pre_dispatch_verification_http import _login

API = "/api/v1/v1.2/pre-dispatch-verifications/tasks"


def _make_task(
    db,
    *,
    customer: str,
    phone: str,
    conclusion: str | None = None,
    assignee_id: str | None = None,
    source_kind: str = "PLATFORM_MANUAL",
    status: str = "PENDING",
    due_at: datetime | None = None,
) -> VerificationTask:
    lead = Lead(
        source_type=source_kind,
        source_kind=source_kind,
        customer_name=customer,
        phone_encrypted=encrypt_text(phone),
        phone_hash=hash_phone(phone),
        city="上海市",
        region_code="310101",
        category_code="OLD_RENOVATION",
        need_summary="确认装修需求真实性",
        consent_confirmed=True,
        status=LeadV12Status.PENDING_TELESALES_VERIFY.value,
        review_status="APPROVED",
        duplicate_status="CLEAR",
        raw_payload={},
    )
    db.add(lead)
    db.flush()
    task = VerificationTask(
        lead_id=lead.id,
        task_type="PRE_DISPATCH_VERIFY",
        status=status,
        assignee_user_id=assignee_id,
        assigned_at=datetime.now(timezone.utc),
        due_at=due_at or (datetime.now(timezone.utc) + timedelta(days=2)),
        verification_conclusion=conclusion,
    )
    db.add(task)
    db.commit()
    return task


def _task_ids(client, headers, **params) -> set[str]:
    response = client.get(API, headers=headers, params=params or None)
    assert response.status_code == 200, response.text
    return {item["id"] for item in response.json()["data"]["items"]}


def test_telesales_filters_narrow_results(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        telesales = db.scalar(select(User).where(User.username == "telesales"))
        assert operation is not None and telesales is not None
        hit = _make_task(
            db,
            customer="张筛选",
            phone="13900139101",
            conclusion="QUALIFIED",
            assignee_id=telesales.id,
        )
        other = _make_task(
            db,
            customer="李其他",
            phone="13900139102",
            conclusion="INVALID",
            assignee_id=telesales.id,
        )
        headers = _login(client, "operation", "Operation123!")

    ids = _task_ids(client, headers, keyword="张筛选")
    assert hit.id in ids and other.id not in ids

    ids = _task_ids(client, headers, conclusion="QUALIFIED")
    assert hit.id in ids and other.id not in ids

    ids = _task_ids(client, headers, assignee_user_id=telesales.id, keyword="张筛选")
    assert hit.id in ids and other.id not in ids

    ids = _task_ids(client, headers, source_kind="SUPPLIER_H5")
    assert hit.id not in ids and other.id not in ids


def test_phone_exact_search_gated_by_export_permission(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        telesales = db.scalar(select(User).where(User.username == "telesales"))
        hit = _make_task(db, customer="精确手机号客户", phone="13900139201")
        _make_task(db, customer="别的客户", phone="13900139202")
        operation_headers = _login(client, "operation", "Operation123!")
        telesales_headers = _login(client, "telesales", "Telesales123!")

    # 运营具备 lead.phone.export：完整手机号精确命中。
    ids = _task_ids(client, operation_headers, phone="13900139201")
    assert hit.id in ids and len(ids) == 1

    # 电销角色不带该权限：拒绝且列表不受影响。
    denied = client.get(API, headers=telesales_headers, params={"phone": "13900139201"})
    assert denied.status_code == 403, denied.text
    allowed = client.get(API, headers=telesales_headers)
    assert allowed.status_code == 200
    assert telesales.id is not None


def test_telesales_self_scope_not_overridden_by_filters(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        telesales = db.scalar(select(User).where(User.username == "telesales"))
        other = db.scalar(select(User).where(User.username == "operation"))
        mine = _make_task(db, customer="我的任务", phone="13900139301", assignee_id=telesales.id)
        theirs = _make_task(db, customer="别人的任务", phone="13900139302", assignee_id=other.id)
        headers = _login(client, "telesales", "Telesales123!")

    ids = _task_ids(client, headers)
    assert mine.id in ids and theirs.id not in ids

    # 电销试图指定他人电销人员筛选：结果仍被强制限定本人。
    ids = _task_ids(client, headers, assignee_user_id=other.id)
    assert theirs.id not in ids


def test_due_time_range_filter(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        near = _make_task(
            db,
            customer="近期任务",
            phone="13900139401",
            due_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        far = _make_task(
            db,
            customer="远期任务",
            phone="13900139402",
            due_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        headers = _login(client, "operation", "Operation123!")

    end = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    ids = _task_ids(client, headers, due_to=end)
    assert near.id in ids and far.id not in ids
