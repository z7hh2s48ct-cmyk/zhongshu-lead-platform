from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from apps.api.src.core.models import Company, Lead, User
from apps.api.src.core.security import encrypt_text, hash_phone
from apps.api.src.core.v12_enums import LeadSourceKind, LeadV12Status


def _lead(*, source_kind: str, company_id: str | None, user_id: str, phone: str, name: str) -> Lead:
    now = datetime.now(timezone.utc)
    return Lead(
        source_type=source_kind,
        source_kind=source_kind,
        submitter_user_id=user_id,
        supplier_company_id=company_id,
        customer_name=name,
        phone_encrypted=encrypt_text(phone),
        phone_hash=hash_phone(phone),
        consent_confirmed=True,
        city="上海市",
        region_code="310000",
        need_summary="计划建设两层乡墅",
        status=LeadV12Status.PENDING_REVIEW.value if company_id else LeadV12Status.READY_DISPATCH.value,
        review_status="PENDING" if company_id else "APPROVED",
        duplicate_status="CLEAR",
        imported_at=now,
        submitted_at=now,
        raw_payload={},
    )


def test_supplier_review_queue_only_returns_supplier_sources(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert company is not None and operation is not None
        supplier = _lead(
            source_kind=LeadSourceKind.SUPPLIER_H5.value,
            company_id=company.id,
            user_id=operation.id,
            phone="13900139001",
            name="供应商客户",
        )
        platform = _lead(
            source_kind=LeadSourceKind.PLATFORM_MANUAL.value,
            company_id=None,
            user_id=operation.id,
            phone="13900139002",
            name="平台客户",
        )
        db.add_all([supplier, platform])
        db.commit()
        supplier_id = supplier.id

    login = client.post(
        "/api/v1/auth/login",
        json={"username": "operation", "password": "Operation123!"},
    )
    assert login.status_code == 200

    response = client.get("/api/v1/v1.2/admin/supplier-leads?review_status=PENDING")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 1
    assert [item["id"] for item in data["items"]] == [supplier_id]
    assert data["items"][0]["phone"] == "13900139001"
    assert data["items"][0]["phone_masked"] == "139****9001"

    detail = client.get(f"/api/v1/v1.2/admin/supplier-leads/{supplier_id}")
    assert detail.status_code == 200
    assert detail.json()["data"]["source_kind"] == LeadSourceKind.SUPPLIER_H5.value


def test_supplier_review_queue_requires_permission(api_client) -> None:
    client, _ = api_client
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "franchise_demo", "password": "Franchise123!"},
    )
    assert login.status_code == 200
    response = client.get("/api/v1/v1.2/admin/supplier-leads")
    assert response.status_code == 403
