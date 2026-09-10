from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from apps.api.src.core.enums import AssignmentStatus, VerificationTaskStatus
from apps.api.src.core.models import (
    Assignment,
    AssignmentEvent,
    AuditLog,
    Company,
    Lead,
    NotificationOutbox,
    PointsAccount,
    PointsLedger,
    User,
    VerificationSubmission,
    VerificationTask,
)
from apps.api.src.core.models_v12 import CompanyLeadCapability, CompanyServiceAreaV12
from apps.api.src.core.security import encrypt_text, fingerprint_phone, hash_phone
from apps.api.src.core.v12_enums import LeadSourceKind, LeadV12Status, VerificationTaskType
from apps.api.src.routers import v12_dispatch as v12_dispatch_router


def _v12_lead(*, user_id: str, phone: str, status: str) -> Lead:
    now = datetime.now(timezone.utc)
    return Lead(
        source_type=LeadSourceKind.PLATFORM_MANUAL.value,
        source_kind=LeadSourceKind.PLATFORM_MANUAL.value,
        submitter_user_id=user_id,
        customer_name="接口隐私测试客户",
        phone_encrypted=encrypt_text(phone),
        phone_hash=hash_phone(phone),
        phone_fingerprint=fingerprint_phone(phone),
        consent_confirmed=True,
        city="上海市",
        region_code="310000",
        category_code="OLD_RENOVATION",
        brand_code="ZHONGSHU",
        need_summary="接口字段隔离测试",
        status=status,
        review_status="APPROVED",
        duplicate_status="CLEAR",
        imported_at=now,
        submitted_at=now,
        raw_payload={},
    )


def _login(client, username: str, password: str) -> None:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200


def test_masked_assignment_phone_reuses_only_masked_decryption(monkeypatch) -> None:
    calls: list[str] = []

    def fake_decrypt(value: str) -> str:
        calls.append(value)
        return "13800138000"

    monkeypatch.setattr(v12_dispatch_router, "decrypt_text", fake_decrypt)
    v12_dispatch_router._masked_encrypted_phone.cache_clear()
    try:
        assert v12_dispatch_router._masked_encrypted_phone("encrypted-phone") == "138****8000"
        assert v12_dispatch_router._masked_encrypted_phone("encrypted-phone") == "138****8000"
        assert calls == ["encrypted-phone"]
    finally:
        v12_dispatch_router._masked_encrypted_phone.cache_clear()


def test_assignment_detail_query_omits_unused_large_columns() -> None:
    statement = str(
        v12_dispatch_router._assignment_detail_projection("assignment-id", "company-id")
    )

    assert "assignments.lead_snapshot" not in statement
    assert "leads.raw_payload" not in statement
    assert "leads.phone_hash" not in statement
    assert "leads.phone_encrypted" in statement
    assert "assignments.company_id" in statement


def _prepare_dispatch_lead(factory, *, phone: str) -> tuple[str, str, str]:
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        operation = db.scalar(select(User).where(User.username == "operation"))
        employee = db.scalar(select(User).where(User.username == "franchise_employee_demo"))
        assert company is not None and operation is not None and employee is not None
        if not db.scalar(
            select(CompanyLeadCapability).where(
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
        if not db.scalar(
            select(CompanyServiceAreaV12).where(
                CompanyServiceAreaV12.company_id == company.id,
                CompanyServiceAreaV12.region_code == "310000",
            )
        ):
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
        account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == company.id))
        assert account is not None
        account.balance = 5000
        lead = _v12_lead(
            user_id=operation.id,
            phone=phone,
            status=LeadV12Status.READY_DISPATCH.value,
        )
        db.add(lead)
        db.commit()
        return lead.id, company.id, employee.id


def test_candidate_api_hides_exact_points_from_operation(api_client) -> None:
    client, factory = api_client
    lead_id, company_id, _employee_id = _prepare_dispatch_lead(factory, phone="13900139201")

    _login(client, "operation", "Operation123!")
    response = client.get(f"/api/v1/v1.2/dispatch-pool/{lead_id}/candidates")
    assert response.status_code == 200
    candidate = next(
        item for item in response.json()["data"]["candidates"] if item["company_id"] == company_id
    )
    assert candidate["points_price"] == 100
    assert "points_balance" not in candidate
    assert "points_reserved" not in candidate
    assert "points_available" not in candidate

    client.post("/api/v1/auth/logout")
    _login(client, "admin", "Admin123!")
    response = client.get(f"/api/v1/v1.2/dispatch-pool/{lead_id}/candidates")
    candidate = next(
        item for item in response.json()["data"]["candidates"] if item["company_id"] == company_id
    )
    assert candidate["points_balance"] == 5000
    assert candidate["points_reserved"] >= 0
    assert candidate["points_available"] == (
        candidate["points_balance"] - candidate["points_reserved"]
    )


def test_dispatch_pool_marks_only_the_latest_submitted_pre_dispatch_verification(api_client) -> None:
    client, factory = api_client
    verified_lead_id, _, _ = _prepare_dispatch_lead(factory, phone="13900139231")
    plain_lead_id, _, _ = _prepare_dispatch_lead(factory, phone="13900139232")
    now = datetime.now(timezone.utc)
    with factory() as db:
        telesales = db.scalar(select(User).where(User.username == "telesales"))
        assert telesales is not None
        task_ids: list[str] = []
        for index, submitted_at in enumerate((now - timedelta(hours=1), now), start=1):
            task = VerificationTask(
                lead_id=verified_lead_id,
                task_type=VerificationTaskType.PRE_DISPATCH_VERIFY.value,
                status=VerificationTaskStatus.RELEASED.value,
                assignee_user_id=telesales.id,
                assigned_at=submitted_at - timedelta(minutes=10),
                started_at=submitted_at - timedelta(minutes=5),
                submitted_at=submitted_at,
                contact_result="CONNECTED",
                verification_conclusion="QUALIFIED",
            )
            db.add(task)
            db.flush()
            db.add(
                VerificationSubmission(
                    task_id=task.id,
                    lead_id=verified_lead_id,
                    result="QUALIFIED",
                    answers_json={},
                    corrections_json={},
                    note=f"第 {index} 次核验备注",
                    submitted_by=telesales.id,
                )
            )
            task_ids.append(task.id)
        db.commit()

    _login(client, "operation", "Operation123!")
    response = client.get("/api/v1/v1.2/dispatch-pool?page=1&page_size=20")
    assert response.status_code == 200, response.text
    items = {item["id"]: item for item in response.json()["data"]["items"]}

    verified = items[verified_lead_id]
    assert verified["has_verification_info"] is True
    assert verified["pre_dispatch_task_id"] == task_ids[-1]
    assert "verification_info" not in verified
    assert "note" not in verified

    plain = items[plain_lead_id]
    assert plain["has_verification_info"] is False
    assert plain["pre_dispatch_task_id"] is None


def test_returned_receiver_is_excluded_until_operation_records_an_exception(api_client) -> None:
    client, factory = api_client
    lead_id, company_id, employee_id = _prepare_dispatch_lead(factory, phone="13900139212")
    with factory() as db:
        lead = db.get(Lead, lead_id)
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert lead is not None and operation is not None
        db.add(
            Assignment(
                lead_id=lead.id,
                company_id=company_id,
                receiver_company_id=company_id,
                status=AssignmentStatus.RETURNED.value,
                points_price=100,
                claim_points=100,
                price_version=1,
                lead_snapshot={},
                assigned_by=operation.id,
                assigned_at=datetime.now(timezone.utc),
                claimed_at=datetime.now(timezone.utc),
                released_at=datetime.now(timezone.utc),
                release_reason="V12_RETURN_APPROVED",
                idempotency_key="returned-receiver-history",
            )
        )
        db.commit()

    _login(client, "operation", "Operation123!")
    candidates = client.get(f"/api/v1/v1.2/dispatch-pool/{lead_id}/candidates")
    assert candidates.status_code == 200, candidates.text
    candidate = next(
        item
        for item in candidates.json()["data"]["candidates"]
        if item["company_id"] == company_id
    )
    assert candidate["eligible"] is False
    assert "RETURNED_RECEIVER_EXCLUDED" in candidate["exclusion_reasons"]

    blocked = client.post(
        f"/api/v1/v1.2/dispatch-pool/{lead_id}/dispatch",
        json={
            "company_id": company_id,
            "employee_user_id": employee_id,
            "idempotency_key": "returned-receiver-blocked",
        },
    )
    assert blocked.status_code == 409, blocked.text
    assert "RETURNED_RECEIVER_EXCLUDED" in blocked.json()["details"]["reasons"]

    override = client.post(
        f"/api/v1/v1.2/dispatch-pool/{lead_id}/dispatch",
        json={
            "company_id": company_id,
            "employee_user_id": employee_id,
            "idempotency_key": "returned-receiver-override",
            "return_receiver_override": True,
            "return_receiver_override_reason": "运营复核后确认原公司可继续承接",
        },
    )
    assert override.status_code == 200, override.text
    assignment_id = override.json()["data"]["id"]
    with factory() as db:
        audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "V12_MANUAL_DISPATCH",
                AuditLog.resource_id == assignment_id,
            )
        )
        assert audit is not None
        assert audit.after_json["return_receiver_override"] is True
        assert audit.metadata_json["reason"] == "运营复核后确认原公司可继续承接"


def test_concurrent_manual_dispatch_replay_has_one_business_side_effect(api_client) -> None:
    client, factory = api_client
    lead_id, company_id, employee_id = _prepare_dispatch_lead(factory, phone="13900139203")
    _login(client, "operation", "Operation123!")
    payload = {
        "company_id": company_id,
        "employee_user_id": employee_id,
        "idempotency_key": "concurrent-v12-manual-dispatch",
        "note": "concurrent idempotency regression",
    }

    def dispatch_once(_: int):
        return client.post(
            f"/api/v1/v1.2/dispatch-pool/{lead_id}/dispatch",
            json=payload,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(dispatch_once, range(8)))

    assert all(response.status_code == 200 for response in responses), [
        response.text for response in responses
    ]
    assignment_ids = {response.json()["data"]["id"] for response in responses}
    assert len(assignment_ids) == 1
    assignment_id = assignment_ids.pop()
    with factory() as db:
        assert db.scalar(
            select(func.count(Assignment.id)).where(
                Assignment.idempotency_key == payload["idempotency_key"]
            )
        ) == 1
        assert db.scalar(
            select(func.count(AssignmentEvent.id)).where(
                AssignmentEvent.assignment_id == assignment_id,
                AssignmentEvent.event_type == "V12_MANUAL_DISPATCH",
            )
        ) == 1
        assert db.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.resource_id == assignment_id,
                AuditLog.action == "V12_MANUAL_DISPATCH",
            )
        ) == 1
        assert db.scalar(
            select(func.count(NotificationOutbox.id)).where(
                NotificationOutbox.aggregate_id == assignment_id,
                NotificationOutbox.event_type == "V12_ASSIGNMENT_DISPATCHED",
            )
        ) == 2


def test_unclaimed_released_assignment_does_not_unlock_phone(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        operation = db.scalar(select(User).where(User.username == "operation"))
        franchise = db.scalar(select(User).where(User.username == "franchise_demo"))
        assert company is not None and operation is not None and franchise is not None
        lead = _v12_lead(user_id=operation.id, phone="13900139202", status=LeadV12Status.READY_DISPATCH.value)
        db.add(lead)
        db.flush()
        assignment = Assignment(
            lead_id=lead.id,
            company_id=company.id,
            receiver_company_id=company.id,
            status=AssignmentStatus.RELEASED.value,
            points_price=100,
            claim_points=100,
            lead_snapshot={"phone_masked": "139****9202"},
            assigned_by=operation.id,
            assigned_at=datetime.now(timezone.utc),
            released_at=datetime.now(timezone.utc),
            release_reason="TEST_RELEASE",
            idempotency_key="privacy-released-assignment",
        )
        db.add(assignment)
        db.commit()
        assignment_id = assignment.id

    _login(client, "franchise_demo", "Franchise123!")
    response = client.get(f"/api/v1/v1.2/assignments/{assignment_id}")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == AssignmentStatus.RELEASED.value
    assert data["phone"] is None
    assert data["phone_masked"] == "139****9202"


def test_assigned_employee_can_refuse_pending_claim_without_consuming_points(api_client) -> None:
    client, factory = api_client
    lead_id, company_id, employee_id = _prepare_dispatch_lead(factory, phone="13900139221")
    _login(client, "operation", "Operation123!")
    dispatched = client.post(
        f"/api/v1/v1.2/dispatch-pool/{lead_id}/dispatch",
        json={
            "company_id": company_id,
            "employee_user_id": employee_id,
            "idempotency_key": "refuse-pending-claim-dispatch",
        },
    )
    assert dispatched.status_code == 200, dispatched.text
    assignment_id = dispatched.json()["data"]["id"]
    client.post("/api/v1/auth/logout")

    with factory() as db:
        before_balance = db.scalar(select(PointsAccount.balance).where(PointsAccount.company_id == company_id))

    _login(client, "franchise_demo", "Franchise123!")
    owner = client.post(
        f"/api/v1/v1.2/assignments/{assignment_id}/refuse",
        json={"reason": "负责人仅接收通知，不能替员工拒领"},
    )
    assert owner.status_code == 403
    client.post("/api/v1/auth/logout")

    _login(client, "franchise_employee_demo", "Employee123!")
    blank_reason = client.post(
        f"/api/v1/v1.2/assignments/{assignment_id}/refuse",
        json={"reason": "   "},
    )
    assert blank_reason.status_code == 422

    refused = client.post(
        f"/api/v1/v1.2/assignments/{assignment_id}/refuse",
        json={"reason": "服务范围临时不可承接"},
    )
    assert refused.status_code == 200, refused.text
    data = refused.json()["data"]
    assert data["status"] == AssignmentStatus.RELEASED.value
    assert data["lead_status"] == LeadV12Status.READY_DISPATCH.value
    assert data["release_reason"] == "REFUSED_CLAIM"

    with factory() as db:
        assignment = db.get(Assignment, assignment_id)
        lead = db.get(Lead, lead_id)
        assert assignment is not None and lead is not None
        assert assignment.status == AssignmentStatus.RELEASED.value
        assert assignment.claimed_at is None
        assert lead.current_assignment_id is None
        assert lead.status == LeadV12Status.READY_DISPATCH.value
        assert db.scalar(select(PointsAccount.balance).where(PointsAccount.company_id == company_id)) == before_balance
        assert db.scalar(
            select(func.count(PointsLedger.id)).where(PointsLedger.business_id == assignment_id)
        ) == 0
        event = db.scalar(
            select(AssignmentEvent).where(
                AssignmentEvent.assignment_id == assignment_id,
                AssignmentEvent.event_type == "V12_ASSIGNMENT_REFUSED",
            )
        )
        assert event is not None
        assert event.payload["reason"] == "服务范围临时不可承接"
