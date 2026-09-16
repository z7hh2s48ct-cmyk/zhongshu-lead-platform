from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.auth import CurrentPrincipal, require_permissions
from ..core.database import get_db
from ..core.enums import LeadStatus
from ..core.errors import AppError
from ..core.models import Assignment, AssignmentEvent, Lead
from ..core.responses import ok, page
from ..schemas.dispatch import DispatchBody, ReleaseAssignmentBody
from ..services.audit import write_audit
from ..services.company_assignment_v12 import require_company_assignment_access
from ..services.dispatch_service import assignment_to_dict, candidate_companies, dispatch_lead, release_assignment
from ..services.lead_service import lead_to_dict
from ..services.lead_points_v12 import get_lead_points_settings

router = APIRouter(prefix="/dispatch", tags=["dispatch"])


@router.get("/qualified-leads")
def qualified_leads(
    request: Request,
    principal=Depends(require_permissions("lead.dispatch")),
    db: Session = Depends(get_db),
    region_code: str | None = Query(default=None),
    category_code: str | None = Query(default=None),
    page_no: int = Query(default=1, alias="page", ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
):
    stmt = select(Lead).where(
        Lead.status == LeadStatus.QUALIFIED,
        Lead.current_assignment_id.is_(None),
        Lead.deleted_at.is_(None),
    )
    count_stmt = select(func.count(Lead.id)).where(
        Lead.status == LeadStatus.QUALIFIED,
        Lead.current_assignment_id.is_(None),
        Lead.deleted_at.is_(None),
    )
    if region_code:
        stmt = stmt.where(Lead.region_code == region_code)
        count_stmt = count_stmt.where(Lead.region_code == region_code)
    if category_code:
        stmt = stmt.where(Lead.category_code == category_code)
        count_stmt = count_stmt.where(Lead.category_code == category_code)
    total = db.scalar(count_stmt) or 0
    items = db.scalars(stmt.order_by(Lead.verified_at.asc(), Lead.created_at.asc()).offset((page_no - 1) * page_size).limit(page_size)).all()
    return ok(request, page([lead_to_dict(x, principal) for x in items], total, page_no, page_size))


@router.get("/leads/{lead_id}/candidates")
def candidates(lead_id: str, request: Request, principal=Depends(require_permissions("lead.dispatch")), db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead or lead.deleted_at is not None:
        raise AppError("LEAD_NOT_FOUND", "客资不存在", 404)
    include_balance = principal.can("points.read") or principal.can("*")
    return ok(request, candidate_companies(db, lead, include_balance=include_balance))


@router.post("/leads/{lead_id}")
def dispatch(
    lead_id: str,
    body: DispatchBody,
    request: Request,
    principal=Depends(require_permissions("lead.dispatch")),
    db: Session = Depends(get_db),
):
    assignment = dispatch_lead(db, lead_id=lead_id, company_id=body.company_id, principal=principal, idempotency_key=body.idempotency_key, reason=body.reason)
    write_audit(db, principal=principal, action="LEAD_DISPATCH", resource_type="assignment", resource_id=assignment.id, company_id=assignment.company_id, after=assignment_to_dict(assignment), request_id=request.state.request_id)
    db.commit()
    return ok(request, assignment_to_dict(assignment), "派发成功")


@router.get("/assignments")
def assignments(
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
    status: str | None = Query(default=None),
    company_id: str | None = Query(default=None),
    page_no: int = Query(default=1, alias="page", ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
):
    stmt = select(Assignment)
    count_stmt = select(func.count(Assignment.id))
    if principal.has_any_role("FRANCHISE_OWNER"):
        live_leads = select(Lead.id).where(Lead.deleted_at.is_(None))
        stmt = stmt.where(
            Assignment.company_id == principal.company_id,
            Assignment.lead_id.in_(live_leads),
        )
        count_stmt = count_stmt.where(
            Assignment.company_id == principal.company_id,
            Assignment.lead_id.in_(live_leads),
        )
    elif not (principal.can("assignment.read") or principal.can("*")):
        raise AppError("FORBIDDEN", "无权查看派发订单", 403)
    elif company_id:
        stmt = stmt.where(Assignment.company_id == company_id)
        count_stmt = count_stmt.where(Assignment.company_id == company_id)
    if status:
        stmt = stmt.where(Assignment.status == status)
        count_stmt = count_stmt.where(Assignment.status == status)
    total = db.scalar(count_stmt) or 0
    items = db.scalars(stmt.order_by(Assignment.assigned_at.desc()).offset((page_no - 1) * page_size).limit(page_size)).all()
    points_settings = get_lead_points_settings(db)
    return ok(
        request,
        page(
            [
                assignment_to_dict(item, points_settings=points_settings)
                for item in items
            ],
            total,
            page_no,
            page_size,
        ),
    )


@router.get("/assignments/{assignment_id}")
def assignment_detail(assignment_id: str, request: Request, principal: CurrentPrincipal, db: Session = Depends(get_db)):
    assignment = db.get(Assignment, assignment_id)
    if not assignment:
        raise AppError("ASSIGNMENT_NOT_FOUND", "派发订单不存在", 404)
    if principal.has_any_role("FRANCHISE_OWNER", "FRANCHISE_EMPLOYEE"):
        require_company_assignment_access(principal, assignment)
        lead = db.get(Lead, assignment.lead_id)
        if lead is None or lead.deleted_at is not None:
            raise AppError("ASSIGNMENT_NOT_FOUND", "派发订单不存在", 404)
    events = db.scalars(select(AssignmentEvent).where(AssignmentEvent.assignment_id == assignment.id).order_by(AssignmentEvent.occurred_at)).all()
    data = assignment_to_dict(
        assignment,
        points_settings=get_lead_points_settings(db),
    )
    data["timeline"] = [{"type": x.event_type, "payload": x.payload, "occurred_at": x.occurred_at.isoformat()} for x in events]
    return ok(request, data)


@router.post("/assignments/{assignment_id}/release")
def release(
    assignment_id: str,
    body: ReleaseAssignmentBody,
    request: Request,
    principal=Depends(require_permissions("assignment.release")),
    db: Session = Depends(get_db),
):
    assignment = db.get(Assignment, assignment_id)
    if not assignment:
        raise AppError("ASSIGNMENT_NOT_FOUND", "派发订单不存在", 404)
    release_assignment(db, assignment, principal=principal, reason=body.reason)
    write_audit(db, principal=principal, action="ASSIGNMENT_RELEASE", resource_type="assignment", resource_id=assignment.id, company_id=assignment.company_id, metadata={"reason": body.reason}, request_id=request.state.request_id)
    db.commit()
    return ok(request)
