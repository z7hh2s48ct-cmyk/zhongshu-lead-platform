from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
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


def test_supply_wallet_migration_preserves_total_and_blocks_bad_accounts(tmp_path: Path) -> None:
    database = tmp_path / "supply-wallet-split.db"
    database_url = f"sqlite:///{database}"
    _alembic(database_url, "upgrade", "0020_audit_action_resource_index")
    engine = create_engine(database_url)
    metadata = sa.MetaData()
    metadata.reflect(engine)
    companies = metadata.tables["companies"]
    accounts = metadata.tables["points_accounts"]
    ledgers = metadata.tables["points_ledgers"]
    now = datetime.now(timezone.utc)
    valid_company, blocked_company, missing_reward_company, sequence_company, orphan_reward_company = (
        str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    )
    valid_account, blocked_account, missing_reward_account, sequence_account, orphan_reward_account = (
        str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    )
    reward_ledger_id = str(uuid.uuid4())
    supplier_rewards = metadata.tables["supplier_lead_rewards"]
    with engine.begin() as connection:
        connection.execute(companies.insert(), [
            _required_row(companies, id=valid_company, code="SPLIT-OK", name="可分账", status="ACTIVE", created_at=now, updated_at=now),
            _required_row(companies, id=blocked_company, code="SPLIT-BAD", name="异常分账", status="ACTIVE", created_at=now, updated_at=now),
            _required_row(companies, id=missing_reward_company, code="SPLIT-MISSING", name="奖励关联缺失", status="ACTIVE", created_at=now, updated_at=now),
            _required_row(companies, id=sequence_company, code="SPLIT-SEQUENCE", name="流水顺序异常", status="ACTIVE", created_at=now, updated_at=now),
            _required_row(companies, id=orphan_reward_company, code="SPLIT-ORPHAN", name="奖励流水缺失", status="ACTIVE", created_at=now, updated_at=now),
        ])
        connection.execute(accounts.insert(), [
            _required_row(accounts, id=valid_account, company_id=valid_company, balance=60, version=1, created_at=now, updated_at=now),
            _required_row(accounts, id=blocked_account, company_id=blocked_company, balance=-1, version=1, created_at=now, updated_at=now),
            _required_row(accounts, id=missing_reward_account, company_id=missing_reward_company, balance=10, version=1, created_at=now, updated_at=now),
            _required_row(accounts, id=sequence_account, company_id=sequence_company, balance=10, version=1, created_at=now, updated_at=now),
            _required_row(accounts, id=orphan_reward_account, company_id=orphan_reward_company, balance=0, version=1, created_at=now, updated_at=now),
        ])
        connection.execute(supplier_rewards.insert(), [
            _required_row(
                supplier_rewards,
                id="reward-1",
                lead_id="reward-lead-1",
                assignment_id="reward-assignment-1",
                supplier_company_id=valid_company,
                receiver_company_id=valid_company,
                status="SETTLED",
                reward_points=100,
                ledger_id=reward_ledger_id,
                created_at=now,
                updated_at=now,
            ),
            _required_row(
                supplier_rewards,
                id="orphan-reward",
                lead_id="orphan-reward-lead",
                assignment_id="orphan-reward-assignment",
                supplier_company_id=orphan_reward_company,
                receiver_company_id=orphan_reward_company,
                status="SETTLED",
                reward_points=20,
                ledger_id=None,
                created_at=now,
                updated_at=now,
            ),
        ])
        connection.execute(ledgers.insert(), [
            _required_row(ledgers, id=reward_ledger_id, account_id=valid_account, company_id=valid_company, ledger_type="REWARD", delta=100, balance_after=100, business_type="V12_SUPPLIER_REWARD", business_id="reward-1", idempotency_key="split-reward-1", created_at=now),
            _required_row(ledgers, id=str(uuid.uuid4()), account_id=valid_account, company_id=valid_company, ledger_type="CLAIM", delta=-40, balance_after=60, business_type="ASSIGNMENT", business_id="claim-1", idempotency_key="split-claim-1", created_at=now + timedelta(seconds=1)),
            _required_row(ledgers, id=str(uuid.uuid4()), account_id=blocked_account, company_id=blocked_company, ledger_type="CLAIM", delta=-1, balance_after=-1, business_type="ASSIGNMENT", business_id="claim-bad", idempotency_key="split-claim-bad", created_at=now),
            _required_row(ledgers, id=str(uuid.uuid4()), account_id=missing_reward_account, company_id=missing_reward_company, ledger_type="REWARD", delta=10, balance_after=10, business_type="V12_SUPPLIER_REWARD", business_id="missing-reward", idempotency_key="split-missing-reward", created_at=now),
            _required_row(ledgers, id=str(uuid.uuid4()), account_id=sequence_account, company_id=sequence_company, ledger_type="ADJUST", delta=20, balance_after=19, business_type="MANUAL_ADJUSTMENT", business_id="bad-sequence-1", idempotency_key="bad-sequence-1", created_at=now),
            _required_row(ledgers, id=str(uuid.uuid4()), account_id=sequence_account, company_id=sequence_company, ledger_type="CLAIM", delta=-10, balance_after=10, business_type="ASSIGNMENT", business_id="bad-sequence-2", idempotency_key="bad-sequence-2", created_at=now + timedelta(seconds=1)),
        ])
    _alembic(database_url, "upgrade", "head")
    metadata = sa.MetaData()
    metadata.reflect(engine, only=["points_accounts", "points_ledgers", "system_configs"])
    accounts, ledgers = metadata.tables["points_accounts"], metadata.tables["points_ledgers"]
    with engine.connect() as connection:
        valid = connection.execute(sa.select(accounts).where(accounts.c.id == valid_account)).mappings().one()
        blocked = connection.execute(sa.select(accounts).where(accounts.c.id == blocked_account)).mappings().one()
        missing_reward = connection.execute(sa.select(accounts).where(accounts.c.id == missing_reward_account)).mappings().one()
        bad_sequence = connection.execute(sa.select(accounts).where(accounts.c.id == sequence_account)).mappings().one()
        orphan_reward = connection.execute(sa.select(accounts).where(accounts.c.id == orphan_reward_account)).mappings().one()
        deltas = connection.execute(sa.select(ledgers.c.point_kind, sa.func.sum(ledgers.c.delta)).where(
            ledgers.c.company_id == valid_company
        ).group_by(ledgers.c.point_kind)).all()
        rate_count = connection.execute(sa.text(
            "SELECT count(*) FROM system_configs WHERE domain='supply_termination' AND key='cashout_rate'"
        )).scalar_one()
        legacy_balance = connection.execute(sa.select(ledgers.c.legacy_balance_after).where(ledgers.c.id == reward_ledger_id)).scalar_one()
        split_metadata = connection.execute(sa.select(ledgers.c.metadata_json).where(
            ledgers.c.business_type == "POINTS_WALLET_SPLIT",
            ledgers.c.company_id == valid_company,
        )).scalars().first()
    assert valid["balance"] == 0
    assert valid["supply_balance"] == 60
    assert valid["balance"] + valid["supply_balance"] == 60
    assert dict(deltas) == {"CUSTOMER": 0, "SUPPLY": 60}
    assert blocked["points_split_status"] == "BLOCKED"
    assert valid["points_split_snapshot_json"] == {
        "rule_version": 1, "old_balance": 60, "ledger_total": 60,
        "reward_net": 100, "customer_result": 0, "supply_result": 60,
        "exception_reason": None,
    }
    assert blocked["points_split_snapshot_json"]["exception_reason"] == "NEGATIVE_BALANCE"
    assert missing_reward["points_split_status"] == "BLOCKED"
    assert missing_reward["points_split_snapshot_json"]["exception_reason"] == "MISSING_REWARD_ASSOCIATION"
    assert bad_sequence["points_split_status"] == "BLOCKED"
    assert bad_sequence["points_split_snapshot_json"]["exception_reason"] == "LEDGER_SEQUENCE_MISMATCH"
    assert orphan_reward["points_split_status"] == "BLOCKED"
    assert orphan_reward["points_split_snapshot_json"]["exception_reason"] == "MISSING_REWARD_LEDGER"
    assert legacy_balance == 100
    assert split_metadata["old_balance"] == 60 and split_metadata["zero_sum"] is True
    assert blocked["balance"] == -1 and blocked["supply_balance"] == 0
    assert rate_count == 0

    _alembic(database_url, "downgrade", "0020_audit_action_resource_index")
    downgraded = sa.MetaData()
    downgraded.reflect(engine, only=["points_accounts", "points_ledgers"])
    old_accounts, old_ledgers = downgraded.tables["points_accounts"], downgraded.tables["points_ledgers"]
    with engine.connect() as connection:
        assert connection.execute(sa.select(old_accounts.c.balance).where(old_accounts.c.id == valid_account)).scalar_one() == 60
        sequence = connection.execute(sa.select(old_ledgers.c.balance_after).where(
            old_ledgers.c.company_id == valid_company
        ).order_by(old_ledgers.c.created_at, old_ledgers.c.id)).scalars().all()
        assert sequence == [100, 60]
        assert connection.execute(sa.select(sa.func.sum(old_ledgers.c.delta)).where(old_ledgers.c.company_id == valid_company)).scalar_one() == 60

    _alembic(database_url, "upgrade", "head")


def test_supply_wallet_downgrade_refuses_to_drop_settlement_history(tmp_path: Path) -> None:
    database = tmp_path / "supply-wallet-history.db"
    database_url = f"sqlite:///{database}"
    _alembic(database_url, "upgrade", "head")
    engine = create_engine(database_url)
    metadata = sa.MetaData()
    metadata.reflect(engine, only=["companies", "supply_termination_requests"])
    companies = metadata.tables["companies"]
    requests = metadata.tables["supply_termination_requests"]
    now = datetime.now(timezone.utc)
    company_id = str(uuid.uuid4())
    with engine.begin() as connection:
        connection.execute(
            companies.insert(),
            _required_row(
                companies,
                id=company_id,
                code="SPLIT-HISTORY",
                name="结算历史保留",
                status="ACTIVE",
                created_at=now,
                updated_at=now,
            ),
        )
        connection.execute(
            requests.insert(),
            _required_row(
                requests,
                company_id=company_id,
                status="TERMINATED",
                reason="已完成线下结算",
                created_at=now,
                updated_at=now,
            ),
        )

    with pytest.raises(RuntimeError, match="cannot downgrade while supply termination history exists"):
        _alembic(database_url, "downgrade", "0020_audit_action_resource_index")

    assert "supply_termination_requests" in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.execute(sa.select(sa.func.count()).select_from(requests)).scalar_one() == 1


def test_supply_wallet_migration_blocks_reward_ledger_status_mismatches(tmp_path: Path) -> None:
    database = tmp_path / "supply-wallet-reward-status.db"
    database_url = f"sqlite:///{database}"
    _alembic(database_url, "upgrade", "0020_audit_action_resource_index")
    engine = create_engine(database_url)
    metadata = sa.MetaData()
    metadata.reflect(engine)
    companies = metadata.tables["companies"]
    accounts = metadata.tables["points_accounts"]
    ledgers = metadata.tables["points_ledgers"]
    rewards = metadata.tables["supplier_lead_rewards"]
    now = datetime.now(timezone.utc)
    cancelled_company, settled_company = str(uuid.uuid4()), str(uuid.uuid4())
    cancelled_account, settled_account = str(uuid.uuid4()), str(uuid.uuid4())
    cancelled_ledger = str(uuid.uuid4())
    settled_ledger, unexpected_reversal = str(uuid.uuid4()), str(uuid.uuid4())
    with engine.begin() as connection:
        connection.execute(companies.insert(), [
            _required_row(
                companies, id=cancelled_company, code="SPLIT-CANCELLED-REWARD",
                name="已取消奖励异常", status="ACTIVE", created_at=now, updated_at=now,
            ),
            _required_row(
                companies, id=settled_company, code="SPLIT-SETTLED-REVERSAL",
                name="已结算冲回异常", status="ACTIVE", created_at=now, updated_at=now,
            ),
        ])
        connection.execute(accounts.insert(), [
            _required_row(
                accounts, id=cancelled_account, company_id=cancelled_company,
                balance=10, version=1, created_at=now, updated_at=now,
            ),
            _required_row(
                accounts, id=settled_account, company_id=settled_company,
                balance=0, version=1, created_at=now, updated_at=now,
            ),
        ])
        connection.execute(rewards.insert(), [
            _required_row(
                rewards, id="cancelled-reward-with-ledger",
                lead_id="cancelled-reward-lead", assignment_id="cancelled-reward-assignment",
                supplier_company_id=cancelled_company, receiver_company_id=cancelled_company,
                status="CANCELLED", reward_points=10, ledger_id=cancelled_ledger,
                created_at=now, updated_at=now,
            ),
            _required_row(
                rewards, id="settled-reward-with-reversal",
                lead_id="settled-reward-lead", assignment_id="settled-reward-assignment",
                supplier_company_id=settled_company, receiver_company_id=settled_company,
                status="SETTLED", reward_points=10, ledger_id=settled_ledger,
                reversal_ledger_id=unexpected_reversal, created_at=now, updated_at=now,
            ),
        ])
        connection.execute(ledgers.insert(), [
            _required_row(
                ledgers, id=cancelled_ledger, account_id=cancelled_account,
                company_id=cancelled_company, ledger_type="REWARD", delta=10,
                balance_after=10, business_type="V12_SUPPLIER_REWARD",
                business_id="cancelled-reward-with-ledger",
                idempotency_key="cancelled-reward-ledger", created_at=now,
            ),
            _required_row(
                ledgers, id=settled_ledger, account_id=settled_account,
                company_id=settled_company, ledger_type="REWARD", delta=10,
                balance_after=10, business_type="V12_SUPPLIER_REWARD",
                business_id="settled-reward-with-reversal",
                idempotency_key="settled-reward-ledger", created_at=now,
            ),
            _required_row(
                ledgers, id=unexpected_reversal, account_id=settled_account,
                company_id=settled_company, ledger_type="REVERSAL", delta=-10,
                balance_after=0, business_type="V12_SUPPLIER_REWARD_REVERSAL",
                business_id="settled-reward-with-reversal",
                idempotency_key="settled-reward-unexpected-reversal",
                related_ledger_id=settled_ledger, created_at=now + timedelta(seconds=1),
            ),
        ])

    _alembic(database_url, "upgrade", "head")
    migrated = sa.MetaData()
    migrated.reflect(engine, only=["points_accounts"])
    migrated_accounts = migrated.tables["points_accounts"]
    with engine.connect() as connection:
        rows = connection.execute(sa.select(migrated_accounts).where(
            migrated_accounts.c.id.in_((cancelled_account, settled_account))
        )).mappings().all()
    snapshots = {row["id"]: row["points_split_snapshot_json"] for row in rows}
    assert snapshots[cancelled_account]["exception_reason"] == "REWARD_STATUS_LEDGER_MISMATCH"
    assert snapshots[settled_account]["exception_reason"] == "REWARD_STATUS_LEDGER_MISMATCH"
