from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
import os
import time
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from apps.api.src.core import models_v12 as _models_v12  # noqa: F401
from apps.api.src.core.auth import Principal
from apps.api.src.core.database import Base
from apps.api.src.core.enums import AssignmentStatus
from apps.api.src.core.errors import AppError
from apps.api.src.core.models import (
    Assignment,
    Company,
    Lead,
    PointsAccount,
    PointsLedger,
    User,
)
from apps.api.src.core.security import encrypt_text, hash_phone
from apps.api.src.core.models_v12 import CompanyLeadCapability, CompanyServiceAreaV12
from apps.api.src.services.claim_service import claim_assignment
from apps.api.src.services.dispatch_v12 import claim_assignment as claim_assignment_v12
from apps.api.src.services.lead_points_v12 import update_lead_points_settings


@pytest.fixture
def claim_points_factory():
    database_url = os.environ.get("CLAIM_POINTS_POSTGRES_TEST_URL", "").strip()
    if not database_url:
        pytest.skip("set CLAIM_POINTS_POSTGRES_TEST_URL to an isolated PostgreSQL database")
    admin = create_engine(database_url, pool_pre_ping=True)
    if admin.dialect.name != "postgresql":
        admin.dispose()
        pytest.skip("claim points concurrency test requires PostgreSQL")
    schema = "claim_points_probe_" + uuid4().hex
    with admin.begin() as connection:
        connection.execute(CreateSchema(schema))
    engine = create_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema} -clock_timeout=8000 -cstatement_timeout=10000"},
    )
    try:
        Base.metadata.create_all(engine)
        yield sessionmaker(
            bind=engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            class_=Session,
        )
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin.dispose()


@pytest.mark.parametrize("claim_mode", ["legacy", "v12"])
def test_settings_publish_claim_and_replay_are_serialized(claim_points_factory, claim_mode) -> None:
    factory = claim_points_factory
    suffix = uuid4().hex[:10]
    publish_ready = Event()
    allow_publish_commit = Event()
    claim_started = Event()
    try:
        with factory() as db:
            actor = User(
                username=f"claim-points-pg-{suffix}",
                display_name="实时积分并发管理员",
                status="ACTIVE",
            )
            owner = User(
                username=f"claim-points-owner-pg-{suffix}",
                display_name="实时积分并发负责人",
                status="ACTIVE",
            )
            company = Company(
                code=f"CPPG-{suffix}",
                name="实时积分并发公司",
                status="ACTIVE",
            )
            db.add_all([actor, owner, company])
            db.flush()
            owner.company_id = company.id
            company.primary_user_id = owner.id
            db.add_all([
                CompanyLeadCapability(company_id=company.id, capability_code="LEAD_RECEIVER", active=True, review_status="APPROVED"),
                CompanyServiceAreaV12(company_id=company.id, region_code="310100", region_level="CITY", is_primary_city=True, active=True, review_status="APPROVED"),
            ])
            update_lead_points_settings(
                db,
                operation_claim_points=100,
                supplier_provision_points=30,
                expected_version=0,
                updated_by=actor.id,
            )
            lead = Lead(
                customer_name="实时积分并发客户",
                phone_encrypted=encrypt_text("13900139999"),
                phone_hash=hash_phone("13900139999"),
                city="上海市",
                region_code="310100",
                category_code="OLD_RENOVATION",
                status="DISPATCHED" if claim_mode == "v12" else "ASSIGNED",
                source_kind="PLATFORM_MANUAL",
                source_type="PLATFORM_MANUAL",
            )
            db.add(lead)
            db.flush()
            assignment = Assignment(
                lead_id=lead.id,
                company_id=company.id,
                status=AssignmentStatus.PENDING_CLAIM.value,
                points_price=100,
                claim_points=100,
                price_version=1,
                lead_snapshot={},
                assigned_by=actor.id,
                assigned_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                idempotency_key=f"claim-points-pg-{suffix}",
            )
            db.add_all([assignment, PointsAccount(company_id=company.id, balance=500, version=1)])
            db.flush()
            lead.current_assignment_id = assignment.id
            db.commit()
            actor_id = actor.id
            owner_id = owner.id
            company_id = company.id
            assignment_id = assignment.id

        principal = Principal(
            user_id=owner_id,
            display_name="实时积分并发负责人",
            company_id=company_id,
            role_codes=frozenset({"FRANCHISE_OWNER"}),
            permission_codes=frozenset({"assignment.own.claim"}),
            session_version=1,
        )

        def claim(db, key, expected_points):
            if claim_mode == "v12":
                result = claim_assignment_v12(
                    db,
                    assignment_id=assignment_id,
                    company_id=company_id,
                    claimed_by=owner_id,
                    expected_points=expected_points,
                )
                return result.assignment, result.ledger
            return claim_assignment(db, assignment_id, principal, key, expected_points=expected_points)

        def publish_price() -> None:
            with factory() as db:
                update_lead_points_settings(
                    db,
                    operation_claim_points=150,
                    supplier_provision_points=30,
                    expected_version=1,
                    updated_by=actor_id,
                )
                publish_ready.set()
                assert allow_publish_commit.wait(timeout=10)
                db.commit()

        def claim_with_stale_price() -> str:
            assert publish_ready.wait(timeout=10)
            with factory() as db:
                claim_started.set()
                try:
                    claim(db, f"stale-claim-{suffix}", 100)
                except AppError as exc:
                    db.rollback()
                    return exc.code
                raise AssertionError("stale expected points must be rejected")

        with ThreadPoolExecutor(max_workers=2) as pool:
            publish_future = pool.submit(publish_price)
            stale_claim_future = pool.submit(claim_with_stale_price)
            assert claim_started.wait(timeout=10)
            time.sleep(0.2)
            assert not stale_claim_future.done()
            allow_publish_commit.set()
            publish_future.result(timeout=10)
            assert stale_claim_future.result(timeout=10) == "CLAIM_PRICE_CHANGED"

        with factory() as db:
            assignment = db.get(Assignment, assignment_id)
            account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == company_id))
            assert assignment is not None and assignment.status == AssignmentStatus.PENDING_CLAIM.value
            assert assignment.points_price == 100
            assert account is not None and account.balance == 500
            assert db.scalar(
                select(func.count(PointsLedger.id)).where(
                    PointsLedger.business_id == assignment_id,
                    PointsLedger.ledger_type == "CLAIM",
                )
            ) == 0

        def claim_latest(index: int) -> tuple[str, int]:
            with factory() as db:
                assignment, ledger = claim(db, f"latest-claim-{suffix}-{index}", 150)
                db.commit()
                return ledger.id, assignment.points_price

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(claim_latest, range(2)))

        assert len({ledger_id for ledger_id, _ in outcomes}) == 1
        assert {points for _, points in outcomes} == {150}
        with factory() as db:
            assignment = db.get(Assignment, assignment_id)
            account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == company_id))
            assert assignment is not None
            assert assignment.points_price == assignment.claim_points == 150
            assert assignment.price_rule_id is None
            assert assignment.price_version == 2
            assert account is not None and account.balance == 350
            assert db.scalar(
                select(func.count(PointsLedger.id)).where(
                    PointsLedger.business_id == assignment_id,
                    PointsLedger.ledger_type == "CLAIM",
                )
            ) == 1
    finally:
        allow_publish_commit.set()
