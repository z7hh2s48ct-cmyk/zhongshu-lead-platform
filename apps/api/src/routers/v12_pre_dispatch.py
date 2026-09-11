from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core import models_v12 as _models_v12  # noqa: F401
from ..core.auth import CurrentPrincipal, require_permissions
from ..core.database import get_db
from ..core.enums import VerificationTaskStatus
from ..core.errors import AppError
from ..core.models import Lead, VerificationSubmission, VerificationTask
from ..core.responses import ok, page
from ..core.security import decrypt_text, mask_phone
from ..core.v12_enums import VerificationTaskType
from ..schemas.v12_lead_supply import (
    PreDispatchAssignBody,
    PreDispatchDispositionBody,
    PreDispatchSubmitBody,
)
from ..services.audit import write_audit
from ..services.pre_dispatch_v12 import (
    assign_pre_dispatch_task,
    decide_pre_dispatch_disposition,
    is_pre_dispatch_task_overdue,
    pre_dispatch_verification_info,
    require_pre_dispatch_task_not_overdue,
    start_pre_dispatch_task,
    submit_pre_dispatch_verification,
)


router = APIRouter(prefix="/v1.2", tags=["v1.2-pre-dispatch-verification"])

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
    if task is None:
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
    active_assignee_can_view_phone = (
        principal.has_any_role("TELESALES")
        and task.assignee_user_id == principal.user_id
        and task.status == VerificationTaskStatus.IN_PROGRESS.value
        and principal.can("lead.phone.read")
        and not is_overdue
    )
    can_view_phone = bool(
        include_phone and (operation_can_view_phone or active_assignee_can_view_phone)
    )
    phone = decrypt_text(lead.phone_encrypted) if lead and can_view_phone else None
    next_owner = None
    if task.status in {
        VerificationTaskStatus.PENDING.value,
        VerificationTaskStatus.ASSIGNED.value,
        VerificationTaskStatus.IN_PROGRESS.value,
    }:
        next_owner = "OPERATION" if is_overdue else "TELESALES"
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
    filters = [VerificationTask.task_type == VerificationTaskType.PRE_DISPATCH_VERIFY.value]
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
                include_phone=principal.has_any_role("OPERATION", "SUPER_ADMIN")
                and (principal.can("*") or principal.can("lead.phone.read")),
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
    require_pre_dispatch_task_not_overdue(task)
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
