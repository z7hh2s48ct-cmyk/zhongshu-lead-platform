from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.auth import CurrentPrincipal, require_permissions
from ..core.database import get_db
from ..core.errors import AppError
from ..core.models import Lead, VerificationTask, VerificationTemplate
from ..core.responses import ok, page
from ..schemas.verification import (
    VerificationAssignBody,
    VerificationSubmitBody,
    VerificationTaskCreateBody,
    VerificationTemplateBody,
)
from ..services.audit import write_audit
from ..services.verification_service import (
    assign_task,
    claim_task,
    create_tasks,
    publish_template,
    reclaim_task,
    submit_verification,
    task_to_dict,
    require_live_verification_lead,
)

router = APIRouter(prefix="/verification", tags=["verification"])


@router.get("/templates")
def list_templates(request: Request, principal=Depends(require_permissions("verification.read")), db: Session = Depends(get_db)):
    items = db.scalars(select(VerificationTemplate).order_by(VerificationTemplate.code, VerificationTemplate.version.desc())).all()
    return ok(request, [{"id": x.id, "code": x.code, "name": x.name, "version": x.version, "status": x.status, "schema": x.schema_json} for x in items])


@router.post("/templates")
def create_template(body: VerificationTemplateBody, request: Request, principal=Depends(require_permissions("*")), db: Session = Depends(get_db)):
    template = publish_template(db, code=body.code, name=body.name, schema=body.schema_definition)
    write_audit(db, principal=principal, action="VERIFICATION_TEMPLATE_PUBLISH", resource_type="verification_template", resource_id=template.id, after={"code": template.code, "version": template.version}, request_id=request.state.request_id)
    db.commit()
    return ok(request, {"id": template.id, "version": template.version})


@router.post("/tasks")
def generate_tasks(body: VerificationTaskCreateBody, request: Request, principal=Depends(require_permissions("verification.read")), db: Session = Depends(get_db)):
    tasks = create_tasks(db, lead_ids=body.lead_ids, assignee_user_id=body.assignee_user_id, assigned_by=principal.user_id, template_code=body.template_code)
    for task in tasks:
        write_audit(db, principal=principal, action="VERIFICATION_TASK_CREATE", resource_type="verification_task", resource_id=task.id, metadata={"lead_id": task.lead_id, "assignee": task.assignee_user_id}, request_id=request.state.request_id)
    db.commit()
    return ok(request, {"created": len(tasks), "task_ids": [x.id for x in tasks]})


@router.get("/tasks")
def list_tasks(
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
    status: str | None = Query(default=None),
    mine: bool = Query(default=False),
    page_no: int = Query(default=1, alias="page", ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
):
    if not (principal.can("verification.read") or principal.can("verification.task.read") or principal.can("*")):
        raise AppError("FORBIDDEN", "无权查看核验任务", 403)
    stmt = select(VerificationTask)
    count_stmt = select(func.count(VerificationTask.id))
    if mine or principal.has_any_role("TELESALES"):
        live_lead_ids = select(Lead.id).where(Lead.deleted_at.is_(None))
        stmt = stmt.where(
            VerificationTask.assignee_user_id == principal.user_id,
            VerificationTask.lead_id.in_(live_lead_ids),
        )
        count_stmt = count_stmt.where(
            VerificationTask.assignee_user_id == principal.user_id,
            VerificationTask.lead_id.in_(live_lead_ids),
        )
    if status:
        stmt = stmt.where(VerificationTask.status == status)
        count_stmt = count_stmt.where(VerificationTask.status == status)
    total = db.scalar(count_stmt) or 0
    items = db.scalars(stmt.order_by(VerificationTask.created_at.desc()).offset((page_no - 1) * page_size).limit(page_size)).all()
    return ok(request, page([task_to_dict(db, x, principal) for x in items], total, page_no, page_size))


@router.get("/tasks/{task_id}")
def get_task(task_id: str, request: Request, principal: CurrentPrincipal, db: Session = Depends(get_db)):
    task = db.get(VerificationTask, task_id)
    if not task:
        raise AppError("VERIFICATION_TASK_NOT_FOUND", "核验任务不存在", 404)
    if principal.has_any_role("TELESALES") and task.assignee_user_id != principal.user_id:
        raise AppError("FORBIDDEN", "无权查看该任务", 403)
    if principal.has_any_role("TELESALES"):
        require_live_verification_lead(db, task)
    return ok(request, task_to_dict(db, task, principal, include_phone=True))


@router.post("/tasks/{task_id}/assign")
def assign(task_id: str, body: VerificationAssignBody, request: Request, principal=Depends(require_permissions("verification.read")), db: Session = Depends(get_db)):
    task = db.get(VerificationTask, task_id)
    if not task:
        raise AppError("VERIFICATION_TASK_NOT_FOUND", "核验任务不存在", 404)
    assign_task(db, task, body.assignee_user_id, principal.user_id)
    write_audit(db, principal=principal, action="VERIFICATION_TASK_ASSIGN", resource_type="verification_task", resource_id=task.id, metadata={"assignee": body.assignee_user_id}, request_id=request.state.request_id)
    db.commit()
    return ok(request)


@router.post("/tasks/{task_id}/reclaim")
def reclaim(task_id: str, request: Request, principal=Depends(require_permissions("verification.read")), db: Session = Depends(get_db)):
    task = db.get(VerificationTask, task_id)
    if not task:
        raise AppError("VERIFICATION_TASK_NOT_FOUND", "核验任务不存在", 404)
    prior_assignee = task.assignee_user_id
    reclaim_task(db, task)
    write_audit(db, principal=principal, action="VERIFICATION_TASK_RECLAIM", resource_type="verification_task", resource_id=task.id, metadata={"prior_assignee": prior_assignee}, request_id=request.state.request_id)
    db.commit()
    return ok(request)


@router.post("/tasks/{task_id}/start")
def start(task_id: str, request: Request, principal=Depends(require_permissions("verification.task.start")), db: Session = Depends(get_db)):
    task = db.get(VerificationTask, task_id)
    if not task:
        raise AppError("VERIFICATION_TASK_NOT_FOUND", "核验任务不存在", 404)
    claim_task(db, task, principal)
    write_audit(db, principal=principal, action="VERIFICATION_TASK_START", resource_type="verification_task", resource_id=task.id, request_id=request.state.request_id)
    db.commit()
    return ok(request, task_to_dict(db, task, principal, include_phone=True))


@router.post("/tasks/{task_id}/claim", include_in_schema=False)
def legacy_claim(task_id: str, request: Request, principal=Depends(require_permissions("verification.task.start")), db: Session = Depends(get_db)):
    """Compatibility endpoint: it only starts a task already assigned by operations."""

    return start(task_id=task_id, request=request, principal=principal, db=db)


@router.post("/tasks/{task_id}/dial")
def dial(task_id: str, request: Request, principal=Depends(require_permissions("lead.phone.dial")), db: Session = Depends(get_db)):
    task = db.get(VerificationTask, task_id)
    if not task or task.assignee_user_id != principal.user_id:
        raise AppError("FORBIDDEN", "无权拨打该客资电话", 403)
    require_live_verification_lead(db, task)
    payload = task_to_dict(db, task, principal, include_phone=True)
    phone = payload["lead"]["phone"]
    write_audit(db, principal=principal, action="LEAD_PHONE_DIAL_CLICK", resource_type="lead", resource_id=task.lead_id, metadata={"task_id": task.id}, request_id=request.state.request_id)
    db.commit()
    return ok(request, {"phone": phone, "tel_url": f"tel:{phone}"})


@router.post("/tasks/{task_id}/submit")
def submit(task_id: str, body: VerificationSubmitBody, request: Request, principal=Depends(require_permissions("verification.submit")), db: Session = Depends(get_db)):
    task = db.get(VerificationTask, task_id)
    if not task:
        raise AppError("VERIFICATION_TASK_NOT_FOUND", "核验任务不存在", 404)
    submission = submit_verification(db, task, principal, body.model_dump())
    write_audit(db, principal=principal, action="VERIFICATION_SUBMIT", resource_type="verification_task", resource_id=task.id, after={"result": submission.result, "lead_id": submission.lead_id}, request_id=request.state.request_id)
    db.commit()
    return ok(request, {"submission_id": submission.id, "result": submission.result})
