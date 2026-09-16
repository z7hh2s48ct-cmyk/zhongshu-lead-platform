from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from apps.api.src.core.models import Company, Lead, User
from apps.api.src.core.security import encrypt_text, fingerprint_phone, hash_phone
from apps.api.src.schemas.v12_reports import LeadReportFilterBody
from apps.api.src.services.lead_export_v12 import list_lead_report_rows
from apps.api.src.services.rbac import ROLE_PERMISSION_MATRIX
from apps.api.tests.test_call_h5_v12_contract import (
    TASKS_ENDPOINT,
    _login,
    _seed_return_verification,
)


ADMIN_JS = Path(__file__).resolve().parents[2] / "admin" / "public" / "v12-operations.js"


def _lead(*, submitter_id: str, supplier_company_id: str, phone: str, name: str) -> Lead:
    now = datetime.now(timezone.utc)
    return Lead(
        source_type="SUPPLIER_H5",
        source_kind="SUPPLIER_H5",
        submitter_user_id=submitter_id,
        supplier_company_id=supplier_company_id,
        customer_name=name,
        phone_encrypted=encrypt_text(phone),
        phone_hash=hash_phone(phone),
        phone_fingerprint=fingerprint_phone(phone),
        consent_confirmed=True,
        city="上海市",
        region_code="310000",
        need_summary="九月十一日需求回归",
        status="DRAFT",
        pending_reason="PRE_DISPATCH_REWORK_REQUIRED",
        imported_at=now,
        submitted_at=now,
        raw_payload={},
    )


def test_lead_report_filters_by_supplier_company_and_pending_reason(db) -> None:
    operation = User(username="feedback-911-operation", display_name="运营录入人", status="ACTIVE")
    db.add(operation)
    first = Company(code="SUP-911-A", name="供资加盟商 A", status="ACTIVE")
    second = Company(code="SUP-911-B", name="供资加盟商 B", status="ACTIVE")
    db.add_all([first, second])
    db.flush()
    db.add_all(
        [
            _lead(
                submitter_id=operation.id,
                supplier_company_id=first.id,
                phone="13900139721",
                name="待运营补充 A",
            ),
            _lead(
                submitter_id=operation.id,
                supplier_company_id=second.id,
                phone="13900139722",
                name="待运营补充 B",
            ),
        ]
    )
    db.commit()

    body = LeadReportFilterBody(
        supplier_company_id=first.id,
        pending_reason="PRE_DISPATCH_REWORK_REQUIRED",
    )
    rows, total = list_lead_report_rows(db, filters=body.filters(), page_no=1, page_size=20)

    assert total == 1
    assert [row.lead.customer_name for row in rows] == ["待运营补充 A"]


def test_company_provided_count_matches_supplier_drilldown_scope(api_client) -> None:
    client, factory = api_client
    operation_headers = _login(client, "operation", "Operation123!")
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        company = Company(code="SUP-911-COUNT", name="供资口径验收加盟商", status="ACTIVE")
        db.add(company)
        db.flush()
        visible = _lead(
            submitter_id=operation.id,
            supplier_company_id=company.id,
            phone="13900139731",
            name="有效加盟商供资",
        )
        deleted = _lead(
            submitter_id=operation.id,
            supplier_company_id=company.id,
            phone="13900139732",
            name="已删除加盟商供资",
        )
        deleted.deleted_at = datetime.now(timezone.utc)
        platform = _lead(
            submitter_id=operation.id,
            supplier_company_id=company.id,
            phone="13900139733",
            name="平台录入客资",
        )
        platform.source_type = "PLATFORM_MANUAL"
        platform.source_kind = "PLATFORM_MANUAL"
        db.add_all([visible, deleted, platform])
        db.commit()
        company_id = company.id

    company_page = client.get(
        "/api/v1/companies?page=1&page_size=200",
        headers=operation_headers,
    )
    assert company_page.status_code == 200, company_page.text
    company_item = next(
        item
        for item in company_page.json()["data"]["items"]
        if item["id"] == company_id
    )

    drilldown = client.post(
        "/api/v1/v1.2/reports/leads/search",
        headers=operation_headers,
        json={
            "source_kind": "SUPPLIER_H5",
            "supplier_company_id": company_id,
            "page": 1,
            "page_size": 20,
        },
    )
    assert drilldown.status_code == 200, drilldown.text
    assert company_item["provided"]["total"] == 1
    assert drilldown.json()["data"]["total"] == 1
    assert [item["customer_name"] for item in drilldown.json()["data"]["items"]] == [
        "有效加盟商供资"
    ]


def test_operation_role_can_read_full_phone() -> None:
    assert "lead.phone.read" in ROLE_PERMISSION_MATRIX["OPERATION"][1]


def test_operation_return_details_include_full_phone_and_verification_note(api_client) -> None:
    client, factory = api_client
    task_id, return_id, phone, telesales_id, _ = _seed_return_verification(factory)
    operation = _login(client, "operation", "Operation123!")
    assigned = client.post(
        f"/api/v1{TASKS_ENDPOINT}/{task_id}/assign",
        headers=operation,
        json={"assignee_user_id": telesales_id, "reason": "运营分配事实核验"},
    )
    assert assigned.status_code == 200, assigned.text
    telesales = _login(client, "telesales", "Telesales123!")
    started = client.post(f"/api/v1{TASKS_ENDPOINT}/{task_id}/start", headers=telesales)
    assert started.status_code == 200, started.text
    submitted = client.post(
        f"/api/v1{TASKS_ENDPOINT}/{task_id}/submit",
        headers=telesales,
        json={
            "contact_result": "CONNECTED",
            "conclusion": "SUPPORT_RETURN",
            "note": "客户确认号码无效，建议支持退回",
        },
    )
    assert submitted.status_code == 200, submitted.text

    task_detail = client.get(
        f"/api/v1{TASKS_ENDPOINT}/{task_id}",
        headers=operation,
    )
    return_detail = client.get(f"/api/v1/v1.2/returns/{return_id}", headers=operation)

    assert task_detail.status_code == 200, task_detail.text
    assert return_detail.status_code == 200, return_detail.text
    assert task_detail.json()["data"]["lead"]["phone"] == phone
    assert task_detail.json()["data"]["verification_info"]["note"] == (
        "客户确认号码无效，建议支持退回"
    )
    assert return_detail.json()["data"]["phone"] == phone
    assert return_detail.json()["data"]["verification"]["note"] == (
        "客户确认号码无效，建议支持退回"
    )


def test_operations_ui_covers_feedback_entries_and_details() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")

    assert "lead-supplier-company-filter" in source
    assert "PRE_DISPATCH_REWORK_REQUIRED" in source
    assert "lead.phone||lead.phone_masked" in source
    assert "item.phone||item.phone_masked" in source
    assert "x.phone||x.phone_masked" in source
    assert "data-company-provided-total" in source
    assert "data-detail-assign" in source
    assert "核验备注" in source
    assert "forcePublicPool:['PLATFORM_MANUAL','FEISHU_IMPORT'].includes(lead.source_kind)" in source
    assert "correction&&id&&!forcePublicPool" in source


def test_telesales_cannot_use_operation_direct_invalid_endpoint(api_client) -> None:
    client, factory = api_client
    _, return_id, _, _, _ = _seed_return_verification(factory)
    telesales = _login(client, "telesales", "Telesales123!")

    response = client.post(
        f"/api/v1/v1.2/returns/{return_id}/direct-invalid",
        headers=telesales,
        json={"note": "电销角色不得执行运营直接判无效"},
    )

    assert response.status_code == 403
