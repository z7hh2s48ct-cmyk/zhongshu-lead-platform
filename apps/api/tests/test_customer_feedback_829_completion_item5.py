from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from apps.api.src.core.auth import Principal
from apps.api.src.core.enums import AssignmentStatus
from apps.api.src.core.errors import AppError
from apps.api.src.core.models import (
    Assignment,
    AuditLog,
    Company,
    Lead,
    LeadDuplicateRelation,
    User,
    VerificationTask,
)
from apps.api.src.core.models_v12 import LeadDedupEvent, SupplierLeadReward
from apps.api.src.core.security import encrypt_text, fingerprint_phone, hash_phone
from apps.api.src.core.v12_enums import (
    DuplicateDecision,
    LeadSourceKind,
    LeadV12Status,
    RewardStatus,
)
from apps.api.src.services.company_service import _restore_dedup_affected_lead
from apps.api.src.services.dedup_v12 import DedupResult
from apps.api.tests.xlsx_reader import read_xlsx


def _login(client) -> None:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "operation", "password": "Operation123!"},
    )
    assert response.status_code == 200, response.text


def _login_franchise(client) -> None:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "franchise_demo", "password": "Franchise123!"},
    )
    assert response.status_code == 200, response.text


def _principal(user: User) -> Principal:
    return Principal(
        user_id=user.id,
        display_name=user.display_name,
        company_id=user.company_id,
        role_codes=frozenset(role.code for role in user.roles),
        permission_codes=frozenset(),
        session_version=user.session_version,
    )


def _lead(
    *,
    operation_id: str,
    source_kind: LeadSourceKind,
    status: LeadV12Status,
    phone: str,
    snapshot_version: int = 1,
    supplier_company_id: str | None = None,
) -> Lead:
    now = datetime.now(timezone.utc)
    return Lead(
        source_type=source_kind.value,
        source_kind=source_kind.value,
        submitter_user_id=operation_id,
        supplier_company_id=supplier_company_id,
        customer_name="更正前客户",
        phone_encrypted=encrypt_text(phone),
        phone_hash=hash_phone(phone),
        phone_fingerprint=fingerprint_phone(phone),
        consent_confirmed=True,
        province="上海市",
        city="上海市",
        district="浦东新区",
        region_code="310115",
        category_code="OLD_RENOVATION",
        brand_code="ZHONGSHU",
        source_channel="OTHER",
        source_detail="历史导入",
        need_summary="第5条完整性验收",
        status=status.value,
        review_status="PENDING",
        duplicate_status="CLEAR",
        imported_at=now,
        submitted_at=now,
        snapshot_version=snapshot_version,
        raw_payload={},
    )


def _rewarded_supplier_lead(
    db,
    *,
    operation: User,
    receiver: Company,
    phone: str,
    lead_status: LeadV12Status,
    assignment_status: AssignmentStatus,
    reward_status: RewardStatus,
) -> tuple[Lead, Assignment, SupplierLeadReward]:
    supplier = Company(code="SUP-ITEM5-REWARD", name="第5条奖励测试供客商")
    db.add(supplier)
    db.flush()
    lead = _lead(
        operation_id=operation.id,
        source_kind=LeadSourceKind.SUPPLIER_H5,
        status=lead_status,
        phone=phone,
        supplier_company_id=supplier.id,
    )
    db.add(lead)
    db.flush()
    now = datetime.now(timezone.utc)
    assignment = Assignment(
        lead_id=lead.id,
        company_id=receiver.id,
        receiver_company_id=receiver.id,
        supplier_company_id=supplier.id,
        status=assignment_status.value,
        points_price=100,
        claim_points=100,
        price_version=1,
        lead_snapshot={"region_code": lead.region_code},
        assigned_by=operation.id,
        assigned_at=now - timedelta(days=2),
        claimed_at=(
            now - timedelta(days=1)
            if assignment_status != AssignmentStatus.PENDING_CLAIM
            else None
        ),
        idempotency_key=f"feedback-829-reward-{reward_status.value.lower()}",
    )
    db.add(assignment)
    db.flush()
    lead.current_assignment_id = assignment.id
    reward = SupplierLeadReward(
        lead_id=lead.id,
        assignment_id=assignment.id,
        supplier_company_id=supplier.id,
        receiver_company_id=receiver.id,
        status=reward_status.value,
        claim_points=100,
        reward_ratio_bps=3000,
        reward_points=30,
        rule_version=1,
        rule_snapshot_json={"version": 1, "ratio_bps": 3000},
        observed_at=now - timedelta(days=1),
        appeal_deadline_at=now + timedelta(days=1),
        reward_due_at=now + timedelta(days=2),
        settled_at=(now - timedelta(hours=1) if reward_status is RewardStatus.SETTLED else None),
    )
    db.add(reward)
    db.flush()
    return lead, assignment, reward


def _patch_correction_dedup(
    monkeypatch: pytest.MonkeyPatch,
    decision: DuplicateDecision,
) -> None:
    from apps.api.src.services import lead_supply_v12

    def evaluate(db, *, lead, normalized_phone, checkpoint, now):
        fingerprint = fingerprint_phone(normalized_phone)
        event = LeadDedupEvent(
            lead_id=lead.id,
            phone_fingerprint=fingerprint,
            checkpoint=checkpoint,
            decision=decision.value,
            details_json={"test_decision": decision.value},
        )
        db.add(event)
        lead.phone_fingerprint = fingerprint
        lead.duplicate_status = decision.value
        db.flush()
        return DedupResult(decision=decision, event_id=event.id)

    monkeypatch.setattr(lead_supply_v12, "evaluate_phone", evaluate)


@pytest.mark.parametrize(
    ("source_kind", "status"),
    [
        (LeadSourceKind.SUPPLIER_H5, LeadV12Status.PENDING_REVIEW),
        (LeadSourceKind.FEISHU_LEGACY, LeadV12Status.PENDING_TELESALES_VERIFY),
    ],
)
def test_unassigned_leads_from_every_source_can_be_corrected_without_changing_workflow_state(
    api_client,
    source_kind: LeadSourceKind,
    status: LeadV12Status,
) -> None:
    client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        assert operation is not None and company is not None
        lead = _lead(
            operation_id=operation.id,
            source_kind=source_kind,
            status=status,
            phone="13900139701",
            supplier_company_id=(
                company.id if source_kind is LeadSourceKind.SUPPLIER_H5 else None
            ),
        )
        db.add(lead)
        db.commit()
        lead_id = lead.id

    _login(client)
    response = client.patch(
        f"/api/v1/v1.2/platform/leads/{lead_id}/correction",
        json={"customer_name": "更正后客户", "expected_snapshot_version": 1},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["customer_name"] == "更正后客户"
    assert data["source_kind"] == source_kind.value
    assert data["status"] == status.value
    assert data["snapshot_version"] == 2


def test_admin_can_read_correction_detail_for_every_source_and_franchise_cannot(
    api_client,
) -> None:
    client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        assert operation is not None and company is not None
        leads = [
            _lead(
                operation_id=operation.id,
                source_kind=source_kind,
                status=LeadV12Status.PENDING_REVIEW,
                phone=f"1390013971{index}",
                supplier_company_id=(
                    company.id
                    if source_kind is LeadSourceKind.SUPPLIER_H5
                    else None
                ),
            )
            for index, source_kind in enumerate(LeadSourceKind)
        ]
        db.add_all(leads)
        db.commit()
        lead_ids = {lead.source_kind: lead.id for lead in leads}

    _login(client)
    for source_kind, lead_id in lead_ids.items():
        response = client.get(f"/api/v1/v1.2/admin/leads/{lead_id}")
        assert response.status_code == 200, response.text
        assert response.json()["data"]["source_kind"] == source_kind

    client.post("/api/v1/auth/logout")
    _login_franchise(client)
    forbidden = client.get(
        f"/api/v1/v1.2/admin/leads/{lead_ids[LeadSourceKind.SUPPLIER_H5.value]}"
    )
    assert forbidden.status_code == 403, forbidden.text
    assert forbidden.json()["code"] == "FORBIDDEN"


def test_dispatched_supplier_lead_requires_reason_and_rechecks_current_receiver(
    api_client,
) -> None:
    client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        assert operation is not None and company is not None
        lead = _lead(
            operation_id=operation.id,
            source_kind=LeadSourceKind.SUPPLIER_H5,
            status=LeadV12Status.DISPATCHED,
            phone="13900139702",
            snapshot_version=4,
            supplier_company_id=company.id,
        )
        db.add(lead)
        db.flush()
        assignment = Assignment(
            lead_id=lead.id,
            company_id=company.id,
            receiver_company_id=company.id,
            status=AssignmentStatus.PENDING_CLAIM.value,
            points_price=100,
            claim_points=100,
            price_version=1,
            lead_snapshot={"region_code": "310115"},
            assigned_by=operation.id,
            assigned_at=datetime.now(timezone.utc),
            idempotency_key="feedback-829-completion-item5-supplier",
        )
        db.add(assignment)
        db.flush()
        lead.current_assignment_id = assignment.id
        db.commit()
        lead_id = lead.id

    _login(client)
    missing_reason = client.patch(
        f"/api/v1/v1.2/platform/leads/{lead_id}/correction",
        json={"region_code": "110000", "expected_snapshot_version": 4},
    )
    assert missing_reason.status_code == 422, missing_reason.text
    assert missing_reason.json()["code"] == "LEAD_CORRECTION_REASON_REQUIRED"

    corrected = client.patch(
        f"/api/v1/v1.2/platform/leads/{lead_id}/correction",
        json={
            "region_code": "110000",
            "reason": "客户确认实际地区为北京",
            "expected_snapshot_version": 4,
        },
    )
    assert corrected.status_code == 200, corrected.text
    data = corrected.json()["data"]
    assert data["province"] == "北京市"
    assert data["city"] == "北京市"
    assert data["district"] is None
    assert data["pending_reason"] == "CORRECTION_REVIEW_REQUIRED"
    assert "SERVICE_REGION_MISMATCH" in data["correction_issues"]

    report = client.post(
        "/api/v1/v1.2/reports/leads/search",
        json={"region": "北京市", "page_size": 200},
    )
    assert report.status_code == 200, report.text
    report_row = next(
        item for item in report.json()["data"]["items"] if item["id"] == lead_id
    )
    assert report_row["province"] == "北京市"
    assert report_row["city"] == "北京市"

    from apps.api.src.services.lead_export_v12 import build_lead_export_workbook

    with factory() as db:
        archive_path, _row_count = build_lead_export_workbook(
            db,
            {"region": "北京市"},
        )
    try:
        rows = read_xlsx(archive_path)["客资明细"]
        export_row = next(row for row in rows if row["客资编号"] == lead_id)
        assert export_row["省份"] == "北京市"
        assert export_row["城市"] == "北京市"
        assert export_row["区县"] == ""
    finally:
        archive_path.unlink(missing_ok=True)

    with factory() as db:
        audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "V12_PLATFORM_LEAD_FACT_CORRECTION",
                AuditLog.resource_id == lead_id,
            )
        )
        assert audit is not None
        assert audit.before_json["region_code"] == "310115"
        assert audit.after_json["region_code"] == "110000"
        assert audit.after_json["province"] == "北京市"
        assert audit.after_json["city"] == "北京市"
        assert audit.metadata_json["reason"] == "客户确认实际地区为北京"

    released = client.post(
        f"/api/v1/v1.2/platform/leads/{lead_id}/correction/release-for-redispatch",
        json={
            "reason": "地区更正后原接收方不再符合资格",
            "expected_snapshot_version": 5,
        },
    )
    assert released.status_code == 200, released.text
    released_data = released.json()["data"]
    assert released_data["lead"]["source_kind"] == LeadSourceKind.SUPPLIER_H5.value
    assert released_data["lead"]["status"] == LeadV12Status.READY_DISPATCH.value
    assert released_data["lead"]["current_assignment_id"] is None
    assert released_data["assignment"]["status"] == AssignmentStatus.RELEASED.value


def test_unassigned_processing_lead_duplicate_phone_change_is_rejected(
    api_client,
) -> None:
    client, factory = api_client
    duplicate_phone = "13900139703"
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert operation is not None
        existing = _lead(
            operation_id=operation.id,
            source_kind=LeadSourceKind.PLATFORM_MANUAL,
            status=LeadV12Status.READY_DISPATCH,
            phone=duplicate_phone,
        )
        target = _lead(
            operation_id=operation.id,
            source_kind=LeadSourceKind.FEISHU_LEGACY,
            status=LeadV12Status.PENDING_OPERATION_DISPOSITION,
            phone="13900139704",
            snapshot_version=2,
        )
        db.add_all([existing, target])
        db.flush()
        submitted_task = VerificationTask(
            lead_id=target.id,
            task_type="PRE_DISPATCH_VERIFY",
            status="SUBMITTED",
            assignee_user_id=operation.id,
            assigned_by=operation.id,
            assigned_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            submitted_at=datetime.now(timezone.utc),
            verification_conclusion="QUALIFIED",
        )
        db.add(submitted_task)
        db.commit()
        target_id = target.id
        submitted_task_id = submitted_task.id

    _login(client)
    corrected = client.patch(
        f"/api/v1/v1.2/platform/leads/{target_id}/correction",
        json={"phone": duplicate_phone, "expected_snapshot_version": 2},
    )

    # 2026-09-19 确认口径：手机号只读，字段校验先于查重拒绝。
    assert corrected.status_code == 422, corrected.text
    assert corrected.json()["code"] == "LEAD_CORRECTION_FIELD_NOT_ALLOWED"
    assert corrected.json()["details"]["fields"] == ["phone"]

    with factory() as db:
        old_task = db.get(VerificationTask, submitted_task_id)
        target = db.get(Lead, target_id)
        pending_task = db.scalar(
            select(VerificationTask)
            .where(
                VerificationTask.lead_id == target_id,
                VerificationTask.status == "PENDING",
            )
        )
        assert old_task is not None and target is not None
        assert old_task.status == "SUBMITTED"
        assert pending_task is None
        assert target.status == LeadV12Status.PENDING_OPERATION_DISPOSITION.value
        assert target.phone_hash == hash_phone("13900139704")
        assert target.snapshot_version == 2
        event = db.scalar(
            select(LeadDedupEvent)
            .where(
                LeadDedupEvent.lead_id == target_id,
                LeadDedupEvent.checkpoint == "UNASSIGNED_CORRECTION",
            )
            .order_by(LeadDedupEvent.created_at.desc())
        )
        assert event is None


def test_region_correction_restarts_in_progress_pre_dispatch_verification(
    api_client,
) -> None:
    client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert operation is not None
        lead = _lead(
            operation_id=operation.id,
            source_kind=LeadSourceKind.FEISHU_LEGACY,
            status=LeadV12Status.PENDING_TELESALES_VERIFY,
            phone="13900139705",
        )
        db.add(lead)
        db.flush()
        old_task = VerificationTask(
            lead_id=lead.id,
            task_type="PRE_DISPATCH_VERIFY",
            status="IN_PROGRESS",
            assignee_user_id=operation.id,
            assigned_by=operation.id,
            assigned_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            due_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db.add(old_task)
        db.commit()
        lead_id = lead.id
        old_task_id = old_task.id

    _login(client)
    corrected = client.patch(
        f"/api/v1/v1.2/platform/leads/{lead_id}/correction",
        json={"region_code": "110000", "expected_snapshot_version": 1},
    )

    assert corrected.status_code == 200, corrected.text
    data = corrected.json()["data"]
    assert data["status"] == LeadV12Status.PENDING_TELESALES_VERIFY.value
    assert data["pending_reason"] == "CORRECTION_REVERIFY_REQUIRED"
    assert data["province"] == "北京市"
    with factory() as db:
        old_task = db.get(VerificationTask, old_task_id)
        new_task = db.scalar(
            select(VerificationTask).where(
                VerificationTask.lead_id == lead_id,
                VerificationTask.status == "PENDING",
            )
        )
        assert old_task is not None and new_task is not None
        assert old_task.status == "RELEASED"
        assert new_task.id != old_task.id


def test_ready_platform_correction_without_location_enters_telesales_queue(
    api_client,
) -> None:
    client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert operation is not None
        lead = _lead(
            operation_id=operation.id,
            source_kind=LeadSourceKind.PLATFORM_MANUAL,
            status=LeadV12Status.READY_DISPATCH,
            phone="13900139706",
        )
        lead.review_status = "APPROVED"
        db.add(lead)
        db.commit()
        lead_id = lead.id

    _login(client)
    corrected = client.patch(
        f"/api/v1/v1.2/platform/leads/{lead_id}/correction",
        json={
            "province": None,
            "city": None,
            "district": None,
            "region_code": None,
            "expected_snapshot_version": 1,
        },
    )

    assert corrected.status_code == 200, corrected.text
    data = corrected.json()["data"]
    assert data["status"] == LeadV12Status.PENDING_TELESALES_VERIFY.value
    assert data["pending_reason"] == "LOCATION_REQUIRES_TELESALES_VERIFY"
    with factory() as db:
        task = db.scalar(
            select(VerificationTask).where(
                VerificationTask.lead_id == lead_id,
                VerificationTask.status == "PENDING",
            )
        )
        assert task is not None


def test_historical_unassigned_correction_without_location_enters_telesales_queue(
    api_client,
) -> None:
    client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        receiver = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        assert operation is not None and receiver is not None
        lead = _lead(
            operation_id=operation.id,
            source_kind=LeadSourceKind.FEISHU_LEGACY,
            status=LeadV12Status.READY_DISPATCH,
            phone="13900139709",
        )
        lead.review_status = "APPROVED"
        db.add(lead)
        db.flush()
        db.add(
            Assignment(
                lead_id=lead.id,
                company_id=receiver.id,
                receiver_company_id=receiver.id,
                status=AssignmentStatus.RELEASED.value,
                points_price=100,
                lead_snapshot={},
                assigned_by=operation.id,
                assigned_at=datetime.now(timezone.utc),
                released_at=datetime.now(timezone.utc),
                release_reason="历史派发已解除",
                idempotency_key="feedback-829-history-location-recheck",
            )
        )
        db.commit()
        lead_id = lead.id

    _login(client)
    corrected = client.patch(
        f"/api/v1/v1.2/platform/leads/{lead_id}/correction",
        json={
            "province": None,
            "city": None,
            "district": None,
            "region_code": None,
            "reason": "客户暂无法确认地区",
            "expected_snapshot_version": 1,
        },
    )

    assert corrected.status_code == 200, corrected.text
    data = corrected.json()["data"]
    assert data["status"] == LeadV12Status.PENDING_TELESALES_VERIFY.value
    assert data["pending_reason"] == "LOCATION_REQUIRES_TELESALES_VERIFY"
    with factory() as db:
        task = db.scalar(
            select(VerificationTask).where(
                VerificationTask.lead_id == lead_id,
                VerificationTask.status == "PENDING",
            )
        )
        assert task is not None


def test_releasing_current_receiver_without_location_enters_telesales_queue(
    api_client,
) -> None:
    client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        receiver = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        assert operation is not None and receiver is not None
        lead = _lead(
            operation_id=operation.id,
            source_kind=LeadSourceKind.FEISHU_LEGACY,
            status=LeadV12Status.DISPATCHED,
            phone="13900139710",
        )
        db.add(lead)
        db.flush()
        assignment = Assignment(
            lead_id=lead.id,
            company_id=receiver.id,
            receiver_company_id=receiver.id,
            status=AssignmentStatus.PENDING_CLAIM.value,
            points_price=100,
            lead_snapshot={"region_code": lead.region_code},
            assigned_by=operation.id,
            assigned_at=datetime.now(timezone.utc),
            idempotency_key="feedback-829-current-location-release",
        )
        db.add(assignment)
        db.flush()
        lead.current_assignment_id = assignment.id
        db.commit()
        lead_id = lead.id

    _login(client)
    corrected = client.patch(
        f"/api/v1/v1.2/platform/leads/{lead_id}/correction",
        json={
            "province": None,
            "city": None,
            "district": None,
            "region_code": None,
            "reason": "客户暂无法确认地区",
            "expected_snapshot_version": 1,
        },
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["data"]["pending_reason"] == "CORRECTION_REVIEW_REQUIRED"

    released = client.post(
        f"/api/v1/v1.2/platform/leads/{lead_id}/correction/release-for-redispatch",
        json={
            "reason": "地区不明需重新电销核验",
            "expected_snapshot_version": 2,
        },
    )

    assert released.status_code == 200, released.text
    lead_data = released.json()["data"]["lead"]
    assert lead_data["status"] == LeadV12Status.PENDING_TELESALES_VERIFY.value
    assert lead_data["pending_reason"] == "LOCATION_REQUIRES_TELESALES_VERIFY"
    assert lead_data["current_assignment_id"] is None
    with factory() as db:
        task = db.scalar(
            select(VerificationTask).where(
                VerificationTask.lead_id == lead_id,
                VerificationTask.status == "PENDING",
            )
        )
        assert task is not None


def test_duplicate_phone_correction_without_location_is_rejected_before_queueing(
    api_client,
) -> None:
    client, factory = api_client
    duplicate_phone = "13900139707"
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert operation is not None
        existing = _lead(
            operation_id=operation.id,
            source_kind=LeadSourceKind.PLATFORM_MANUAL,
            status=LeadV12Status.READY_DISPATCH,
            phone=duplicate_phone,
        )
        target = _lead(
            operation_id=operation.id,
            source_kind=LeadSourceKind.FEISHU_LEGACY,
            status=LeadV12Status.READY_DISPATCH,
            phone="13900139708",
        )
        target.province = None
        target.city = None
        target.district = None
        target.region_code = None
        db.add_all([existing, target])
        db.commit()
        target_id = target.id

    _login(client)
    corrected = client.patch(
        f"/api/v1/v1.2/platform/leads/{target_id}/correction",
        json={"phone": duplicate_phone, "expected_snapshot_version": 1},
    )
    # 2026-09-19 确认口径：手机号只读，字段校验先于查重与派单队列。
    assert corrected.status_code == 422, corrected.text
    assert corrected.json()["code"] == "LEAD_CORRECTION_FIELD_NOT_ALLOWED"
    assert corrected.json()["details"]["fields"] == ["phone"]

    with factory() as db:
        lead = db.get(Lead, target_id)
        task = db.scalar(
            select(VerificationTask).where(
                VerificationTask.lead_id == target_id,
                VerificationTask.status == "PENDING",
            )
        )
        assert lead is not None
        assert lead.status == LeadV12Status.READY_DISPATCH.value
        assert lead.phone_hash == hash_phone("13900139708")
        assert task is None


@pytest.mark.parametrize(
    ("decision", "assignment_status", "lead_status", "reward_status"),
    [
        (
            DuplicateDecision.REWARD_DUPLICATE,
            AssignmentStatus.PENDING_CLAIM,
            LeadV12Status.DISPATCHED,
            RewardStatus.WAITING_CLAIM,
        ),
        (
            DuplicateDecision.HARD_DUPLICATE,
            AssignmentStatus.COMPLETED,
            LeadV12Status.COMPLETED,
            RewardStatus.OBSERVING,
        ),
    ],
)
def test_phone_correction_cancels_unsettled_ineligible_supplier_reward(
    api_client,
    monkeypatch,
    decision: DuplicateDecision,
    assignment_status: AssignmentStatus,
    lead_status: LeadV12Status,
    reward_status: RewardStatus,
) -> None:
    client, factory = api_client
    _patch_correction_dedup(monkeypatch, decision)
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        receiver = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        assert operation is not None and receiver is not None
        lead, _assignment, reward = _rewarded_supplier_lead(
            db,
            operation=operation,
            receiver=receiver,
            phone="13900139741",
            lead_status=lead_status,
            assignment_status=assignment_status,
            reward_status=reward_status,
        )
        db.commit()
        lead_id = lead.id
        reward_id = reward.id

    _login(client)
    corrected = client.patch(
        f"/api/v1/v1.2/platform/leads/{lead_id}/correction",
        json={
            "phone": "13900139742",
            "reason": "核对后更正联系电话",
            "expected_snapshot_version": 1,
        },
    )

    # 2026-09-19 确认口径：手机号只读，更正在字段校验即被拒绝，奖励不受影响。
    assert corrected.status_code == 422, corrected.text
    assert corrected.json()["code"] == "LEAD_CORRECTION_FIELD_NOT_ALLOWED"
    assert corrected.json()["details"]["fields"] == ["phone"]
    with factory() as db:
        reward = db.get(SupplierLeadReward, reward_id)
        audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "V12_PLATFORM_LEAD_FACT_CORRECTION",
                AuditLog.resource_id == lead_id,
            )
        )
        lead = db.get(Lead, lead_id)
        assert reward is not None and audit is None and lead is not None
        assert reward.status == reward_status.value
        assert reward.cancelled_at is None
        assert reward.exception_reason is None
        assert lead.phone_hash == hash_phone("13900139741")


def test_phone_change_correction_keeps_supplier_reward_intact(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        receiver = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        assert operation is not None and receiver is not None
        lead, _assignment, reward = _rewarded_supplier_lead(
            db,
            operation=operation,
            receiver=receiver,
            phone="13900139743",
            lead_status=LeadV12Status.DISPATCHED,
            assignment_status=AssignmentStatus.PENDING_CLAIM,
            reward_status=RewardStatus.WAITING_CLAIM,
        )
        db.commit()
        lead_id = lead.id
        reward_id = reward.id

    _login(client)
    rejected = client.patch(
        f"/api/v1/v1.2/platform/leads/{lead_id}/correction",
        json={
            "phone": "13900139744",
            "reason": "核对后更正联系电话",
            "expected_snapshot_version": 1,
        },
    )
    # 2026-09-19 确认口径：手机号只读，更正不再触发查重与奖励取消/恢复链路。
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["code"] == "LEAD_CORRECTION_FIELD_NOT_ALLOWED"
    assert rejected.json()["details"]["fields"] == ["phone"]
    with factory() as db:
        reward = db.get(SupplierLeadReward, reward_id)
        event_count = db.scalar(
            select(func.count(LeadDedupEvent.id)).where(
                LeadDedupEvent.lead_id == lead_id
            )
        )
        assert reward is not None
        assert reward.status == RewardStatus.WAITING_CLAIM.value
        assert reward.cancelled_at is None
        assert reward.exception_reason is None
        assert event_count == 0


def test_settled_supplier_reward_blocks_phone_correction_and_rolls_back(
    api_client,
    monkeypatch,
) -> None:
    client, factory = api_client
    _patch_correction_dedup(monkeypatch, DuplicateDecision.HARD_DUPLICATE)
    original_phone = "13900139746"
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        receiver = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        assert operation is not None and receiver is not None
        lead, _assignment, reward = _rewarded_supplier_lead(
            db,
            operation=operation,
            receiver=receiver,
            phone=original_phone,
            lead_status=LeadV12Status.COMPLETED,
            assignment_status=AssignmentStatus.COMPLETED,
            reward_status=RewardStatus.SETTLED,
        )
        db.commit()
        lead_id = lead.id
        reward_id = reward.id

    _login(client)
    blocked = client.patch(
        f"/api/v1/v1.2/platform/leads/{lead_id}/correction",
        json={
            "phone": "13900139747",
            "reason": "已结算奖励的电话更正",
            "expected_snapshot_version": 1,
        },
    )

    # 2026-09-19 确认口径：手机号只读，更正在字段校验即被拒绝，已结算奖励不受影响。
    assert blocked.status_code == 422, blocked.text
    assert blocked.json()["code"] == "LEAD_CORRECTION_FIELD_NOT_ALLOWED"
    assert blocked.json()["details"]["fields"] == ["phone"]
    with factory() as db:
        lead = db.get(Lead, lead_id)
        reward = db.get(SupplierLeadReward, reward_id)
        event_count = db.scalar(
            select(func.count(LeadDedupEvent.id)).where(
                LeadDedupEvent.lead_id == lead_id
            )
        )
        assert lead is not None and reward is not None
        assert lead.phone_hash == hash_phone(original_phone)
        assert lead.snapshot_version == 1
        assert reward.status == RewardStatus.SETTLED.value
        assert event_count == 0


def test_unresolved_correction_blocks_supplier_review(api_client) -> None:
    from apps.api.src.services.lead_supply_v12 import review_supplier_lead

    _client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        assert operation is not None and company is not None
        lead = _lead(
            operation_id=operation.id,
            source_kind=LeadSourceKind.SUPPLIER_H5,
            status=LeadV12Status.PENDING_REVIEW,
            phone="13900139711",
            supplier_company_id=company.id,
        )
        lead.pending_reason = "CORRECTION_REVIEW_REQUIRED"
        lead.raw_payload = {"correction_issues": ["DEDUP_DUPLICATE"]}
        db.add(lead)
        db.flush()

        with pytest.raises(AppError) as exc_info:
            review_supplier_lead(
                db,
                lead=lead,
                reviewer=_principal(operation),
                decision="INFO_INCOMPLETE",
                note="更正异常未处理前不能继续初审",
            )

        assert exc_info.value.code == "LEAD_CORRECTION_REVIEW_REQUIRED"
        assert lead.pending_reason == "CORRECTION_REVIEW_REQUIRED"


def test_unresolved_correction_blocks_telesales_submission(api_client) -> None:
    from apps.api.src.services.pre_dispatch_v12 import submit_pre_dispatch_verification

    _client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert operation is not None
        lead = _lead(
            operation_id=operation.id,
            source_kind=LeadSourceKind.FEISHU_LEGACY,
            status=LeadV12Status.PENDING_TELESALES_VERIFY,
            phone="13900139712",
        )
        lead.pending_reason = "CORRECTION_REVIEW_REQUIRED"
        lead.raw_payload = {"correction_issues": ["DEDUP_DUPLICATE"]}
        db.add(lead)
        db.flush()
        task = VerificationTask(
            lead_id=lead.id,
            task_type="PRE_DISPATCH_VERIFY",
            status="IN_PROGRESS",
            assignee_user_id=operation.id,
            assigned_by=operation.id,
            assigned_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            due_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db.add(task)
        db.flush()

        with pytest.raises(AppError) as exc_info:
            submit_pre_dispatch_verification(
                db,
                task_id=task.id,
                principal=_principal(operation),
                contact_result="CONNECTED",
                conclusion="QUALIFIED",
                note="客户事实已核实",
            )

        assert exc_info.value.code == "LEAD_CORRECTION_REVIEW_REQUIRED"
        assert task.status == "IN_PROGRESS"
        assert lead.pending_reason == "CORRECTION_REVIEW_REQUIRED"


def test_unresolved_correction_blocks_operation_disposition(api_client) -> None:
    from apps.api.src.services.pre_dispatch_v12 import decide_pre_dispatch_disposition

    _client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert operation is not None
        lead = _lead(
            operation_id=operation.id,
            source_kind=LeadSourceKind.FEISHU_LEGACY,
            status=LeadV12Status.PENDING_OPERATION_DISPOSITION,
            phone="13900139713",
        )
        lead.pending_reason = "CORRECTION_REVIEW_REQUIRED"
        lead.raw_payload = {"correction_issues": ["DEDUP_DUPLICATE"]}
        db.add(lead)
        db.flush()
        task = VerificationTask(
            lead_id=lead.id,
            task_type="PRE_DISPATCH_VERIFY",
            status="SUBMITTED",
            assignee_user_id=operation.id,
            assigned_by=operation.id,
            assigned_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            submitted_at=datetime.now(timezone.utc),
            verification_conclusion="QUALIFIED",
        )
        db.add(task)
        db.flush()

        with pytest.raises(AppError) as exc_info:
            decide_pre_dispatch_disposition(
                db,
                lead_id=lead.id,
                principal=_principal(operation),
                decision="APPROVE_POOL",
                note="运营确认进入派发池",
            )

        assert exc_info.value.code == "LEAD_CORRECTION_REVIEW_REQUIRED"
        assert lead.status == LeadV12Status.PENDING_OPERATION_DISPOSITION.value
        assert lead.pending_reason == "CORRECTION_REVIEW_REQUIRED"


def test_v12_lead_cannot_bypass_correction_rules_through_legacy_write_routes(
    api_client,
) -> None:
    client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert operation is not None
        target = _lead(
            operation_id=operation.id,
            source_kind=LeadSourceKind.FEISHU_LEGACY,
            status=LeadV12Status.READY_DISPATCH,
            phone="13900139721",
        )
        duplicate = _lead(
            operation_id=operation.id,
            source_kind=LeadSourceKind.PLATFORM_MANUAL,
            status=LeadV12Status.READY_DISPATCH,
            phone="13900139722",
        )
        db.add_all([target, duplicate])
        db.flush()
        db.add(
            LeadDuplicateRelation(
                lead_id=target.id,
                duplicate_lead_id=duplicate.id,
                reason="PHONE_WITHIN_WINDOW",
            )
        )
        db.commit()
        target_id = target.id
        duplicate_id = duplicate.id
        fingerprint_before = target.phone_fingerprint

    _login(client)
    staging = client.patch(
        f"/api/v1/leads/{target_id}/staging",
        json={"phone": "13900139723", "region_code": "110000"},
    )
    assert staging.status_code == 409, staging.text
    assert staging.json()["code"] == "LEAD_CORRECTION_API_REQUIRED"

    duplicate_decision = client.post(
        f"/api/v1/leads/{target_id}/duplicate-decision",
        json={"duplicate_lead_id": duplicate_id, "decision": "NOT_DUPLICATE"},
    )
    assert duplicate_decision.status_code == 409, duplicate_decision.text
    assert duplicate_decision.json()["code"] == "LEAD_CORRECTION_API_REQUIRED"

    with factory() as db:
        target = db.get(Lead, target_id)
        assert target is not None
        assert target.phone_fingerprint == fingerprint_before
        assert target.region_code == "310115"


@pytest.mark.parametrize("task_type", ["PRE_DISPATCH_VERIFY", "RETURN_VERIFY"])
def test_legacy_verification_route_cannot_mutate_v12_task(
    api_client,
    task_type: str,
) -> None:
    client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        telesales = db.scalar(select(User).where(User.username == "telesales"))
        assert operation is not None and telesales is not None
        lead = _lead(
            operation_id=operation.id,
            source_kind=LeadSourceKind.FEISHU_LEGACY,
            status=LeadV12Status.PENDING_TELESALES_VERIFY,
            phone="13900139724",
        )
        db.add(lead)
        db.flush()
        task = VerificationTask(
            lead_id=lead.id,
            task_type=task_type,
            status="ASSIGNED",
            assignee_user_id=telesales.id,
            assigned_by=operation.id,
            assigned_at=datetime.now(timezone.utc),
            due_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db.add(task)
        db.commit()
        task_id = task.id

    response = client.post(
        "/api/v1/auth/login",
        json={"username": "telesales", "password": "Telesales123!"},
    )
    assert response.status_code == 200, response.text
    legacy_start = client.post(f"/api/v1/verification/tasks/{task_id}/start")
    assert legacy_start.status_code == 409, legacy_start.text
    assert legacy_start.json()["code"] == "PRE_DISPATCH_V12_API_REQUIRED"

    with factory() as db:
        task = db.get(VerificationTask, task_id)
        assert task is not None
        assert task.status == "ASSIGNED"


@pytest.mark.parametrize(
    "source_kind",
    [LeadSourceKind.SUPPLIER_H5, LeadSourceKind.FEISHU_LEGACY],
)
def test_correction_consent_is_read_only_for_every_ready_source(
    api_client,
    source_kind: LeadSourceKind,
) -> None:
    client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        assert operation is not None and company is not None
        lead = _lead(
            operation_id=operation.id,
            source_kind=source_kind,
            status=LeadV12Status.READY_DISPATCH,
            phone="13900139728",
            supplier_company_id=(
                company.id if source_kind == LeadSourceKind.SUPPLIER_H5 else None
            ),
        )
        lead.review_status = "APPROVED"
        db.add(lead)
        db.commit()
        lead_id = lead.id

    _login(client)
    response = client.patch(
        f"/api/v1/v1.2/platform/leads/{lead_id}/correction",
        json={"consent_confirmed": False, "expected_snapshot_version": 1},
    )
    # 2026-09-19 确认口径：授权标记只读，字段校验先于提交校验拒绝。
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "LEAD_CORRECTION_FIELD_NOT_ALLOWED"
    assert response.json()["details"]["fields"] == ["consent_confirmed"]

    with factory() as db:
        lead = db.get(Lead, lead_id)
        assert lead is not None
        assert lead.consent_confirmed is True
        assert lead.status == LeadV12Status.READY_DISPATCH.value


def test_ready_feishu_correction_without_location_enters_telesales_queue(
    api_client,
) -> None:
    client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert operation is not None
        lead = _lead(
            operation_id=operation.id,
            source_kind=LeadSourceKind.FEISHU_LEGACY,
            status=LeadV12Status.READY_DISPATCH,
            phone="13900139729",
        )
        lead.review_status = "APPROVED"
        db.add(lead)
        db.commit()
        lead_id = lead.id

    _login(client)
    response = client.patch(
        f"/api/v1/v1.2/platform/leads/{lead_id}/correction",
        json={
            "province": None,
            "city": None,
            "district": None,
            "region_code": None,
            "expected_snapshot_version": 1,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == LeadV12Status.PENDING_TELESALES_VERIFY.value

    with factory() as db:
        lead = db.get(Lead, lead_id)
        task = db.scalar(
            select(VerificationTask).where(
                VerificationTask.lead_id == lead_id,
                VerificationTask.task_type == "PRE_DISPATCH_VERIFY",
            )
        )
        assert lead is not None and task is not None
        assert lead.status == LeadV12Status.PENDING_TELESALES_VERIFY.value
        assert lead.pending_reason == "LOCATION_REQUIRES_TELESALES_VERIFY"


def test_rejected_supplier_revision_cannot_clear_unresolved_correction(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        franchise = db.scalar(select(User).where(User.username == "franchise_demo"))
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        assert franchise is not None and company is not None
        lead = _lead(
            operation_id=franchise.id,
            source_kind=LeadSourceKind.SUPPLIER_H5,
            status=LeadV12Status.INVALID,
            phone="13900139725",
            supplier_company_id=company.id,
        )
        lead.review_status = "REJECTED"
        lead.pending_reason = "CORRECTION_REVIEW_REQUIRED"
        lead.raw_payload = {"correction_issues": ["DEDUP_DUPLICATE"]}
        db.add(lead)
        db.commit()
        lead_id = lead.id

    _login_franchise(client)
    revised = client.post(f"/api/v1/v1.2/supplier/leads/{lead_id}/revise")
    assert revised.status_code == 409, revised.text
    assert revised.json()["code"] == "LEAD_CORRECTION_REVIEW_REQUIRED"

    with factory() as db:
        lead = db.get(Lead, lead_id)
        assert lead is not None
        assert lead.status == LeadV12Status.INVALID.value
        assert lead.pending_reason == "CORRECTION_REVIEW_REQUIRED"


def test_historical_terminal_lead_rejects_duplicate_phone_correction(
    api_client,
) -> None:
    client, factory = api_client
    duplicate_phone = "13900139726"
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        assert operation is not None and company is not None
        existing = _lead(
            operation_id=operation.id,
            source_kind=LeadSourceKind.PLATFORM_MANUAL,
            status=LeadV12Status.READY_DISPATCH,
            phone=duplicate_phone,
        )
        historical = _lead(
            operation_id=operation.id,
            source_kind=LeadSourceKind.FEISHU_LEGACY,
            status=LeadV12Status.CLOSED,
            phone="13900139727",
            snapshot_version=3,
        )
        historical.review_status = "REJECTED"
        historical.pending_reason = "HISTORICAL_CLOSED"
        db.add_all([existing, historical])
        db.flush()
        db.add(
            Assignment(
                lead_id=historical.id,
                company_id=company.id,
                receiver_company_id=company.id,
                status=AssignmentStatus.COMPLETED.value,
                points_price=100,
                lead_snapshot={},
                assigned_by=operation.id,
                assigned_at=datetime.now(timezone.utc),
                idempotency_key="feedback-829-completion-item5-history",
            )
        )
        db.commit()
        lead_id = historical.id

    _login(client)
    corrected = client.patch(
        f"/api/v1/v1.2/platform/leads/{lead_id}/correction",
        json={
            "phone": duplicate_phone,
            "reason": "历史客资联系电话核对更正",
            "expected_snapshot_version": 3,
        },
    )
    # 2026-09-19 确认口径：手机号只读，历史客资同样在字段校验被拒。
    assert corrected.status_code == 422, corrected.text
    assert corrected.json()["code"] == "LEAD_CORRECTION_FIELD_NOT_ALLOWED"
    assert corrected.json()["details"]["fields"] == ["phone"]

    with factory() as db:
        lead = db.get(Lead, lead_id)
        assert lead is not None
        assert lead.status == LeadV12Status.CLOSED.value
        assert lead.review_status == "REJECTED"
        assert lead.pending_reason == "HISTORICAL_CLOSED"
        assert lead.phone_hash == hash_phone("13900139727")
        assert lead.snapshot_version == 3


def test_test_company_cleanup_does_not_clear_correction_review_blocker() -> None:
    lead = _lead(
        operation_id="operation-user",
        source_kind=LeadSourceKind.FEISHU_LEGACY,
        status=LeadV12Status.DUPLICATE,
        phone="13900139731",
    )
    lead.pending_reason = "CORRECTION_REVIEW_REQUIRED"
    lead.raw_payload = {"correction_issues": ["DEDUP_DUPLICATE"]}

    _restore_dedup_affected_lead(
        lead,
        blocks_dispatch=False,
        decision="CLEAR",
    )

    assert lead.status == LeadV12Status.DUPLICATE.value
    assert lead.pending_reason == "CORRECTION_REVIEW_REQUIRED"
    assert lead.raw_payload["correction_issues"] == ["DEDUP_DUPLICATE"]
