"""PostgreSQL-only cross-session concurrency coverage for V1.2 operations."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import os
from types import SimpleNamespace
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from apps.api.src.core.auth import Principal
from apps.api.src.core.enums import AssignmentStatus, FollowStatus
from apps.api.src.core.errors import AppError
from apps.api.src.core.models import (
    Assignment,
    AuditLog,
    Company,
    FollowUp,
    Lead,
    LeadExportTask,
    PointsAccount,
    PointsLedger,
    Role,
    User,
)
from apps.api.src.core.models_v12 import CompanyLeadCapability, CompanyServiceAreaV12
from apps.api.src.core.security import encrypt_text, fingerprint_phone, hash_phone
from apps.api.src.core.v12_enums import DuplicateDecision, LeadSourceKind, LeadV12Status
from apps.api.src.routers import v12_insights as insights_router
from apps.api.src.routers import v12_lead_supply as lead_supply_router
from apps.api.src.schemas.v12_lead_supply import LeadQuickDispatchBody
from apps.api.src.services.claim_service import claim_assignment
from apps.api.src.services.dedup_v12 import apply_submission_decision, evaluate_phone
from apps.api.src.services.followup_service import add_followup
from apps.api.src.services.lead_supply_v12 import release_corrected_lead_for_redispatch


def test_same_new_phone_is_deduplicated_across_database_sessions() -> None:
    engine, factory = _postgres_factory()
    suffix = uuid4().hex[:10]
    shared_phone = f"138{int(suffix[:8], 16) % 100_000_000:08d}"
    try:
        with factory() as db:
            operation = User(
                username=f"dedup-pg-{suffix}",
                display_name="查重并发运营",
                status="ACTIVE",
            )
            db.add(operation)
            db.flush()
            leads = []
            for index in range(2):
                original_phone = f"137{index}{int(suffix[:7], 16) % 10_000_000:07d}"
                lead = Lead(
                    source_type=LeadSourceKind.PLATFORM_MANUAL.value,
                    source_kind=LeadSourceKind.PLATFORM_MANUAL.value,
                    submitter_user_id=operation.id,
                    customer_name=f"并发查重客户{index}",
                    phone_encrypted=encrypt_text(original_phone),
                    phone_hash=hash_phone(original_phone),
                    phone_fingerprint=fingerprint_phone(original_phone),
                    consent_confirmed=True,
                    province="上海市",
                    city="上海市",
                    district="浦东新区",
                    region_code="310115",
                    source_channel="OTHER",
                    source_detail="并发查重回归",
                    status=LeadV12Status.DRAFT.value,
                    review_status="DRAFT",
                    duplicate_status=DuplicateDecision.CLEAR.value,
                    imported_at=datetime.now(timezone.utc),
                    raw_payload={},
                )
                db.add(lead)
                leads.append(lead)
            db.commit()
            lead_ids = [lead.id for lead in leads]

        barrier = Barrier(2)

        def correct_once(lead_id: str) -> str:
            with factory() as db:
                lead = db.get(Lead, lead_id)
                assert lead is not None
                lead.phone_encrypted = encrypt_text(shared_phone)
                lead.phone_hash = hash_phone(shared_phone)
                lead.phone_fingerprint = fingerprint_phone(shared_phone)
                barrier.wait(timeout=10)
                result = evaluate_phone(
                    db,
                    lead=lead,
                    normalized_phone=shared_phone,
                    checkpoint="POSTGRES_CONCURRENT_CORRECTION",
                )
                apply_submission_decision(lead, result)
                db.commit()
                return result.decision.value

        with ThreadPoolExecutor(max_workers=2) as pool:
            decisions = list(pool.map(correct_once, lead_ids))

        assert sorted(decisions) == sorted(
            [
                DuplicateDecision.CLEAR.value,
                DuplicateDecision.HARD_DUPLICATE.value,
            ]
        )
    finally:
        engine.dispose()


def _postgres_factory():
    database_url = os.environ.get("V12_E2E_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("requires the disposable V12 E2E PostgreSQL database")
    engine = create_engine(database_url, pool_pre_ping=True)
    if engine.dialect.name != "postgresql":
        engine.dispose()
        pytest.skip("quick-dispatch advisory-lock coverage is PostgreSQL only")
    if "assignments" not in inspect(engine).get_table_names():
        engine.dispose()
        pytest.skip("database schema not initialized; run scripts/run_v12_e2e.py")
    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )
    return engine, factory


def test_same_quick_dispatch_key_is_serialized_across_database_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = _postgres_factory()
    suffix = uuid4().hex[:10]
    phone = f"139{int(suffix[:8], 16) % 100_000_000:08d}"
    idempotency_key = f"quick-dispatch-pg-{suffix}"
    try:
        with factory() as db:
            company = Company(code=f"QDPG-{suffix}", name="快捷派发并发公司", status="ACTIVE")
            operation = User(
                username=f"qdpg-{suffix}",
                display_name="快捷派发并发运营",
                status="ACTIVE",
            )
            db.add_all([company, operation])
            db.flush()
            owner_role = db.scalar(select(Role).where(Role.code == "FRANCHISE_OWNER"))
            employee_role = db.scalar(select(Role).where(Role.code == "FRANCHISE_EMPLOYEE"))
            assert owner_role is not None and employee_role is not None
            owner = User(
                username=f"qdpg-owner-{suffix}",
                display_name="快捷派发负责人",
                status="ACTIVE",
                company_id=company.id,
                roles=[owner_role],
            )
            employee = User(
                username=f"qdpg-employee-{suffix}",
                display_name="快捷派发员工",
                status="ACTIVE",
                company_id=company.id,
                roles=[employee_role],
            )
            db.add_all([owner, employee])
            db.flush()
            company.primary_user_id = owner.id
            db.add_all(
                [
                    CompanyLeadCapability(
                        company_id=company.id,
                        capability_code="LEAD_RECEIVER",
                        active=True,
                        review_status="APPROVED",
                    ),
                    CompanyServiceAreaV12(
                        company_id=company.id,
                        region_code="310115",
                        region_level="DISTRICT",
                        active=True,
                        review_status="APPROVED",
                    ),
                    PointsAccount(company_id=company.id, balance=100, version=0),
                ]
            )
            db.commit()
            company_id = company.id
            operation_id = operation.id
            employee_user_id = employee.id

        body = LeadQuickDispatchBody(
            customer_name="快捷派发并发客户",
            phone=phone,
            city="上海市",
            district="浦东新区",
            region_code="310115",
            category_code="OLD_RENOVATION",
            brand_code="ZHONGSHU",
            source_channel="OTHER",
            source_detail="并发回归测试",
            consent_confirmed=True,
            company_id=company_id,
            employee_user_id=employee_user_id,
            idempotency_key=idempotency_key,
            note="同一幂等键并发快捷派发",
        )
        principal = Principal(
            user_id=operation_id,
            display_name="快捷派发并发运营",
            company_id=None,
            role_codes=frozenset({"OPERATION"}),
            permission_codes=frozenset({"lead.manual.manage", "lead.dispatch"}),
            session_version=1,
        )
        barrier = Barrier(2)
        monkeypatch.setattr(
            lead_supply_router,
            "manual_dispatch_idempotency_guard",
            lambda _key: nullcontext(),
        )

        def dispatch_once(index: int) -> dict:
            with factory() as db:
                barrier.wait(timeout=10)
                request = SimpleNamespace(
                    state=SimpleNamespace(request_id=f"quick-dispatch-pg-{suffix}-{index}")
                )
                return lead_supply_router.quick_dispatch_platform_lead(
                    body=body,
                    request=request,
                    principal=principal,
                    db=db,
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(dispatch_once, range(2)))

        assert all(response["code"] == "OK" for response in responses)
        assignment_ids = {response["data"]["assignment"]["id"] for response in responses}
        lead_ids = {response["data"]["lead"]["id"] for response in responses}
        assert len(assignment_ids) == 1
        assert len(lead_ids) == 1
        assert sorted(response["data"]["idempotent"] for response in responses) == [False, True]

        assignment_id = assignment_ids.pop()
        with factory() as db:
            assert db.scalar(
                select(func.count(Lead.id)).where(Lead.phone_hash.is_not(None), Lead.id.in_(lead_ids))
            ) == 1
            assert db.scalar(
                select(func.count(Assignment.id)).where(
                    Assignment.idempotency_key == idempotency_key
                )
            ) == 1
            assert db.scalar(
                select(func.count(AuditLog.id)).where(
                    AuditLog.action == "V12_MANUAL_DISPATCH",
                    AuditLog.resource_id == assignment_id,
                )
            ) == 1
            account = db.scalar(
                select(PointsAccount).where(PointsAccount.company_id == company_id)
            )
            assignment = db.get(Assignment, assignment_id)
            assert account is not None and account.balance == 100
            assert assignment is not None and assignment.points_price == 100
    finally:
        engine.dispose()


def _seed_correction_race(
    factory,
    *,
    suffix: str,
    claimed: bool,
) -> tuple[str, str, str, Principal, Principal, int]:
    now = datetime.now(timezone.utc)
    phone = f"138{int(suffix[:8], 16) % 100_000_000:08d}"
    with factory() as db:
        company = Company(
            code=f"CRPG-{suffix}",
            name="更正释放并发公司",
            status="ACTIVE",
        )
        operation = User(
            username=f"crpg-operation-{suffix}",
            display_name="更正释放运营",
            status="ACTIVE",
        )
        owner = User(
            username=f"crpg-owner-{suffix}",
            display_name="更正释放加盟商",
            status="ACTIVE",
        )
        db.add_all([company, operation, owner])
        db.flush()
        owner.company_id = company.id
        company.primary_user_id = owner.id
        db.add(PointsAccount(company_id=company.id, balance=100, version=1))
        lead = Lead(
            source_type="MANUAL",
            source_channel="OTHER",
            source_detail="PostgreSQL 并发回归",
            customer_name="更正释放并发客户",
            phone_encrypted=encrypt_text(phone),
            phone_hash=hash_phone(phone),
            phone_fingerprint=fingerprint_phone(phone),
            city="上海市",
            district="浦东新区",
            region_code="310115",
            source_kind=LeadSourceKind.PLATFORM_MANUAL.value,
            consent_confirmed=True,
            duplicate_status="CLEAR",
            review_status="APPROVED",
            status=LeadV12Status.DISPATCHED.value,
            snapshot_version=3,
            raw_payload={},
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
            assigned_at=now,
            expires_at=now + timedelta(hours=1),
            idempotency_key=f"correction-race-{suffix}",
        )
        db.add(assignment)
        db.flush()
        lead.current_assignment_id = assignment.id
        db.commit()
        company_id = company.id
        owner_id = owner.id
        operation_id = operation.id
        lead_id = lead.id
        assignment_id = assignment.id

    franchise_principal = Principal(
        user_id=owner_id,
        display_name="更正释放加盟商",
        company_id=company_id,
        role_codes=frozenset({"FRANCHISE_OWNER"}),
        permission_codes=frozenset(
            {"assignment.own.claim", "followup.own.manage"}
        ),
        session_version=1,
    )
    operation_principal = Principal(
        user_id=operation_id,
        display_name="更正释放运营",
        company_id=None,
        role_codes=frozenset({"OPERATION"}),
        permission_codes=frozenset({"lead.manual.manage"}),
        session_version=1,
    )
    if claimed:
        with factory() as db:
            claim_assignment(
                db,
                assignment_id,
                franchise_principal,
                f"correction-race-claim-{suffix}",
            )
            db.commit()
    with factory() as db:
        lead = db.get(Lead, lead_id)
        assert lead is not None
        lead.pending_reason = "CORRECTION_REVIEW_REQUIRED"
        lead.raw_payload = {
            **dict(lead.raw_payload or {}),
            "correction_issues": ["SERVICE_REGION_MISMATCH"],
        }
        db.commit()
    return (
        company_id,
        lead_id,
        assignment_id,
        operation_principal,
        franchise_principal,
        3,
    )


def test_correction_release_and_claim_are_serialized_across_database_sessions() -> None:
    engine, factory = _postgres_factory()
    suffix = uuid4().hex[:10]
    try:
        (
            company_id,
            lead_id,
            assignment_id,
            operation_principal,
            franchise_principal,
            snapshot_version,
        ) = _seed_correction_race(factory, suffix=suffix, claimed=False)
        barrier = Barrier(2)

        def release_once() -> str:
            with factory() as db:
                barrier.wait(timeout=10)
                try:
                    release_corrected_lead_for_redispatch(
                        db,
                        lead_id=lead_id,
                        principal=operation_principal,
                        reason="更正后原接收方不再符合资格",
                        expected_snapshot_version=snapshot_version,
                    )
                    db.commit()
                    return "RELEASED"
                except AppError as exc:
                    db.rollback()
                    return exc.code

        def claim_once() -> str:
            with factory() as db:
                barrier.wait(timeout=10)
                try:
                    claim_assignment(
                        db,
                        assignment_id,
                        franchise_principal,
                        f"correction-race-claim-{suffix}",
                    )
                    db.commit()
                    return "CLAIMED"
                except AppError as exc:
                    db.rollback()
                    return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            release_future = pool.submit(release_once)
            claim_future = pool.submit(claim_once)
            outcomes = {
                release_future.result(timeout=20),
                claim_future.result(timeout=20),
            }

        assert "RELEASED" in outcomes
        assert outcomes & {
            "LEAD_CORRECTION_REVIEW_REQUIRED",
            "ASSIGNMENT_NOT_CLAIMABLE",
        }
        with factory() as db:
            assignment = db.get(Assignment, assignment_id)
            lead = db.get(Lead, lead_id)
            account = db.scalar(
                select(PointsAccount).where(PointsAccount.company_id == company_id)
            )
            assert assignment is not None
            assert assignment.status == AssignmentStatus.RELEASED.value
            assert lead is not None
            assert lead.status == LeadV12Status.READY_DISPATCH.value
            assert lead.current_assignment_id is None
            assert account is not None and int(account.balance) == 100
            assert db.scalar(
                select(func.count(PointsLedger.id)).where(
                    PointsLedger.business_id == assignment_id,
                    PointsLedger.delta < 0,
                )
            ) == 0
    finally:
        engine.dispose()


def test_correction_release_and_followup_are_serialized_across_database_sessions() -> None:
    engine, factory = _postgres_factory()
    suffix = uuid4().hex[:10]
    try:
        (
            company_id,
            lead_id,
            assignment_id,
            operation_principal,
            franchise_principal,
            snapshot_version,
        ) = _seed_correction_race(factory, suffix=suffix, claimed=True)
        barrier = Barrier(2)

        def release_once() -> str:
            with factory() as db:
                barrier.wait(timeout=10)
                try:
                    release_corrected_lead_for_redispatch(
                        db,
                        lead_id=lead_id,
                        principal=operation_principal,
                        reason="更正后退回积分并重新派发",
                        expected_snapshot_version=snapshot_version,
                    )
                    db.commit()
                    return "RELEASED"
                except AppError as exc:
                    db.rollback()
                    return exc.code

        def followup_once() -> str:
            with factory() as db:
                barrier.wait(timeout=10)
                assignment = db.get(Assignment, assignment_id)
                assert assignment is not None
                try:
                    add_followup(
                        db,
                        assignment=assignment,
                        principal=franchise_principal,
                        status=FollowStatus.CONTACTED.value,
                        note="并发尝试跟进",
                        next_followup_at=None,
                    )
                    db.commit()
                    return "FOLLOWED"
                except AppError as exc:
                    db.rollback()
                    return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            release_future = pool.submit(release_once)
            followup_future = pool.submit(followup_once)
            outcomes = {
                release_future.result(timeout=20),
                followup_future.result(timeout=20),
            }

        assert "RELEASED" in outcomes
        assert outcomes & {
            "LEAD_CORRECTION_REVIEW_REQUIRED",
            "FOLLOWUP_NOT_ALLOWED",
            "FOLLOWUP_ASSIGNMENT_STALE",
        }
        with factory() as db:
            assignment = db.get(Assignment, assignment_id)
            lead = db.get(Lead, lead_id)
            account = db.scalar(
                select(PointsAccount).where(PointsAccount.company_id == company_id)
            )
            assert assignment is not None
            assert assignment.status == AssignmentStatus.RELEASED.value
            assert lead is not None
            assert lead.status == LeadV12Status.READY_DISPATCH.value
            assert lead.current_assignment_id is None
            assert account is not None and int(account.balance) == 100
            assert db.scalar(
                select(func.count(FollowUp.id)).where(
                    FollowUp.assignment_id == assignment_id
                )
            ) == 0
    finally:
        engine.dispose()


def test_lead_export_queue_limit_is_serialized_across_database_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = _postgres_factory()
    suffix = uuid4().hex[:10]
    try:
        with factory() as db:
            operation = User(
                username=f"export-pg-{suffix}",
                display_name="导出队列并发运营",
                status="ACTIVE",
            )
            db.add(operation)
            db.flush()
            db.add(
                LeadExportTask(
                    requested_by=operation.id,
                    requested_by_name=operation.display_name,
                    status="PENDING",
                    filters_json={
                        "created_from": None,
                        "created_to": None,
                        "source_kind": None,
                        "receiver_company_id": None,
                        "lead_status": None,
                        "assignment_status": None,
                        "assigned_by_user_id": None,
                    },
                    include_full_phone=True,
                    idempotency_key=f"export-pg-existing-{suffix}",
                )
            )
            db.commit()
            operation_id = operation.id

        principal = Principal(
            user_id=operation_id,
            display_name="导出队列并发运营",
            company_id=None,
            role_codes=frozenset({"OPERATION"}),
            permission_codes=frozenset({"lead.phone.export"}),
            session_version=1,
        )
        monkeypatch.setattr(
            insights_router,
            "_lead_export_queue_guard",
            lambda: nullcontext(),
        )
        monkeypatch.setattr(
            insights_router,
            "get_settings",
            lambda: SimpleNamespace(
                lead_export_active_per_user_limit=2,
                lead_export_active_global_limit=1_000,
                lead_export_rolling_24h_per_user_limit=1_000,
            ),
        )
        barrier = Barrier(2)

        def request_once(index: int) -> str:
            with factory() as db:
                barrier.wait(timeout=10)
                try:
                    response = insights_router.request_lead_export(
                        body=insights_router.ScopedLeadExportRequestBody(
                            idempotency_key=f"export-pg-new-{suffix}-{index}"
                        ),
                        request=SimpleNamespace(
                            state=SimpleNamespace(
                                request_id=f"export-pg-request-{suffix}-{index}"
                            )
                        ),
                        principal=principal,
                        db=db,
                    )
                    return response["code"]
                except AppError as exc:
                    db.rollback()
                    return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(request_once, range(2)))

        assert sorted(outcomes) == ["LEAD_EXPORT_ACTIVE_LIMIT", "OK"]
        with factory() as db:
            assert db.scalar(
                select(func.count(LeadExportTask.id)).where(
                    LeadExportTask.requested_by == operation_id,
                    LeadExportTask.status.in_(("PENDING", "RUNNING")),
                )
            ) == 2
    finally:
        engine.dispose()
