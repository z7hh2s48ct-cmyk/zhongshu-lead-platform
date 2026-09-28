"""2026-09-28 客户反馈第 1+3 条（待人工派发池）：

第 1 条：派发池列表内联展示每条客资的可承接加盟商摘要
（dispatchable_names 前 3 家 + dispatchable_count 总数），
无可承接加盟商时 count=0，运营一眼可见"漏派"与"派不出去"的客资。

第 3 条：派发池关键词搜索——完整 11 位手机号走哈希精确匹配，
其余按客户姓名模糊匹配。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from apps.api.src.core.enums import AssignmentStatus
from apps.api.src.core.models import (
    Assignment,
    Company,
    Lead,
    PointsAccount,
    User,
)
from apps.api.src.core.models_v12 import CompanyLeadCapability, CompanyServiceAreaV12
from apps.api.src.core.security import encrypt_text, fingerprint_phone, hash_phone
from apps.api.src.core.v12_enums import LeadSourceKind, LeadV12Status
from apps.api.tests.test_v12_dispatch_http import _login

API = "/api/v1/v1.2/dispatch-pool"


def _pool_lead(*, operation_id: str, customer: str, phone: str, region_code: str = "310101") -> Lead:
    now = datetime.now(timezone.utc)
    return Lead(
        source_type=LeadSourceKind.PLATFORM_MANUAL.value,
        source_kind=LeadSourceKind.PLATFORM_MANUAL.value,
        submitter_user_id=operation_id,
        customer_name=customer,
        phone_encrypted=encrypt_text(phone),
        phone_hash=hash_phone(phone),
        phone_fingerprint=fingerprint_phone(phone),
        consent_confirmed=True,
        city="上海市",
        region_code=region_code,
        category_code="OLD_RENOVATION",
        brand_code="ZHONGSHU",
        need_summary="928 派发池测试客资",
        status=LeadV12Status.READY_DISPATCH.value,
        review_status="APPROVED",
        duplicate_status="CLEAR",
        imported_at=now,
        submitted_at=now,
        raw_payload={},
    )


def _ensure_receiver(db, *, code: str, name: str, districts: list[str]) -> Company:
    """确保存在一家可承接加盟商：ACTIVE + 承接能力 + 区域覆盖 + 积分充足。"""

    company = db.scalar(select(Company).where(Company.code == code))
    if company is None:
        company = Company(code=code, name=name, status="ACTIVE")
        db.add(company)
        db.flush()
    assert company is not None
    for district in districts:
        exists = db.scalar(
            select(CompanyServiceAreaV12.id).where(
                CompanyServiceAreaV12.company_id == company.id,
                CompanyServiceAreaV12.region_code == district,
            )
        )
        if not exists:
            db.add(
                CompanyServiceAreaV12(
                    company_id=company.id,
                    region_code=district,
                    region_level="DISTRICT" if district != "310000" else "CITY",
                    is_primary_city=district == "310000",
                    active=True,
                    review_status="APPROVED",
                )
            )
    if not db.scalar(
        select(CompanyLeadCapability.id).where(
            CompanyLeadCapability.company_id == company.id,
            CompanyLeadCapability.capability_code == "LEAD_RECEIVER",
        )
    ):
        db.add(
            CompanyLeadCapability(
                company_id=company.id,
                capability_code="LEAD_RECEIVER",
                active=True,
                review_status="APPROVED",
            )
        )
    account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == company.id))
    if account is None:
        db.add(PointsAccount(company_id=company.id, balance=5000))
    else:
        account.balance = 5000
    return company


def _pool_items(client, **params) -> list[dict]:
    response = client.get(API, params=params or None)
    assert response.status_code == 200, response.text
    return response.json()["data"]["items"]


def test_dispatch_pool_lists_dispatchable_companies(api_client) -> None:
    """可承接加盟商摘要：区域命中且积分足够的公司进入摘要，不匹配的不进入。"""

    client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert operation is not None
        hit = _ensure_receiver(db, code="SH-DEMO", name="上海演示加盟商", districts=["310101"])
        miss = _ensure_receiver(db, code="GD-NOPE", name="广州不覆盖加盟商", districts=["440103"])
        assert hit.code == "SH-DEMO" and miss.code == "GD-NOPE"
        receiver_name = hit.name  # 种子公司可能已存在，名称以实际为准。
        lead = _pool_lead(operation_id=operation.id, customer="张九二八", phone="13911112222")
        db.add(lead)
        db.commit()
        lead_id = lead.id

    _login(client, "operation", "Operation123!")
    items = _pool_items(client)
    target = next(item for item in items if item["id"] == lead_id)
    assert target["dispatchable_count"] == 1
    assert target["dispatchable_names"] == [receiver_name]


def test_dispatch_pool_flags_no_dispatchable_company(api_client) -> None:
    """无任何公司可承接的客资：count=0、names 为空，页面据此标红。"""

    client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert operation is not None
        _ensure_receiver(db, code="SH-DEMO", name="上海演示加盟商", districts=["310101"])
        lead = _pool_lead(
            operation_id=operation.id,
            customer="孤岛客资",
            phone="13800001111",
            region_code="530102",  # 昆明，无公司覆盖
        )
        db.add(lead)
        db.commit()
        lead_id = lead.id

    _login(client, "operation", "Operation123!")
    items = _pool_items(client)
    target = next(item for item in items if item["id"] == lead_id)
    assert target["dispatchable_count"] == 0
    assert target["dispatchable_names"] == []


def test_dispatch_pool_summary_excludes_returned_receiver(api_client) -> None:
    """曾领取后退回的公司不进入可承接摘要（与候选弹窗口径一致）。"""

    client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert operation is not None
        company = _ensure_receiver(db, code="SH-DEMO", name="上海演示加盟商", districts=["310101"])
        lead = _pool_lead(operation_id=operation.id, customer="退回黑名单客资", phone="13800002222")
        db.add(lead)
        db.flush()
        db.add(
            Assignment(
                lead_id=lead.id,
                company_id=company.id,
                receiver_company_id=company.id,
                status=AssignmentStatus.RETURNED.value,
                points_price=100,
                assigned_by=operation.id,
                assigned_at=datetime.now(timezone.utc) - timedelta(days=3),
                claim_points=100,
            )
        )
        db.commit()
        lead_id = lead.id

    _login(client, "operation", "Operation123!")
    items = _pool_items(client)
    target = next(item for item in items if item["id"] == lead_id)
    assert target["dispatchable_count"] == 0


def test_dispatch_pool_keyword_searches_name_and_full_phone(api_client) -> None:
    """第 3 条：完整 11 位号码精确匹配，其余按客户姓名模糊匹配。"""

    client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert operation is not None
        lead_a = _pool_lead(operation_id=operation.id, customer="张九二八", phone="13911112222")
        lead_b = _pool_lead(operation_id=operation.id, customer="李测试", phone="13933334444")
        db.add_all([lead_a, lead_b])
        db.commit()
        name_hit_id, phone_hit_id = lead_a.id, lead_b.id

    _login(client, "operation", "Operation123!")
    by_name = _pool_items(client, keyword="九二八")
    assert {item["id"] for item in by_name} == {name_hit_id}
    by_phone = _pool_items(client, keyword="13933334444")
    assert {item["id"] for item in by_phone} == {phone_hit_id}
    # 短数字串按姓名模糊处理，不应误当号码搜索，也不应报错。
    short_digits = _pool_items(client, keyword="139")
    assert {item["id"] for item in short_digits} == set()


def test_dispatch_pool_keyword_combines_with_region_filter(api_client) -> None:
    """搜索与既有地区筛选可叠加。"""

    client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert operation is not None
        lead_a = _pool_lead(operation_id=operation.id, customer="叠加筛选甲", phone="13811110001")
        lead_b = _pool_lead(
            operation_id=operation.id,
            customer="叠加筛选乙",
            phone="13811110002",
            region_code="530102",
        )
        db.add_all([lead_a, lead_b])
        db.commit()
        hit_id = lead_a.id

    _login(client, "operation", "Operation123!")
    combined = _pool_items(client, keyword="叠加筛选", region_code="310101")
    assert {item["id"] for item in combined} == {hit_id}
