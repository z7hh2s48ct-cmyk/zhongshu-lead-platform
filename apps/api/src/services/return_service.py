from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.auth import Principal
from ..core.config import get_settings
from ..core.enums import AssignmentStatus, EvidenceType, LeadStatus, PointsLedgerType, ReturnStatus
from ..core.errors import AppError
from ..core.models import Assignment, AssignmentEvent, Lead, PointsLedger, ReturnEvidence, ReturnRequest
from ..core.time import as_utc, utcnow
from .notification_service import create_station_message, enqueue_outbox
from .points_service import change_points
from .lead_deletion_v12 import require_lead_not_deleted

settings = get_settings()


def create_or_update_return(
    db: Session,
    *,
    assignment: Assignment,
    principal: Principal,
    reason_code: str,
    description: str,
) -> ReturnRequest:
    if assignment.company_id != principal.company_id:
        raise AppError("FORBIDDEN", "无权申请退回该客资", 403)
    require_lead_not_deleted(db.get(Lead, assignment.lead_id))
    if assignment.status not in {AssignmentStatus.CLAIMED, AssignmentStatus.FOLLOWING, AssignmentStatus.RETURN_PENDING}:
        raise AppError("RETURN_NOT_ALLOWED", "订单当前不可申请退回", 409)
    if not assignment.claimed_at:
        raise AppError("RETURN_NOT_CLAIMED", "未领取客资不能申请退回", 409)
    claimed_at = as_utc(assignment.claimed_at)
    if claimed_at is None:
        raise AppError("RETURN_NOT_CLAIMED", "未领取客资不能申请退回", 409)
    due_at = claimed_at + timedelta(hours=settings.return_window_hours)
    if utcnow() > due_at:
        raise AppError("RETURN_WINDOW_EXPIRED", "已超过退回申请时限", 409)
    item = db.scalar(select(ReturnRequest).where(ReturnRequest.assignment_id == assignment.id))
    if item and item.status == ReturnStatus.APPROVED:
        raise AppError("RETURN_ALREADY_APPROVED", "该订单已经退回成功", 409)
    if not item:
        item = ReturnRequest(
            assignment_id=assignment.id,
            lead_id=assignment.lead_id,
            company_id=assignment.company_id,
            reason_code=reason_code,
            description=description,
            status=ReturnStatus.DRAFT,
            submitted_by=principal.user_id,
            due_at=due_at,
        )
        db.add(item)
    else:
        item.reason_code = reason_code
        item.description = description
        if item.status in {ReturnStatus.REJECTED, ReturnStatus.NEED_MORE, ReturnStatus.CANCELLED}:
            item.status = ReturnStatus.DRAFT
    db.flush()
    return item


def add_evidence(
    db: Session,
    *,
    request: ReturnRequest,
    evidence_type: str,
    object_key: str,
    original_name: str,
    mime_type: str,
    file_size: int,
    sha256: str,
    duration_seconds: int | None,
    uploaded_by: str,
) -> ReturnEvidence:
    require_lead_not_deleted(db.get(Lead, request.lead_id))
    if request.status not in {ReturnStatus.DRAFT, ReturnStatus.NEED_MORE}:
        raise AppError("RETURN_EVIDENCE_LOCKED", "当前状态不能上传证据", 409)
    evidence = ReturnEvidence(
        return_request_id=request.id,
        evidence_type=evidence_type,
        object_key=object_key,
        original_name=original_name,
        mime_type=mime_type,
        file_size=file_size,
        sha256=sha256,
        duration_seconds=duration_seconds,
        uploaded_by=uploaded_by,
    )
    db.add(evidence)
    db.flush()
    return evidence


def submit_return(db: Session, request: ReturnRequest, principal: Principal) -> ReturnRequest:
    if request.company_id != principal.company_id or request.submitted_by != principal.user_id:
        raise AppError("FORBIDDEN", "无权提交该退回申请", 403)
    require_lead_not_deleted(db.get(Lead, request.lead_id))
    if request.status not in {ReturnStatus.DRAFT, ReturnStatus.NEED_MORE}:
        raise AppError("RETURN_NOT_SUBMITTABLE", "退回申请当前不可提交", 409)
    now = utcnow()
    due_at = as_utc(request.due_at)
    if due_at and now > due_at:
        raise AppError("RETURN_WINDOW_EXPIRED", "已超过退回申请时限", 409)
    counts = dict(
        db.execute(
            select(ReturnEvidence.evidence_type, func.count(ReturnEvidence.id))
            .where(ReturnEvidence.return_request_id == request.id)
            .group_by(ReturnEvidence.evidence_type)
        ).all()
    )
    evidence_count = counts.get(EvidenceType.CHAT_SCREENSHOT, 0) + counts.get(EvidenceType.CALL_RECORDING, 0)
    if evidence_count < 1:
        raise AppError("RETURN_EVIDENCE_REQUIRED", "请至少上传 1 张沟通截图或 1 份电话录音", 422)
    request.status = ReturnStatus.PENDING
    request.submitted_at = now
    assignment = db.get(Assignment, request.assignment_id)
    lead = db.get(Lead, request.lead_id)
    if assignment:
        assignment.status = AssignmentStatus.RETURN_PENDING
        db.add(AssignmentEvent(assignment_id=assignment.id, event_type="RETURN_SUBMITTED", actor_user_id=principal.user_id, payload={"return_request_id": request.id, "reason": request.reason_code}))
    if lead:
        lead.status = LeadStatus.RETURN_PENDING
    enqueue_outbox(db, event_key=f"return:{request.id}:submitted", event_type="RETURN_SUBMITTED", aggregate_type="return_request", aggregate_id=request.id, payload={"company_id": request.company_id})
    return request


def review_return(
    db: Session,
    *,
    request: ReturnRequest,
    principal: Principal,
    decision: str,
    note: str,
) -> PointsLedger | None:
    if request.status != ReturnStatus.PENDING:
        raise AppError("RETURN_NOT_REVIEWABLE", "申请状态已变化，请刷新", 409)
    assignment = db.scalar(select(Assignment).where(Assignment.id == request.assignment_id).with_for_update())
    lead = db.scalar(select(Lead).where(Lead.id == request.lead_id).with_for_update())
    if not assignment or not lead:
        raise AppError("RETURN_DATA_MISSING", "退回关联数据不完整", 409)
    require_lead_not_deleted(lead)
    now = utcnow()
    request.reviewed_by = principal.user_id
    request.reviewed_at = now
    request.review_note = note
    refund_ledger: PointsLedger | None = None
    if decision == "NEED_MORE":
        request.status = ReturnStatus.NEED_MORE
        create_station_message(db, user_id=request.submitted_by, company_id=request.company_id, scene="RETURN_NEED_MORE", title="退回申请需补充材料", body=note, deep_link=f"/h5/#/returns/{request.id}")
    elif decision == "REJECT":
        request.status = ReturnStatus.REJECTED
        assignment.status = AssignmentStatus.FOLLOWING
        lead.status = LeadStatus.FOLLOWING
        db.add(AssignmentEvent(assignment_id=assignment.id, event_type="RETURN_REJECTED", actor_user_id=principal.user_id, payload={"note": note}))
        create_station_message(db, user_id=request.submitted_by, company_id=request.company_id, scene="RETURN_REJECTED", title="退回申请未通过", body=note, deep_link=f"/h5/#/leads/{assignment.id}")
    else:
        claim_ledger = db.scalar(
            select(PointsLedger).where(
                PointsLedger.company_id == request.company_id,
                PointsLedger.business_type == "ASSIGNMENT",
                PointsLedger.business_id == assignment.id,
                PointsLedger.ledger_type == PointsLedgerType.CLAIM,
            )
        )
        if not claim_ledger:
            raise AppError("RETURN_CLAIM_LEDGER_MISSING", "未找到原领取扣分流水", 409)
        refund_ledger = change_points(
            db,
            company_id=request.company_id,
            delta=abs(claim_ledger.delta),
            ledger_type=PointsLedgerType.RETURN,
            business_type="RETURN_REQUEST",
            business_id=request.id,
            idempotency_key=f"return:{request.id}:refund",
            related_ledger_id=claim_ledger.id,
            created_by=principal.user_id,
            metadata={"assignment_id": assignment.id, "actual_claim_points": abs(claim_ledger.delta)},
        )
        request.status = ReturnStatus.APPROVED
        request.refund_points = abs(claim_ledger.delta)
        request.refund_ledger_id = refund_ledger.id
        assignment.status = AssignmentStatus.RETURNED
        assignment.released_at = now
        assignment.release_reason = "RETURN_APPROVED"
        lead.current_assignment_id = None
        lead.status = LeadStatus.QUALIFIED
        lead.pending_reason = "RETURN_APPROVED_MANUAL_REVIEW"
        db.add(AssignmentEvent(assignment_id=assignment.id, event_type="RETURN_APPROVED", actor_user_id=principal.user_id, payload={"refund_points": request.refund_points, "refund_ledger_id": refund_ledger.id}))
        create_station_message(db, user_id=request.submitted_by, company_id=request.company_id, scene="RETURN_APPROVED", title="退回审核通过", body=f"已返还{request.refund_points}积分。", deep_link=f"/h5/#/returns/{request.id}")
    enqueue_outbox(db, event_key=f"return:{request.id}:review:{request.status}", event_type=f"RETURN_{request.status}", aggregate_type="return_request", aggregate_id=request.id, payload={"company_id": request.company_id, "decision": decision})
    return refund_ledger


def return_to_dict(db: Session, item: ReturnRequest, include_evidence: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": item.id,
        "assignment_id": item.assignment_id,
        "lead_id": item.lead_id,
        "company_id": item.company_id,
        "reason_code": item.reason_code,
        "description": item.description,
        "status": item.status,
        "submitted_at": item.submitted_at.isoformat() if item.submitted_at else None,
        "reviewed_at": item.reviewed_at.isoformat() if item.reviewed_at else None,
        "review_note": item.review_note,
        "refund_points": item.refund_points,
        "due_at": item.due_at.isoformat() if item.due_at else None,
    }
    if include_evidence:
        evidences = db.scalars(select(ReturnEvidence).where(ReturnEvidence.return_request_id == item.id).order_by(ReturnEvidence.created_at)).all()
        data["evidences"] = [
            {
                "id": e.id,
                "type": e.evidence_type,
                "original_name": e.original_name,
                "mime_type": e.mime_type,
                "file_size": e.file_size,
                "sha256": e.sha256,
                "duration_seconds": e.duration_seconds,
            }
            for e in evidences
        ]
    return data
