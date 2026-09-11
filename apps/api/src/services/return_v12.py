from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.auth import Principal
from ..core.config import get_settings
from ..core.enums import AssignmentStatus, EvidenceType, PointsLedgerType, VerificationTaskStatus
from ..core.errors import AppError
from ..core.models import (
    Assignment,
    AssignmentEvent,
    Lead,
    PointsLedger,
    ReturnEvidence,
    ReturnRequest,
    Role,
    User,
    UserRole,
    VerificationTask,
)
from ..core.models_v12 import SupplierLeadReward
from ..core.security import decrypt_text, mask_phone
from ..core.state_machine_v12 import assert_lead_transition, assert_return_transition
from ..core.time import as_utc
from ..core.v12_enums import (
    LeadV12Status,
    ReturnReasonCode,
    ReturnV12Status,
    RewardStatus,
    VerificationTaskType,
)
from .points_service import change_points
from .company_assignment_v12 import require_return_request_access
from .lead_correction_guard import require_correction_review_resolved


logger = logging.getLogger("zhongshu.return_v12")

VALID_RETURN_REASONS = {item.value for item in ReturnReasonCode}
RETURN_EVIDENCE_TYPES = {
    EvidenceType.CHAT_SCREENSHOT.value,
    EvidenceType.CALL_RECORDING.value,
}
ACTIVE_RETURN_TASK_STATUSES = {
    VerificationTaskStatus.PENDING.value,
    VerificationTaskStatus.ASSIGNED.value,
    VerificationTaskStatus.IN_PROGRESS.value,
}


def _return_task_is_overdue(task: VerificationTask) -> bool:
    due_at = as_utc(task.due_at)
    return bool(
        due_at is not None
        and due_at <= _now()
        and task.status in {
            VerificationTaskStatus.ASSIGNED.value,
            VerificationTaskStatus.IN_PROGRESS.value,
        }
    )


def _require_return_task_not_overdue(task: VerificationTask) -> None:
    if _return_task_is_overdue(task):
        raise AppError("RETURN_VERIFY_TASK_OVERDUE", "退回事实核验任务已超时，请联系运营人员改派", 409)


def require_return_verification_task_not_overdue(task: VerificationTask) -> None:
    """Keep every executable return-verification action behind the same deadline rule."""
    _require_return_task_not_overdue(task)


@dataclass(frozen=True, slots=True)
class ReturnSubmitResult:
    request: ReturnRequest
    task: VerificationTask | None
    expired: bool = False
    idempotent: bool = False


@dataclass(frozen=True, slots=True)
class ReturnFinalReviewResult:
    request: ReturnRequest
    refund_ledger: PointsLedger | None
    idempotent: bool = False


@dataclass(frozen=True, slots=True)
class ReturnVerificationAssignmentResult:
    task: VerificationTask
    before: dict[str, Any]
    after: dict[str, Any]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get_return(db: Session, return_id: str, *, lock: bool = False) -> ReturnRequest:
    stmt = select(ReturnRequest).where(ReturnRequest.id == return_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    item = db.scalar(stmt)
    if item is None:
        raise AppError("RETURN_NOT_FOUND", "退回申请不存在", 404)
    return item


def _get_assignment(db: Session, assignment_id: str, *, lock: bool = False) -> Assignment:
    stmt = select(Assignment).where(Assignment.id == assignment_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    item = db.scalar(stmt)
    if item is None:
        raise AppError("ASSIGNMENT_NOT_FOUND", "派发单不存在", 404)
    return item


def _get_lead(db: Session, lead_id: str, *, lock: bool = False) -> Lead:
    stmt = select(Lead).where(Lead.id == lead_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    item = db.scalar(stmt)
    if item is None:
        raise AppError("LEAD_NOT_FOUND", "客资不存在", 404)
    return item


def _appeal_deadline(assignment: Assignment) -> datetime:
    claimed_at = as_utc(assignment.claimed_at)
    if not claimed_at:
        raise AppError("RETURN_NOT_CLAIMED", "未领取客资不能申请退回", 409)
    # Old unsubmitted records follow the same rule during a rolling release.
    deadline = claimed_at + timedelta(hours=48)
    assignment.appeal_deadline_at = deadline
    return deadline


def _initial_deadline(assignment: Assignment, request: ReturnRequest) -> datetime:
    deadline = _appeal_deadline(assignment)
    request.appeal_deadline_at = deadline
    request.due_at = deadline
    return deadline


def _lock_return_context(db: Session, return_id: str) -> tuple[Assignment, Lead, ReturnRequest]:
    reference = _get_return(db, return_id)
    assignment = _get_assignment(db, reference.assignment_id, lock=True)
    lead = _get_lead(db, assignment.lead_id, lock=True)
    request = _get_return(db, return_id, lock=True)
    if request.assignment_id != assignment.id or request.lead_id != lead.id:
        raise AppError("RETURN_ASSIGNMENT_CONFLICT", "退回申请与当前派发单不一致", 409)
    return assignment, lead, request


def create_or_update_return_draft(
    db: Session,
    *,
    assignment_id: str,
    principal: Principal,
    reason_code: str,
    description: str,
) -> ReturnRequest:
    assignment = _get_assignment(db, assignment_id, lock=True)
    lead = _get_lead(db, assignment.lead_id, lock=True)
    # Assignment is the first lock in every return mutation. It also serializes
    # the missing-row case so concurrent draft creators cannot both insert.
    item = db.scalar(
        select(ReturnRequest)
        .where(ReturnRequest.assignment_id == assignment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    require_return_request_access(
        principal,
        assignment,
        submitted_by=item.submitted_by if item else None,
    )
    require_correction_review_resolved(lead)
    reason = reason_code.strip().upper()
    if reason not in VALID_RETURN_REASONS:
        raise AppError("RETURN_REASON_INVALID", "退回原因不在 V1.2 冻结范围内", 422)

    if item:
        if item.company_id != principal.company_id:
            raise AppError("FORBIDDEN", "无权修改该退回申请", 403)
        if item.status not in {
            ReturnV12Status.DRAFT.value,
            ReturnV12Status.NEED_MORE_EVIDENCE.value,
        }:
            raise AppError("RETURN_NOT_EDITABLE", "退回申请当前不可编辑", 409, {"status": item.status})
        if item.submitted_at is None:
            deadline = _initial_deadline(assignment, item)
            if _now() >= deadline:
                raise AppError("RETURN_WINDOW_EXPIRED", "已超过领取后 48 小时退回申诉期", 409)
        item.reason_code = reason
        item.description = description.strip()
        db.flush()
        return item

    if assignment.status not in {
        AssignmentStatus.CLAIMED.value,
        AssignmentStatus.FOLLOWING.value,
        AssignmentStatus.COMPLETED.value,
    }:
        raise AppError("RETURN_NOT_ALLOWED", "派发单当前不可创建退回申请", 409, {"status": assignment.status})
    deadline = _appeal_deadline(assignment)
    if _now() >= deadline:
        raise AppError("RETURN_WINDOW_EXPIRED", "已超过领取后 48 小时退回申诉期", 409)
    item = ReturnRequest(
        assignment_id=assignment.id,
        lead_id=assignment.lead_id,
        company_id=assignment.company_id,
        reason_code=reason,
        reason_version=1,
        description=description.strip(),
        status=ReturnV12Status.DRAFT.value,
        submitted_by=principal.user_id,
        due_at=deadline,
        appeal_deadline_at=deadline,
    )
    db.add(item)
    db.flush()
    return item


def prepare_return_evidence_upload(
    db: Session,
    *,
    request: ReturnRequest,
    principal: Principal,
) -> ReturnRequest:
    assignment, _, locked_request = _lock_return_context(db, request.id)
    require_return_request_access(
        principal,
        assignment,
        submitted_by=locked_request.submitted_by,
    )
    if locked_request.status not in {
        ReturnV12Status.DRAFT.value,
        ReturnV12Status.NEED_MORE_EVIDENCE.value,
    }:
        raise AppError("RETURN_EVIDENCE_LOCKED", "当前状态不能上传证据", 409)
    allowed_assignment_statuses = {
        ReturnV12Status.DRAFT.value: {
            AssignmentStatus.CLAIMED.value,
            AssignmentStatus.FOLLOWING.value,
            AssignmentStatus.COMPLETED.value,
        },
        ReturnV12Status.NEED_MORE_EVIDENCE.value: {
            AssignmentStatus.RETURN_PENDING.value,
        },
    }
    if assignment.status not in allowed_assignment_statuses[locked_request.status]:
        raise AppError(
            "RETURN_EVIDENCE_ASSIGNMENT_STATE_INVALID",
            "派发单状态已变化，不能继续上传退回证据",
            409,
            {"assignment_status": assignment.status},
        )
    if locked_request.submitted_at is None:
        deadline = _initial_deadline(assignment, locked_request)
        if _now() >= deadline:
            raise AppError("RETURN_WINDOW_EXPIRED", "已超过领取后 48 小时退回申诉期", 409)
    return locked_request


def add_return_evidence(
    db: Session,
    *,
    request: ReturnRequest,
    principal: Principal,
    evidence_type: str,
    object_key: str,
    original_name: str,
    mime_type: str,
    file_size: int,
    sha256: str,
    duration_seconds: int | None,
) -> ReturnEvidence:
    request = prepare_return_evidence_upload(
        db,
        request=request,
        principal=principal,
    )
    normalized_type = evidence_type.strip().upper()
    if normalized_type not in RETURN_EVIDENCE_TYPES:
        raise AppError("EVIDENCE_TYPE_INVALID", "证据类型无效", 422)
    evidence = ReturnEvidence(
        return_request_id=request.id,
        evidence_type=normalized_type,
        object_key=object_key,
        original_name=original_name,
        mime_type=mime_type,
        file_size=file_size,
        sha256=sha256,
        duration_seconds=duration_seconds,
        uploaded_by=principal.user_id,
    )
    db.add(evidence)
    db.flush()
    return evidence


def _evidence_summary(db: Session, return_id: str) -> dict[str, int]:
    rows = db.execute(
        select(ReturnEvidence.evidence_type, func.count(ReturnEvidence.id))
        .where(ReturnEvidence.return_request_id == return_id)
        .group_by(ReturnEvidence.evidence_type)
    ).all()
    return {str(evidence_type): int(count) for evidence_type, count in rows}


def _supplementary_evidence_count(db: Session, request: ReturnRequest) -> int:
    if request.status != ReturnV12Status.NEED_MORE_EVIDENCE.value:
        return 0
    evidences = db.execute(
        select(ReturnEvidence.sha256, ReturnEvidence.created_at)
        .where(ReturnEvidence.return_request_id == request.id)
    ).all()
    event = db.scalar(
        select(AssignmentEvent)
        .where(AssignmentEvent.assignment_id == request.assignment_id,
               AssignmentEvent.event_type == "V12_RETURN_NEED_MORE")
        .order_by(AssignmentEvent.occurred_at.desc(), AssignmentEvent.id.desc())
        .limit(1)
    )
    if event is None:
        # Historical/manual NEED_MORE rows may not have a review event. Evidence
        # that existed by the first submission is the safest recoverable baseline.
        submitted_at = as_utc(request.submitted_at)
        if submitted_at is None:
            return 0
        baseline = [digest for digest, created_at in evidences
                    if as_utc(created_at) <= submitted_at]
        return len({digest for digest, _ in evidences} - set(baseline))
    baseline = (event.payload or {}).get("evidence_sha256")
    if baseline is None:
        # Compatibility for existing NEED_MORE records: files already present
        # when the reviewer requested supplementation form the old baseline.
        baseline = [digest for digest, created_at in evidences
                    if as_utc(created_at) <= as_utc(event.occurred_at)]
    return len({digest for digest, _ in evidences} - set(baseline))


def _active_return_task(db: Session, return_id: str) -> VerificationTask | None:
    return db.scalar(
        select(VerificationTask)
        .where(
            VerificationTask.return_request_id == return_id,
            VerificationTask.task_type == VerificationTaskType.RETURN_VERIFY.value,
            VerificationTask.status.in_(ACTIVE_RETURN_TASK_STATUSES),
        )
        .order_by(VerificationTask.created_at.desc())
        .limit(1)
    )


def _freeze_reward(db: Session, assignment_id: str, now: datetime) -> SupplierLeadReward | None:
    reward = db.scalar(
        select(SupplierLeadReward)
        .where(SupplierLeadReward.assignment_id == assignment_id)
        .with_for_update()
    )
    if reward and reward.status == RewardStatus.OBSERVING.value:
        reward.status = RewardStatus.FROZEN.value
        reward.frozen_at = now
    return reward


def submit_return_request(
    db: Session,
    *,
    return_id: str,
    principal: Principal,
) -> ReturnSubmitResult:
    assignment_for_access, lead, request = _lock_return_context(db, return_id)
    require_return_request_access(
        principal,
        assignment_for_access,
        submitted_by=request.submitted_by,
    )
    if request.status in {
        ReturnV12Status.VERIFYING.value,
        ReturnV12Status.REVIEWING.value,
    }:
        task = _active_return_task(db, request.id)
        if task is None and request.verification_task_id:
            task = db.get(VerificationTask, request.verification_task_id)
        return ReturnSubmitResult(request=request, task=task, idempotent=True)
    if request.status not in {
        ReturnV12Status.DRAFT.value,
        ReturnV12Status.NEED_MORE_EVIDENCE.value,
    }:
        raise AppError("RETURN_NOT_SUBMITTABLE", "退回申请当前不可提交", 409, {"status": request.status})

    now = _now()
    initial_submission = request.submitted_at is None
    if initial_submission:
        require_correction_review_resolved(lead)
    deadline = _initial_deadline(assignment_for_access, request) if initial_submission else None
    if initial_submission and now >= deadline:
        assert_return_transition(ReturnV12Status.DRAFT, ReturnV12Status.EXPIRED)
        request.status = ReturnV12Status.EXPIRED.value
        db.flush()
        return ReturnSubmitResult(request=request, task=None, expired=True)

    counts = _evidence_summary(db, request.id)
    screenshot_count = counts.get(EvidenceType.CHAT_SCREENSHOT.value, 0)
    recording_count = counts.get(EvidenceType.CALL_RECORDING.value, 0)
    evidence_count = screenshot_count + recording_count
    if evidence_count < 1:
        raise AppError(
            "RETURN_EVIDENCE_REQUIRED",
            "请至少上传 1 张沟通截图或 1 份电话录音",
            422,
            {"evidence_count": evidence_count},
        )

    if not initial_submission and _supplementary_evidence_count(db, request) < 1:
        raise AppError("RETURN_SUPPLEMENT_REQUIRED", "请按审核要求补充新的证据材料后再提交", 422)

    assignment = assignment_for_access
    previous_assignment_status = assignment.status
    previous_lead_status = lead.status
    if initial_submission:
        if assignment.status not in {
            AssignmentStatus.CLAIMED.value,
            AssignmentStatus.FOLLOWING.value,
            AssignmentStatus.COMPLETED.value,
        }:
            raise AppError("RETURN_ASSIGNMENT_STATE_INVALID", "派发单当前不可提交退回申请", 409)
        request.submitted_at = now
    elif assignment.status != AssignmentStatus.RETURN_PENDING.value:
        raise AppError("RETURN_ASSIGNMENT_STATE_INVALID", "退回补充材料与派发单状态不一致", 409)

    active_task = _active_return_task(db, request.id)
    if active_task:
        return ReturnSubmitResult(request=request, task=active_task, idempotent=True)
    if request.status == ReturnV12Status.DRAFT.value:
        assert_return_transition(ReturnV12Status.DRAFT, ReturnV12Status.SUBMITTED)
    else:
        assert_return_transition(ReturnV12Status.NEED_MORE_EVIDENCE, ReturnV12Status.SUBMITTED)
    assert_return_transition(ReturnV12Status.SUBMITTED, ReturnV12Status.VERIFYING)
    request.status = ReturnV12Status.VERIFYING.value
    request.reviewed_by = None
    request.reviewed_at = None
    request.review_note = None
    request.final_decision_reason = None
    assignment.status = AssignmentStatus.RETURN_PENDING.value
    task = VerificationTask(
        lead_id=lead.id,
        template_id=None,
        template_version=1,
        status=VerificationTaskStatus.PENDING.value,
        task_type=VerificationTaskType.RETURN_VERIFY.value,
        return_request_id=request.id,
        assignment_id=assignment.id,
    )
    db.add(task)
    db.flush()
    request.verification_task_id = task.id
    _freeze_reward(db, assignment.id, now)
    db.add(
        AssignmentEvent(
            assignment_id=assignment.id,
            event_type="V12_RETURN_SUBMITTED",
            actor_user_id=principal.user_id,
            payload={
                "return_request_id": request.id,
                "verification_task_id": task.id,
                "reason_code": request.reason_code,
                "evidence_count": evidence_count,
                "initial_submission": initial_submission,
                "previous_assignment_status": previous_assignment_status,
                "previous_lead_status": previous_lead_status,
            },
        )
    )
    db.flush()
    return ReturnSubmitResult(request=request, task=task)


def _require_telesales_user(db: Session, user_id: str) -> User:
    user = db.scalar(
        select(User)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(User.id == user_id, User.status == "ACTIVE", Role.code == "TELESALES")
    )
    if user is None:
        raise AppError("TELESALES_USER_REQUIRED", "任务只能分配给有效电销人员", 422)
    return user


def _return_task_assignment_snapshot(task: VerificationTask) -> dict[str, Any]:
    return {
        "lead_id": task.lead_id,
        "return_request_id": task.return_request_id,
        "assignee_user_id": task.assignee_user_id,
        "assigned_by": task.assigned_by,
        "assigned_at": task.assigned_at.isoformat() if task.assigned_at else None,
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "submitted_at": task.submitted_at.isoformat() if task.submitted_at else None,
        "status": task.status,
        "lock_version": task.lock_version,
    }


def assign_return_verification_task(
    db: Session,
    *,
    task_id: str,
    assignee_user_id: str,
    assigned_by: str,
    reason: str,
) -> ReturnVerificationAssignmentResult:
    task = db.scalar(select(VerificationTask).where(VerificationTask.id == task_id).with_for_update())
    if task is None or task.task_type != VerificationTaskType.RETURN_VERIFY.value:
        raise AppError("RETURN_VERIFY_TASK_NOT_FOUND", "退回核验任务不存在", 404)
    if task.status not in {
        VerificationTaskStatus.PENDING.value,
        VerificationTaskStatus.ASSIGNED.value,
    } and not (
        task.status == VerificationTaskStatus.IN_PROGRESS.value and _return_task_is_overdue(task)
    ):
        raise AppError("RETURN_VERIFY_TASK_NOT_ASSIGNABLE", "退回核验任务当前不可分配", 409)
    _require_telesales_user(db, assignee_user_id)
    if not reason.strip():
        raise AppError("RETURN_VERIFY_ASSIGN_REASON_REQUIRED", "派发或改派核验任务必须填写原因", 422)
    before = _return_task_assignment_snapshot(task)
    now = _now()
    task.assignee_user_id = assignee_user_id
    task.assigned_by = assigned_by
    task.assigned_at = now
    task.started_at = None
    task.due_at = now + timedelta(hours=get_settings().return_verification_hours)
    task.contact_result = None
    task.verification_conclusion = None
    task.submitted_at = None
    task.status = VerificationTaskStatus.ASSIGNED.value
    task.lock_version += 1
    db.flush()
    return ReturnVerificationAssignmentResult(
        task=task,
        before=before,
        after=_return_task_assignment_snapshot(task),
    )


def claim_return_verification_task(
    db: Session,
    *,
    task_id: str,
    principal: Principal,
) -> VerificationTask:
    task = db.scalar(select(VerificationTask).where(VerificationTask.id == task_id).with_for_update())
    if task is None or task.task_type != VerificationTaskType.RETURN_VERIFY.value:
        raise AppError("RETURN_VERIFY_TASK_NOT_FOUND", "退回核验任务不存在", 404)
    _require_return_task_not_overdue(task)
    if task.status == VerificationTaskStatus.IN_PROGRESS.value and task.assignee_user_id == principal.user_id:
        return task
    if task.status == VerificationTaskStatus.PENDING.value or task.assignee_user_id is None:
        raise AppError(
            "RETURN_VERIFY_TASK_NOT_ASSIGNED",
            "退回核验任务须由运营人员派发后才能开始",
            409,
        )
    if task.status != VerificationTaskStatus.ASSIGNED.value:
        raise AppError("RETURN_VERIFY_TASK_NOT_STARTABLE", "退回核验任务当前不可开始", 409)
    if task.assignee_user_id != principal.user_id:
        raise AppError("FORBIDDEN", "退回核验任务已分配给其他电销人员", 403)
    task.status = VerificationTaskStatus.IN_PROGRESS.value
    task.started_at = task.started_at or _now()
    task.lock_version += 1
    db.flush()
    return task


def submit_return_verification(
    db: Session,
    *,
    task_id: str,
    principal: Principal,
    contact_result: str,
    conclusion: str,
    note: str,
) -> VerificationTask:
    reference = db.get(VerificationTask, task_id)
    if reference is None or reference.task_type != VerificationTaskType.RETURN_VERIFY.value:
        raise AppError("RETURN_VERIFY_TASK_NOT_FOUND", "退回核验任务不存在", 404)
    _, _, request = _lock_return_context(db, reference.return_request_id)
    task = db.scalar(
        select(VerificationTask).where(VerificationTask.id == task_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if task.status == VerificationTaskStatus.SUBMITTED.value and task.assignee_user_id == principal.user_id:
        return task
    _require_return_task_not_overdue(task)
    if task.status != VerificationTaskStatus.IN_PROGRESS.value or task.assignee_user_id != principal.user_id:
        raise AppError("RETURN_VERIFY_TASK_NOT_OWNED", "任务不属于当前电销人员或状态已变化", 409)
    if request.status != ReturnV12Status.VERIFYING.value:
        raise AppError("RETURN_NOT_VERIFYING", "退回申请当前不在电销核验阶段", 409)
    task.contact_result = contact_result.strip().upper()
    task.verification_conclusion = conclusion.strip().upper()
    task.status = VerificationTaskStatus.SUBMITTED.value
    task.submitted_at = _now()
    task.lock_version += 1
    assert_return_transition(ReturnV12Status.VERIFYING, ReturnV12Status.REVIEWING)
    request.status = ReturnV12Status.REVIEWING.value
    request.review_note = note.strip()
    db.add(
        AssignmentEvent(
            assignment_id=request.assignment_id,
            event_type="V12_RETURN_VERIFY_SUBMITTED",
            actor_user_id=principal.user_id,
            payload={
                "return_request_id": request.id,
                "verification_task_id": task.id,
                "contact_result": task.contact_result,
                "conclusion": task.verification_conclusion,
                "note": note.strip(),
            },
        )
    )
    db.flush()
    return task


def _claim_ledger(db: Session, request: ReturnRequest, assignment: Assignment) -> PointsLedger:
    ledger = db.scalar(
        select(PointsLedger)
        .where(
            PointsLedger.company_id == request.company_id,
            PointsLedger.business_id == assignment.id,
            PointsLedger.ledger_type == PointsLedgerType.CLAIM.value,
            PointsLedger.business_type.in_(("V12_ASSIGNMENT_CLAIM", "ASSIGNMENT")),
        )
        .order_by(PointsLedger.created_at.desc())
    )
    if ledger is None:
        raise AppError("RETURN_CLAIM_LEDGER_MISSING", "未找到原领取扣分流水", 409)
    return ledger


def _current_verification_task(db: Session, request: ReturnRequest) -> VerificationTask:
    task = db.scalar(
        select(VerificationTask).where(VerificationTask.id == request.verification_task_id)
        .with_for_update().execution_options(populate_existing=True)
    ) if request.verification_task_id else None
    if task is None or task.task_type != VerificationTaskType.RETURN_VERIFY.value:
        raise AppError("RETURN_VERIFY_TASK_MISSING", "退回申请缺少后置电销核验任务", 409)
    if task.status != VerificationTaskStatus.SUBMITTED.value or not task.verification_conclusion:
        raise AppError("RETURN_VERIFY_CONCLUSION_REQUIRED", "电销事实核验完成后才能终审", 409)
    return task


def _restore_assignment_status(lead: Lead) -> str:
    if lead.status in {LeadV12Status.CLOSED.value, LeadV12Status.COMPLETED.value}:
        return AssignmentStatus.COMPLETED.value
    return (
        AssignmentStatus.FOLLOWING.value
        if lead.status == LeadV12Status.FOLLOWING.value
        else AssignmentStatus.CLAIMED.value
    )


def final_review_return(
    db: Session,
    *,
    return_id: str,
    principal: Principal,
    decision: str,
    note: str,
) -> ReturnFinalReviewResult:
    assignment, lead, request = _lock_return_context(db, return_id)
    normalized_decision = decision.strip().upper()
    if request.status == ReturnV12Status.APPROVED.value and normalized_decision == "APPROVE":
        ledger = db.get(PointsLedger, request.refund_ledger_id) if request.refund_ledger_id else None
        return ReturnFinalReviewResult(request=request, refund_ledger=ledger, idempotent=True)
    if request.status == ReturnV12Status.REJECTED.value and normalized_decision == "REJECT":
        return ReturnFinalReviewResult(request=request, refund_ledger=None, idempotent=True)
    if request.status != ReturnV12Status.REVIEWING.value:
        raise AppError("RETURN_NOT_FINAL_REVIEWABLE", "退回申请当前不可终审", 409, {"status": request.status})
    if normalized_decision not in {"APPROVE", "REJECT", "NEED_MORE"}:
        raise AppError("RETURN_FINAL_DECISION_INVALID", "终审决定无效", 422)

    task = _current_verification_task(db, request)
    required_conclusion = {
        "APPROVE": "SUPPORT_RETURN",
        "REJECT": "DOES_NOT_SUPPORT_RETURN",
    }.get(normalized_decision)
    if required_conclusion and task.verification_conclusion != required_conclusion:
        raise AppError(
            "RETURN_FINAL_DECISION_CONFLICT",
            "终审决定必须与电销核实结论一致；客资可用时仍由原领取人继续跟进",
            409,
            {"decision": normalized_decision, "verification_conclusion": task.verification_conclusion},
        )
    reward = db.scalar(
        select(SupplierLeadReward)
        .where(SupplierLeadReward.assignment_id == assignment.id)
        .with_for_update()
    )
    now = _now()
    request.reviewed_by = principal.user_id
    request.reviewed_at = now
    request.review_note = note.strip()
    request.final_decision_reason = note.strip()
    # SQLite's points-account lock refreshes the session. Persist the review
    # trail first so that refresh cannot discard the reviewer and decision note.
    db.flush()

    if normalized_decision == "NEED_MORE":
        assert_return_transition(ReturnV12Status.REVIEWING, ReturnV12Status.NEED_MORE_EVIDENCE)
        request.status = ReturnV12Status.NEED_MORE_EVIDENCE.value
        task.status = VerificationTaskStatus.RELEASED.value
        task.lock_version += 1
        db.add(
            AssignmentEvent(
                assignment_id=assignment.id,
                event_type="V12_RETURN_NEED_MORE",
                actor_user_id=principal.user_id,
                payload={
                    "return_request_id": request.id,
                    "verification_task_id": task.id,
                    "note": note.strip(),
                    "evidence_sha256": sorted(set(db.scalars(
                        select(ReturnEvidence.sha256).where(ReturnEvidence.return_request_id == request.id)
                    ).all())),
                },
            )
        )
        db.flush()
        return ReturnFinalReviewResult(request=request, refund_ledger=None)

    if normalized_decision == "REJECT":
        assert_return_transition(ReturnV12Status.REVIEWING, ReturnV12Status.REJECTED)
        request.status = ReturnV12Status.REJECTED.value
        task.status = VerificationTaskStatus.RELEASED.value
        task.lock_version += 1
        assignment.status = _restore_assignment_status(lead)
        if lead.status not in {
            LeadV12Status.CLAIMED.value, LeadV12Status.FOLLOWING.value,
            LeadV12Status.COMPLETED.value, LeadV12Status.CLOSED.value,
        }:
            lead.status = LeadV12Status.CLAIMED.value
        if reward and reward.status == RewardStatus.FROZEN.value:
            reward.status = RewardStatus.OBSERVING.value
        db.add(
            AssignmentEvent(
                assignment_id=assignment.id,
                event_type="V12_RETURN_REJECTED",
                actor_user_id=principal.user_id,
                payload={
                    "return_request_id": request.id,
                    "verification_task_id": task.id,
                    "verification_conclusion": task.verification_conclusion,
                    "note": note.strip(),
                },
            )
        )
        db.flush()
        return ReturnFinalReviewResult(request=request, refund_ledger=None)

    if reward and reward.status == RewardStatus.SETTLED.value:
        raise AppError("REWARD_ALREADY_SETTLED", "供应商奖励已结算，需走异常冲正流程", 409)
    claim_ledger = _claim_ledger(db, request, assignment)
    refund_points = abs(int(claim_ledger.delta))
    refund_ledger = change_points(
        db,
        company_id=request.company_id,
        delta=refund_points,
        ledger_type=PointsLedgerType.RETURN.value,
        business_type="V12_RETURN_REFUND",
        business_id=request.id,
        idempotency_key=f"v12-return:{request.id}:refund",
        related_ledger_id=claim_ledger.id,
        created_by=principal.user_id,
        metadata={
            "assignment_id": assignment.id,
            "lead_id": lead.id,
            "claim_ledger_id": claim_ledger.id,
            "actual_claim_points": refund_points,
        },
    )
    assert_return_transition(ReturnV12Status.REVIEWING, ReturnV12Status.APPROVED)
    request.status = ReturnV12Status.APPROVED.value
    request.refund_points = refund_points
    request.refund_ledger_id = refund_ledger.id
    task.status = VerificationTaskStatus.RELEASED.value
    task.lock_version += 1
    assignment.status = AssignmentStatus.RETURNED.value
    assignment.released_at = now
    assignment.release_reason = "V12_RETURN_APPROVED"
    lead.current_assignment_id = None
    # 终审支持退回意味着该客资被确认无效，不再回到派发池。
    assert_lead_transition(lead.status, LeadV12Status.CLOSED)
    lead.status = LeadV12Status.CLOSED.value
    lead.pending_reason = "RETURN_VERIFIED_INVALID"
    lead.current_follow_status = None
    if reward and reward.status in {
        RewardStatus.WAITING_CLAIM.value,
        RewardStatus.FROZEN.value,
        RewardStatus.OBSERVING.value,
    }:
        reward.status = RewardStatus.CANCELLED.value
        reward.cancelled_at = now
    db.add(
        AssignmentEvent(
            assignment_id=assignment.id,
            event_type="V12_RETURN_APPROVED",
            actor_user_id=principal.user_id,
            payload={
                "return_request_id": request.id,
                "verification_task_id": task.id,
                "verification_conclusion": task.verification_conclusion,
                "refund_points": refund_points,
                "refund_ledger_id": refund_ledger.id,
                "reward_id": reward.id if reward else None,
                "note": note.strip(),
            },
        )
    )
    db.flush()
    return ReturnFinalReviewResult(request=request, refund_ledger=refund_ledger)


def return_request_to_dict(
    db: Session,
    item: ReturnRequest,
    *,
    include_evidence: bool = False,
    include_phone: bool = False,
    leads_by_id: dict[str, Lead] | None = None,
    assignments_by_id: dict[str, Assignment] | None = None,
    users_by_id: dict[str, User] | None = None,
    tasks_by_id: dict[str, VerificationTask] | None = None,
    rewards_by_assignment_id: dict[str, SupplierLeadReward] | None = None,
) -> dict[str, Any]:
    lead = (
        leads_by_id.get(item.lead_id)
        if leads_by_id is not None
        else db.get(Lead, item.lead_id)
    )
    assignment = (
        assignments_by_id.get(item.assignment_id)
        if assignments_by_id is not None
        else db.get(Assignment, item.assignment_id)
    )
    submitter = (
        users_by_id.get(item.submitted_by)
        if users_by_id is not None and item.submitted_by
        else db.get(User, item.submitted_by)
        if item.submitted_by
        else None
    )
    snapshot = assignment.lead_snapshot if assignment and assignment.lead_snapshot else {}
    phone_masked = snapshot.get("phone_masked")
    if not phone_masked and lead:
        phone_masked = mask_phone(decrypt_text(lead.phone_encrypted))
    data: dict[str, Any] = {
        "id": item.id,
        "assignment_id": item.assignment_id,
        "lead_id": item.lead_id,
        "company_id": item.company_id,
        "assignment_code": f"PF-{item.assignment_id[:8].upper()}",
        "customer_name": snapshot.get("customer_name") or (lead.customer_name if lead else None),
        "phone": decrypt_text(lead.phone_encrypted) if lead and include_phone else None,
        "phone_masked": phone_masked,
        "city": snapshot.get("city") or (lead.city if lead else None),
        "district": snapshot.get("district") or (lead.district if lead else None),
        "region_code": snapshot.get("region_code") or (lead.region_code if lead else None),
        "submitted_by_name": submitter.display_name if submitter else None,
        "reason_code": item.reason_code,
        "description": item.description,
        "status": item.status,
        "submitted_by": item.submitted_by,
        "submitted_at": item.submitted_at.isoformat() if item.submitted_at else None,
        "appeal_deadline_at": (item.appeal_deadline_at or item.due_at).isoformat()
        if (item.appeal_deadline_at or item.due_at)
        else None,
        "verification_task_id": item.verification_task_id,
        "reviewed_by": item.reviewed_by,
        "reviewed_at": item.reviewed_at.isoformat() if item.reviewed_at else None,
        "review_note": item.review_note,
        "final_decision_reason": item.final_decision_reason,
        "refund_points": item.refund_points,
        "refund_ledger_id": item.refund_ledger_id,
    }
    if include_evidence:
        data["supplementary_evidence_count"] = _supplementary_evidence_count(db, item)
        evidences = db.scalars(
            select(ReturnEvidence)
            .where(ReturnEvidence.return_request_id == item.id)
            .order_by(ReturnEvidence.created_at.asc())
        ).all()
        data["evidences"] = [
            {
                "id": evidence.id,
                "type": evidence.evidence_type,
                "original_name": evidence.original_name,
                "mime_type": evidence.mime_type,
                "file_size": evidence.file_size,
                "sha256": evidence.sha256,
                "duration_seconds": evidence.duration_seconds,
                "created_at": evidence.created_at.isoformat(),
            }
            for evidence in evidences
        ]
        data["evidence_summary"] = _evidence_summary(db, item.id)
    task = (
        tasks_by_id.get(item.verification_task_id)
        if tasks_by_id is not None and item.verification_task_id
        else db.get(VerificationTask, item.verification_task_id)
        if item.verification_task_id
        else None
    )
    if task:
        data["verification"] = {
            "task_id": task.id,
            "status": task.status,
            "assignee_user_id": task.assignee_user_id,
            "contact_result": task.contact_result,
            "conclusion": task.verification_conclusion,
            "submitted_at": task.submitted_at.isoformat() if task.submitted_at else None,
            "due_at": task.due_at.isoformat() if task.due_at else None,
            "is_overdue": _return_task_is_overdue(task),
        }
        if include_evidence and task.submitted_at is not None:
            submission_event = next(
                (
                    event
                    for event in db.scalars(
                        select(AssignmentEvent)
                        .where(
                            AssignmentEvent.assignment_id == task.assignment_id,
                            AssignmentEvent.event_type == "V12_RETURN_VERIFY_SUBMITTED",
                        )
                        .order_by(AssignmentEvent.occurred_at.desc(), AssignmentEvent.id.desc())
                    ).all()
                    if (event.payload or {}).get("verification_task_id") == task.id
                ),
                None,
            )
            data["verification"]["note"] = (
                (submission_event.payload or {}).get("note") if submission_event else None
            )
    reward = (
        rewards_by_assignment_id.get(item.assignment_id)
        if rewards_by_assignment_id is not None
        else db.scalar(
            select(SupplierLeadReward).where(
                SupplierLeadReward.assignment_id == item.assignment_id
            )
        )
    )
    if reward:
        data["reward"] = {
            "id": reward.id,
            "status": reward.status,
            "reward_points": reward.reward_points,
            "reward_due_at": reward.reward_due_at.isoformat() if reward.reward_due_at else None,
        }
    return data


def return_request_list_to_dict(
    db: Session,
    items: list[ReturnRequest],
    *,
    include_phone: bool = False,
) -> list[dict[str, Any]]:
    """Serialize one return-request page with a fixed number of relation queries."""
    if not items:
        return []
    lead_ids = {item.lead_id for item in items}
    assignment_ids = {item.assignment_id for item in items}
    user_ids = {item.submitted_by for item in items if item.submitted_by}
    task_ids = {item.verification_task_id for item in items if item.verification_task_id}
    leads_by_id = {
        lead.id: lead
        for lead in db.scalars(select(Lead).where(Lead.id.in_(lead_ids))).all()
    }
    assignments_by_id = {
        assignment.id: assignment
        for assignment in db.scalars(
            select(Assignment).where(Assignment.id.in_(assignment_ids))
        ).all()
    }
    users_by_id = {
        user.id: user
        for user in db.scalars(select(User).where(User.id.in_(user_ids))).all()
    }
    tasks_by_id = (
        {
            task.id: task
            for task in db.scalars(
                select(VerificationTask).where(VerificationTask.id.in_(task_ids))
            ).all()
        }
        if task_ids
        else {}
    )
    rewards_by_assignment_id = {
        reward.assignment_id: reward
        for reward in db.scalars(
            select(SupplierLeadReward).where(
                SupplierLeadReward.assignment_id.in_(assignment_ids)
            )
        ).all()
    }
    return [
        return_request_to_dict(
            db,
            item,
            include_phone=include_phone,
            leads_by_id=leads_by_id,
            assignments_by_id=assignments_by_id,
            users_by_id=users_by_id,
            tasks_by_id=tasks_by_id,
            rewards_by_assignment_id=rewards_by_assignment_id,
        )
        for item in items
    ]


def return_verification_task_list_to_dict(
    db: Session,
    tasks: list[VerificationTask],
    principal: Principal,
    *,
    include_phone: bool = False,
) -> list[dict[str, Any]]:
    """Serialize a verification-task page without per-row relation queries."""
    if not tasks:
        return []
    request_ids = {task.return_request_id for task in tasks if task.return_request_id}
    lead_ids = {task.lead_id for task in tasks}
    assignment_ids = {task.assignment_id for task in tasks if task.assignment_id}
    requests_by_id = {
        item.id: item
        for item in db.scalars(select(ReturnRequest).where(ReturnRequest.id.in_(request_ids))).all()
    }
    leads_by_id = {
        item.id: item for item in db.scalars(select(Lead).where(Lead.id.in_(lead_ids))).all()
    }
    assignments_by_id = {
        item.id: item
        for item in db.scalars(select(Assignment).where(Assignment.id.in_(assignment_ids))).all()
    }
    evidence_summaries: dict[str, dict[str, int]] = {request_id: {} for request_id in request_ids}
    for request_id, evidence_type, count in db.execute(
        select(
            ReturnEvidence.return_request_id,
            ReturnEvidence.evidence_type,
            func.count(ReturnEvidence.id),
        )
        .where(ReturnEvidence.return_request_id.in_(request_ids))
        .group_by(ReturnEvidence.return_request_id, ReturnEvidence.evidence_type)
    ).all():
        evidence_summaries[str(request_id)][str(evidence_type)] = int(count)
    return [
        return_verification_task_to_dict(
            db,
            task,
            principal,
            include_phone=include_phone,
            requests_by_id=requests_by_id,
            leads_by_id=leads_by_id,
            assignments_by_id=assignments_by_id,
            evidence_summaries=evidence_summaries,
        )
        for task in tasks
    ]


def return_verification_task_to_dict(
    db: Session,
    task: VerificationTask,
    principal: Principal,
    *,
    include_phone: bool = False,
    include_verification_info: bool = False,
    requests_by_id: dict[str, ReturnRequest] | None = None,
    leads_by_id: dict[str, Lead] | None = None,
    assignments_by_id: dict[str, Assignment] | None = None,
    evidence_summaries: dict[str, dict[str, int]] | None = None,
) -> dict[str, Any]:
    request = (
        requests_by_id.get(task.return_request_id) if requests_by_id is not None and task.return_request_id
        else db.get(ReturnRequest, task.return_request_id) if task.return_request_id else None
    )
    lead = leads_by_id.get(task.lead_id) if leads_by_id is not None else db.get(Lead, task.lead_id)
    assignment = (
        assignments_by_id.get(task.assignment_id)
        if assignments_by_id is not None and task.assignment_id
        else db.get(Assignment, task.assignment_id) if task.assignment_id else None
    )
    is_overdue = _return_task_is_overdue(task)
    operation_can_view_phone = principal.has_any_role("OPERATION", "SUPER_ADMIN") and (
        principal.can("lead.phone.read") or principal.can("*")
    )
    active_assignee_can_view_phone = (
        task.assignee_user_id == principal.user_id
        and (principal.can("lead.phone.read") or principal.can("*"))
        and not is_overdue
    )
    can_view_phone = bool(
        include_phone and (operation_can_view_phone or active_assignee_can_view_phone)
    )
    phone = decrypt_text(lead.phone_encrypted) if lead and can_view_phone else None
    snapshot = assignment.lead_snapshot if assignment and assignment.lead_snapshot else {}
    phone_masked = snapshot.get("phone_masked")
    if not phone_masked and lead:
        phone_masked = mask_phone(phone or decrypt_text(lead.phone_encrypted))
    result = {
        "id": task.id,
        "task_type": task.task_type,
        "status": task.status,
        "assignee_user_id": task.assignee_user_id,
        "assigned_at": task.assigned_at.isoformat() if task.assigned_at else None,
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "is_overdue": is_overdue,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "submitted_at": task.submitted_at.isoformat() if task.submitted_at else None,
        "contact_result": task.contact_result,
        "conclusion": task.verification_conclusion,
        "return_request": {
            "id": request.id if request else None,
            "status": request.status if request else None,
            "reason_code": request.reason_code if request else None,
            "description": request.description if request else None,
            "appeal_deadline_at": (request.appeal_deadline_at or request.due_at).isoformat()
            if request and (request.appeal_deadline_at or request.due_at)
            else None,
            "evidence_summary": (
                evidence_summaries.get(request.id, {})
                if request and evidence_summaries is not None
                else _evidence_summary(db, request.id)
                if request
                else {}
            ),
        },
        "assignment": {
            "id": assignment.id if assignment else None,
            "company_id": assignment.company_id if assignment else None,
            "status": assignment.status if assignment else None,
        },
        "lead": {
            "id": lead.id if lead else None,
            "customer_name": lead.customer_name if lead else None,
            "phone": phone,
            "phone_masked": phone_masked,
            "city": lead.city if lead else None,
            "district": lead.district if lead else None,
            "region_code": lead.region_code if lead else None,
            "need_summary": lead.need_summary if lead else None,
        },
    }
    if (
        include_verification_info
        and task.submitted_at is not None
    ):
        submission_event = next(
            (
                event
                for event in db.scalars(
                    select(AssignmentEvent)
                    .where(
                        AssignmentEvent.assignment_id == task.assignment_id,
                        AssignmentEvent.event_type == "V12_RETURN_VERIFY_SUBMITTED",
                    )
                    .order_by(AssignmentEvent.occurred_at.desc(), AssignmentEvent.id.desc())
                ).all()
                if (event.payload or {}).get("verification_task_id") == task.id
            ),
            None,
        )
        submitted_by = (
            submission_event.actor_user_id if submission_event else task.assignee_user_id
        )
        submitter = db.get(User, submitted_by) if submitted_by else None
        submitted_at = task.submitted_at or (
            submission_event.occurred_at if submission_event else None
        )
        if submission_event is None:
            logger.warning(
                "return verification submission event missing task_id=%s return_request_id=%s",
                task.id,
                request.id if request else task.return_request_id,
            )
        result["verification_info"] = {
            "submitted_by": submitted_by,
            "submitted_by_name": (
                submitter.display_name or submitter.username
                if submitter is not None
                else None
            ),
            "submitted_at": submitted_at.isoformat() if submitted_at else None,
            "contact_result": task.contact_result,
            "conclusion": task.verification_conclusion,
            "note": (
                (submission_event.payload or {}).get("note")
                if submission_event
                else None
            ),
        }
    elif include_verification_info:
        result["verification_info"] = None
    return result
