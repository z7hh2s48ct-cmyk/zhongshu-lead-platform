from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core import models_v12 as _models_v12  # noqa: F401
from ..core.auth import CurrentPrincipal, require_permissions
from ..core.database import get_db
from ..core.enums import VerificationTaskStatus
from ..core.errors import AppError
from ..core.models import AuditLog, Lead, VerificationSubmission, VerificationTask
from ..core.responses import ok, page
from ..core.security import decrypt_text, mask_phone
from ..core.v12_enums import VerificationTaskType
from ..schemas.v12_lead_supply import (
    LeadLifecycleReasonBody,
    PreDispatchAssignBody,
    PreDispatchDispositionBody,
    PreDispatchRequeueBody,
    PreDispatchSubmitBody,
)
from ..services.audit import write_audit
from ..services.pre_dispatch_v12 import (
    assign_pre_dispatch_task,
    decide_pre_dispatch_disposition,
    is_pre_dispatch_task_overdue,
    pre_dispatch_verification_info,
    requeue_unreachable_pre_dispatch,
    list_historical_rework_leads,
    reopen_closed_lead,
    restore_historical_rework_lead,
    start_pre_dispatch_task,
    submit_pre_dispatch_verification,
)


router = APIRouter(prefix="/v1.2", tags=["v1.2-pre-dispatch-verification"])

_BEIJING_TZ = timezone(timedelta(hours=8))
_DIAL_AUDIT_ACTIONS = ("V12_PRE_DISPATCH_DIAL_CLICK", "V12_RETURN_VERIFY_DIAL")

_OPEN_TASK_STATUSES = (
    VerificationTaskStatus.PENDING.value,
    VerificationTaskStatus.ASSIGNED.value,
    VerificationTaskStatus.IN_PROGRESS.value,
    VerificationTaskStatus.SUBMITTED.value,
)


def _task_or_raise(db: Session, task_id: str) -> VerificationTask:
    task = db.scalar(
        select(VerificationTask).where(
            VerificationTask.id == task_id,
            VerificationTask.task_type == VerificationTaskType.PRE_DISPATCH_VERIFY.value,
        )
    )
    if task is None or db.scalar(
        select(Lead.id).where(Lead.id == task.lead_id, Lead.deleted_at.is_(None))
    ) is None:
        raise AppError("PRE_DISPATCH_TASK_NOT_FOUND", "前置电销核验任务不存在", 404)
    return task


def _task_to_dict(
    db: Session,
    task: VerificationTask,
    principal: CurrentPrincipal,
    *,
    include_phone: bool = False,
    include_verification_info: bool = False,
    leads_by_id: dict[str, Lead] | None = None,
) -> dict:
    lead = (
        leads_by_id.get(task.lead_id)
        if leads_by_id is not None
        else db.get(Lead, task.lead_id)
    )
    is_overdue = is_pre_dispatch_task_overdue(task)
    operation_can_view_phone = principal.has_any_role("OPERATION", "SUPER_ADMIN") and (
        principal.can("lead.phone.read") or principal.can("*")
    )
    assignee_can_view_phone = (
        principal.has_any_role("TELESALES")
        and task.assignee_user_id == principal.user_id
        and principal.can("lead.phone.read")
        and (
            task.status == VerificationTaskStatus.IN_PROGRESS.value
            or task.submitted_at is not None
        )
    )
    can_view_phone = bool(
        include_phone and (operation_can_view_phone or assignee_can_view_phone)
    )
    phone = decrypt_text(lead.phone_encrypted) if lead and can_view_phone else None
    next_owner = None
    if task.status in {
        VerificationTaskStatus.PENDING.value,
        VerificationTaskStatus.ASSIGNED.value,
        VerificationTaskStatus.IN_PROGRESS.value,
    }:
        next_owner = "TELESALES"
    elif task.status == VerificationTaskStatus.SUBMITTED.value:
        next_owner = "OPERATION"
    result = {
        "id": task.id,
        "task_type": task.task_type,
        "status": task.status,
        "lead_id": task.lead_id,
        "assignee_user_id": task.assignee_user_id,
        "assigned_at": task.assigned_at.isoformat() if task.assigned_at else None,
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "is_overdue": is_overdue,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "submitted_at": task.submitted_at.isoformat() if task.submitted_at else None,
        "contact_result": task.contact_result,
        "conclusion": task.verification_conclusion,
        "requires_qualified_verification": bool(
            lead and str(lead.pending_reason or "").startswith("PRE_DISPATCH_REVERIFY_")
        ),
        "lead": {
            "source_kind": lead.source_kind if lead else None,
            "customer_name": lead.customer_name if lead else None,
            "phone": phone,
            "phone_masked": mask_phone(phone or decrypt_text(lead.phone_encrypted)) if lead else None,
            "city": lead.city if lead else None,
            "district": lead.district if lead else None,
            "need_summary": lead.need_summary if lead else None,
            "status": lead.status if lead else None,
            "next_owner": next_owner if lead else None,
        },
    }
    if include_verification_info:
        result["verification_info"] = pre_dispatch_verification_info(db, task)
    return result


def _task_list_to_dict(
    db: Session,
    tasks: list[VerificationTask],
    principal: CurrentPrincipal,
    *,
    include_phone: bool = False,
) -> list[dict]:
    lead_ids = list(dict.fromkeys(task.lead_id for task in tasks))
    leads = (
        db.scalars(select(Lead).where(Lead.id.in_(lead_ids))).all()
        if lead_ids
        else []
    )
    leads_by_id = {lead.id: lead for lead in leads}
    return [
        _task_to_dict(
            db,
            task,
            principal,
            include_phone=include_phone,
            leads_by_id=leads_by_id,
        )
        for task in tasks
    ]


def _require_telesales(principal: CurrentPrincipal) -> None:
    if not principal.has_any_role("TELESALES"):
        raise AppError("FORBIDDEN", "仅电销人员可执行前置核验任务", 403)


@router.get("/admin/leads/pre-dispatch-rework-history")
def list_pre_dispatch_rework_history(
    request: Request,
    principal=Depends(require_permissions("lead.supplier.review")),
    db: Session = Depends(get_db),
    page_no: int = Query(default=1, alias="page", ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    items, total = list_historical_rework_leads(
        db,
        page_no=page_no,
        page_size=page_size,
    )
    return ok(
        request,
        page(
            [
                {
                    "id": item["lead"].id,
                    "customer_name": item["lead"].customer_name,
                    "status": item["lead"].status,
                    "pending_reason": item["lead"].pending_reason,
                    "rework_pending": item["rework_pending"],
                    "latest_task_id": item["latest_task_id"],
                }
                for item in items
            ],
            total,
            page_no,
            page_size,
        ),
    )


@router.post("/admin/leads/{lead_id}/pre-dispatch-rework-restore")
def restore_pre_dispatch_rework_history(
    lead_id: str,
    body: LeadLifecycleReasonBody,
    request: Request,
    principal=Depends(require_permissions("lead.supplier.review")),
    db: Session = Depends(get_db),
):
    lead = restore_historical_rework_lead(
        db,
        lead_id=lead_id,
        principal=principal,
        reason=body.reason,
    )
    write_audit(
        db,
        principal=principal,
        action="V12_PRE_DISPATCH_REWORK_RESTORE",
        resource_type="lead",
        resource_id=lead.id,
        after={"status": lead.status, "pending_reason": lead.pending_reason},
        reason=body.reason,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, {"id": lead.id, "status": lead.status, "pending_reason": lead.pending_reason})


@router.post("/admin/leads/{lead_id}/reopen")
def reopen_closed_lead_endpoint(
    lead_id: str,
    body: LeadLifecycleReasonBody,
    request: Request,
    principal=Depends(require_permissions("lead.supplier.review")),
    db: Session = Depends(get_db),
):
    lead = reopen_closed_lead(
        db,
        lead_id=lead_id,
        principal=principal,
        reason=body.reason,
    )
    write_audit(
        db,
        principal=principal,
        action="V12_CLOSED_LEAD_REOPEN",
        resource_type="lead",
        resource_id=lead.id,
        after={"status": lead.status, "pending_reason": lead.pending_reason},
        reason=body.reason,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, {"id": lead.id, "status": lead.status, "pending_reason": lead.pending_reason})


@router.post("/admin/leads/{lead_id}/pre-dispatch-verification")
def assign_pre_dispatch_verification(
    lead_id: str,
    body: PreDispatchAssignBody,
    request: Request,
    principal=Depends(require_permissions("lead.supplier.review")),
    db: Session = Depends(get_db),
):
    assignment = assign_pre_dispatch_task(
        db,
        lead_id=lead_id,
        assignee_user_id=body.assignee_user_id,
        assigned_by=principal.user_id,
        reason=body.reason,
        template_code=body.template_code,
    )
    task = assignment.task
    write_audit(
        db,
        principal=principal,
        action="V12_PRE_DISPATCH_VERIFY_ASSIGN",
        resource_type="verification_task",
        resource_id=task.id,
        before=assignment.before,
        after=assignment.after,
        reason=body.reason,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, _task_to_dict(db, task, principal), "前置电销核验已派发")


@router.get("/pre-dispatch-verifications/dial-stats")
def get_telesales_dial_stats(
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
):
    """电销本人通话量统计：今日/本周/本月（北京时间自然周期）。

    口径（2026-09-19 按常规做法确认）：
    - dials：点击拨号的次数（前置核验 + 退回核验的拨号审计事件），含未接通；
    - submitted：本人已提交核验结论的任务数。
    """
    if not (principal.can("dashboard.telesales.read") or principal.can("*")):
        raise AppError("FORBIDDEN", "无权查看通话统计", 403)
    now_utc = datetime.now(timezone.utc)
    local_now = now_utc.astimezone(_BEIJING_TZ)
    today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    bounds = {
        "today": today_start,
        "week": today_start - timedelta(days=today_start.weekday()),
        "month": today_start.replace(day=1),
    }
    stats: dict[str, dict[str, int]] = {}
    for key, start in bounds.items():
        dials = int(
            db.scalar(
                select(func.count(AuditLog.id)).where(
                    AuditLog.actor_user_id == principal.user_id,
                    AuditLog.action.in_(_DIAL_AUDIT_ACTIONS),
                    AuditLog.created_at >= start,
                )
            )
            or 0
        )
        submitted = int(
            db.scalar(
                select(func.count(VerificationTask.id)).where(
                    VerificationTask.assignee_user_id == principal.user_id,
                    VerificationTask.submitted_at.is_not(None),
                    VerificationTask.submitted_at >= start,
                )
            )
            or 0
        )
        stats[key] = {"dials": dials, "submitted": submitted}
    return ok(request, {"period": "BEIJING_NATURAL", **stats})


@router.get("/pre-dispatch-verifications/tasks")
def list_pre_dispatch_tasks(
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
    status: str | None = Query(default=None),
    lead_id: str | None = Query(default=None),
    submitted_history: bool = False,
    page_no: int = Query(default=1, alias="page", ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
):
    if not (principal.can("verification.read") or principal.can("verification.task.read") or principal.can("*")):
        raise AppError("FORBIDDEN", "无权查看前置核验任务", 403)
    filters = [
        VerificationTask.task_type == VerificationTaskType.PRE_DISPATCH_VERIFY.value,
        VerificationTask.lead_id.in_(select(Lead.id).where(Lead.deleted_at.is_(None))),
    ]
    if lead_id:
        filters.append(VerificationTask.lead_id == lead_id)
    if principal.has_any_role("TELESALES"):
        filters.append(VerificationTask.assignee_user_id == principal.user_id)
    if submitted_history:
        filters.append(
            VerificationTask.id.in_(select(VerificationSubmission.task_id))
        )
    elif status:
        filters.append(VerificationTask.status == status.strip().upper())
    else:
        filters.append(VerificationTask.status.in_(_OPEN_TASK_STATUSES))
    total = int(db.scalar(select(func.count(VerificationTask.id)).where(*filters)) or 0)
    order_by = (
        (
            VerificationTask.submitted_at.desc(),
            VerificationTask.created_at.desc(),
            VerificationTask.id.desc(),
        )
        if submitted_history
        else (
            VerificationTask.assigned_at.desc(),
            VerificationTask.created_at.desc(),
            VerificationTask.id.desc(),
        )
    )
    tasks = list(db.scalars(
        select(VerificationTask)
        .where(*filters)
        .order_by(*order_by)
        .offset((page_no - 1) * page_size)
        .limit(page_size)
    ).all())
    return ok(
        request,
        page(
            _task_list_to_dict(
                db,
                tasks,
                principal,
                include_phone=(
                    submitted_history and principal.has_any_role("TELESALES")
                ) or (
                    principal.has_any_role("OPERATION", "SUPER_ADMIN")
                    and (principal.can("*") or principal.can("lead.phone.read"))
                ),
            ),
            total,
            page_no,
            page_size,
        ),
    )


@router.get("/pre-dispatch-verifications/tasks/{task_id}")
def pre_dispatch_task_detail(
    task_id: str,
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
):
    task = _task_or_raise(db, task_id)
    if principal.has_any_role("TELESALES") and task.assignee_user_id != principal.user_id:
        raise AppError("FORBIDDEN", "无权查看其他电销任务", 403)
    if not (principal.can("verification.read") or principal.can("verification.task.read") or principal.can("*")):
        raise AppError("FORBIDDEN", "无权查看前置核验任务", 403)
    return ok(
        request,
        _task_to_dict(
            db,
            task,
            principal,
            include_phone=True,
            include_verification_info=True,
        ),
    )


@router.post("/pre-dispatch-verifications/tasks/{task_id}/start")
def start_pre_dispatch_verification(
    task_id: str,
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
):
    _require_telesales(principal)
    if not principal.can("verification.task.start"):
        raise AppError("FORBIDDEN", "无权开始前置核验任务", 403)
    task = start_pre_dispatch_task(db, task_id=task_id, principal=principal)
    write_audit(
        db,
        principal=principal,
        action="V12_PRE_DISPATCH_VERIFY_START",
        resource_type="verification_task",
        resource_id=task.id,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, _task_to_dict(db, task, principal, include_phone=True), "已开始核验")


@router.post("/pre-dispatch-verifications/tasks/{task_id}/dial")
def dial_pre_dispatch_verification(
    task_id: str,
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
):
    _require_telesales(principal)
    if not principal.can("lead.phone.dial"):
        raise AppError("FORBIDDEN", "无权拨打客户电话", 403)
    task = _task_or_raise(db, task_id)
    if task.assignee_user_id != principal.user_id or task.status != VerificationTaskStatus.IN_PROGRESS.value:
        raise AppError("PRE_DISPATCH_TASK_NOT_OWNED", "仅进行中的本人任务可拨号", 409)
    payload = _task_to_dict(db, task, principal, include_phone=True)
    phone = payload["lead"]["phone"]
    if not phone:
        raise AppError("PRE_DISPATCH_PHONE_REQUIRED", "前置电话核验需要客户联系电话", 422)
    write_audit(
        db,
        principal=principal,
        action="V12_PRE_DISPATCH_DIAL_CLICK",
        resource_type="lead",
        resource_id=task.lead_id,
        metadata={"task_id": task.id},
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, {"phone": phone, "tel_url": f"tel:{phone}"})


@router.post("/pre-dispatch-verifications/tasks/{task_id}/submit")
def submit_pre_dispatch_task(
    task_id: str,
    body: PreDispatchSubmitBody,
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
):
    _require_telesales(principal)
    if not principal.can("verification.submit"):
        raise AppError("FORBIDDEN", "无权提交前置核验结论", 403)
    submission = submit_pre_dispatch_verification(
        db,
        task_id=task_id,
        principal=principal,
        contact_result=body.contact_result,
        conclusion=body.conclusion,
        note=body.note,
    )
    write_audit(
        db,
        principal=principal,
        action="V12_PRE_DISPATCH_VERIFY_SUBMIT",
        resource_type="verification_task",
        resource_id=task_id,
        after={"lead_id": submission.lead_id, "conclusion": submission.result, "next_owner": "OPERATION"},
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, {"submission_id": submission.id, "result": submission.result}, "核验结论已提交运营处置")


@router.post("/admin/leads/{lead_id}/pre-dispatch-verification/requeue")
def requeue_pre_dispatch_verification(
    lead_id: str,
    body: PreDispatchRequeueBody,
    request: Request,
    principal=Depends(require_permissions("lead.supplier.review")),
    db: Session = Depends(get_db),
):
    result = requeue_unreachable_pre_dispatch(
        db,
        lead_id=lead_id,
        principal=principal,
        reason=body.reason,
    )
    if not result.idempotent:
        write_audit(
            db,
            principal=principal,
            action="V12_PRE_DISPATCH_VERIFY_REQUEUE",
            resource_type="verification_task",
            resource_id=result.task.id,
            before={"task_id": result.previous_task_id, "status": "SUBMITTED"},
            after={
                "task_id": result.task.id,
                "lead_id": result.task.lead_id,
                "status": result.task.status,
                "previous_task_id": result.previous_task_id,
            },
            reason=body.reason,
            request_id=request.state.request_id,
        )
        db.commit()
    return ok(
        request,
        {
            "task": {
                "id": result.task.id,
                "status": result.task.status,
                "assignee_user_id": result.task.assignee_user_id,
                "due_at": result.task.due_at.isoformat() if result.task.due_at else None,
            },
            "previous_task_id": result.previous_task_id,
            "idempotent": result.idempotent,
        },
        "客资已重新进入前置核验队列",
    )


@router.post("/admin/leads/{lead_id}/pre-dispatch-disposition")
def decide_pre_dispatch_lead(
    lead_id: str,
    body: PreDispatchDispositionBody,
    request: Request,
    principal=Depends(require_permissions("lead.supplier.review")),
    db: Session = Depends(get_db),
):
    lead = decide_pre_dispatch_disposition(
        db,
        lead_id=lead_id,
        principal=principal,
        decision=body.decision,
        note=body.note,
    )
    write_audit(
        db,
        principal=principal,
        action="V12_PRE_DISPATCH_DISPOSITION",
        resource_type="lead",
        resource_id=lead.id,
        after={
            "status": lead.status,
            "review_status": lead.review_status,
            "lead_snapshot": {
                "source_kind": lead.source_kind,
                "customer_name": lead.customer_name,
                "city": lead.city,
                "district": lead.district,
                "region_code": lead.region_code,
                "category_code": lead.category_code,
                "need_summary": lead.need_summary,
                "review_note": lead.review_note,
                "pending_reason": lead.pending_reason,
            },
        },
        reason=body.note,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(
        request,
        {
            "lead_id": lead.id,
            "status": lead.status,
            "review_status": lead.review_status,
            "pending_reason": lead.pending_reason,
        },
        "运营处置已完成",
    )
