from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select

from apps.api.src.core.enums import AssignmentStatus
from apps.api.src.core.models import (
    Assignment,
    AuditLog,
    Company,
    Lead,
    Notification,
    PointsAccount,
    PointsLedger,
    User,
)
from apps.api.src.core.models_v12 import CompanyLeadCapability, CompanyServiceAreaV12
from apps.api.src.core.security import encrypt_text, fingerprint_phone, hash_phone
from apps.api.src.core.v12_enums import LeadSourceKind, LeadV12Status


def _login(client, username: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    token = response.cookies.get("access_token")
    assert token
    return {"Authorization": f"Bearer {token}"}


def _prepare_receiver_and_lead(factory, *, phone: str) -> tuple[str, str, str, str]:
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        operation = db.scalar(select(User).where(User.username == "operation"))
        employee = db.scalar(select(User).where(User.username == "franchise_employee_demo"))
        owner = db.scalar(select(User).where(User.username == "franchise_demo"))
        assert company is not None and operation is not None
        assert employee is not None and owner is not None
        if db.scalar(
            select(CompanyLeadCapability).where(
                CompanyLeadCapability.company_id == company.id,
                CompanyLeadCapability.capability_code == "LEAD_RECEIVER",
            )
        ) is None:
            db.add(
                CompanyLeadCapability(
                    company_id=company.id,
                    capability_code="LEAD_RECEIVER",
                    active=True,
                    review_status="APPROVED",
                )
            )
        if db.scalar(
            select(CompanyServiceAreaV12).where(
                CompanyServiceAreaV12.company_id == company.id,
                CompanyServiceAreaV12.region_code == "310000",
            )
        ) is None:
            db.add(
                CompanyServiceAreaV12(
                    company_id=company.id,
                    region_code="310000",
                    region_level="CITY",
                    is_primary_city=True,
                    active=True,
                    review_status="APPROVED",
                )
            )
        if db.scalar(
            select(CompanyServiceAreaV12).where(
                CompanyServiceAreaV12.company_id == company.id,
                CompanyServiceAreaV12.region_code == "310101",
            )
        ) is None:
            # 2026-09-19 S7：客资派发必须落到区县级，服务区域同步补区级行。
            db.add(
                CompanyServiceAreaV12(
                    company_id=company.id,
                    region_code="310101",
                    region_level="DISTRICT",
                    active=True,
                    review_status="APPROVED",
                )
            )
        account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == company.id))
        assert account is not None
        account.balance = 5000
        now = datetime.now(timezone.utc)
        lead = Lead(
            source_type=LeadSourceKind.PLATFORM_MANUAL.value,
            source_kind=LeadSourceKind.PLATFORM_MANUAL.value,
            submitter_user_id=operation.id,
            customer_name="九月十日直派客户",
            phone_encrypted=encrypt_text(phone),
            phone_hash=hash_phone(phone),
            phone_fingerprint=fingerprint_phone(phone),
            consent_confirmed=True,
            city="上海市",
            region_code="310101",
            category_code="OLD_RENOVATION",
            brand_code="ZHONGSHU",
            need_summary="验证运营直派员工",
            status=LeadV12Status.READY_DISPATCH.value,
            review_status="APPROVED",
            duplicate_status="CLEAR",
            imported_at=now,
            submitted_at=now,
            raw_payload={},
        )
        db.add(lead)
        db.commit()
        return lead.id, company.id, employee.id, owner.id


def test_dispatch_pool_exposes_authorized_phone_and_source_names(api_client) -> None:
    client, factory = api_client
    lead_id, _company_id, _employee_id, _owner_id = _prepare_receiver_and_lead(
        factory,
        phone="13900139701",
    )

    headers = _login(client, "operation", "Operation123!")
    response = client.get("/api/v1/v1.2/dispatch-pool", headers=headers)

    assert response.status_code == 200, response.text
    item = next(row for row in response.json()["data"]["items"] if row["id"] == lead_id)
    assert item["phone"] == "13900139701"
    assert item["phone_masked"] == "139****9701"
    assert item["submitter_name"] == "运营管理员"
    assert item["supplier_company_name"] is None
    with factory() as db:
        audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "V12_DISPATCH_POOL_PHONE_READ",
                AuditLog.request_id == response.json()["request_id"],
            )
        )
        assert audit is not None
        assert audit.after_json["lead_ids"]


def test_new_manual_dispatch_requires_and_persists_employee(api_client) -> None:
    client, factory = api_client
    lead_id, company_id, employee_id, _owner_id = _prepare_receiver_and_lead(
        factory,
        phone="13900139702",
    )
    headers = _login(client, "operation", "Operation123!")

    missing = client.post(
        f"/api/v1/v1.2/dispatch-pool/{lead_id}/dispatch",
        headers=headers,
        json={"company_id": company_id, "idempotency_key": "feedback-910-missing-employee"},
    )
    assert missing.status_code == 422, missing.text
    assert missing.json()["code"] == "DISPATCH_EMPLOYEE_REQUIRED"

    created = client.post(
        f"/api/v1/v1.2/dispatch-pool/{lead_id}/dispatch",
        headers=headers,
        json={
            "company_id": company_id,
            "employee_user_id": employee_id,
            "idempotency_key": "feedback-910-direct-employee",
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["data"]["internal_assignee_user_id"] == employee_id


def test_assigned_employee_claims_once_while_owner_is_view_only(api_client) -> None:
    client, factory = api_client
    lead_id, company_id, employee_id, owner_id = _prepare_receiver_and_lead(
        factory,
        phone="13900139703",
    )
    operation_headers = _login(client, "operation", "Operation123!")
    dispatched = client.post(
        f"/api/v1/v1.2/dispatch-pool/{lead_id}/dispatch",
        headers=operation_headers,
        json={
            "company_id": company_id,
            "employee_user_id": employee_id,
            "idempotency_key": "feedback-910-employee-claim",
        },
    )
    assert dispatched.status_code == 200, dispatched.text
    assignment_id = dispatched.json()["data"]["id"]
    with factory() as db:
        before_balance = db.scalar(
            select(PointsAccount.balance).where(PointsAccount.company_id == company_id)
        )
        recipients = set(
            db.scalars(
                select(Notification.user_id).where(
                    Notification.scene == "V12_ASSIGNMENT_DISPATCHED"
                )
            ).all()
        )
        assert recipients == {owner_id, employee_id}

    disable_pending_employee = client.post(
        f"/api/v1/companies/{company_id}/accounts/{employee_id}/disable",
        headers=operation_headers,
        json={},
    )
    assert disable_pending_employee.status_code == 409, disable_pending_employee.text
    assert disable_pending_employee.json()["code"] == "COMPANY_ACCOUNT_HANDOVER_REQUIRED"

    owner_headers = _login(client, "franchise_demo", "Franchise123!")
    owner_detail = client.get(
        f"/api/v1/v1.2/assignments/{assignment_id}",
        headers=owner_headers,
    )
    owner_claim = client.post(
        f"/api/v1/v1.2/assignments/{assignment_id}/claim",
        headers=owner_headers,
    )
    assert owner_detail.status_code == 200, owner_detail.text
    assert owner_claim.status_code == 403, owner_claim.text

    employee_headers = _login(client, "franchise_employee_demo", "Employee123!")
    claimed = client.post(
        f"/api/v1/v1.2/assignments/{assignment_id}/claim",
        headers=employee_headers,
    )
    replayed = client.post(
        f"/api/v1/v1.2/assignments/{assignment_id}/claim",
        headers=employee_headers,
    )
    assert claimed.status_code == 200, claimed.text
    assert replayed.status_code == 200, replayed.text
    assert claimed.json()["data"]["assignment"]["phone"] == "13900139703"

    with factory() as db:
        assignment = db.get(Assignment, assignment_id)
        after_balance = db.scalar(
            select(PointsAccount.balance).where(PointsAccount.company_id == company_id)
        )
        claim_ledgers = db.scalar(
            select(func.count(PointsLedger.id)).where(
                PointsLedger.company_id == company_id,
                PointsLedger.business_id == assignment_id,
            )
        )
        assert assignment is not None
        assert assignment.status == AssignmentStatus.CLAIMED.value
        assert assignment.internal_assignee_user_id == employee_id
        assert after_balance == before_balance - assignment.points_price
        assert claim_ledgers == 1


def test_operation_deletes_own_draft_then_phone_can_be_re_entered(api_client) -> None:
    client, factory = api_client
    operation_headers = _login(client, "operation", "Operation123!")
    created = client.post(
        "/api/v1/v1.2/platform/leads",
        headers=operation_headers,
        json={"customer_name": "待删除客户", "phone": "13900139704"},
    )
    assert created.status_code == 200, created.text
    lead_id = created.json()["data"]["id"]

    preview = client.get(
        f"/api/v1/v1.2/operation/leads/{lead_id}/deletion-preview",
        headers=operation_headers,
    )
    deleted = client.request(
        "DELETE",
        f"/api/v1/v1.2/operation/leads/{lead_id}",
        headers=operation_headers,
        json={"reason": "客户要求停止处理"},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["data"]["deletable"] is True
    assert deleted.status_code == 200, deleted.text

    listed = client.get("/api/v1/v1.2/platform/leads", headers=operation_headers)
    assert lead_id not in {item["id"] for item in listed.json()["data"]["items"]}
    duplicate = client.post(
        "/api/v1/v1.2/platform/leads",
        headers=operation_headers,
        json={"customer_name": "重复录入客户", "phone": "+86 13900139704"},
    )
    # 2026-09-17 确认口径：手机号唯一排除逻辑删除记录，删除后同号可新录入。
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["data"]["id"] != lead_id
    with factory() as db:
        lead = db.get(Lead, lead_id)
        assert lead is not None
        assert lead.deleted_at is not None
        assert lead.delete_reason == "客户要求停止处理"
        audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "V12_OPERATION_LEAD_DELETE",
                AuditLog.resource_id == lead_id,
            )
        )
        assert audit is not None
        assert audit.metadata_json["reason"] == "客户要求停止处理"


def test_franchise_employee_uploads_and_only_sees_own_leads(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        assert company is not None
        capability = db.scalar(
            select(CompanyLeadCapability).where(
                CompanyLeadCapability.company_id == company.id,
                CompanyLeadCapability.capability_code == "LEAD_SUPPLIER",
            )
        )
        if capability is None:
            capability = CompanyLeadCapability(
                company_id=company.id,
                capability_code="LEAD_SUPPLIER",
            )
            db.add(capability)
        capability.active = True
        capability.review_status = "APPROVED"
        db.commit()

    employee_headers = _login(client, "franchise_employee_demo", "Employee123!")
    created = client.post(
        "/api/v1/v1.2/supplier/leads",
        headers=employee_headers,
        json={
            "customer_name": "员工上传客户",
            "phone": "13900139705",
            "city": "上海市",
            "region_code": "310101",
            "consent_confirmed": True,
        },
    )
    assert created.status_code == 200, created.text
    employee_id = created.json()["data"]["submitter_user_id"]
    listed = client.get("/api/v1/v1.2/supplier/leads", headers=employee_headers)
    assert listed.status_code == 200, listed.text
    assert {item["submitter_user_id"] for item in listed.json()["data"]["items"]} == {
        employee_id
    }

    owner_headers = _login(client, "franchise_demo", "Franchise123!")
    owner_listed = client.get(
        "/api/v1/v1.2/supplier/leads",
        headers=owner_headers,
    )
    assert owner_listed.status_code == 200, owner_listed.text
    assert created.json()["data"]["id"] in {
        item["id"] for item in owner_listed.json()["data"]["items"]
    }
