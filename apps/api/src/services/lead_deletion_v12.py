from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core import models_v12  # noqa: F401 - registers source and submitter columns on Lead
from ..core.auth import Principal
from ..core.enums import VerificationTaskStatus
from ..core.errors import AppError
from ..core.models import Assignment, Lead, ReturnRequest, VerificationTask
from ..core.v12_enums import LeadSourceKind, LeadV12Status, ReturnV12Status


DELETABLE_LEAD_STATUSES = frozenset(
    {
        LeadV12Status.DRAFT.value,
        LeadV12Status.DUPLICATE.value,
        LeadV12Status.READY_DISPATCH.value,
        LeadV12Status.PUBLIC_POOL.value,
        LeadV12Status.PENDING_REVIEW.value,
        LeadV12Status.PENDING_OPERATION_DISPOSITION.value,
    }
)
DELETABLE_LEAD_SOURCES = frozenset(
    {LeadSourceKind.PLATFORM_MANUAL.value, LeadSourceKind.FEISHU_IMPORT.value}
)
_ACTIVE_VERIFICATION_STATUSES = (
    VerificationTaskStatus.PENDING.value,
    VerificationTaskStatus.ASSIGNED.value,
    VerificationTaskStatus.IN_PROGRESS.value,
)
_ACTIVE_RETURN_STATUSES = (
    ReturnV12Status.DRAFT.value,
    ReturnV12Status.SUBMITTED.value,
    ReturnV12Status.VERIFYING.value,
    ReturnV12Status.REVIEWING.value,
    ReturnV12Status.NEED_MORE_EVIDENCE.value,
)
_BLOCKER_MESSAGES = {
    "LEAD_ALREADY_DELETED": "该客资已经删除",
    "LEAD_DELETE_STATE_NOT_ALLOWED": "当前客资状态不支持删除",
    "LEAD_DELETE_DISPATCH_HISTORY_EXISTS": "客资已有派发记录，不能删除",
    "LEAD_DELETE_VERIFICATION_ACTIVE": "客资正在核验，请先完成核验处理",
    "LEAD_DELETE_RETURN_ACTIVE": "客资正在退回处理，不能删除",
}


@dataclass(frozen=True)
class LeadDeletionResult:
    lead: Lead
    idempotent: bool


def require_lead_not_deleted(lead: Lead | None) -> Lead:
    if lead is None or lead.deleted_at is not None:
        raise AppError("LEAD_NOT_FOUND", "客资不存在", 404)
    return lead


def _owned_lead(
    db: Session, *, lead_id: str, principal: Principal, lock: bool = False
) -> Lead:
    if not principal.has_any_role("OPERATION"):
        raise AppError("FORBIDDEN", "仅运营人员可删除本人上传的客资", 403)
    statement = select(Lead).where(Lead.id == lead_id)
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    lead = db.scalar(statement)
    if lead is None:
        raise AppError("LEAD_NOT_FOUND", "客资不存在", 404)
    if lead.submitter_user_id != principal.user_id:
        raise AppError("FORBIDDEN", "只能删除本人上传的客资", 403)
    if lead.source_kind not in DELETABLE_LEAD_SOURCES:
        raise AppError("FORBIDDEN", "仅本人后台录入或飞书导入的客资支持删除", 403)
    return lead


def _deletion_preview(db: Session, lead: Lead) -> dict[str, Any]:
    impact = {
        "assignment_history": int(
            db.scalar(select(func.count(Assignment.id)).where(Assignment.lead_id == lead.id)) or 0
        ),
        "active_verification_tasks": int(
            db.scalar(
                select(func.count(VerificationTask.id)).where(
                    VerificationTask.lead_id == lead.id,
                    VerificationTask.status.in_(_ACTIVE_VERIFICATION_STATUSES),
                )
            )
            or 0
        ),
        "active_return_requests": int(
            db.scalar(
                select(func.count(ReturnRequest.id)).where(
                    ReturnRequest.lead_id == lead.id,
                    ReturnRequest.status.in_(_ACTIVE_RETURN_STATUSES),
                )
            )
            or 0
        ),
    }
    blockers: list[str] = []
    if lead.deleted_at is not None:
        blockers.append("LEAD_ALREADY_DELETED")
    if lead.status not in DELETABLE_LEAD_STATUSES:
        blockers.append("LEAD_DELETE_STATE_NOT_ALLOWED")
    if lead.current_assignment_id or impact["assignment_history"]:
        blockers.append("LEAD_DELETE_DISPATCH_HISTORY_EXISTS")
    if impact["active_verification_tasks"]:
        blockers.append("LEAD_DELETE_VERIFICATION_ACTIVE")
    if impact["active_return_requests"]:
        blockers.append("LEAD_DELETE_RETURN_ACTIVE")
    return {
        "lead_id": lead.id,
        "customer_name": lead.customer_name,
        "source_kind": lead.source_kind,
        "status": lead.status,
        "deleted": lead.deleted_at is not None,
        "deletable": not blockers,
        "blockers": blockers,
        "blocker_messages": [_BLOCKER_MESSAGES[code] for code in blockers],
        "impact": impact,
    }


def preview_lead_deletion(
    db: Session, *, lead_id: str, principal: Principal
) -> dict[str, Any]:
    return _deletion_preview(db, _owned_lead(db, lead_id=lead_id, principal=principal))


def delete_own_lead(
    db: Session, *, lead_id: str, principal: Principal, reason: str
) -> LeadDeletionResult:
    normalized_reason = reason.strip()
    if not 2 <= len(normalized_reason) <= 500:
        raise AppError("LEAD_DELETE_REASON_REQUIRED", "删除原因必须为 2 至 500 个字符", 422)

    # Dispatch and verification creation also lock the lead before changing its workflow.
    # Recheck every blocker under that lock; a previous preview cannot authorize deletion.
    lead = _owned_lead(db, lead_id=lead_id, principal=principal, lock=True)
    if lead.deleted_at is not None:
        return LeadDeletionResult(lead=lead, idempotent=True)
    preview = _deletion_preview(db, lead)
    if preview["blockers"]:
        code = preview["blockers"][0]
        raise AppError(code, _BLOCKER_MESSAGES[code], 409, preview)

    # Keep source identifiers and all historical records so reimports remain idempotent.
    lead.deleted_at = datetime.now(timezone.utc)
    lead.deleted_by = principal.user_id
    lead.delete_reason = normalized_reason
    db.flush()
    return LeadDeletionResult(lead=lead, idempotent=False)
