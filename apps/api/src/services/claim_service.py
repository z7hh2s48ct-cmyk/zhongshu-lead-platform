from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.auth import Principal
from ..core.config import get_settings
from ..core.enums import AssignmentStatus, LeadStatus, PointsLedgerType
from ..core.errors import AppError
from ..core.models import Assignment, AssignmentEvent, Company, Lead, PointsLedger
from ..core.time import as_utc, utcnow
from .company_assignment_v12 import require_company_assignment_access
from .lead_correction_guard import require_correction_review_resolved
from .lead_service import lead_to_dict
from .notification_service import create_station_message, enqueue_outbox
from .points_service import change_points, points_available_for_dispatch

settings = get_settings()


def claim_assignment(db: Session, assignment_id: str, principal: Principal, idempotency_key: str) -> tuple[Assignment, PointsLedger]:
    if not principal.company_id:
        raise AppError("COMPANY_CONTEXT_REQUIRED", "当前账号未绑定加盟商公司", 403)
    assignment = db.scalar(
        select(Assignment)
        .where(Assignment.id == assignment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not assignment:
        raise AppError("ASSIGNMENT_NOT_FOUND", "派发订单不存在", 404)
    if assignment.company_id != principal.company_id:
        raise AppError("FORBIDDEN", "无权领取该客资", 403)
    if assignment.internal_assignee_user_id:
        if (
            assignment.internal_assignee_user_id != principal.user_id
            or not principal.has_any_role("FRANCHISE_EMPLOYEE")
            or not principal.can("assignment.employee.claim")
        ):
            raise AppError("ASSIGNMENT_EMPLOYEE_FORBIDDEN", "该客资仅限被指定员工领取", 403)
    elif not principal.has_any_role("FRANCHISE_OWNER"):
        raise AppError("FORBIDDEN", "仅加盟商负责人可领取历史未指定员工的客资", 403)
    lead = db.scalar(
        select(Lead)
        .where(Lead.id == assignment.lead_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if lead is None or lead.deleted_at is not None:
        raise AppError("LEAD_NOT_FOUND", "客资不存在", 404)
    require_correction_review_resolved(lead)
    existing_ledger = db.scalar(
        select(PointsLedger).where(
            PointsLedger.company_id == assignment.company_id,
            PointsLedger.business_type == "ASSIGNMENT",
            PointsLedger.business_id == assignment.id,
            PointsLedger.ledger_type == PointsLedgerType.CLAIM,
        )
    )
    if assignment.status in {AssignmentStatus.CLAIMED, AssignmentStatus.FOLLOWING} and existing_ledger:
        return assignment, existing_ledger
    if assignment.status != AssignmentStatus.PENDING_CLAIM:
        raise AppError("ASSIGNMENT_NOT_CLAIMABLE", "该客资已失效或已处理", 409)
    now = utcnow()
    expires_at = as_utc(assignment.expires_at)
    if expires_at and expires_at <= now:
        raise AppError("ASSIGNMENT_EXPIRED", "该客资已过领取时限", 409)
    company = db.get(Company, assignment.company_id)
    if not company or company.status != "ACTIVE":
        raise AppError("COMPANY_DISABLED", "加盟商公司已停用", 403)

    ledger = change_points(
        db,
        company_id=assignment.company_id,
        delta=-assignment.points_price,
        ledger_type=PointsLedgerType.CLAIM,
        business_type="ASSIGNMENT",
        business_id=assignment.id,
        idempotency_key=f"claim:{assignment.id}:{idempotency_key}",
        created_by=principal.user_id,
        metadata={"price_version": assignment.price_version},
    )
    assignment.status = AssignmentStatus.CLAIMED
    assignment.claimed_at = now
    assignment.first_followup_due_at = now + timedelta(hours=settings.first_followup_hours)
    if lead:
        lead.status = LeadStatus.CLAIMED
    db.add(AssignmentEvent(assignment_id=assignment.id, event_type="CLAIMED", actor_user_id=principal.user_id, payload={"points": assignment.points_price, "ledger_id": ledger.id}))
    create_station_message(db, user_id=principal.user_id, company_id=assignment.company_id, scene="CLAIM_SUCCESS", title="客资领取成功", body=f"已扣除{assignment.points_price}积分，可查看完整联系方式。", deep_link=f"/h5/#/leads/{assignment.id}")
    enqueue_outbox(db, event_key=f"assignment:{assignment.id}:claimed", event_type="ASSIGNMENT_CLAIMED", aggregate_type="assignment", aggregate_id=assignment.id, payload={"company_id": assignment.company_id, "user_id": principal.user_id})
    return assignment, ledger


def own_assignment_detail(db: Session, assignment: Assignment, principal: Principal) -> dict[str, Any]:
    require_company_assignment_access(principal, assignment)
    lead = db.get(Lead, assignment.lead_id)
    if lead is None or lead.deleted_at is not None:
        raise AppError("ASSIGNMENT_NOT_FOUND", "派发订单不存在", 404)
    correction_blocked = bool(
        lead and lead.pending_reason == "CORRECTION_REVIEW_REQUIRED"
    )
    unlocked = not correction_blocked and assignment.status in {
        AssignmentStatus.CLAIMED,
        AssignmentStatus.FOLLOWING,
        AssignmentStatus.RETURN_PENDING,
        AssignmentStatus.COMPLETED,
    }
    balance, reserved, available = points_available_for_dispatch(db, assignment.company_id)
    if lead is not None:
        current_lead = lead_to_dict(lead, principal, reveal_phone=unlocked)
    else:
        current_lead = dict(assignment.lead_snapshot)
        current_lead.update(
            {
                "phone": None,
                "phone_masked": current_lead.get("phone_masked"),
            }
        )
    current_lead.update(
        {
            "contact_unlocked": unlocked,
            "correction_blocked": correction_blocked,
        }
    )
    return {
        "id": assignment.id,
        "status": assignment.status,
        "points_price": assignment.points_price,
        "assigned_at": assignment.assigned_at.isoformat(),
        "claimed_at": assignment.claimed_at.isoformat() if assignment.claimed_at else None,
        "expires_at": assignment.expires_at.isoformat() if assignment.expires_at else None,
        "first_followup_due_at": assignment.first_followup_due_at.isoformat() if assignment.first_followup_due_at else None,
        "lead": current_lead,
        "historical_lead_snapshot": dict(assignment.lead_snapshot),
        "points": {"balance": balance, "pending_claim_points": reserved, "available": available},
    }


def run_assignment_timeouts(db: Session) -> dict[str, int]:
    now = utcnow()
    reminded = 0
    expired = 0
    pending = db.scalars(
        select(Assignment)
        .join(Lead, Lead.id == Assignment.lead_id)
        .where(
            Assignment.status == AssignmentStatus.PENDING_CLAIM,
            Lead.deleted_at.is_(None),
        )
    ).all()
    for assignment in pending:
        assigned_at = as_utc(assignment.assigned_at) or now
        expires_at = as_utc(assignment.expires_at)
        hours = (now - assigned_at).total_seconds() / 3600
        if hours >= settings.assignment_expire_hours or (expires_at and expires_at <= now):
            assignment.status = AssignmentStatus.EXPIRED
            assignment.released_at = now
            assignment.release_reason = "UNCLAIMED_TIMEOUT"
            lead = db.get(Lead, assignment.lead_id)
            if lead and lead.current_assignment_id == assignment.id:
                lead.current_assignment_id = None
                lead.status = LeadStatus.QUALIFIED
                lead.pending_reason = "UNCLAIMED_TIMEOUT"
            db.add(AssignmentEvent(assignment_id=assignment.id, event_type="EXPIRED", payload={"hours": hours}))
            enqueue_outbox(db, event_key=f"assignment:{assignment.id}:expired", event_type="ASSIGNMENT_EXPIRED", aggregate_type="assignment", aggregate_id=assignment.id, payload={"company_id": assignment.company_id})
            expired += 1
        elif hours >= settings.assignment_reminder_hours and assignment.reminder_sent_at is None:
            assignment.reminder_sent_at = now
            create_station_message(db, user_id=None, company_id=assignment.company_id, scene="CLAIM_REMINDER", title="客资即将过期", body="该客资尚未领取，请尽快处理。", deep_link=f"/h5/#/leads/{assignment.id}")
            enqueue_outbox(db, event_key=f"assignment:{assignment.id}:reminder", event_type="ASSIGNMENT_REMINDER", aggregate_type="assignment", aggregate_id=assignment.id, payload={"company_id": assignment.company_id})
            reminded += 1
    return {"reminded": reminded, "expired": expired}
