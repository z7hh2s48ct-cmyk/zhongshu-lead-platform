"""Apply the 48-hour rule to unsubmitted returns, retaining reversible events.

Revision ID: 0019_return_48h
Revises: 0018_lead_soft_delete
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from alembic import op
import sqlalchemy as sa

revision = "0019_return_48h"
down_revision = "0018_lead_soft_delete"
branch_labels = None
depends_on = None

MIGRATED = "RETURN_WINDOW_48H_MIGRATED"
ROLLED_BACK = "RETURN_WINDOW_48H_ROLLED_BACK"
SKIPPED = "RETURN_WINDOW_48H_ROLLBACK_SKIPPED"


def _utc(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(value):
    moment = _utc(value)
    return moment.isoformat() if moment else None


def _tables(bind):
    metadata = sa.MetaData()
    return tuple(sa.Table(name, metadata, autoload_with=bind) for name in
                 ("assignments", "return_requests", "assignment_events"))


def preview(bind) -> list[dict]:
    """Return identifiers and timestamps only; no customer or credential data."""
    assignments, requests, _ = _tables(bind)
    rows = bind.execute(sa.select(
        assignments.c.id.label("assignment_id"), assignments.c.claimed_at,
        assignments.c.appeal_deadline_at.label("old_assignment_deadline"),
        requests.c.id.label("return_id"),
        requests.c.appeal_deadline_at.label("old_return_deadline"),
        requests.c.due_at.label("old_return_due_at"),
    ).select_from(assignments.outerjoin(requests, requests.c.assignment_id == assignments.c.id))
        .where(assignments.c.receiver_company_id.is_not(None), assignments.c.claimed_at.is_not(None),
               assignments.c.status.in_(("CLAIMED", "FOLLOWING", "COMPLETED")),
               sa.or_(requests.c.id.is_(None), sa.and_(requests.c.submitted_at.is_(None), requests.c.status == "DRAFT")))
        .order_by(assignments.c.id)).mappings()
    result = []
    for row in rows:
        new_deadline = _utc(row["claimed_at"]) + timedelta(hours=48)
        if (_utc(row["old_assignment_deadline"]) == new_deadline
                and (row["return_id"] is None or
                     (_utc(row["old_return_deadline"]) == new_deadline and _utc(row["old_return_due_at"]) == new_deadline))):
            continue
        result.append({
            "assignment_id": row["assignment_id"], "return_id": row["return_id"],
            "claimed_at": _iso(row["claimed_at"]), "new_deadline": _iso(new_deadline),
            "old_assignment_deadline": _iso(row["old_assignment_deadline"]),
            "old_return_deadline": _iso(row["old_return_deadline"]),
            "old_return_due_at": _iso(row["old_return_due_at"]),
        })
    return result


def _event(bind, events, assignment_id, event_type, payload):
    bind.execute(events.insert().values(id=str(uuid4()), assignment_id=assignment_id,
        event_type=event_type, actor_user_id=None, payload=payload,
        occurred_at=datetime.now(timezone.utc)))


def upgrade() -> None:
    bind = op.get_bind()
    assignments, requests, events = _tables(bind)
    # Run migrations with application writes paused. Locks also protect against
    # an accidentally concurrent submission while its candidate is being read.
    for candidate in preview(bind):
        assignment = bind.execute(sa.select(assignments).where(assignments.c.id == candidate["assignment_id"])
            .with_for_update()).mappings().one()
        request = bind.execute(sa.select(requests).where(requests.c.assignment_id == assignment["id"])
            .with_for_update()).mappings().first()
        if request and (request["submitted_at"] is not None or request["status"] != "DRAFT"):
            continue
        if assignment["status"] not in {"CLAIMED", "FOLLOWING", "COMPLETED"}:
            continue
        new_deadline = _utc(assignment["claimed_at"]) + timedelta(hours=48)
        payload = dict(candidate, return_id=request["id"] if request else None,
            old_assignment_deadline=_iso(assignment["appeal_deadline_at"]),
            old_return_deadline=_iso(request["appeal_deadline_at"]) if request else None,
            old_return_due_at=_iso(request["due_at"]) if request else None,
            new_deadline=_iso(new_deadline), rule="CLAIMED_AT_48H")
        bind.execute(assignments.update().where(assignments.c.id == assignment["id"])
            .values(appeal_deadline_at=new_deadline))
        if request:
            bind.execute(requests.update().where(requests.c.id == request["id"])
                .values(appeal_deadline_at=new_deadline, due_at=new_deadline))
        _event(bind, events, assignment["id"], MIGRATED, payload)


def downgrade() -> None:
    bind = op.get_bind()
    assignments, requests, events = _tables(bind)
    rollback_events = bind.execute(sa.select(events.c.payload).where(events.c.event_type.in_((ROLLED_BACK, SKIPPED)))).scalars()
    handled = {payload["migration_event_id"] for payload in rollback_events}
    changes = bind.execute(sa.select(events).where(events.c.event_type == MIGRATED)
        .order_by(events.c.occurred_at.desc(), events.c.id.desc())).mappings().all()
    for change in changes:
        if change["id"] in handled:
            continue
        payload = change["payload"]
        assignment = bind.execute(sa.select(assignments).where(assignments.c.id == change["assignment_id"])
            .with_for_update()).mappings().one()
        request = bind.execute(sa.select(requests).where(requests.c.assignment_id == assignment["id"])
            .with_for_update()).mappings().first()
        deadline = _utc(payload["new_deadline"])
        reason = None
        if request and request["submitted_at"] is not None:
            reason = "RETURN_SUBMITTED_AFTER_MIGRATION"
        elif (request["id"] if request else None) != payload["return_id"]:
            reason = "RETURN_CREATED_AFTER_MIGRATION"
        elif _utc(assignment["appeal_deadline_at"]) != deadline or (request and (
            _utc(request["appeal_deadline_at"]) != deadline or _utc(request["due_at"]) != deadline
        )):
            reason = "DEADLINE_CHANGED_AFTER_MIGRATION"
        if reason is None:
            bind.execute(assignments.update().where(assignments.c.id == assignment["id"])
                .values(appeal_deadline_at=_utc(payload["old_assignment_deadline"])))
            if request:
                bind.execute(requests.update().where(requests.c.id == request["id"])
                    .values(appeal_deadline_at=_utc(payload["old_return_deadline"]), due_at=_utc(payload["old_return_due_at"])))
        _event(bind, events, assignment["id"], SKIPPED if reason else ROLLED_BACK,
               {"migration_event_id": change["id"], "reason": reason})
