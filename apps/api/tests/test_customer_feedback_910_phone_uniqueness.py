from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, func, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from apps.api.src.core.errors import AppError
from apps.api.src.core.models import Lead
from apps.api.src.core.models_v12 import LeadDedupEvent
from apps.api.src.core.security import encrypt_text, fingerprint_phone, hash_phone
from apps.api.src.core.v12_enums import LeadSourceKind, LeadV12Status
from apps.api.src.services.phone_uniqueness import require_unique_lead_phone


PHONE = "13800139010"


def _lead(db: Session, phone: str = PHONE, **values) -> Lead:
    lead = Lead(
        **{
            "customer_name": "同名测试客户",
            "phone_encrypted": encrypt_text(phone),
            "phone_hash": hash_phone(phone),
            "phone_fingerprint": fingerprint_phone(phone),
            "status": "DRAFT",
            "source_kind": "PLATFORM_MANUAL",
            **values,
        }
    )
    db.add(lead)
    db.flush()
    return lead


def _assert_duplicate(db: Session, phone: str = PHONE, **kwargs) -> AppError:
    with pytest.raises(AppError) as caught:
        require_unique_lead_phone(db, phone=phone, **kwargs)
    assert caught.value.code == "LEAD_PHONE_DUPLICATE"
    assert caught.value.status_code == 409
    assert caught.value.details is None
    return caught.value


@pytest.mark.parametrize("status", [*LeadV12Status, "IMPORTED", "IMPORT_ERROR", "DUPLICATE_REVIEW"])
def test_same_phone_is_reserved_in_every_workflow_state(db, status) -> None:
    _lead(db, status=status)

    _assert_duplicate(db)


@pytest.mark.parametrize("source_kind", [*LeadSourceKind, None])
def test_same_phone_is_reserved_across_all_input_sources(db, source_kind) -> None:
    _lead(db, source_kind=source_kind)

    _assert_duplicate(db)


def test_soft_deleted_leads_no_longer_reserve_phone(db) -> None:
    """2026-09-17 确认口径：手机号唯一排除逻辑删除记录；同号仅剩已删记录时可新录入。"""
    old_time = datetime.now(timezone.utc) - timedelta(days=2000)
    _lead(
        db,
        status="INVALID",
        is_test=True,
        submitted_at=old_time,
        imported_at=old_time,
        deleted_at=datetime.now(timezone.utc),
        delete_reason="用户删除测试记录",
    )
    db.commit()
    db.expire_all()

    normalized = require_unique_lead_phone(db, phone=PHONE)
    assert normalized == PHONE


def test_undeleted_duplicate_still_blocks_even_when_deleted_exists(db) -> None:
    """历史/逻辑删除分开对待：只要存在未删除记录，同号仍被拦截。"""
    old_time = datetime.now(timezone.utc) - timedelta(days=2000)
    _lead(
        db,
        status="INVALID",
        deleted_at=old_time,
        delete_reason="历史删除记录",
    )
    _lead(db)
    db.commit()
    db.expire_all()

    _assert_duplicate(db)


def test_country_code_and_separators_do_not_create_another_phone_identity(db) -> None:
    _lead(db)

    _assert_duplicate(db, "+86 138-0013-9010")


def test_legacy_phone_hash_is_checked_when_fingerprint_is_missing(db) -> None:
    _lead(db, phone_fingerprint=None, source_kind=None)

    _assert_duplicate(db)


def test_fingerprint_is_checked_when_legacy_hash_uses_another_identity(db) -> None:
    _lead(db, phone_hash=hash_phone("13800139011"))

    _assert_duplicate(db)


def test_matching_only_name_does_not_block_a_different_phone(db) -> None:
    _lead(db)

    assert require_unique_lead_phone(db, phone="+86 138-0013-9011") == "13800139011"


def test_edit_excludes_itself_but_still_checks_other_records(db) -> None:
    current = _lead(db)
    other = _lead(db, "13800139011")

    assert require_unique_lead_phone(db, phone=PHONE, exclude_lead_id=current.id) == PHONE
    _assert_duplicate(db, "13800139011", exclude_lead_id=current.id)
    assert other.phone_hash == hash_phone("13800139011")


def test_excluding_one_historical_duplicate_does_not_hide_another(db) -> None:
    current = _lead(db)
    _lead(db)

    _assert_duplicate(db, exclude_lead_id=current.id)


def test_conflict_does_not_disclose_the_matching_customer_or_phone(db) -> None:
    existing = _lead(db, customer_name="不可泄露的客户姓名")

    error = _assert_duplicate(db)

    assert PHONE not in error.message
    assert existing.id not in error.message
    assert existing.customer_name not in error.message


def test_empty_drafts_do_not_reserve_a_phone(db) -> None:
    _lead(db, "")

    assert require_unique_lead_phone(db, phone="") == ""
    assert require_unique_lead_phone(db, phone="   ") == ""


def test_strict_check_preserves_reward_dedup_state_and_the_callers_transaction(db) -> None:
    existing = _lead(db, duplicate_status="OVERRIDDEN", status="READY_DISPATCH")
    existing_id = existing.id

    _assert_duplicate(db)

    assert existing.duplicate_status == "OVERRIDDEN"
    assert existing.status == "READY_DISPATCH"
    assert db.scalar(select(func.count(LeadDedupEvent.id))) == 0
    db.rollback()
    assert db.get(Lead, existing_id) is None


def test_postgres_concurrent_new_drafts_reserve_the_phone_once() -> None:
    database_url = os.environ.get("V12_E2E_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("requires the disposable V12 E2E PostgreSQL database")
    engine = create_engine(database_url, pool_pre_ping=True)
    if engine.dialect.name != "postgresql":
        engine.dispose()
        pytest.skip("phone identity advisory-lock coverage requires PostgreSQL")
    if "leads" not in inspect(engine).get_table_names():
        engine.dispose()
        pytest.skip("database schema must be initialized before this test")
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    run_id = uuid4().hex
    phone = f"199{int(run_id[:8], 16) % 100_000_000:08d}"
    lead_ids = [str(uuid4()), str(uuid4())]
    barrier = Barrier(2)

    def create_once(lead_id: str) -> str:
        with factory() as session:
            session.execute(text("SET LOCAL lock_timeout = '10s'"))
            barrier.wait(timeout=10)
            try:
                require_unique_lead_phone(session, phone=phone)
                _lead(session, phone, id=lead_id)
                session.commit()
                return "CREATED"
            except AppError as error:
                session.rollback()
                assert error.code == "LEAD_PHONE_DUPLICATE"
                assert error.status_code == 409
                assert error.details is None
                return error.code

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create_once, lead_ids))
        assert sorted(results) == ["CREATED", "LEAD_PHONE_DUPLICATE"]
        with factory() as session:
            assert session.scalar(select(func.count(Lead.id)).where(Lead.id.in_(lead_ids))) == 1
    finally:
        with factory() as session:
            session.execute(delete(Lead).where(Lead.id.in_(lead_ids)))
            session.commit()
        engine.dispose()
