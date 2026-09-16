"""Pause the 48-hour appeal clock for returns already under review.

Revision ID: 0022_return_clock_pause
Revises: 0021_supply_termination_wallets

Downgrade is only safe during a coordinated application write pause. Current
clock values are copied to assignment events before the columns are removed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from uuid import uuid4

from alembic import op
import sqlalchemy as sa

revision = "0022_return_clock_pause"
down_revision = "0021_supply_termination_wallets"
branch_labels = None
depends_on = None

MIGRATED = "RETURN_CLOCK_PAUSE_MIGRATED"
DOWNGRADE_SNAPSHOT = "RETURN_CLOCK_PAUSE_DOWNGRADE_SNAPSHOT"
SNAPSHOT_RESTORED = "RETURN_CLOCK_PAUSE_SNAPSHOT_RESTORED"
RESTORE_BLOCKED = "RETURN_CLOCK_PAUSE_RESTORE_BLOCKED"
ACTIVE_RETURN_STATUSES = (
    "SUBMITTED",
    "VERIFYING",
    "REVIEWING",
    "NEED_MORE_EVIDENCE",
)
CLOCK_COLUMNS = (
    "appeal_paused_at",
    "appeal_remaining_seconds",
    "appeal_resumed_at",
)


def _utc(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _iso(value):
    moment = _utc(value)
    return moment.isoformat() if moment else None


def _columns(table: str) -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(table)
    }


def _tables(bind):
    metadata = sa.MetaData()
    return tuple(
        sa.Table(name, metadata, autoload_with=bind)
        for name in ("assignments", "return_requests", "leads", "assignment_events")
    )


def _event_exists(bind, events, assignment_id: str, event_type: str) -> bool:
    return bool(
        bind.execute(
            sa.select(events.c.id)
            .where(
                events.c.assignment_id == assignment_id,
                events.c.event_type == event_type,
            )
            .limit(1)
        ).scalar_one_or_none()
    )


def _event(
    bind,
    events,
    assignment_id: str,
    event_type: str,
    payload: dict,
    *,
    deduplicate: bool = True,
) -> None:
    if deduplicate and _event_exists(bind, events, assignment_id, event_type):
        return
    bind.execute(
        events.insert().values(
            id=str(uuid4()),
            assignment_id=assignment_id,
            event_type=event_type,
            actor_user_id=None,
            payload=payload,
            occurred_at=datetime.now(timezone.utc),
        )
    )


def _add_columns() -> None:
    existing = _columns("assignments")
    additions = (
        sa.Column("appeal_paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("appeal_remaining_seconds", sa.Integer(), nullable=True),
        sa.Column("appeal_resumed_at", sa.DateTime(timezone=True), nullable=True),
    )
    for column in additions:
        if column.name not in existing:
            op.add_column("assignments", column)


def _latest_downgrade_snapshots(bind, events) -> dict[str, dict]:
    rows = bind.execute(
        sa.select(events)
        .where(events.c.event_type == DOWNGRADE_SNAPSHOT)
        .order_by(events.c.occurred_at.desc(), events.c.id.desc())
    ).mappings().all()
    latest: dict[str, dict] = {}
    for row in rows:
        latest.setdefault(row["assignment_id"], row)
    return latest


def _handled_snapshot_ids(bind, events) -> set[str]:
    payloads = bind.execute(
        sa.select(events.c.payload).where(
            events.c.event_type.in_((SNAPSHOT_RESTORED, RESTORE_BLOCKED))
        )
    ).scalars().all()
    return {
        payload["snapshot_event_id"]
        for payload in payloads
        if payload.get("snapshot_event_id")
    }


def _restore_downgrade_snapshots(
    bind,
    assignments,
    requests,
    events,
) -> set[str]:
    snapshots = _latest_downgrade_snapshots(bind, events)
    handled = _handled_snapshot_ids(bind, events)
    ignored_event_types = {
        MIGRATED,
        DOWNGRADE_SNAPSHOT,
        SNAPSHOT_RESTORED,
        RESTORE_BLOCKED,
    }
    for assignment_id, snapshot in snapshots.items():
        if snapshot["id"] in handled:
            continue
        assignment = bind.execute(
            sa.select(assignments)
            .where(assignments.c.id == assignment_id)
            .with_for_update()
        ).mappings().first()
        if assignment is None:
            continue
        if any(assignment[column] is not None for column in CLOCK_COLUMNS):
            continue
        request = bind.execute(
            sa.select(requests)
            .where(requests.c.assignment_id == assignment_id)
            .with_for_update()
        ).mappings().first()
        payload = snapshot["payload"]
        reason = None
        if assignment["status"] != payload.get("assignment_status"):
            reason = "ASSIGNMENT_STATUS_CHANGED_AFTER_DOWNGRADE"
        elif _utc(assignment["appeal_deadline_at"]) != _utc(
            payload.get("appeal_deadline_at")
        ):
            reason = "APPEAL_DEADLINE_CHANGED_AFTER_DOWNGRADE"
        elif (request["id"] if request else None) != payload.get("return_id"):
            reason = "RETURN_REQUEST_CHANGED_AFTER_DOWNGRADE"
        elif (request["status"] if request else None) != payload.get("return_status"):
            reason = "RETURN_STATUS_CHANGED_AFTER_DOWNGRADE"
        elif _utc(request["submitted_at"] if request else None) != _utc(
            payload.get("return_submitted_at")
        ):
            reason = "RETURN_SUBMISSION_CHANGED_AFTER_DOWNGRADE"
        else:
            later_event = bind.execute(
                sa.select(events.c.id)
                .where(
                    events.c.assignment_id == assignment_id,
                    events.c.occurred_at > snapshot["occurred_at"],
                    events.c.event_type.not_in(ignored_event_types),
                )
                .limit(1)
            ).scalar_one_or_none()
            if later_event is not None:
                reason = "BUSINESS_EVENT_RECORDED_AFTER_DOWNGRADE"
        if reason:
            _event(
                bind,
                events,
                assignment_id,
                RESTORE_BLOCKED,
                {
                    "snapshot_event_id": snapshot["id"],
                    "reason": reason,
                    "requires_manual_review": True,
                },
                deduplicate=False,
            )
            continue
        bind.execute(
            assignments.update()
            .where(assignments.c.id == assignment_id)
            .values(
                appeal_paused_at=_utc(payload.get("appeal_paused_at")),
                appeal_remaining_seconds=payload.get("appeal_remaining_seconds"),
                appeal_resumed_at=_utc(payload.get("appeal_resumed_at")),
            )
        )
        _event(
            bind,
            events,
            assignment_id,
            SNAPSHOT_RESTORED,
            {"snapshot_event_id": snapshot["id"]},
            deduplicate=False,
        )
    return set(snapshots)


def upgrade() -> None:
    _add_columns()
    bind = op.get_bind()
    assignments, requests, leads, events = _tables(bind)
    snapshot_assignment_ids = _restore_downgrade_snapshots(
        bind,
        assignments,
        requests,
        events,
    )
    fresh_filters = []
    if snapshot_assignment_ids:
        fresh_filters.append(assignments.c.id.not_in(snapshot_assignment_ids))
    candidates = bind.execute(
        sa.select(
            assignments.c.id.label("assignment_id"),
            requests.c.id.label("return_id"),
        )
        .select_from(
            assignments.join(requests, requests.c.assignment_id == assignments.c.id)
            .join(leads, leads.c.id == assignments.c.lead_id)
        )
        .where(
            assignments.c.status == "RETURN_PENDING",
            assignments.c.receiver_company_id.is_not(None),
            assignments.c.claimed_at.is_not(None),
            assignments.c.appeal_paused_at.is_(None),
            assignments.c.appeal_remaining_seconds.is_(None),
            assignments.c.appeal_resumed_at.is_(None),
            leads.c.deleted_at.is_(None),
            requests.c.submitted_at.is_not(None),
            requests.c.status.in_(ACTIVE_RETURN_STATUSES),
            *fresh_filters,
        )
        .order_by(assignments.c.id)
    ).mappings().all()
    for candidate in candidates:
        assignment = bind.execute(
            sa.select(assignments)
            .where(assignments.c.id == candidate["assignment_id"])
            .with_for_update()
        ).mappings().one()
        request = bind.execute(
            sa.select(requests)
            .where(requests.c.id == candidate["return_id"])
            .with_for_update()
        ).mappings().one()
        lead_deleted_at = bind.execute(
            sa.select(leads.c.deleted_at).where(leads.c.id == assignment["lead_id"])
        ).scalar_one()
        if (
            assignment["status"] != "RETURN_PENDING"
            or assignment["receiver_company_id"] is None
            or assignment["claimed_at"] is None
            or assignment["appeal_paused_at"] is not None
            or assignment["appeal_remaining_seconds"] is not None
            or assignment["appeal_resumed_at"] is not None
            or lead_deleted_at is not None
            or request["submitted_at"] is None
            or request["status"] not in ACTIVE_RETURN_STATUSES
        ):
            continue
        paused_at = _utc(request["submitted_at"])
        deadline = _utc(assignment["claimed_at"]) + timedelta(hours=48)
        remaining_seconds = max(
            0,
            math.ceil((deadline - paused_at).total_seconds()),
        )
        payload = {
            "rule": "PAUSE_ON_FORMAL_RETURN_SUBMISSION",
            "return_id": request["id"],
            "old_appeal_paused_at": _iso(assignment["appeal_paused_at"]),
            "old_appeal_remaining_seconds": assignment["appeal_remaining_seconds"],
            "old_appeal_resumed_at": _iso(assignment["appeal_resumed_at"]),
            "new_appeal_paused_at": _iso(paused_at),
            "new_appeal_remaining_seconds": remaining_seconds,
            "new_appeal_resumed_at": None,
        }
        bind.execute(
            assignments.update()
            .where(assignments.c.id == assignment["id"])
            .values(
                appeal_paused_at=paused_at,
                appeal_remaining_seconds=remaining_seconds,
                appeal_resumed_at=None,
            )
        )
        _event(bind, events, assignment["id"], MIGRATED, payload)


def downgrade() -> None:
    existing = _columns("assignments")
    if not set(CLOCK_COLUMNS).issubset(existing):
        return
    bind = op.get_bind()
    assignments, requests, _, events = _tables(bind)
    clock_rows = bind.execute(
        sa.select(
            assignments.c.id,
            assignments.c.appeal_paused_at,
            assignments.c.appeal_remaining_seconds,
            assignments.c.appeal_resumed_at,
        )
        .where(
            sa.or_(
                assignments.c.appeal_paused_at.is_not(None),
                assignments.c.appeal_remaining_seconds.is_not(None),
                assignments.c.appeal_resumed_at.is_not(None),
            )
        )
        .order_by(assignments.c.id)
    ).mappings().all()
    for row in clock_rows:
        request = bind.execute(
            sa.select(requests)
            .where(requests.c.assignment_id == row["id"])
            .with_for_update()
        ).mappings().first()
        assignment_state = bind.execute(
            sa.select(
                assignments.c.status,
                assignments.c.appeal_deadline_at,
            )
            .where(assignments.c.id == row["id"])
            .with_for_update()
        ).mappings().one()
        _event(
            bind,
            events,
            row["id"],
            DOWNGRADE_SNAPSHOT,
            {
                "appeal_paused_at": _iso(row["appeal_paused_at"]),
                "appeal_remaining_seconds": row["appeal_remaining_seconds"],
                "appeal_resumed_at": _iso(row["appeal_resumed_at"]),
                "appeal_deadline_at": _iso(assignment_state["appeal_deadline_at"]),
                "assignment_status": assignment_state["status"],
                "return_id": request["id"] if request else None,
                "return_status": request["status"] if request else None,
                "return_submitted_at": _iso(request["submitted_at"] if request else None),
                "rollback_mode": "COORDINATED_WRITE_PAUSE_ONLY",
                "warning": "Restore these values before resuming return writes after rollback.",
            },
            deduplicate=False,
        )
    for column_name in reversed(CLOCK_COLUMNS):
        op.drop_column("assignments", column_name)
