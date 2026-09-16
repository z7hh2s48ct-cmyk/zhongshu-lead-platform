from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..core import models_v12 as _models_v12  # noqa: F401
from ..core.auth import Principal
from ..core.config import get_settings
from ..core.enums import VerificationTaskStatus
from ..core.errors import AppError
from ..core.models import AuditLog, Lead, Role, User, VerificationSubmission, VerificationTask
from ..core.security import decrypt_text, normalize_phone
from ..core.state_machine_v12 import assert_lead_transition
from ..core.time import as_utc
from ..core.v12_enums import LeadSourceKind, LeadV12Status, VerificationTaskType
from .dispatch_v12 import approved_lead_pool_target
from .lead_correction_guard import require_correction_review_resolved
from .supply_termination import require_supply_write_enabled
from .verification_service import latest_published_template


_ACTIVE_TASK_STATUSES = {
    VerificationTaskStatus.PENDING.value,
    VerificationTaskStatus.ASSIGNED.value,
    VerificationTaskStatus.IN_PROGRESS.value,
}
_CONCLUSIONS = {"QUALIFIED", "INFO_INCOMPLETE", "UNVERIFIABLE", "INVALID", "DUPLICATE"}
_DISPOSITIONS = {"APPROVE_POOL", "RETURN_REWORK", "DUPLICATE", "CLOSE"}
_REQUEUE_CONTACT_RESULTS = {
    "NO_ANSWER",
    "NOT_ANSWERED",
    "MISSED_CALL",
    "UNREACHABLE",
    "REFUSED",
    "REJECTED_CALL",
    "CALL_REJECTED",
    "无人接听",
    "未接",
    "未接听",
    "拒接",
    "拒绝接听",
}
_REQUEUE_LEAD_STATUSES = {
    LeadV12Status.PENDING_OPERATION_DISPOSITION.value,
    LeadV12Status.CLOSED.value,
    LeadV12Status.INVALID.value,
}
_OPERATION_DRAFT_SOURCE_KINDS = {
    LeadSourceKind.PLATFORM_MANUAL.value,
    LeadSourceKind.FEISHU_IMPORT.value,
}


@dataclass(frozen=True, slots=True)
class PreDispatchAssignmentResult:
    task: VerificationTask
    before: dict[str, Any] | None
    after: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PreDispatchRequeueResult:
    task: VerificationTask
    previous_task_id: str
    idempotent: bool


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require_operation(principal: Principal) -> None:
    if not principal.has_any_role("OPERATION", "SUPER_ADMIN"):
        raise AppError("FORBIDDEN", "仅运营人员可执行该客资操作", 403)


def reopen_closed_lead(
    db: Session,
    *,
    lead_id: str,
    principal: Principal,
    reason: str,
) -> Lead:
    """Return a closed lead to dispatch readiness without mutating prior rounds."""

    _require_operation(principal)
    normalized_reason = reason.strip()
    if len(normalized_reason) < 2:
        raise AppError("LEAD_REOPEN_REASON_REQUIRED", "重新启用原因至少 2 个字符", 422)
    lead = _lead_or_raise(db, lead_id, lock=True)
    if lead.status != LeadV12Status.CLOSED.value:
        raise AppError("LEAD_REOPEN_STATE_INVALID", "只有已关闭客资可以重新启用", 409)
    latest_task = db.scalar(
        select(VerificationTask)
        .where(
            VerificationTask.lead_id == lead.id,
            VerificationTask.task_type
            == VerificationTaskType.PRE_DISPATCH_VERIFY.value,
        )
        .order_by(VerificationTask.created_at.desc(), VerificationTask.id.desc())
        .limit(1)
    )
    if (
        latest_task is not None
        and latest_task.status
        in {
            VerificationTaskStatus.SUBMITTED.value,
            VerificationTaskStatus.RELEASED.value,
        }
        and is_pre_dispatch_task_requeueable(latest_task)
    ):
        raise AppError(
            "PRE_DISPATCH_REVERIFY_REQUIRED",
            "未接、拒接或无法核验的客资必须先重新进入电销核验",
            409,
        )
    assert_lead_transition(lead.status, LeadV12Status.READY_DISPATCH)
    lead.status = LeadV12Status.READY_DISPATCH.value
    lead.current_assignment_id = None
    lead.pending_reason = "REOPENED_FOR_REDISPATCH"
    lead.review_status = "APPROVED"
    lead.review_note = normalized_reason
    lead.reviewed_at = _now()
    lead.snapshot_version += 1
    db.flush()
    return lead


def _historical_rework_audit_filter():
    return (
        AuditLog.action == "V12_PRE_DISPATCH_DISPOSITION",
        AuditLog.resource_type == "lead",
        AuditLog.resource_id.is_not(None),
        or_(
            AuditLog.after_json["pending_reason"].as_string()
            == "PRE_DISPATCH_REWORK_REQUIRED",
            AuditLog.after_json["lead_snapshot"]["pending_reason"].as_string()
            == "PRE_DISPATCH_REWORK_REQUIRED",
        ),
    )


def _historical_rework_ids_query():
    audited = select(AuditLog.resource_id.label("lead_id")).where(
        *_historical_rework_audit_filter()
    )
    current = select(Lead.id.label("lead_id")).where(
        Lead.pending_reason == "PRE_DISPATCH_REWORK_REQUIRED"
    )
    return audited.union(current).subquery("historical_rework_lead_ids")


def list_historical_rework_leads(
    db: Session,
    *,
    page_no: int,
    page_size: int,
) -> tuple[list[dict[str, Any]], int]:
    """Page every lead ever returned for operation rework, even after marker drift."""

    history_ids = _historical_rework_ids_query()
    base = (
        select(Lead)
        .join(history_ids, history_ids.c.lead_id == Lead.id)
        .where(Lead.deleted_at.is_(None))
    )
    total = int(db.scalar(select(func.count()).select_from(base.subquery())) or 0)
    leads = list(
        db.scalars(
            base.order_by(Lead.created_at.desc(), Lead.id.desc())
            .offset((page_no - 1) * page_size)
            .limit(page_size)
        ).all()
    )
    if not leads:
        return [], total
    latest_tasks = latest_submitted_pre_dispatch_task_ids(
        db, [lead.id for lead in leads]
    )
    items = [
        {
            "lead": lead,
            "latest_task_id": latest_tasks.get(lead.id),
            "rework_pending": lead.pending_reason
            == "PRE_DISPATCH_REWORK_REQUIRED",
        }
        for lead in leads
    ]
    return items, total


def restore_historical_rework_lead(
    db: Session,
    *,
    lead_id: str,
    principal: Principal,
    reason: str,
) -> Lead:
    _require_operation(principal)
    if len(reason.strip()) < 2:
        raise AppError("PRE_DISPATCH_REWORK_REASON_REQUIRED", "恢复原因至少 2 个字符", 422)
    if not db.scalar(
        select(AuditLog.id)
        .where(*_historical_rework_audit_filter(), AuditLog.resource_id == lead_id)
        .limit(1)
    ):
        raise AppError("PRE_DISPATCH_REWORK_HISTORY_NOT_FOUND", "未找到该客资的历史待补充记录", 404)
    lead = _lead_or_raise(db, lead_id, lock=True)
    if lead.status != LeadV12Status.DRAFT.value:
        raise AppError(
            "PRE_DISPATCH_REWORK_RESTORE_STATE_INVALID",
            "只有仍处于草稿阶段的历史客资可以恢复到待运营补充",
            409,
        )
    lead.pending_reason = "PRE_DISPATCH_REWORK_REQUIRED"
    lead.review_note = reason.strip()
    lead.snapshot_version += 1
    db.flush()
    return lead


def _due_at(now: datetime) -> datetime:
    return now + timedelta(hours=get_settings().pre_dispatch_verification_hours)


def is_pre_dispatch_task_overdue(task: VerificationTask) -> bool:
    if task.status not in _ACTIVE_TASK_STATUSES:
        return False
    due_at = as_utc(task.due_at)
    return due_at is not None and due_at <= _now()


def is_pre_dispatch_task_requeueable(task: VerificationTask | None) -> bool:
    if task is None:
        return False
    contact_result = (task.contact_result or "").strip().upper()
    return (
        task.verification_conclusion == "UNVERIFIABLE"
        or contact_result in _REQUEUE_CONTACT_RESULTS
    )


def latest_submitted_pre_dispatch_task_ids(
    db: Session,
    lead_ids: list[str],
) -> dict[str, str]:
    """Return one latest submitted verification task per lead without loading notes."""

    unique_lead_ids = list(dict.fromkeys(lead_ids))
    if not unique_lead_ids:
        return {}
    ranked_tasks = (
        select(
            VerificationTask.lead_id.label("lead_id"),
            VerificationTask.id.label("task_id"),
            func.row_number()
            .over(
                partition_by=VerificationTask.lead_id,
                order_by=(
                    VerificationTask.submitted_at.desc(),
                    VerificationTask.created_at.desc(),
                    VerificationTask.id.desc(),
                ),
            )
            .label("position"),
        )
        .join(
            VerificationSubmission,
            VerificationSubmission.task_id == VerificationTask.id,
        )
        .where(
            VerificationTask.lead_id.in_(unique_lead_ids),
            VerificationTask.task_type == VerificationTaskType.PRE_DISPATCH_VERIFY.value,
        )
        .subquery()
    )
    rows = db.execute(
        select(ranked_tasks.c.lead_id, ranked_tasks.c.task_id).where(
            ranked_tasks.c.position == 1
        )
    ).all()
    return {row.lead_id: row.task_id for row in rows}


def pre_dispatch_verification_info(
    db: Session,
    task: VerificationTask,
) -> dict[str, Any] | None:
    """Load the submitted facts for an authorized task-detail response."""

    row = db.execute(
        select(VerificationSubmission, User)
        .outerjoin(User, User.id == VerificationSubmission.submitted_by)
        .where(VerificationSubmission.task_id == task.id)
        .order_by(VerificationSubmission.created_at.desc(), VerificationSubmission.id.desc())
    ).first()
    if row is None:
        return None
    submission, submitter = row
    return {
        "submitted_by": submission.submitted_by,
        "submitted_by_name": (
            submitter.display_name or submitter.username if submitter is not None else None
        ),
        "submitted_at": (
            task.submitted_at or submission.created_at
        ).isoformat(),
        "contact_result": task.contact_result,
        "conclusion": task.verification_conclusion,
        "note": submission.note,
    }


def restart_pre_dispatch_after_correction(
    db: Session,
    *,
    lead: Lead,
) -> VerificationTask:
    """Invalidate verification of changed facts and queue a fresh task."""

    if lead.status not in {
        LeadV12Status.PENDING_TELESALES_VERIFY.value,
        LeadV12Status.PENDING_OPERATION_DISPOSITION.value,
    }:
        raise AppError(
            "PRE_DISPATCH_RESTART_STATE_INVALID",
            "当前客资不在可重新核验的阶段",
            409,
        )
    tasks = list(
        db.scalars(
            select(VerificationTask)
            .where(
                VerificationTask.lead_id == lead.id,
                VerificationTask.task_type
                == VerificationTaskType.PRE_DISPATCH_VERIFY.value,
                VerificationTask.status.in_(
                    {*_ACTIVE_TASK_STATUSES, VerificationTaskStatus.SUBMITTED.value}
                ),
            )
            .order_by(VerificationTask.created_at.asc(), VerificationTask.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    for task in tasks:
        task.status = VerificationTaskStatus.RELEASED.value
        task.lock_version += 1
    if lead.status == LeadV12Status.PENDING_OPERATION_DISPOSITION.value:
        assert_lead_transition(lead.status, LeadV12Status.DRAFT)
        lead.status = LeadV12Status.DRAFT.value
        assert_lead_transition(lead.status, LeadV12Status.PENDING_REVIEW)
        lead.status = LeadV12Status.PENDING_REVIEW.value
    lead.review_status = "PENDING"
    lead.pending_reason = None
    db.flush()
    return queue_pre_dispatch_task(
        db,
        lead_id=lead.id,
        reason="CORRECTION_REVERIFY_REQUIRED",
    )


def _lead_or_raise(db: Session, lead_id: str, *, lock: bool = False) -> Lead:
    stmt = select(Lead).where(Lead.id == lead_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    lead = db.scalar(stmt)
    if lead is None or lead.deleted_at is not None:
        raise AppError("LEAD_NOT_FOUND", "客资不存在", 404)
    return lead


def _telesales_or_raise(db: Session, user_id: str) -> User:
    user = db.scalar(
        select(User)
        .join(User.roles)
        .where(User.id == user_id, User.status == "ACTIVE", Role.code == "TELESALES")
    )
    if user is None:
        raise AppError("PRE_DISPATCH_TELESALES_REQUIRED", "任务只能派发给启用中的电销人员", 422)
    return user


def _task_or_raise(db: Session, task_id: str, *, lock: bool = False) -> VerificationTask:
    stmt = select(VerificationTask).where(
        VerificationTask.id == task_id,
        VerificationTask.task_type == VerificationTaskType.PRE_DISPATCH_VERIFY.value,
    )
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    task = db.scalar(stmt)
    if task is None:
        raise AppError("PRE_DISPATCH_TASK_NOT_FOUND", "前置电销核验任务不存在", 404)
    return task


def _lead_then_task_for_update(
    db: Session,
    task_id: str,
) -> tuple[Lead, VerificationTask]:
    """Lock every pre-dispatch mutation in the same Lead -> Task order."""

    lead_id = db.scalar(
        select(VerificationTask.lead_id).where(
            VerificationTask.id == task_id,
            VerificationTask.task_type == VerificationTaskType.PRE_DISPATCH_VERIFY.value,
        )
    )
    if lead_id is None:
        raise AppError("PRE_DISPATCH_TASK_NOT_FOUND", "前置电销核验任务不存在", 404)
    lead = _lead_or_raise(db, lead_id, lock=True)
    task = _task_or_raise(db, task_id, lock=True)
    if task.lead_id != lead.id:
        raise AppError("PRE_DISPATCH_TASK_CONFLICT", "前置电销核验任务关联客资已变化", 409)
    return lead, task


def _assignment_snapshot(task: VerificationTask) -> dict[str, Any]:
    return {
        "lead_id": task.lead_id,
        "assignee_user_id": task.assignee_user_id,
        "assigned_by": task.assigned_by,
        "assigned_at": task.assigned_at.isoformat() if task.assigned_at else None,
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "submitted_at": task.submitted_at.isoformat() if task.submitted_at else None,
        "status": task.status,
        "lock_version": task.lock_version,
    }


def queue_pre_dispatch_task(
    db: Session,
    *,
    lead_id: str,
    reason: str,
) -> VerificationTask:
    """Place a lead into the telesales queue before anyone can dispatch it."""

    normalized_reason = reason.strip()
    if not normalized_reason:
        raise AppError("PRE_DISPATCH_REASON_REQUIRED", "派发前置核验必须填写原因", 422)
    lead = _lead_or_raise(db, lead_id, lock=True)
    require_correction_review_resolved(lead)
    if lead.status == LeadV12Status.PENDING_TELESALES_VERIFY.value:
        existing = db.scalar(
            select(VerificationTask)
            .where(
                VerificationTask.lead_id == lead.id,
                VerificationTask.task_type == VerificationTaskType.PRE_DISPATCH_VERIFY.value,
                VerificationTask.status.in_(_ACTIVE_TASK_STATUSES),
            )
            .order_by(VerificationTask.created_at.desc())
            .with_for_update()
        )
        if existing is not None:
            return existing
    elif lead.status != LeadV12Status.PENDING_REVIEW.value:
        raise AppError("PRE_DISPATCH_LEAD_STATE_INVALID", "当前客资不可进入前置电销核验队列", 409)

    task = VerificationTask(
        lead_id=lead.id,
        task_type=VerificationTaskType.PRE_DISPATCH_VERIFY.value,
        status=VerificationTaskStatus.PENDING.value,
    )
    db.add(task)
    if lead.status != LeadV12Status.PENDING_TELESALES_VERIFY.value:
        assert_lead_transition(lead.status, LeadV12Status.PENDING_TELESALES_VERIFY)
        lead.status = LeadV12Status.PENDING_TELESALES_VERIFY.value
    lead.pending_reason = normalized_reason
    db.flush()
    return task


def assign_pre_dispatch_task(
    db: Session,
    *,
    lead_id: str,
    assignee_user_id: str,
    assigned_by: str,
    reason: str,
    template_code: str = "PRE_DISPATCH",
) -> PreDispatchAssignmentResult:
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise AppError("PRE_DISPATCH_REASON_REQUIRED", "派发前置核验必须填写原因", 422)
    lead = _lead_or_raise(db, lead_id, lock=True)
    require_correction_review_resolved(lead)
    allowed_statuses = {
        LeadV12Status.PENDING_REVIEW.value,
        LeadV12Status.PENDING_TELESALES_VERIFY.value,
    }
    if lead.status == LeadV12Status.DRAFT.value:
        if lead.source_kind not in _OPERATION_DRAFT_SOURCE_KINDS:
            raise AppError(
                "PRE_DISPATCH_LEAD_STATE_INVALID",
                "只有运营录入或飞书导入的公海池草稿可直接进入电销核实",
                409,
            )
    elif lead.status not in allowed_statuses:
        raise AppError("PRE_DISPATCH_LEAD_STATE_INVALID", "当前客资不可派发前置电销核验", 409)
    phone = normalize_phone(decrypt_text(lead.phone_encrypted) or "")
    if len(phone) != 11 or not phone.startswith("1"):
        raise AppError(
            "PRE_DISPATCH_PHONE_REQUIRED",
            "手机号必填且必须为 11 位有效号码",
            422,
        )
    if lead.duplicate_status not in {None, "", "CLEAR"}:
        raise AppError(
            "PRE_DISPATCH_DUPLICATE_UNRESOLVED",
            "手机号查重结论尚未处理，不能派发电销核验",
            409,
        )
    _telesales_or_raise(db, assignee_user_id)
    active = db.scalar(
        select(VerificationTask)
        .where(
            VerificationTask.lead_id == lead.id,
            VerificationTask.task_type == VerificationTaskType.PRE_DISPATCH_VERIFY.value,
            VerificationTask.status.in_(_ACTIVE_TASK_STATUSES),
        )
        .with_for_update()
    )
    now = _now()
    if active is not None:
        before = _assignment_snapshot(active)
        if active.assignee_user_id == assignee_user_id:
            return PreDispatchAssignmentResult(task=active, before=before, after=before)
        active.assignee_user_id = assignee_user_id
        active.assigned_by = assigned_by
        active.assigned_at = now
        active.started_at = None
        active.due_at = _due_at(now)
        active.contact_result = None
        active.verification_conclusion = None
        active.submitted_at = None
        active.status = VerificationTaskStatus.ASSIGNED.value
        active.lock_version += 1
        db.flush()
        return PreDispatchAssignmentResult(
            task=active,
            before=before,
            after=_assignment_snapshot(active),
        )
    template = latest_published_template(db, template_code)
    task = VerificationTask(
        lead_id=lead.id,
        template_id=template.id,
        template_version=template.version,
        task_type=VerificationTaskType.PRE_DISPATCH_VERIFY.value,
        status=VerificationTaskStatus.ASSIGNED.value,
        assignee_user_id=assignee_user_id,
        assigned_by=assigned_by,
        assigned_at=now,
        due_at=_due_at(now),
    )
    db.add(task)
    assert_lead_transition(lead.status, LeadV12Status.PENDING_TELESALES_VERIFY)
    lead.status = LeadV12Status.PENDING_TELESALES_VERIFY.value
    lead.pending_reason = normalized_reason
    db.flush()
    return PreDispatchAssignmentResult(
        task=task,
        before=None,
        after=_assignment_snapshot(task),
    )


def start_pre_dispatch_task(db: Session, *, task_id: str, principal: Principal) -> VerificationTask:
    lead, task = _lead_then_task_for_update(db, task_id)
    require_correction_review_resolved(lead)
    if task.status == VerificationTaskStatus.IN_PROGRESS.value and task.assignee_user_id == principal.user_id:
        return task
    if task.status != VerificationTaskStatus.ASSIGNED.value or task.assignee_user_id is None:
        raise AppError("PRE_DISPATCH_TASK_NOT_ASSIGNED", "任务须由运营派发后才能开始", 409)
    if task.assignee_user_id != principal.user_id:
        raise AppError("FORBIDDEN", "任务已派发给其他电销人员", 403)
    task.status = VerificationTaskStatus.IN_PROGRESS.value
    task.started_at = task.started_at or _now()
    task.lock_version += 1
    db.flush()
    return task


def submit_pre_dispatch_verification(
    db: Session,
    *,
    task_id: str,
    principal: Principal,
    contact_result: str,
    conclusion: str,
    note: str,
) -> VerificationSubmission:
    lead, task = _lead_then_task_for_update(db, task_id)
    normalized_conclusion = conclusion.strip().upper()
    normalized_note = note.strip()
    if normalized_conclusion not in _CONCLUSIONS:
        raise AppError("PRE_DISPATCH_CONCLUSION_INVALID", "前置核验结论无效", 422)
    if not normalized_note:
        raise AppError("PRE_DISPATCH_NOTE_REQUIRED", "提交核验结论必须填写事实说明", 422)
    if task.status == VerificationTaskStatus.SUBMITTED.value and task.assignee_user_id == principal.user_id:
        submission = db.scalar(
            select(VerificationSubmission)
            .where(VerificationSubmission.task_id == task.id)
            .order_by(VerificationSubmission.created_at.desc())
        )
        if submission is not None:
            return submission
    if task.status != VerificationTaskStatus.IN_PROGRESS.value or task.assignee_user_id != principal.user_id:
        raise AppError("PRE_DISPATCH_TASK_NOT_OWNED", "任务不属于当前电销人员或尚未开始", 409)
    require_correction_review_resolved(lead)
    if lead.status != LeadV12Status.PENDING_TELESALES_VERIFY.value:
        raise AppError("PRE_DISPATCH_LEAD_STATE_INVALID", "客资当前不在前置电销核验阶段", 409)
    task.contact_result = contact_result.strip().upper()
    task.verification_conclusion = normalized_conclusion
    task.status = VerificationTaskStatus.SUBMITTED.value
    task.submitted_at = _now()
    task.lock_version += 1
    assert_lead_transition(lead.status, LeadV12Status.PENDING_OPERATION_DISPOSITION)
    lead.status = LeadV12Status.PENDING_OPERATION_DISPOSITION.value
    # A callback/re-entered lead must actually pass the new telesales round.
    # Preserve its marker through submission; ordinary first-round operational
    # discretion for incomplete information remains unchanged.
    prefix = "PRE_DISPATCH_REVERIFY" if lead.pending_reason == "PRE_DISPATCH_REVERIFY_REQUIRED" else "PRE_DISPATCH"
    lead.pending_reason = f"{prefix}_{normalized_conclusion}"
    submission = VerificationSubmission(
        task_id=task.id,
        lead_id=lead.id,
        result=normalized_conclusion,
        answers_json={},
        corrections_json={},
        note=normalized_note,
        submitted_by=principal.user_id,
    )
    db.add(submission)
    db.flush()
    return submission


def requeue_unreachable_pre_dispatch(
    db: Session,
    *,
    lead_id: str,
    principal: Principal,
    reason: str,
) -> PreDispatchRequeueResult:
    """Queue a fresh verification round while preserving the submitted round."""

    _require_operation(principal)
    normalized_reason = reason.strip()
    if len(normalized_reason) < 2:
        raise AppError("PRE_DISPATCH_REQUEUE_REASON_REQUIRED", "重新核验原因至少 2 个字符", 422)
    candidate = _lead_or_raise(db, lead_id)
    is_idempotent_retry = (
        candidate.status == LeadV12Status.PENDING_TELESALES_VERIFY.value
        and candidate.pending_reason == "PRE_DISPATCH_REVERIFY_REQUIRED"
    )
    if candidate.status not in _REQUEUE_LEAD_STATUSES and not is_idempotent_retry:
        raise AppError(
            "PRE_DISPATCH_REQUEUE_STATE_INVALID",
            "只有待运营处置、已关闭或无效的未接通客资可以重新进入核验队列",
            409,
        )
    supplier_company_id = candidate.supplier_company_id
    if supplier_company_id:
        require_supply_write_enabled(db, supplier_company_id)
    lead = _lead_or_raise(db, lead_id, lock=True)
    if lead.supplier_company_id != supplier_company_id:
        raise AppError(
            "PRE_DISPATCH_REQUEUE_CONCURRENT_CHANGE",
            "客资供资方已变更，请刷新后重试",
            409,
        )
    if lead.current_assignment_id is not None:
        raise AppError(
            "PRE_DISPATCH_REQUEUE_ASSIGNMENT_EXISTS",
            "客资已有派发记录，不能重新进入前置核验队列",
            409,
        )
    active = db.scalar(
        select(VerificationTask)
        .where(
            VerificationTask.lead_id == lead.id,
            VerificationTask.task_type == VerificationTaskType.PRE_DISPATCH_VERIFY.value,
            VerificationTask.status.in_(_ACTIVE_TASK_STATUSES),
        )
        .order_by(VerificationTask.created_at.desc(), VerificationTask.id.desc())
        .with_for_update()
    )
    if (
        lead.status == LeadV12Status.PENDING_TELESALES_VERIFY.value
        and lead.pending_reason == "PRE_DISPATCH_REVERIFY_REQUIRED"
        and active is not None
    ):
        previous_task_id = db.scalar(
            select(VerificationTask.id)
            .where(
                VerificationTask.lead_id == lead.id,
                VerificationTask.task_type == VerificationTaskType.PRE_DISPATCH_VERIFY.value,
                VerificationTask.status == VerificationTaskStatus.RELEASED.value,
                VerificationTask.submitted_at.is_not(None),
            )
            .order_by(VerificationTask.submitted_at.desc(), VerificationTask.id.desc())
            .limit(1)
        )
        return PreDispatchRequeueResult(
            task=active,
            previous_task_id=previous_task_id or active.id,
            idempotent=True,
        )
    if lead.status not in _REQUEUE_LEAD_STATUSES:
        raise AppError(
            "PRE_DISPATCH_REQUEUE_STATE_INVALID",
            "只有待运营处置、已关闭或无效的未接通客资可以重新进入核验队列",
            409,
        )
    previous = db.scalar(
        select(VerificationTask)
        .where(
            VerificationTask.lead_id == lead.id,
            VerificationTask.task_type == VerificationTaskType.PRE_DISPATCH_VERIFY.value,
        )
        .order_by(VerificationTask.created_at.desc(), VerificationTask.id.desc())
        .with_for_update()
    )
    if previous is None or previous.status not in {
        VerificationTaskStatus.SUBMITTED.value,
        VerificationTaskStatus.RELEASED.value,
    }:
        raise AppError("PRE_DISPATCH_CONCLUSION_REQUIRED", "未找到可重新核验的电销结论", 409)
    if not is_pre_dispatch_task_requeueable(previous):
        raise AppError(
            "PRE_DISPATCH_REQUEUE_CONCLUSION_INVALID",
            "仅无人接听、未接、拒接或无法核验的客资可以重新进入核验队列",
            409,
        )
    if previous.status == VerificationTaskStatus.SUBMITTED.value:
        previous.status = VerificationTaskStatus.RELEASED.value
        previous.lock_version += 1
    assert_lead_transition(lead.status, LeadV12Status.PENDING_TELESALES_VERIFY)
    lead.status = LeadV12Status.PENDING_TELESALES_VERIFY.value
    lead.review_status = "PENDING"
    lead.pending_reason = "PRE_DISPATCH_REVERIFY_REQUIRED"
    lead.review_note = normalized_reason
    lead.reviewed_at = None
    lead.snapshot_version += 1
    task = VerificationTask(
        lead_id=lead.id,
        template_id=previous.template_id,
        template_version=previous.template_version,
        task_type=VerificationTaskType.PRE_DISPATCH_VERIFY.value,
        status=VerificationTaskStatus.PENDING.value,
    )
    db.add(task)
    db.flush()
    return PreDispatchRequeueResult(
        task=task,
        previous_task_id=previous.id,
        idempotent=False,
    )


def decide_pre_dispatch_disposition(
    db: Session,
    *,
    lead_id: str,
    principal: Principal,
    decision: str,
    note: str,
) -> Lead:
    normalized_decision = decision.strip().upper()
    normalized_note = note.strip()
    if normalized_decision not in _DISPOSITIONS:
        raise AppError("PRE_DISPATCH_DECISION_INVALID", "运营处置决定无效", 422)
    if not normalized_note:
        raise AppError("PRE_DISPATCH_NOTE_REQUIRED", "运营处置必须填写说明", 422)
    lead = _lead_or_raise(db, lead_id, lock=True)
    require_correction_review_resolved(lead)
    if lead.status != LeadV12Status.PENDING_OPERATION_DISPOSITION.value:
        raise AppError("PRE_DISPATCH_LEAD_STATE_INVALID", "当前客资不在待运营处置阶段", 409)
    task = db.scalar(
        select(VerificationTask)
        .where(
            VerificationTask.lead_id == lead.id,
            VerificationTask.task_type == VerificationTaskType.PRE_DISPATCH_VERIFY.value,
            VerificationTask.status == VerificationTaskStatus.SUBMITTED.value,
        )
        .order_by(VerificationTask.submitted_at.desc())
        .with_for_update()
    )
    if task is None or not task.verification_conclusion:
        raise AppError("PRE_DISPATCH_CONCLUSION_REQUIRED", "电销提交事实结论后才能运营处置", 409)
    if normalized_decision == "APPROVE_POOL":
        if (str(lead.pending_reason or "").startswith("PRE_DISPATCH_REVERIFY_")
            and task.verification_conclusion != "QUALIFIED"):
            raise AppError("PRE_DISPATCH_NOT_QUALIFIED", "重新核验通过后才能进入派发池", 409)
        target = approved_lead_pool_target(db, lead)
        lead.review_status = "APPROVED"
        lead.pending_reason = (
            "PUBLIC_POOL_NO_LOCAL_RECEIVER"
            if target is LeadV12Status.PUBLIC_POOL
            else None
        )
    elif normalized_decision == "RETURN_REWORK":
        target = LeadV12Status.DRAFT
        lead.review_status = "DRAFT"
        lead.pending_reason = "PRE_DISPATCH_REWORK_REQUIRED"
    elif normalized_decision == "DUPLICATE":
        target = LeadV12Status.DUPLICATE
        lead.review_status = "PENDING"
        lead.pending_reason = "PRE_DISPATCH_DUPLICATE"
    else:
        supplier = lead.source_kind == LeadSourceKind.SUPPLIER_H5.value
        target = LeadV12Status.INVALID if supplier else LeadV12Status.CLOSED
        lead.review_status = "REJECTED"
        lead.pending_reason = "PRE_DISPATCH_SUPPLIER_INVALID" if supplier else "PRE_DISPATCH_CLOSED"
    assert_lead_transition(lead.status, target)
    lead.status = target.value
    lead.review_note = normalized_note
    lead.reviewed_at = _now()
    task.status = VerificationTaskStatus.RELEASED.value
    task.lock_version += 1
    db.flush()
    return lead
