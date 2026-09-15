from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..core.enums import AssignmentStatus
from ..core.models import Assignment, AssignmentEvent, Lead
from ..core.time import as_utc
from ..core.v12_enums import LeadV12Status
from .claim_service import run_assignment_timeouts as run_assignment_timeouts_legacy
from .lead_correction_guard import CORRECTION_REVIEW_REASON, lead_requires_correction_review
from .notification_v12 import emit_assignment_notifications

settings = get_settings()


def run_assignment_timeouts_active(db: Session) -> dict[str, int]:
    """Route every supported timeout entrypoint through the active business version."""

    if settings.legacy_write_enabled:
        return run_assignment_timeouts_legacy(db)
    return run_assignment_timeouts_v12(db)


def drain_assignment_timeouts_active(
    db: Session, *, batch_size: int = 200, max_batches: int = 50,
) -> dict[str, int]:
    """Job-only boundary: commit bounded batches independently of other jobs."""
    if settings.legacy_write_enabled:
        result = run_assignment_timeouts_legacy(db)
        db.commit()
        return result
    result = {"reminded": 0, "expired": 0}
    for _ in range(max_batches):
        batch = run_assignment_timeouts_v12(db, batch_size=batch_size)
        db.commit()
        for key in result:
            result[key] += batch[key]
        if sum(batch.values()) < batch_size:
            break
    return result


def run_assignment_timeouts_v12(
    db: Session, *, now: datetime | None = None, batch_size: int = 200,
) -> dict[str, int]:
    """Expire only active V1.2 manual dispatches and return their leads to the V1.2 pool.

    A V1.2 pending assignment is identified by the lead/assignment invariant created by
    dispatch_manually: the lead is DISPATCHED and points at the same current_assignment_id.
    Historical V1.0.1 pending assignments are deliberately not mutated here.

    Candidate discovery selects only primary keys. Each row is then reloaded under a
    database lock with ``populate_existing`` so a worker that waited for another worker
    cannot act on stale ``reminder_sent_at`` or status values from SQLAlchemy's identity map.
    """

    current = as_utc(now) or datetime.now(timezone.utc)
    reminded = 0
    expired = 0
    pending_ids = db.scalars(
        select(Assignment.id)
        .join(Lead, Lead.id == Assignment.lead_id)
        .where(
            Assignment.status == AssignmentStatus.PENDING_CLAIM.value,
            Lead.deleted_at.is_(None),
            Lead.status == LeadV12Status.DISPATCHED.value,
            Lead.current_assignment_id == Assignment.id,
            or_(Lead.pending_reason.is_(None), Lead.pending_reason != CORRECTION_REVIEW_REASON),
            or_(
                Assignment.expires_at <= current,
                and_(
                    Assignment.reminder_sent_at.is_(None),
                    Assignment.assigned_at <= current - timedelta(hours=settings.assignment_reminder_hours),
                ),
            ),
        )
        .order_by(Assignment.assigned_at.asc(), Assignment.id.asc())
        .limit(max(1, batch_size))
        .with_for_update(of=Assignment, skip_locked=True)
    ).all()

    for assignment_id in pending_ids:
        assignment = db.scalar(
            select(Assignment)
            .where(Assignment.id == assignment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if assignment is None or assignment.status != AssignmentStatus.PENDING_CLAIM.value:
            continue
        lead = db.scalar(
            select(Lead)
            .where(Lead.id == assignment.lead_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            lead is None
            or lead.deleted_at is not None
            or lead.current_assignment_id != assignment.id
            or lead.status != LeadV12Status.DISPATCHED.value
        ):
            continue
        if lead_requires_correction_review(lead):
            continue

        assigned_at = as_utc(assignment.assigned_at) or current
        expires_at = as_utc(assignment.expires_at)
        hours = max(0.0, (current - assigned_at).total_seconds() / 3600)
        # Match the claim API: only a persisted deadline expires a dispatch.
        is_expired = bool(expires_at and expires_at <= current)

        if is_expired:
            assignment.status = AssignmentStatus.EXPIRED.value
            assignment.released_at = current
            assignment.release_reason = "UNCLAIMED_TIMEOUT"
            lead.current_assignment_id = None
            lead.status = LeadV12Status.READY_DISPATCH.value
            lead.pending_reason = "UNCLAIMED_TIMEOUT"
            db.add(
                AssignmentEvent(
                    assignment_id=assignment.id,
                    event_type="V12_ASSIGNMENT_EXPIRED",
                    payload={"hours": hours, "lead_id": lead.id},
                )
            )
            emit_assignment_notifications(
                db, assignment=assignment,
                event_key=f"v12-assignment:{assignment.id}:expired",
                event_type="V12_ASSIGNMENT_EXPIRED",
                title="客资未领取已回收",
                body="该客资已超过领取截止时间，现已回收至平台待派发池。",
                deep_link=f"/h5/v12-workbench.html?view=assignments&id={assignment.id}",
            )
            expired += 1
            continue

        if hours >= settings.assignment_reminder_hours and assignment.reminder_sent_at is None:
            assignment.reminder_sent_at = current
            body = "该客资尚未领取，请尽快处理；历史派发单未设置截止时间。"
            if expires_at:
                deadline = expires_at.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
                body = f"该客资尚未领取，领取截止时间为 {deadline}（北京时间），请尽快处理。"
            emit_assignment_notifications(
                db, assignment=assignment,
                event_key=f"v12-assignment:{assignment.id}:reminder",
                event_type="V12_ASSIGNMENT_REMINDER",
                title="客资即将过期",
                body=body,
                deep_link=f"/h5/v12-workbench.html?view=assignments&id={assignment.id}",
            )
            reminded += 1

    return {"reminded": reminded, "expired": expired}
