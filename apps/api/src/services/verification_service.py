from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.auth import Principal
from ..core.enums import LeadStatus, VerificationResult, VerificationTaskStatus
from ..core.errors import AppError
from ..core.models import Lead, User, VerificationSubmission, VerificationTask, VerificationTemplate
from ..core.security import decrypt_text, mask_phone
from ..core.v12_enums import VerificationTaskType


def _require_legacy_verification_task(task: VerificationTask) -> None:
    if task.task_type in {
        VerificationTaskType.PRE_DISPATCH_VERIFY.value,
        VerificationTaskType.RETURN_VERIFY.value,
    }:
        task_label = (
            "前置电销核验"
            if task.task_type == VerificationTaskType.PRE_DISPATCH_VERIFY.value
            else "退回电销核验"
        )
        raise AppError(
            "PRE_DISPATCH_V12_API_REQUIRED",
            f"{task_label}任务请使用 V1.2 专用入口",
            409,
        )


def latest_published_template(db: Session, code: str) -> VerificationTemplate:
    template = db.scalar(
        select(VerificationTemplate)
        .where(VerificationTemplate.code == code, VerificationTemplate.status == "PUBLISHED")
        .order_by(VerificationTemplate.version.desc())
    )
    if not template:
        raise AppError("VERIFICATION_TEMPLATE_MISSING", "核验模板尚未发布", 409)
    return template


def publish_template(db: Session, *, code: str, name: str, schema: dict[str, Any]) -> VerificationTemplate:
    latest = db.scalar(select(func.max(VerificationTemplate.version)).where(VerificationTemplate.code == code)) or 0
    template = VerificationTemplate(
        code=code,
        name=name,
        version=latest + 1,
        schema_json=schema,
        status="PUBLISHED",
        effective_at=datetime.now(timezone.utc),
    )
    db.add(template)
    return template


def create_tasks(
    db: Session,
    *,
    lead_ids: list[str],
    assignee_user_id: str,
    assigned_by: str,
    template_code: str,
) -> list[VerificationTask]:
    template = latest_published_template(db, template_code)
    assignee = db.scalar(select(User).where(User.id == assignee_user_id))
    if assignee is None or "TELESALES" not in {role.code for role in assignee.roles}:
        raise AppError("VERIFICATION_TELESALES_REQUIRED", "核验任务只能派发给启用中的电销人员", 422)
    if assignee.status != "ACTIVE":
        raise AppError("VERIFICATION_TELESALES_DISABLED", "不能向已停用的电销人员派发任务", 409)
    created: list[VerificationTask] = []
    for lead_id in lead_ids:
        lead = db.get(Lead, lead_id)
        if not lead or lead.status not in {LeadStatus.IMPORTED, LeadStatus.VERIFYING}:
            continue
        existing = db.scalar(
            select(VerificationTask).where(
                VerificationTask.lead_id == lead_id,
                VerificationTask.status.in_([
                    VerificationTaskStatus.PENDING,
                    VerificationTaskStatus.ASSIGNED,
                    VerificationTaskStatus.IN_PROGRESS,
                ]),
            )
        )
        if existing:
            continue
        task = VerificationTask(
            lead_id=lead_id,
            template_id=template.id,
            template_version=template.version,
            status=VerificationTaskStatus.ASSIGNED,
            assignee_user_id=assignee_user_id,
            assigned_by=assigned_by,
            assigned_at=datetime.now(timezone.utc),
            task_type=VerificationTaskType.LEAD_VERIFY_LEGACY.value,
        )
        lead.status = LeadStatus.VERIFYING
        lead.pending_reason = None
        db.add(task)
        created.append(task)
    db.flush()
    return created


def assign_task(db: Session, task: VerificationTask, assignee_user_id: str, assigned_by: str) -> VerificationTask:
    _require_legacy_verification_task(task)
    if task.status not in {VerificationTaskStatus.PENDING, VerificationTaskStatus.ASSIGNED}:
        raise AppError("VERIFICATION_TASK_NOT_ASSIGNABLE", "任务当前不可分配", 409)
    assignee = db.scalar(select(User).where(User.id == assignee_user_id))
    if assignee is None or "TELESALES" not in {role.code for role in assignee.roles}:
        raise AppError("VERIFICATION_TELESALES_REQUIRED", "核验任务只能派发给启用中的电销人员", 422)
    if assignee.status != "ACTIVE":
        raise AppError("VERIFICATION_TELESALES_DISABLED", "不能向已停用的电销人员派发任务", 409)
    task.assignee_user_id = assignee_user_id
    task.assigned_by = assigned_by
    task.assigned_at = datetime.now(timezone.utc)
    task.status = VerificationTaskStatus.ASSIGNED
    task.lock_version += 1
    return task


def reclaim_task(db: Session, task: VerificationTask) -> VerificationTask:
    _require_legacy_verification_task(task)
    if task.status not in {VerificationTaskStatus.PENDING, VerificationTaskStatus.ASSIGNED}:
        raise AppError("VERIFICATION_TASK_NOT_RECLAIMABLE", "任务当前不可回收", 409)
    task.assignee_user_id = None
    task.assigned_by = None
    task.assigned_at = None
    task.status = VerificationTaskStatus.PENDING
    task.lock_version += 1
    return task


def claim_task(db: Session, task: VerificationTask, principal: Principal) -> VerificationTask:
    _require_legacy_verification_task(task)
    if task.status not in {VerificationTaskStatus.PENDING, VerificationTaskStatus.ASSIGNED}:
        if task.assignee_user_id == principal.user_id and task.status == VerificationTaskStatus.IN_PROGRESS:
            return task
        raise AppError("VERIFICATION_TASK_CLAIMED", "任务已由其他人员处理", 409)
    if task.assignee_user_id is None:
        raise AppError("VERIFICATION_TASK_NOT_ASSIGNED", "任务尚未由运营人员派发", 409)
    if task.assignee_user_id != principal.user_id:
        raise AppError("FORBIDDEN", "任务已分配给其他电销人员", 403)
    task.status = VerificationTaskStatus.IN_PROGRESS
    task.started_at = task.started_at or datetime.now(timezone.utc)
    task.lock_version += 1
    return task


def task_to_dict(db: Session, task: VerificationTask, principal: Principal, *, include_phone: bool = False) -> dict[str, Any]:
    lead = db.get(Lead, task.lead_id)
    template = db.get(VerificationTemplate, task.template_id) if task.template_id else None
    phone = decrypt_text(lead.phone_encrypted) if lead else None
    can_view = include_phone and (principal.can("lead.phone.read") or principal.can("*"))
    return {
        "id": task.id,
        "lead_id": task.lead_id,
        "status": task.status,
        "assignee_user_id": task.assignee_user_id,
        "template_version": task.template_version,
        "template": template.schema_json if template else {},
        "lead": {
            "customer_name": lead.customer_name if lead else None,
            "phone": phone if can_view else None,
            "phone_masked": mask_phone(phone),
            "city": lead.city if lead else None,
            "district": lead.district if lead else None,
            "category_code": lead.category_code if lead else None,
            "brand_code": lead.brand_code if lead else None,
            "source_channel": lead.source_channel if lead else None,
            "need_summary": lead.need_summary if lead else None,
        },
        "assigned_at": task.assigned_at.isoformat() if task.assigned_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
    }


def submit_verification(db: Session, task: VerificationTask, principal: Principal, payload: dict[str, Any]) -> VerificationSubmission:
    _require_legacy_verification_task(task)
    if task.status != VerificationTaskStatus.IN_PROGRESS or task.assignee_user_id != principal.user_id:
        raise AppError("VERIFICATION_TASK_NOT_OWNED", "任务不属于当前人员或状态已变化", 409)
    lead = db.get(Lead, task.lead_id)
    if not lead:
        raise AppError("LEAD_NOT_FOUND", "客资不存在", 404)
    result = payload["result"]
    invalid_reason = payload.get("invalid_reason")
    if result == VerificationResult.INVALID and not invalid_reason:
        raise AppError("VERIFICATION_INVALID_REASON_REQUIRED", "无效客资必须选择原因", 422)
    corrections = payload.get("corrections") or {}
    _apply_corrections(lead, corrections)
    if result == VerificationResult.QUALIFIED:
        required = [lead.region_code, lead.category_code]
        if not all(required):
            raise AppError("VERIFICATION_REQUIRED_FIELDS", "标准地区和业务类目完整后才能通过", 422)
        lead.status = LeadStatus.QUALIFIED
        lead.pending_reason = None
        lead.verified_at = datetime.now(timezone.utc)
    elif result == VerificationResult.INVALID:
        lead.status = LeadStatus.INVALID
        lead.pending_reason = invalid_reason
    elif result == VerificationResult.DUPLICATE:
        lead.status = LeadStatus.INVALID
        lead.pending_reason = "DUPLICATE_CONFIRMED"
    else:
        lead.status = LeadStatus.VERIFYING
        lead.pending_reason = "NEED_MORE"
    submission = VerificationSubmission(
        task_id=task.id,
        lead_id=lead.id,
        result=result,
        invalid_reason=invalid_reason,
        answers_json=payload.get("answers") or {},
        corrections_json=corrections,
        note=payload.get("note"),
        submitted_by=principal.user_id,
    )
    db.add(submission)
    task.status = VerificationTaskStatus.SUBMITTED
    task.submitted_at = datetime.now(timezone.utc)
    task.lock_version += 1
    db.flush()
    return submission


def _apply_corrections(lead: Lead, corrections: dict[str, Any]) -> None:
    allowed = {
        "customer_name",
        "province",
        "city",
        "district",
        "region_code",
        "category_code",
        "brand_code",
        "need_summary",
        "budget_min",
        "budget_max",
    }
    for key, value in corrections.items():
        if key in allowed and value is not None:
            setattr(lead, key, value)
