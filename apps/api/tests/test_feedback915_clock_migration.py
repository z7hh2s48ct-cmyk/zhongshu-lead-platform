from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import MetaData, Table, func, inspect, select

from apps.api.src.core import models_v12 as _models_v12  # noqa: F401
from apps.api.src.core.enums import AssignmentStatus
from apps.api.src.core.models import AssignmentEvent
from apps.api.src.core.time import as_utc
from apps.api.src.core.v12_enums import ReturnV12Status
from apps.api.src.services.return_v12 import create_or_update_return_draft
from apps.api.tests.test_return_concurrency_postgres import postgres_factory  # noqa: F401
from apps.api.tests.test_v12_return_workflow import _principal, _workflow_setup


MIGRATED = "RETURN_CLOCK_PAUSE_MIGRATED"
DOWNGRADE_SNAPSHOT = "RETURN_CLOCK_PAUSE_DOWNGRADE_SNAPSHOT"
SNAPSHOT_RESTORED = "RETURN_CLOCK_PAUSE_SNAPSHOT_RESTORED"
RESTORE_BLOCKED = "RETURN_CLOCK_PAUSE_RESTORE_BLOCKED"


def _migration(db, monkeypatch):
    path = (
        Path(__file__).resolve().parents[3]
        / "migrations/versions/0022_return_clock_pause.py"
    )
    spec = importlib.util.spec_from_file_location("return_clock_pause_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module,
        "op",
        Operations(MigrationContext.configure(db.connection())),
    )
    return module


def _submitted_return(db, setup, *, status: str):
    request = create_or_update_return_draft(
        db,
        assignment_id=setup["assignment"].id,
        principal=_principal(setup["receiver_user"], "return.own.manage"),
        reason_code="EMPTY_NUMBER",
        description="迁移测试中的正式退回申请",
    )
    claimed_at = datetime.now(timezone.utc) - timedelta(hours=30)
    submitted_at = claimed_at + timedelta(hours=10, microseconds=1)
    setup["assignment"].claimed_at = claimed_at
    setup["assignment"].status = AssignmentStatus.RETURN_PENDING.value
    request.submitted_at = submitted_at
    request.status = status
    return request, claimed_at, submitted_at


def _assert_upgrade_and_downgrade(db, monkeypatch) -> None:
    eligible = _workflow_setup(db, suffix="-PAUSE-ELIGIBLE")
    eligible_request, claimed_at, submitted_at = _submitted_return(
        db,
        eligible,
        status=ReturnV12Status.VERIFYING.value,
    )

    draft = _workflow_setup(db, suffix="-PAUSE-DRAFT")
    draft_request = create_or_update_return_draft(
        db,
        assignment_id=draft["assignment"].id,
        principal=_principal(draft["receiver_user"], "return.own.manage"),
        reason_code="EMPTY_NUMBER",
        description="尚未正式提交",
    )

    processed = _workflow_setup(db, suffix="-PAUSE-PROCESSED")
    processed_request, _, _ = _submitted_return(
        db,
        processed,
        status=ReturnV12Status.APPROVED.value,
    )
    processed["assignment"].status = AssignmentStatus.RETURNED.value

    deleted = _workflow_setup(db, suffix="-PAUSE-DELETED")
    deleted_request, _, _ = _submitted_return(
        db,
        deleted,
        status=ReturnV12Status.SUBMITTED.value,
    )
    deleted["lead"].deleted_at = datetime.now(timezone.utc)

    existing = _workflow_setup(db, suffix="-PAUSE-EXISTING")
    existing_request, _, _ = _submitted_return(
        db,
        existing,
        status=ReturnV12Status.REVIEWING.value,
    )
    existing_pause = datetime.now(timezone.utc) - timedelta(hours=3)
    existing_resume = datetime.now(timezone.utc) - timedelta(hours=1)
    existing["assignment"].appeal_paused_at = existing_pause
    existing["assignment"].appeal_remaining_seconds = 7200
    existing["assignment"].appeal_resumed_at = existing_resume
    eligible_assignment_id = eligible["assignment"].id
    existing_assignment_id = existing["assignment"].id

    reward_snapshot = {
        item["reward"].id: (
            item["reward"].status,
            as_utc(item["reward"].reward_due_at),
        )
        for item in (eligible, draft, processed, deleted, existing)
    }
    db.commit()

    migration = _migration(db, monkeypatch)
    migration.upgrade()
    migration.upgrade()
    db.expire_all()

    assert as_utc(eligible["assignment"].appeal_paused_at) == as_utc(submitted_at)
    assert eligible["assignment"].appeal_remaining_seconds == 136800
    assert eligible["assignment"].appeal_resumed_at is None
    for assignment in (
        draft["assignment"],
        processed["assignment"],
        deleted["assignment"],
    ):
        assert assignment.appeal_paused_at is None
        assert assignment.appeal_remaining_seconds is None
        assert assignment.appeal_resumed_at is None
    assert as_utc(existing["assignment"].appeal_paused_at) == as_utc(existing_pause)
    assert existing["assignment"].appeal_remaining_seconds == 7200
    assert as_utc(existing["assignment"].appeal_resumed_at) == as_utc(existing_resume)
    assert db.scalar(
        select(func.count(AssignmentEvent.id)).where(
            AssignmentEvent.event_type == MIGRATED,
            AssignmentEvent.assignment_id == eligible["assignment"].id,
        )
    ) == 1
    assert db.scalar(
        select(func.count(AssignmentEvent.id)).where(
            AssignmentEvent.event_type == MIGRATED,
            AssignmentEvent.assignment_id.in_(
                (
                    draft["assignment"].id,
                    processed["assignment"].id,
                    deleted["assignment"].id,
                    existing["assignment"].id,
                )
            ),
        )
    ) == 0
    assert {
        item["reward"].id: (
            item["reward"].status,
            as_utc(item["reward"].reward_due_at),
        )
        for item in (eligible, draft, processed, deleted, existing)
    } == reward_snapshot

    resumed_at = datetime.now(timezone.utc)
    eligible["assignment"].appeal_resumed_at = resumed_at
    eligible_request.status = ReturnV12Status.REJECTED.value
    db.flush()
    migration.downgrade()
    migration.downgrade()

    columns = {column["name"] for column in inspect(db.connection()).get_columns("assignments")}
    assert "appeal_paused_at" not in columns
    assert "appeal_remaining_seconds" not in columns
    assert "appeal_resumed_at" not in columns
    snapshot = db.scalar(
        select(AssignmentEvent)
        .where(
            AssignmentEvent.event_type == DOWNGRADE_SNAPSHOT,
            AssignmentEvent.assignment_id == eligible["assignment"].id,
        )
        .order_by(AssignmentEvent.occurred_at.desc())
    )
    assert snapshot is not None
    assert snapshot.payload["appeal_remaining_seconds"] == 136800
    assert snapshot.payload["appeal_resumed_at"] == resumed_at.isoformat()
    assert snapshot.payload["assignment_status"] == AssignmentStatus.RETURN_PENDING.value
    assert snapshot.payload["return_status"] == ReturnV12Status.REJECTED.value
    assert snapshot.payload["rollback_mode"] == "COORDINATED_WRITE_PAUSE_ONLY"

    migration.upgrade()
    db.expire_all()
    assert as_utc(eligible["assignment"].appeal_paused_at) == as_utc(submitted_at)
    assert eligible["assignment"].appeal_remaining_seconds == 136800
    assert as_utc(eligible["assignment"].appeal_resumed_at) == as_utc(resumed_at)
    assert eligible_request.status == ReturnV12Status.REJECTED.value
    restored = db.scalar(
        select(AssignmentEvent).where(
            AssignmentEvent.event_type == SNAPSHOT_RESTORED,
            AssignmentEvent.assignment_id == eligible["assignment"].id,
        )
    )
    assert restored is not None
    assert restored.payload["snapshot_event_id"] == snapshot.id

    migration.downgrade()
    connection = db.connection()
    reflected = MetaData()
    assignments = Table("assignments", reflected, autoload_with=connection)
    events = Table("assignment_events", reflected, autoload_with=connection)
    connection.execute(
        assignments.update()
        .where(assignments.c.id == eligible_assignment_id)
        .values(status=AssignmentStatus.RELEASED.value)
    )
    connection.execute(
        events.insert().values(
            id="feedback915-business-event-" + eligible_assignment_id[:8],
            assignment_id=eligible_assignment_id,
            event_type="RETURN_REJECTED_AFTER_DOWNGRADE",
            actor_user_id=None,
            payload={},
            occurred_at=datetime.now(timezone.utc) + timedelta(seconds=1),
        )
    )
    connection.execute(
        events.insert().values(
            id="feedback915-business-event-" + existing_assignment_id[:8],
            assignment_id=existing_assignment_id,
            event_type="RETURN_REVIEWED_AFTER_DOWNGRADE",
            actor_user_id=None,
            payload={},
            occurred_at=datetime.now(timezone.utc) + timedelta(seconds=1),
        )
    )
    migration.upgrade()
    reflected = MetaData()
    assignments = Table("assignments", reflected, autoload_with=connection)
    blocked_row = connection.execute(
        select(
            assignments.c.appeal_paused_at,
            assignments.c.appeal_remaining_seconds,
            assignments.c.appeal_resumed_at,
        ).where(assignments.c.id == eligible_assignment_id)
    ).one()
    assert tuple(blocked_row) == (None, None, None)
    blocked = connection.execute(
        select(events.c.payload)
        .where(
            events.c.event_type == RESTORE_BLOCKED,
            events.c.assignment_id == eligible_assignment_id,
        )
        .order_by(events.c.occurred_at.desc())
    ).scalars().first()
    assert blocked is not None
    assert blocked["requires_manual_review"] is True
    assert blocked["reason"] in {
        "ASSIGNMENT_STATUS_CHANGED_AFTER_DOWNGRADE",
        "BUSINESS_EVENT_RECORDED_AFTER_DOWNGRADE",
    }
    event_only_row = connection.execute(
        select(
            assignments.c.appeal_paused_at,
            assignments.c.appeal_remaining_seconds,
            assignments.c.appeal_resumed_at,
        ).where(assignments.c.id == existing_assignment_id)
    ).one()
    assert tuple(event_only_row) == (None, None, None)
    event_only_blocked = connection.execute(
        select(events.c.payload)
        .where(
            events.c.event_type == RESTORE_BLOCKED,
            events.c.assignment_id == existing_assignment_id,
        )
        .order_by(events.c.occurred_at.desc())
    ).scalars().first()
    assert event_only_blocked is not None
    assert event_only_blocked["reason"] == "BUSINESS_EVENT_RECORDED_AFTER_DOWNGRADE"

    assert eligible_request.id
    assert draft_request.id
    assert processed_request.id
    assert deleted_request.id
    assert existing_request.id
    assert claimed_at < submitted_at


def test_return_clock_pause_migration_on_sqlite(db, monkeypatch) -> None:
    _assert_upgrade_and_downgrade(db, monkeypatch)


def test_return_clock_pause_migration_on_postgresql(postgres_factory, monkeypatch) -> None:
    with postgres_factory() as db:
        _assert_upgrade_and_downgrade(db, monkeypatch)
