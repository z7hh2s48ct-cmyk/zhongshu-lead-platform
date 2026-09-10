from __future__ import annotations

from datetime import date, datetime, timezone
import os
import subprocess
import sys
from pathlib import Path
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, inspect

ROOT = Path(__file__).resolve().parents[3]


def _alembic(database_url: str, *args: str) -> None:
    if len(args) != 2 or args[0] not in {"upgrade", "downgrade"}:
        raise ValueError("migration tests only allow upgrade or downgrade with one revision")

    # Each historical migration needs a clean interpreter, independent of prior tests.
    result = subprocess.run(
        ["python", "-m", "alembic", *args],
        executable=sys.executable,
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=90,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stdout + result.stderr)


def _required_row(table: sa.Table, **overrides) -> dict:
    """Build a valid migration fixture from the reflected historical schema."""

    now = datetime.now(timezone.utc)
    row: dict = {}
    for column in table.columns:
        if column.name in overrides:
            row[column.name] = overrides[column.name]
            continue
        if column.nullable or column.default is not None or column.server_default is not None:
            continue
        if column.primary_key:
            row[column.name] = str(uuid.uuid4())
        elif isinstance(column.type, sa.DateTime):
            row[column.name] = now
        elif isinstance(column.type, sa.Date):
            row[column.name] = date.today()
        elif isinstance(column.type, sa.Boolean):
            row[column.name] = False
        elif isinstance(column.type, (sa.Integer, sa.BigInteger)):
            row[column.name] = 0
        elif isinstance(column.type, sa.JSON):
            row[column.name] = {}
        else:
            row[column.name] = "fixture"
    row.update(overrides)
    return row


def test_v101_raw_fixture_insert_uses_lead_test_server_default(tmp_path: Path) -> None:
    database = tmp_path / "v101-raw-fixture.db"
    database_url = f"sqlite:///{database}"
    _alembic(database_url, "upgrade", "0001_initial")
    engine = create_engine(database_url)
    lead_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO leads (
                    id, source_type, customer_name, phone_encrypted, phone_hash,
                    acquisition_cost_cents, status, imported_at, snapshot_version,
                    raw_payload, created_at, updated_at
                ) VALUES (
                    :id, 'MANUAL', 'V1.0.1 migration fixture', 'encrypted', :phone_hash,
                    0, 'QUALIFIED', :now, 1, '{}', :now, :now
                )
                """
            ),
            {"id": lead_id, "phone_hash": f"hash-{lead_id}", "now": now},
        )
        is_test = connection.execute(
            sa.text("SELECT is_test FROM leads WHERE id = :id"),
            {"id": lead_id},
        ).scalar_one()

    assert is_test in (False, 0)


def _seed_legacy_reward_before_snapshot_migration(engine) -> str:
    metadata = sa.MetaData()
    metadata.reflect(engine)
    users = metadata.tables["users"]
    companies = metadata.tables["companies"]
    leads = metadata.tables["leads"]
    assignments = metadata.tables["assignments"]
    rewards = metadata.tables["supplier_lead_rewards"]

    user_id = str(uuid.uuid4())
    supplier_id = str(uuid.uuid4())
    receiver_id = str(uuid.uuid4())
    lead_id = str(uuid.uuid4())
    assignment_id = str(uuid.uuid4())
    reward_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    with engine.begin() as connection:
        connection.execute(
            users.insert(),
            _required_row(
                users,
                id=user_id,
                username=f"migration-{user_id[:8]}",
                display_name="迁移测试用户",
                status="ACTIVE",
                session_version=1,
                created_at=now,
                updated_at=now,
            ),
        )
        connection.execute(
            companies.insert(),
            [
                _required_row(
                    companies,
                    id=supplier_id,
                    code=f"MS-{supplier_id[:8]}",
                    name="历史奖励供应商",
                    status="ACTIVE",
                    level_code="V1",
                    created_at=now,
                    updated_at=now,
                ),
                _required_row(
                    companies,
                    id=receiver_id,
                    code=f"MR-{receiver_id[:8]}",
                    name="历史奖励接收方",
                    status="ACTIVE",
                    level_code="V1",
                    created_at=now,
                    updated_at=now,
                ),
            ],
        )
        connection.execute(
            leads.insert(),
            _required_row(
                leads,
                id=lead_id,
                source_type="SUPPLIER_H5",
                source_kind="SUPPLIER_H5",
                submitter_user_id=user_id,
                supplier_company_id=supplier_id,
                customer_name="历史奖励客户",
                phone_encrypted="ciphertext",
                phone_hash="h" * 64,
                phone_fingerprint="f" * 64,
                consent_confirmed=True,
                status="CLAIMED",
                review_status="APPROVED",
                duplicate_status="CLEAR",
                acquisition_cost_cents=0,
                imported_at=now,
                submitted_at=now,
                snapshot_version=1,
                raw_payload={},
                created_at=now,
                updated_at=now,
            ),
        )
        connection.execute(
            assignments.insert(),
            _required_row(
                assignments,
                id=assignment_id,
                lead_id=lead_id,
                company_id=receiver_id,
                receiver_company_id=receiver_id,
                supplier_company_id=supplier_id,
                status="CLAIMED",
                points_price=200,
                claim_points=200,
                price_version=1,
                lead_snapshot={},
                assigned_by=user_id,
                assigned_at=now,
                claimed_at=now,
                idempotency_key=f"migration-assignment-{assignment_id}",
                created_at=now,
                updated_at=now,
            ),
        )
        connection.execute(
            rewards.insert(),
            _required_row(
                rewards,
                id=reward_id,
                lead_id=lead_id,
                assignment_id=assignment_id,
                supplier_company_id=supplier_id,
                receiver_company_id=receiver_id,
                status="OBSERVING",
                claim_points=200,
                reward_ratio_bps=2750,
                reward_points=55,
                rule_version=7,
                created_at=now,
                updated_at=now,
            ),
        )
    return reward_id


def test_v12_migration_upgrades_and_downgrades_from_v101(tmp_path: Path) -> None:
    database = tmp_path / "migration.db"
    database_url = f"sqlite:///{database}"
    _alembic(database_url, "upgrade", "0003_v12_active_assignment")

    engine = create_engine(database_url)
    reward_id = _seed_legacy_reward_before_snapshot_migration(engine)
    _alembic(database_url, "upgrade", "head")

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "calendar_days" in tables
    assert "supplier_lead_rewards" in tables
    assert "phone_fingerprint" in {column["name"] for column in inspector.get_columns("leads")}
    assert "is_test" in {column["name"] for column in inspector.get_columns("leads")}
    assert "appeal_deadline_at" in {column["name"] for column in inspector.get_columns("assignments")}
    assert "rule_snapshot_json" in {
        column["name"] for column in inspector.get_columns("supplier_lead_rewards")
    }
    assert "review_note" in {
        column["name"] for column in inspector.get_columns("company_lead_capabilities")
    }
    indexes = {item["name"]: item for item in inspector.get_indexes("assignments")}
    assert indexes["uq_assignments_active_lead_v12"]["unique"] == 1

    metadata = sa.MetaData()
    metadata.reflect(engine, only=["supplier_lead_rewards"])
    rewards = metadata.tables["supplier_lead_rewards"]
    with engine.connect() as connection:
        snapshot = connection.execute(
            sa.select(rewards.c.rule_snapshot_json).where(rewards.c.id == reward_id)
        ).scalar_one()
    assert snapshot["legacy_backfill"] is True
    assert snapshot["ratio_bps"] == 2750
    assert snapshot["version"] == 7
    assert snapshot["hard_duplicate_days"] == 90
    assert snapshot["reward_duplicate_days"] == 180
    assert snapshot["historical_suspect_days"] == 365

    _alembic(database_url, "downgrade", "0001_initial")
    inspector = inspect(engine)
    assert "calendar_days" not in set(inspector.get_table_names())
    assert "phone_fingerprint" not in {column["name"] for column in inspector.get_columns("leads")}
    assert "is_test" not in {column["name"] for column in inspector.get_columns("leads")}
    assert "uq_assignments_active_lead_v12" not in {
        item["name"] for item in inspector.get_indexes("assignments")
    }

    # A release rollback may later need to move forward again.  Verify that
    # SQLite's batch-table path can restore the current head after rollback.
    _alembic(database_url, "upgrade", "head")
    inspector = inspect(engine)
    assignment_columns = {column["name"] for column in inspector.get_columns("assignments")}
    assert {"internal_assignee_user_id", "internal_assigned_by", "internal_assigned_at"} <= assignment_columns
    assert "is_test" in {column["name"] for column in inspector.get_columns("leads")}


def test_storage_cleanup_downgrade_refuses_to_drop_unfinished_jobs(tmp_path: Path) -> None:
    database = tmp_path / "storage-cleanup-downgrade.db"
    database_url = f"sqlite:///{database}"
    _alembic(database_url, "upgrade", "head")
    engine = create_engine(database_url)
    metadata = sa.MetaData()
    metadata.reflect(engine, only=["storage_cleanup_outbox"])
    cleanup = metadata.tables["storage_cleanup_outbox"]
    cleanup_id = str(uuid.uuid4())
    with engine.begin() as connection:
        connection.execute(
            cleanup.insert().values(
                id=cleanup_id,
                event_key=f"migration-test:{cleanup_id}",
                object_key="returns/private.bin",
                storage_backend="local",
                storage_namespace="/private/storage",
                source_type="return_evidence",
                source_id=str(uuid.uuid4()),
                reason="迁移回滚安全测试",
                status="PENDING",
                attempts=0,
                created_at=datetime.now(timezone.utc),
            )
        )

    with pytest.raises(RuntimeError) as exc_info:
        _alembic(database_url, "downgrade", "0013_internal_user_test")
    assert "unfinished storage cleanup jobs" in str(exc_info.value)
    assert "storage_cleanup_outbox" in set(inspect(engine).get_table_names())

    with engine.begin() as connection:
        connection.execute(
            cleanup.update()
            .where(cleanup.c.id == cleanup_id)
            .values(status="DELETED")
        )
    _alembic(database_url, "downgrade", "0013_internal_user_test")
    assert "storage_cleanup_outbox" not in set(inspect(engine).get_table_names())


def test_lead_test_flag_migration_defaults_false_and_is_reversible(tmp_path: Path) -> None:
    database = tmp_path / "lead-test-flag.db"
    database_url = f"sqlite:///{database}"
    _alembic(database_url, "upgrade", "head")
    engine = create_engine(database_url)
    metadata = sa.MetaData()
    metadata.reflect(engine, only=["leads"])
    leads = metadata.tables["leads"]
    lead_id = str(uuid.uuid4())
    with engine.begin() as connection:
        connection.execute(
            leads.insert().values(
                **_required_row(
                    leads,
                    id=lead_id,
                    customer_name="默认正式客资",
                    phone_encrypted="encrypted",
                    phone_hash=f"hash-{lead_id}",
                    raw_payload={},
                )
            )
        )
        assert connection.execute(
            sa.select(leads.c.is_test).where(leads.c.id == lead_id)
        ).scalar_one() is False

    _alembic(database_url, "downgrade", "0015_customer_feedback_829")
    assert "is_test" not in {
        column["name"] for column in inspect(engine).get_columns("leads")
    }
    _alembic(database_url, "upgrade", "head")
    assert "is_test" in {
        column["name"] for column in inspect(engine).get_columns("leads")
    }


def test_pre_dispatch_template_migration_publishes_default_and_is_reversible(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pre-dispatch-template.db"
    database_url = f"sqlite:///{database}"
    _alembic(database_url, "upgrade", "0016_lead_test_flag")
    engine = create_engine(database_url)
    metadata = sa.MetaData()
    metadata.reflect(engine, only=["verification_templates"])
    templates = metadata.tables["verification_templates"]
    draft_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    with engine.begin() as connection:
        connection.execute(
            templates.insert().values(
                **_required_row(
                    templates,
                    id=draft_id,
                    code="PRE_DISPATCH",
                    name="运营草稿",
                    version=3,
                    schema_json={"fields": [{"key": "draft-only"}]},
                    status="DRAFT",
                    effective_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
        )

    _alembic(database_url, "upgrade", "head")

    with engine.connect() as connection:
        rows = connection.execute(
            sa.select(
                templates.c.id,
                templates.c.name,
                templates.c.version,
                templates.c.schema_json,
                templates.c.status,
            )
            .where(templates.c.code == "PRE_DISPATCH")
            .order_by(templates.c.version)
        ).mappings().all()

    assert rows == [
        {
            "id": draft_id,
            "name": "运营草稿",
            "version": 3,
            "schema_json": {"fields": [{"key": "draft-only"}]},
            "status": "DRAFT",
        },
        {
            "id": "1f7b6405-9e0f-4ec7-a073-1dbd02b46137",
            "name": "前置电销核验模板",
            "version": 4,
            "schema_json": {"fields": []},
            "status": "PUBLISHED",
        },
    ]

    _alembic(database_url, "downgrade", "0016_lead_test_flag")
    with engine.connect() as connection:
        remaining = connection.execute(
            sa.select(templates.c.id, templates.c.status).where(
                templates.c.code == "PRE_DISPATCH"
            )
        ).mappings().all()

    assert remaining == [{"id": draft_id, "status": "DRAFT"}]


def test_pre_dispatch_template_migration_keeps_existing_published_versions(
    tmp_path: Path,
) -> None:
    database = tmp_path / "existing-pre-dispatch-template.db"
    database_url = f"sqlite:///{database}"
    _alembic(database_url, "upgrade", "0016_lead_test_flag")
    engine = create_engine(database_url)
    metadata = sa.MetaData()
    metadata.reflect(engine, only=["verification_templates"])
    templates = metadata.tables["verification_templates"]
    now = datetime.now(timezone.utc)
    existing_ids = [str(uuid.uuid4()), str(uuid.uuid4())]

    with engine.begin() as connection:
        connection.execute(
            templates.insert(),
            [
                _required_row(
                    templates,
                    id=template_id,
                    code="PRE_DISPATCH",
                    name=f"现有已发布模板 v{version}",
                    version=version,
                    schema_json={"fields": []},
                    status="PUBLISHED",
                    effective_at=now,
                    created_at=now,
                    updated_at=now,
                )
                for version, template_id in enumerate(existing_ids, start=1)
            ],
        )

    _alembic(database_url, "upgrade", "head")
    with engine.connect() as connection:
        current_ids = connection.execute(
            sa.select(templates.c.id)
            .where(templates.c.code == "PRE_DISPATCH")
            .order_by(templates.c.version)
        ).scalars().all()

    assert current_ids == existing_ids

    _alembic(database_url, "downgrade", "0016_lead_test_flag")
    with engine.connect() as connection:
        remaining_ids = connection.execute(
            sa.select(templates.c.id)
            .where(templates.c.code == "PRE_DISPATCH")
            .order_by(templates.c.version)
        ).scalars().all()

    assert remaining_ids == existing_ids


def test_pre_dispatch_template_downgrade_refuses_referenced_template(
    tmp_path: Path,
) -> None:
    database = tmp_path / "referenced-pre-dispatch-template.db"
    database_url = f"sqlite:///{database}"
    _alembic(database_url, "upgrade", "head")
    engine = create_engine(database_url)
    metadata = sa.MetaData()
    metadata.reflect(
        engine,
        only=["leads", "verification_templates", "verification_tasks"],
    )
    leads = metadata.tables["leads"]
    templates = metadata.tables["verification_templates"]
    tasks = metadata.tables["verification_tasks"]
    lead_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    template_id = "1f7b6405-9e0f-4ec7-a073-1dbd02b46137"

    with engine.begin() as connection:
        template_version = connection.execute(
            sa.select(templates.c.version).where(templates.c.id == template_id)
        ).scalar_one()
        connection.execute(
            leads.insert().values(
                **_required_row(
                    leads,
                    id=lead_id,
                    source_type="PLATFORM_MANUAL",
                    source_kind="PLATFORM_MANUAL",
                    customer_name="模板回滚保护测试客户",
                    phone_encrypted="encrypted",
                    phone_hash=f"hash-{lead_id}",
                    phone_fingerprint=f"fingerprint-{lead_id}",
                    consent_confirmed=True,
                    status="PENDING_TELESALES_VERIFY",
                    review_status="PENDING",
                    duplicate_status="CLEAR",
                    raw_payload={},
                )
            )
        )
        connection.execute(
            tasks.insert().values(
                **_required_row(
                    tasks,
                    id=task_id,
                    lead_id=lead_id,
                    template_id=template_id,
                    template_version=template_version,
                    task_type="PRE_DISPATCH_VERIFY",
                    status="ASSIGNED",
                    lock_version=1,
                )
            )
        )

    with pytest.raises(RuntimeError) as exc_info:
        _alembic(database_url, "downgrade", "0016_lead_test_flag")

    assert "verification tasks reference the seeded template" in str(exc_info.value)
    with engine.connect() as connection:
        assert connection.execute(
            sa.select(templates.c.id).where(templates.c.id == template_id)
        ).scalar_one() == template_id

    with engine.begin() as connection:
        connection.execute(tasks.delete().where(tasks.c.id == task_id))
    _alembic(database_url, "downgrade", "0016_lead_test_flag")


def test_feedback_migration_downgrade_refuses_to_drop_business_data(
    tmp_path: Path,
) -> None:
    database = tmp_path / "feedback-downgrade.db"
    database_url = f"sqlite:///{database}"
    _alembic(database_url, "upgrade", "head")
    engine = create_engine(database_url)
    metadata = sa.MetaData()
    metadata.reflect(engine, only=["leads", "lead_export_tasks"])
    leads = metadata.tables["leads"]
    export_tasks = metadata.tables["lead_export_tasks"]
    lead_id = str(uuid.uuid4())
    with engine.begin() as connection:
        connection.execute(
            leads.insert().values(
                **_required_row(
                    leads,
                    id=lead_id,
                    source_type="PLATFORM_MANUAL",
                    source_kind="PLATFORM_MANUAL",
                    customer_name="迁移回滚测试客户",
                    phone_encrypted="encrypted",
                    phone_hash=f"hash-{lead_id}",
                    phone_fingerprint=f"fingerprint-{lead_id}",
                    consent_confirmed=True,
                    source_detail="老客户转介绍",
                    status="DRAFT",
                    review_status="PENDING",
                    duplicate_status="PENDING",
                    raw_payload={},
                )
            )
        )

    with pytest.raises(RuntimeError) as exc_info:
        _alembic(database_url, "downgrade", "0014_storage_cleanup")
    assert "lead source details exist" in str(exc_info.value)
    inspector = inspect(engine)
    assert "lead_export_tasks" in set(inspector.get_table_names())
    assert "source_detail" in {
        column["name"] for column in inspector.get_columns("leads")
    }

    export_id = str(uuid.uuid4())
    with engine.begin() as connection:
        connection.execute(
            leads.update().where(leads.c.id == lead_id).values(source_detail=None)
        )
        connection.execute(
            export_tasks.insert().values(
                **_required_row(
                    export_tasks,
                    id=export_id,
                    requested_by=None,
                    requested_by_name="迁移回滚测试运营",
                    status="COMPLETED",
                    filters_json={},
                    include_full_phone=True,
                    idempotency_key=f"migration-{export_id}",
                    row_count=1,
                )
            )
        )

    with pytest.raises(RuntimeError) as exc_info:
        _alembic(database_url, "downgrade", "0014_storage_cleanup")
    assert "lead export tasks exist" in str(exc_info.value)
    assert "lead_export_tasks" in set(inspect(engine).get_table_names())

    with engine.begin() as connection:
        connection.execute(export_tasks.delete())
    _alembic(database_url, "downgrade", "0014_storage_cleanup")
    inspector = inspect(engine)
    assert "lead_export_tasks" not in set(inspector.get_table_names())
    assert "source_detail" not in {
        column["name"] for column in inspector.get_columns("leads")
    }
