from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.auth import CurrentPrincipal
from ..core.database import get_db
from ..core.errors import AppError
from ..core.models import Company, PointsAccount, SupplyTerminationRequest
from ..core.responses import ok, page
from ..schemas.supply_termination import (
    SupplyCooperationReopenBody,
    SupplyTerminationConfirmBody,
    SupplyTerminationCreateBody,
    SupplyTerminationPaymentBody,
    SupplyTerminationReviewBody,
    SupplyTerminationNoteBody,
)
from ..services.audit import write_audit
from ..services.points_service import get_or_create_account
from ..services.supply_termination import (
    confirm_offline_payment,
    cancel_termination,
    create_termination_request,
    decide_termination,
    fail_offline_payment,
    record_offline_payment,
    refresh_termination,
    reopen_supply_cooperation,
    termination_to_dict,
)

router = APIRouter(prefix="/v1.2", tags=["supply-termination"])


def _require_owner(principal: CurrentPrincipal) -> str:
    if not principal.company_id or not principal.can("supply.termination.own.manage"):
        raise AppError("FORBIDDEN", "仅加盟商负责人可以申请终止客资合作", 403)
    return principal.company_id


def _require_franchise_member(principal: CurrentPrincipal) -> str:
    if not principal.company_id or not (
        principal.can("supply.termination.own.manage")
        or principal.can("supply.termination.own.read")
    ):
        raise AppError("FORBIDDEN", "仅加盟商成员可以查看客资合作状态", 403)
    return principal.company_id


def _require_permission(principal: CurrentPrincipal, code: str, message: str) -> None:
    if not principal.can(code):
        raise AppError("FORBIDDEN", message, 403)


def _include_sensitive(principal: CurrentPrincipal) -> bool:
    return principal.can("supply.termination.own.manage") or principal.can("supply.termination.pay")


def _masked_payment_reference(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 4:
        return "*" * len(value)
    return ("*" * (len(value) - 4)) + value[-4:]


@router.get("/supply-termination/current")
def current_termination(request: Request, principal: CurrentPrincipal, db: Session = Depends(get_db)):
    company_id = _require_franchise_member(principal)
    company = db.get(Company, company_id)
    account = get_or_create_account(db, company_id)
    can_manage = principal.can("supply.termination.own.manage")
    items = db.scalars(select(SupplyTerminationRequest).where(
        SupplyTerminationRequest.company_id == company_id
    ).order_by(SupplyTerminationRequest.created_at.desc()).limit(20)).all()
    item = items[0] if items else None
    data = {
        "cooperation_status": company.supplier_cooperation_status if company else None,
        "customer_points_balance": int(account.balance) if can_manage else None,
        "supply_points_balance": int(account.supply_balance) if can_manage else None,
        "request": termination_to_dict(db, item, include_sensitive=can_manage, company=company) if item else None,
        "history": [
            termination_to_dict(db, history_item, include_sensitive=can_manage, company=company)
            for history_item in items
        ],
    }
    return ok(request, data)


@router.post("/supply-termination/requests")
def create_request(body: SupplyTerminationCreateBody, request: Request, principal: CurrentPrincipal, db: Session = Depends(get_db)):
    company_id = _require_owner(principal)
    item = create_termination_request(
        db, company_id=company_id, requested_by=principal.user_id,
        reason=body.reason, payee_name=body.payee_name,
        payee_account=body.payee_account, payment_method=body.payment_method,
    )
    write_audit(db, principal=principal, action="V12_SUPPLY_TERMINATION_REQUEST", resource_type="supply_termination", resource_id=item.id, company_id=company_id, after={"status": item.status, "blockers": item.blockers_json}, request_id=request.state.request_id)
    db.commit()
    return ok(request, termination_to_dict(db, item), "终止客资合作申请已提交")


@router.post("/supply-termination/current/cancel")
def cancel_current(body: SupplyTerminationNoteBody, request: Request, principal: CurrentPrincipal, db: Session = Depends(get_db)):
    company_id = _require_owner(principal)
    item = cancel_termination(db, company_id=company_id, cancelled_by=principal.user_id, note=body.note)
    write_audit(db, principal=principal, action="V12_SUPPLY_TERMINATION_CANCEL", resource_type="supply_termination", resource_id=item.id, company_id=company_id, after={"status": item.status}, reason=body.note, request_id=request.state.request_id)
    db.commit()
    return ok(request, termination_to_dict(db, item), "终止客资合作申请已取消")


@router.get("/supply-terminations")
def list_requests(request: Request, principal: CurrentPrincipal, db: Session = Depends(get_db), status: str | None = None, company_id: str | None = None, page_no: int = Query(1, alias="page", ge=1), page_size: int = Query(20, ge=1, le=200)):
    _require_permission(principal, "supply.termination.read", "无权查看终止客资合作申请")
    stmt = select(SupplyTerminationRequest)
    count_stmt = select(func.count(SupplyTerminationRequest.id))
    if status:
        stmt, count_stmt = stmt.where(SupplyTerminationRequest.status == status), count_stmt.where(SupplyTerminationRequest.status == status)
    if company_id:
        stmt, count_stmt = stmt.where(SupplyTerminationRequest.company_id == company_id), count_stmt.where(SupplyTerminationRequest.company_id == company_id)
    total = int(db.scalar(count_stmt) or 0)
    items = db.scalars(stmt.order_by(SupplyTerminationRequest.created_at.desc()).offset((page_no - 1) * page_size).limit(page_size)).all()
    companies = {
        company.id: company
        for company in db.scalars(select(Company).where(
            Company.id.in_({item.company_id for item in items})
        )).all()
    } if items else {}
    return ok(request, page([
        termination_to_dict(
            db,
            item,
            include_sensitive=_include_sensitive(principal),
            company=companies.get(item.company_id),
        )
        for item in items
    ], total, page_no, page_size))


@router.post("/supply-terminations/{request_id}/refresh")
def refresh(request_id: str, request: Request, principal: CurrentPrincipal, db: Session = Depends(get_db)):
    _require_permission(principal, "supply.termination.review", "无权复检终止客资合作申请")
    item = refresh_termination(db, request_id=request_id)
    write_audit(db, principal=principal, action="V12_SUPPLY_TERMINATION_REFRESH", resource_type="supply_termination", resource_id=item.id, company_id=item.company_id, after={"status": item.status, "blockers": item.blockers_json}, request_id=request.state.request_id)
    db.commit()
    return ok(request, termination_to_dict(db, item, include_sensitive=_include_sensitive(principal)))


@router.post("/supply-terminations/{request_id}/review")
def review(request_id: str, body: SupplyTerminationReviewBody, request: Request, principal: CurrentPrincipal, db: Session = Depends(get_db)):
    _require_permission(principal, "supply.termination.review", "无权审核终止客资合作申请")
    item = decide_termination(db, request_id=request_id, decision=body.decision, decided_by=principal.user_id, note=body.note)
    write_audit(db, principal=principal, action="V12_SUPPLY_TERMINATION_REVIEW", resource_type="supply_termination", resource_id=item.id, company_id=item.company_id, after={"status": item.status, "general_points_snapshot": item.general_points_snapshot, "cash_amount_cents_snapshot": item.cash_amount_cents_snapshot}, reason=body.note, request_id=request.state.request_id)
    db.commit()
    return ok(request, termination_to_dict(db, item, include_sensitive=_include_sensitive(principal)))


@router.post("/supply-terminations/{request_id}/payment")
@router.patch("/supply-terminations/{request_id}/payment")
def payment(request_id: str, body: SupplyTerminationPaymentBody, request: Request, principal: CurrentPrincipal, db: Session = Depends(get_db)):
    _require_permission(principal, "supply.termination.pay", "无权登记终止合作付款")
    item = record_offline_payment(db, request_id=request_id, recorded_by=principal.user_id, external_reference=body.external_reference, paid_at=body.paid_at, payment_amount_cents=body.payment_amount_cents, note=body.note, proof_url=body.proof_url)
    write_audit(db, principal=principal, action="V12_SUPPLY_TERMINATION_PAYMENT_RECORDED", resource_type="supply_termination", resource_id=item.id, company_id=item.company_id, after={"status": item.status, "payment_external_reference_masked": _masked_payment_reference(item.payment_external_reference), "paid_at": item.paid_at.isoformat() if item.paid_at else None}, request_id=request.state.request_id)
    db.commit()
    return ok(request, termination_to_dict(db, item), "线下付款已登记")


@router.post("/supply-terminations/{request_id}/payment/fail")
def payment_failed(request_id: str, body: SupplyTerminationNoteBody, request: Request, principal: CurrentPrincipal, db: Session = Depends(get_db)):
    _require_permission(principal, "supply.termination.pay", "无权回退终止合作付款")
    item = fail_offline_payment(db, request_id=request_id, failed_by=principal.user_id, note=body.note)
    write_audit(db, principal=principal, action="V12_SUPPLY_TERMINATION_PAYMENT_FAILED", resource_type="supply_termination", resource_id=item.id, company_id=item.company_id, after={"status": item.status}, reason=body.note, request_id=request.state.request_id)
    db.commit()
    return ok(request, termination_to_dict(db, item), "付款失败已回退，未核销积分")


@router.post("/supply-terminations/{request_id}/confirm")
def confirm(request_id: str, body: SupplyTerminationConfirmBody, request: Request, principal: CurrentPrincipal, db: Session = Depends(get_db)):
    _require_permission(principal, "supply.termination.pay", "无权确认终止合作付款")
    if not body.confirmed:
        raise AppError("SUPPLY_TERMINATION_CONFIRM_REQUIRED", "请确认线下款项已实际支付", 422)
    item = confirm_offline_payment(db, request_id=request_id, confirmed_by=principal.user_id, idempotency_key=body.idempotency_key)
    write_audit(db, principal=principal, action="V12_SUPPLY_TERMINATION_CONFIRMED", resource_type="supply_termination", resource_id=item.id, company_id=item.company_id, after={"status": item.status, "terminated_at": item.terminated_at.isoformat() if item.terminated_at else None}, request_id=request.state.request_id)
    db.commit()
    return ok(request, termination_to_dict(db, item), "积分已核销，客资合作已终止")


@router.post("/companies/{company_id}/supply-cooperation/reopen")
def reopen(company_id: str, body: SupplyCooperationReopenBody, request: Request, principal: CurrentPrincipal, db: Session = Depends(get_db)):
    _require_permission(principal, "supply.termination.reopen", "无权恢复客资提供合作")
    company = reopen_supply_cooperation(db, company_id=company_id, reopened_by=principal.user_id, note=body.note)
    write_audit(db, principal=principal, action="V12_SUPPLY_COOPERATION_REOPEN", resource_type="company", resource_id=company.id, company_id=company.id, after={"supplier_cooperation_status": company.supplier_cooperation_status}, reason=body.note, request_id=request.state.request_id)
    db.commit()
    return ok(request, {"company_id": company.id, "supplier_cooperation_status": company.supplier_cooperation_status}, "客资提供合作已恢复")
