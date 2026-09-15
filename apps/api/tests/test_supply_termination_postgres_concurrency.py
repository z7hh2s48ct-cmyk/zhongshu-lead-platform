"""Exercise supply-write and termination races against PostgreSQL row locks."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

from sqlalchemy import event, select

from apps.api.src.core import models_v12  # noqa: F401
from apps.api.src.core.models import Company, Lead, SupplyTerminationRequest, User
from apps.api.src.services.supply_termination import (
    create_termination_request,
    require_supply_write_enabled,
)
from apps.api.tests.test_return_concurrency_postgres import (
    postgres_factory as postgres_factory,
)


def _is_company_lock(statement: str) -> bool:
    normalized = " ".join(statement.upper().split())
    return "FROM COMPANIES" in normalized and "FOR UPDATE" in normalized


def test_supply_write_committed_before_termination_is_reported_as_blocker(
    postgres_factory,
) -> None:
    with postgres_factory() as db:
        company = Company(code="TERM-PG-RACE", name="终止合作并发测试", status="ACTIVE")
        db.add(company)
        db.flush()
        owner = User(
            username="term-pg-race-owner",
            display_name="并发测试负责人",
            password_hash="test",
            status="ACTIVE",
            company_id=company.id,
        )
        db.add(owner)
        db.flush()
        company.primary_user_id = owner.id
        company_id = company.id
        owner_id = owner.id
        db.commit()

    supply_locked = Event()
    termination_attempted = Event()

    def create_supply_lead_first() -> str:
        with postgres_factory() as db:
            connection = db.connection()
            paused = False

            def pause_after_company_lock(
                conn, cursor, statement, parameters, context, executemany
            ):
                nonlocal paused
                if _is_company_lock(statement) and not paused:
                    paused = True
                    supply_locked.set()
                    assert termination_attempted.wait(3), (
                        "termination did not contend on Company"
                    )

            event.listen(connection, "after_cursor_execute", pause_after_company_lock)
            require_supply_write_enabled(db, company_id)
            lead = Lead(
                source_type="SUPPLIER_H5",
                source_kind="SUPPLIER_H5",
                submitter_user_id=owner_id,
                supplier_company_id=company_id,
                customer_name="并发新增客资",
                phone_encrypted="test-encrypted",
                phone_hash="term-pg-race-phone",
                status="DRAFT",
            )
            db.add(lead)
            db.commit()
            return lead.id

    def request_termination_second() -> str:
        assert supply_locked.wait(3), "supply write did not acquire the Company lock"
        with postgres_factory() as db:
            connection = db.connection()

            def signal_company_attempt(
                conn, cursor, statement, parameters, context, executemany
            ):
                if _is_company_lock(statement):
                    termination_attempted.set()

            event.listen(connection, "before_cursor_execute", signal_company_attempt)
            item = create_termination_request(
                db,
                company_id=company_id,
                requested_by=owner_id,
                reason="不再提供新客资",
                payee_name="张三",
                payee_account="6222000012345678",
                payment_method="BANK_TRANSFER",
            )
            db.commit()
            return item.id

    with ThreadPoolExecutor(max_workers=2) as executor:
        supply = executor.submit(create_supply_lead_first)
        termination = executor.submit(request_termination_second)
        lead_id = supply.result(timeout=10)
        request_id = termination.result(timeout=10)

    with postgres_factory() as db:
        item = db.get(SupplyTerminationRequest, request_id)
        company = db.get(Company, company_id)
        blocker = next(
            entry
            for entry in item.blockers_json
            if entry["code"] == "SUPPLIED_LEADS_UNFINISHED"
        )
        assert blocker["record_ids"] == [lead_id]
        assert blocker["count"] == 1
        assert company.supplier_cooperation_status == "TERMINATION_PENDING"
        assert db.scalar(select(Lead.id).where(Lead.id == lead_id)) == lead_id
