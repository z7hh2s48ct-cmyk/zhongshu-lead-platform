from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.auth import CurrentPrincipal, require_permissions
from ..core.database import get_db
from ..core.errors import AppError
from ..core.models import Assignment, FollowUp, Lead
from ..core.responses import ok
from ..schemas.followups import FollowUpBody
from ..services.audit import write_audit
from ..services.followup_service import add_followup, followup_to_dict, run_followup_overdue
from ..services.company_assignment_v12 import require_company_assignment_access

router = APIRouter(prefix="/followups", tags=["followups"])


@router.get("/assignments/{assignment_id}")
def list_assignment_followups(assignment_id: str, request: Request, principal: CurrentPrincipal, db: Session = Depends(get_db)):
    assignment = db.get(Assignment, assignment_id)
    if not assignment:
        raise AppError("ASSIGNMENT_NOT_FOUND", "派发订单不存在", 404)
    if principal.has_any_role("FRANCHISE_OWNER", "FRANCHISE_EMPLOYEE"):
        require_company_assignment_access(principal, assignment)
        lead = db.get(Lead, assignment.lead_id)
        if lead is None or lead.deleted_at is not None:
            raise AppError("ASSIGNMENT_NOT_FOUND", "派发订单不存在", 404)
    elif not (principal.can("assignment.read") or principal.can("*")):
        raise AppError("FORBIDDEN", "无权查看跟进记录", 403)
    items = db.scalars(select(FollowUp).where(FollowUp.assignment_id == assignment_id).order_by(FollowUp.created_at.desc())).all()
    return ok(request, [followup_to_dict(x) for x in items])


@router.post("/assignments/{assignment_id}")
def create_followup(assignment_id: str, body: FollowUpBody, request: Request, principal=Depends(require_permissions("followup.own.manage")), db: Session = Depends(get_db)):
    assignment = db.get(Assignment, assignment_id)
    if not assignment:
        raise AppError("ASSIGNMENT_NOT_FOUND", "派发订单不存在", 404)
    item = add_followup(db, assignment=assignment, principal=principal, status=body.status, note=body.note, next_followup_at=body.next_followup_at)
    write_audit(db, principal=principal, action="FOLLOWUP_CREATE", resource_type="followup", resource_id=item.id, company_id=assignment.company_id, after=followup_to_dict(item), request_id=request.state.request_id)
    db.commit()
    return ok(request, followup_to_dict(item), "跟进已保存")


@router.post("/jobs/overdue")
def run_overdue_job(request: Request, principal=Depends(require_permissions("*")), db: Session = Depends(get_db)):
    result = run_followup_overdue(db)
    write_audit(db, principal=principal, action="JOB_FOLLOWUP_OVERDUE", resource_type="job", after=result, request_id=request.state.request_id)
    db.commit()
    return ok(request, result)
