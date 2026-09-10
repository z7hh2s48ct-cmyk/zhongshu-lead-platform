from datetime import timedelta
import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select

from apps.api.src.core.models import AssignmentEvent
from apps.api.src.core.time import as_utc
from apps.api.src.services.return_v12 import create_or_update_return_draft
from apps.api.tests.test_v12_return_workflow import _principal, _submit_and_verify, _workflow_setup
from apps.api.tests.test_return_concurrency_postgres import postgres_factory  # noqa: F401


def _migration(db, monkeypatch):
    path = Path(__file__).resolve().parents[3] / "migrations/versions/0019_return_48h.py"
    spec = importlib.util.spec_from_file_location("return_48h_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "op", Operations(MigrationContext.configure(db.connection())))
    return module


def test_return_deadline_migration_previews_preserves_submitted_and_rolls_back(db, monkeypatch):
    fresh = _workflow_setup(db, suffix="-NEW")
    drafted = _workflow_setup(db, suffix="-DRAFT")
    submitted = _workflow_setup(db, suffix="-SUBMITTED")
    request = create_or_update_return_draft(db, assignment_id=drafted["assignment"].id,
        principal=_principal(drafted["receiver_user"], "return.own.manage"),
        reason_code="EMPTY_NUMBER", description="旧版待提交草稿迁移")
    accepted, _ = _submit_and_verify(db, submitted)
    old = as_utc(drafted["assignment"].claimed_at) + timedelta(days=5)
    for setup in (fresh, drafted, submitted):
        setup["assignment"].appeal_deadline_at = old
    request.appeal_deadline_at = request.due_at = old
    accepted.appeal_deadline_at = accepted.due_at = old
    db.commit()
    original_rewards = {setup["reward"].id: as_utc(setup["reward"].reward_due_at) for setup in (fresh, drafted, submitted)}
    migration = _migration(db, monkeypatch)
    preview = migration.preview(db.connection())
    assert {row["assignment_id"] for row in preview} == {fresh["assignment"].id, drafted["assignment"].id}
    migration.upgrade()
    migration.upgrade()
    db.expire_all()
    assert as_utc(drafted["assignment"].appeal_deadline_at) == as_utc(drafted["assignment"].claimed_at) + timedelta(hours=48)
    assert as_utc(request.appeal_deadline_at) == as_utc(request.due_at) == as_utc(drafted["assignment"].appeal_deadline_at)
    assert as_utc(accepted.appeal_deadline_at) == old
    assert as_utc(submitted["assignment"].appeal_deadline_at) == old
    assert db.scalar(select(func.count(AssignmentEvent.id)).where(AssignmentEvent.event_type == "RETURN_WINDOW_48H_MIGRATED")) == 2
    migration.downgrade()
    db.expire_all()
    assert as_utc(drafted["assignment"].appeal_deadline_at) == old
    assert as_utc(request.appeal_deadline_at) == as_utc(request.due_at) == old
    for setup in (fresh, drafted, submitted):
        assert as_utc(setup["reward"].reward_due_at) == original_rewards[setup["reward"].id]


def test_return_deadline_rollback_does_not_rewrite_later_submissions(db, monkeypatch):
    setup = _workflow_setup(db)
    migration = _migration(db, monkeypatch)
    migration.upgrade()
    db.expire_all()
    accepted, _ = _submit_and_verify(db, setup)
    deadline = as_utc(accepted.appeal_deadline_at)
    db.flush()
    migration.downgrade()
    db.expire_all()
    assert as_utc(accepted.appeal_deadline_at) == deadline
    assert as_utc(setup["assignment"].appeal_deadline_at) == deadline
    assert db.scalar(select(AssignmentEvent).where(AssignmentEvent.event_type == "RETURN_WINDOW_48H_ROLLBACK_SKIPPED")) is not None


def test_return_deadline_migration_on_postgresql(postgres_factory, monkeypatch):
    with postgres_factory() as db:
        test_return_deadline_migration_previews_preserves_submitted_and_rolls_back(db, monkeypatch)


def test_return_deadline_rollback_preserves_later_submissions_on_postgresql(postgres_factory, monkeypatch):
    with postgres_factory() as db:
        test_return_deadline_rollback_does_not_rewrite_later_submissions(db, monkeypatch)
