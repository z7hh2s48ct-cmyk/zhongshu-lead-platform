from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from apps.api.src.core.auth import Principal
from apps.api.src.core.models import Lead, LeadExportTask, User
from apps.api.src.core.security import hash_phone
from apps.api.src.core.v12_enums import LeadSourceKind, LeadV12Status
from apps.api.src.routers.v12_insights import ScopedLeadExportRequestBody
from apps.api.src.schemas.v12_reports import LeadReportFilterBody
from apps.api.src.services.lead_export_v12 import (
    build_lead_export_workbook,
    list_lead_report_rows,
)
from apps.api.src.services.public_pool_v12 import (
    create_public_pool_lead,
    list_public_pool_leads,
    transfer_public_pool_lead,
    update_public_pool_lead,
)


def _operation(db) -> tuple[User, Principal]:
    user = User(display_name="九月十二日公海运营", status="ACTIVE")
    db.add(user)
    db.flush()
    return user, Principal(
        user_id=user.id,
        display_name=user.display_name,
        company_id=None,
        role_codes=frozenset({"OPERATION"}),
        permission_codes=frozenset({"lead.manual.manage", "lead.phone.export"}),
        session_version=1,
    )


def _create_lead(db, principal: Principal, *, phone: str, name: str) -> Lead:
    return create_public_pool_lead(
        db,
        principal=principal,
        values={
            "customer_name": name,
            "phone": phone,
            "region_code": "420100",
            "city": "武汉市",
            "source_channel": "OTHER",
            "source_detail": "九月十二日回归",
            "consent_confirmed": True,
        },
    )


def test_public_pool_phone_is_exact_hash_filter_and_combines_with_keyword(db) -> None:
    _, principal = _operation(db)
    matching = _create_lead(
        db,
        principal,
        phone="138 0013 8120",
        name="仙桃目标客户",
    )
    other = _create_lead(db, principal, phone="13800138121", name="仙桃其他客户")
    db.flush()

    items, total = list_public_pool_leads(
        db,
        phone_hash=hash_phone("13800138120"),
        keyword="仙桃",
        page_no=1,
        page_size=1,
    )

    assert total == 1
    assert [item.id for item in items] == [matching.id]

    all_items, all_total = list_public_pool_leads(db, phone_hash=None)
    assert all_total == 2
    assert {item.id for item in all_items} == {matching.id, other.id}


def test_public_pool_export_uses_phone_hash_without_persisting_plaintext(db) -> None:
    _, principal = _operation(db)
    _create_lead(db, principal, phone="13800138122", name="导出目标")
    _create_lead(db, principal, phone="13800138123", name="导出其他")
    db.commit()

    body = ScopedLeadExportRequestBody(
        scope="PUBLIC_POOL",
        phone="138 0013 8122",
        idempotency_key="feedback-912-phone-export",
    )
    filters = body.filters()

    assert filters["phone_hash"] == hash_phone("13800138122")
    assert "phone" not in filters
    assert "13800138122" not in str(filters)

    archive_path, count = build_lead_export_workbook(db, filters)
    archive_path.unlink(missing_ok=True)

    assert count == 1


def test_public_pool_phone_query_rejects_incomplete_number_and_stores_only_hash(
    api_client,
) -> None:
    client, factory = api_client
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "operation", "password": "Operation123!"},
    )
    assert login.status_code == 200, login.text

    created = client.post(
        "/api/v1/v1.2/public-pool/leads",
        json={
            "customer_name": "手机号查询目标",
            "phone": "13900139811",
            "region_code": "310000",
            "city": "上海市",
            "source_channel": "OTHER",
            "source_detail": "九月十二日接口回归",
            "consent_confirmed": True,
        },
    )
    assert created.status_code == 200, created.text
    created_id = created.json()["data"]["id"]

    exact = client.get(
        "/api/v1/v1.2/public-pool/leads?phone=139%200013%209811&page_size=1"
    )
    assert exact.status_code == 200, exact.text
    assert exact.json()["data"]["total"] == 1
    assert [item["id"] for item in exact.json()["data"]["items"]] == [created_id]

    unfiltered = client.get("/api/v1/v1.2/public-pool/leads")
    blank = client.get("/api/v1/v1.2/public-pool/leads?phone=%20%20")
    assert blank.status_code == 200, blank.text
    assert blank.json()["data"]["total"] == unfiltered.json()["data"]["total"]

    invalid = client.get("/api/v1/v1.2/public-pool/leads?phone=123")
    assert invalid.status_code == 422, invalid.text

    export = client.post(
        "/api/v1/v1.2/reports/leads/exports",
        json={
            "scope": "PUBLIC_POOL",
            "phone": "139 0013 9811",
            "idempotency_key": "feedback-912-api-phone-export",
        },
    )
    assert export.status_code == 200, export.text
    task_id = export.json()["data"]["id"]

    with factory() as db:
        task = db.scalar(select(LeadExportTask).where(LeadExportTask.id == task_id))
        assert task is not None
        assert task.filters_json["phone_hash"] == hash_phone("13900139811")
        assert "phone" not in task.filters_json
        assert "13900139811" not in str(task.filters_json)


def test_historical_operation_rework_stays_visible_until_successful_transfer(db) -> None:
    _, principal = _operation(db)
    rework = _create_lead(db, principal, phone="13800138124", name="历史待补客户")
    other = _create_lead(db, principal, phone="13800138125", name="普通公海客户")
    rework.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rework.source_kind = LeadSourceKind.PLATFORM_MANUAL.value
    rework.source_type = LeadSourceKind.PLATFORM_MANUAL.value
    rework.pending_reason = "PRE_DISPATCH_REWORK_REQUIRED"
    other.pending_reason = "PUBLIC_POOL_INCOMPLETE"
    db.flush()

    pending, total = list_lead_report_rows(
        db,
        filters=LeadReportFilterBody(
            pending_reason="PRE_DISPATCH_REWORK_REQUIRED",
        ).filters(),
        page_no=1,
        page_size=20,
    )
    assert total == 1
    assert [item.lead.id for item in pending] == [rework.id]

    update_public_pool_lead(
        db,
        lead=rework,
        principal=principal,
        values={"city": "武汉市"},
    )
    assert rework.pending_reason == "PRE_DISPATCH_REWORK_REQUIRED"

    transferred = transfer_public_pool_lead(db, lead=rework, principal=principal)
    assert transferred.transferred is True
    assert rework.status == LeadV12Status.READY_DISPATCH.value
    assert rework.pending_reason is None

    pending, total = list_lead_report_rows(
        db,
        filters=LeadReportFilterBody(
            pending_reason="PRE_DISPATCH_REWORK_REQUIRED",
        ).filters(),
        page_no=1,
        page_size=20,
    )
    assert total == 0
    assert pending == []
