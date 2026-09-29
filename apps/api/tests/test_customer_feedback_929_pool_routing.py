from sqlalchemy import event, select

from apps.api.src.core.auth import Principal
from apps.api.src.core.models import Lead, Region, User
from apps.api.src.core.v12_enums import LeadSourceKind, LeadV12Status
from apps.api.src.services.dispatch_v12 import (
    has_receiver_coverage,
    receiver_coverage_for_leads,
    route_approved_lead_to_pool,
)
from apps.api.src.services.lead_supply_v12 import create_draft, submit_draft
from apps.api.src.services.public_pool_v12 import (
    list_public_pool_leads,
    rematch_no_receiver_public_pool_leads,
    transfer_public_pool_lead,
)
from apps.api.tests.test_customer_feedback_928_dispatch_pool import _pool_lead
from apps.api.tests.test_public_pool_auto_rematch import _receiver
from apps.api.tests.test_pre_dispatch_verification_http import _login


def test_platform_lead_without_receiver_enters_public_pool(db) -> None:
    operation = User(display_name="平台运营", status="ACTIVE")
    db.add(operation)
    db.flush()
    lead = _pool_lead(
        operation_id=operation.id,
        customer="平台待匹配客户",
        phone="13800139129",
        region_code="530102",
    )
    lead.status = LeadV12Status.PENDING_REVIEW.value
    db.add(lead)

    target = route_approved_lead_to_pool(db, lead)

    assert lead.source_kind == LeadSourceKind.PLATFORM_MANUAL.value
    assert target is LeadV12Status.PUBLIC_POOL
    assert lead.status == LeadV12Status.PUBLIC_POOL.value
    assert lead.pending_reason == "PUBLIC_POOL_NO_LOCAL_RECEIVER"


def test_platform_public_pool_lead_is_considered_for_rematch(db) -> None:
    operation = User(display_name="平台运营", status="ACTIVE")
    db.add(operation)
    db.flush()
    lead = _pool_lead(
        operation_id=operation.id,
        customer="平台公海客户",
        phone="13800139130",
        region_code="530102",
    )
    lead.status = LeadV12Status.PUBLIC_POOL.value
    lead.pending_reason = "PUBLIC_POOL_NO_LOCAL_RECEIVER"
    lead.source_channel = "MANUAL"
    db.add(lead)
    db.flush()

    result = rematch_no_receiver_public_pool_leads(db)

    assert result["scanned"] == 1
    assert result["blocked"] == 1
    assert result["errors"] == 0
    assert db.get(Lead, lead.id).status == LeadV12Status.PUBLIC_POOL.value
    visible, total = list_public_pool_leads(db, customer_source="OPERATION_ENTRY")
    assert total == 1
    assert visible[0].id == lead.id

    _receiver(db, code="PLATFORM-REMATCH", region_code="530102")
    result = rematch_no_receiver_public_pool_leads(db)

    assert result["transferred"] == 1
    assert result["errors"] == 0
    assert db.get(Lead, lead.id).status == LeadV12Status.READY_DISPATCH.value


def test_public_pool_list_uses_batch_receiver_check(api_client, monkeypatch) -> None:
    from apps.api.src.routers import v12_public_pool

    client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert operation is not None
        for suffix, phone in (("甲", "13800139131"), ("乙", "13800139132")):
            lead = _pool_lead(
                operation_id=operation.id,
                customer=f"同区域公海客户{suffix}",
                phone=phone,
                region_code="530102",
            )
            lead.status = LeadV12Status.PUBLIC_POOL.value
            lead.pending_reason = "PUBLIC_POOL_NO_LOCAL_RECEIVER"
            db.add(lead)
        db.commit()

    checked: list[int] = []
    original = v12_public_pool.receiver_coverage_for_leads

    def checked_batch(db, leads):
        checked.append(len(leads))
        return original(db, leads)

    monkeypatch.setattr(
        v12_public_pool,
        "receiver_coverage_for_leads",
        checked_batch,
    )
    _login(client, "operation", "Operation123!")
    response = client.get("/api/v1/v1.2/public-pool/leads", params={"keyword": "同区域公海客户"})
    assert response.status_code == 200, response.text
    assert len(response.json()["data"]["items"]) == 2
    assert checked == [2]


def test_receiver_coverage_batch_matches_single_check_with_bounded_queries(db) -> None:
    db.add_all([
        Region(code="420100", name="武汉市", level="CITY", aliases=[], active=True),
        Region(code="420102", name="江岸区", level="DISTRICT", parent_code="420100", aliases=[], active=True),
    ])
    db.flush()
    receiver = _receiver(db, code="BATCH-RECEIVER", region_code="420102")
    leads = [
        Lead(region_code="420102"),
        Lead(region_code="420102", supplier_company_id=receiver.id),
        Lead(region_code="420100"),
        *[Lead(region_code=f"999{index:03d}") for index in range(30)],
    ]
    expected = {
        (lead.region_code, lead.supplier_company_id): has_receiver_coverage(db, lead)
        for lead in leads
    }
    statements: list[str] = []

    def count_query(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db.get_bind(), "before_cursor_execute", count_query)
    try:
        actual = receiver_coverage_for_leads(db, leads)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", count_query)
    assert actual == expected
    assert len(statements) <= 4


def test_wechat_only_public_pool_lead_can_transfer_after_receiver_appears(db) -> None:
    operation = User(display_name="平台运营", status="ACTIVE")
    db.add(operation)
    db.add_all([
        Region(code="420100", name="武汉市", level="CITY", aliases=[], active=True),
        Region(code="420102", name="江岸区", level="DISTRICT", parent_code="420100", aliases=[], active=True),
    ])
    db.flush()
    principal = Principal(
        user_id=operation.id, display_name="平台运营", company_id=None,
        role_codes=frozenset({"OPERATION"}),
        permission_codes=frozenset({"lead.manual.manage"}), session_version=1,
    )
    lead = create_draft(
        db, principal=principal, source_kind=LeadSourceKind.PLATFORM_MANUAL,
        values={
            "customer_name": "仅微信客户", "customer_wechat": "wx_pool_929",
            "region_code": "420102", "city": "武汉市",
            "source_channel": "OTHER", "source_detail": "线下活动",
            "consent_confirmed": True,
        },
    )
    submit_draft(db, lead=lead, principal=principal)
    assert lead.status == LeadV12Status.PUBLIC_POOL.value

    _receiver(db, code="WECHAT-RECEIVER", region_code="420102")
    result = transfer_public_pool_lead(db, lead=lead, principal=principal)
    assert result.transferred is True
    assert lead.status == LeadV12Status.READY_DISPATCH.value
