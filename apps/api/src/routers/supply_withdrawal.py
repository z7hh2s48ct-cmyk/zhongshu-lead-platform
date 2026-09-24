from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.auth import CurrentPrincipal, require_permissions
from ..core.database import get_db
from ..core.errors import AppError
from ..core.models import SupplyPointsWithdrawal
from ..core.responses import ok, page
from ..services.audit import write_audit
from ..services.supply_withdrawal import (
    get_withdrawal_policy,
    publish_withdrawal_policy,
)
from ..services.supply_withdrawal import (
    available_supply_points,
    cancel_withdrawal,
    confirm_offline_payment,
    correct_payment_registration,
    create_withdrawal,
    fail_offline_payment,
    record_offline_payment,
    request_payment_change,
    review_payment_change,
    review_withdrawal,
    withdrawal_to_dict,
)

router = APIRouter(prefix="/v1.2", tags=["v1.2-supply-withdrawal"])


class SupplyWithdrawalCreateBody(BaseModel):
    points_requested: int = Field(ge=1)
    payee_name: str = Field(min_length=1, max_length=128)
    payee_account: str = Field(min_length=1, max_length=256)
    payment_method: str = Field(min_length=2, max_length=32)
    payee_qrcode_url: str | None = Field(default=None, max_length=1024)


class SupplyWithdrawalReviewBody(BaseModel):
    decision: str = Field(min_length=2, max_length=16)
    review_note: str = Field(min_length=2, max_length=1000)


class SupplyWithdrawalPaymentBody(BaseModel):
    # 平台内部付款登记编号：留空则后端系统生成；手动填写需全局唯一。
    registration_no: str | None = Field(default=None, max_length=64)
    # 编号来源：SYSTEM / MANUAL；系统生成值即使被编辑仍保留 SYSTEM。
    registration_no_source: str | None = Field(default=None, max_length=16)
    # 外部交易流水号：付款渠道实际提供时填写，可空；不得伪造。
    external_reference: str | None = Field(default=None, max_length=128)
    amount_cents: int = Field(ge=0)
    note: str | None = Field(default=None, max_length=1000)
    proof_url: str | None = Field(default=None, max_length=1024)


class SupplyWithdrawalPaymentCorrectionBody(BaseModel):
    reason: str = Field(min_length=2, max_length=1000)
    registration_no: str | None = Field(default=None, max_length=64)
    registration_no_source: str | None = Field(default=None, max_length=16)
    external_reference: str | None = Field(default=None, max_length=128)
    note: str | None = Field(default=None, max_length=1000)
    proof_url: str | None = Field(default=None, max_length=1024)


class SupplyWithdrawalNoteBody(BaseModel):
    note: str = Field(min_length=2, max_length=1000)


class SupplyWithdrawalPaymentChangeBody(BaseModel):
    payee_name: str = Field(min_length=1, max_length=128)
    payee_account: str = Field(min_length=1, max_length=256)
    payment_method: str = Field(min_length=2, max_length=32)
    payee_qrcode_url: str | None = Field(default=None, max_length=1024)
    reason: str = Field(min_length=2, max_length=1000)


class SupplyWithdrawalChangeReviewBody(BaseModel):
    decision: str = Field(min_length=2, max_length=16)


def _require_owner(principal: CurrentPrincipal) -> str:
    if not principal.company_id or not principal.has_any_role("FRANCHISE_OWNER"):
        raise AppError("FORBIDDEN", "仅加盟商负责人可操作供客积分提现", 403)
    if not (principal.can("points.own.read") or principal.can("*")):
        raise AppError("FORBIDDEN", "无权操作供客积分提现", 403)
    return principal.company_id


def _can_manage_withdrawals(principal: CurrentPrincipal) -> bool:
    # 2026-09-23 反馈 D3：审核/付款收紧为仅超级管理员（OPERATION 只读）。
    return principal.can("*")


class SupplyWithdrawalPolicyBody(BaseModel):
    min_withdrawal_points: int = Field(ge=1, le=10_000_000)
    fee_rate_bp: int = Field(ge=0, le=10000)


@router.get("/supply-withdrawals/policy")
def read_withdrawal_policy(
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
):
    return ok(request, get_withdrawal_policy(db))


@router.put("/supply-withdrawals/policy")
def update_withdrawal_policy(
    body: SupplyWithdrawalPolicyBody,
    request: Request,
    principal=Depends(require_permissions("*")),
    db: Session = Depends(get_db),
):
    value = publish_withdrawal_policy(
        db,
        min_withdrawal_points=body.min_withdrawal_points,
        fee_rate_bp=body.fee_rate_bp,
        principal=principal,
    )
    db.commit()
    return ok(request, value, "提现策略已发布")


@router.get("/supply-withdrawals/available")
def get_available_supply_points(
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
):
    company_id = _require_owner(principal)
    return ok(request, available_supply_points(db, company_id=company_id))


@router.post("/supply-withdrawals")
def create_supply_withdrawal(
    body: SupplyWithdrawalCreateBody,
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
):
    company_id = _require_owner(principal)
    item = create_withdrawal(
        db,
        company_id=company_id,
        requested_by=principal.user_id,
        points_requested=body.points_requested,
        payee_name=body.payee_name,
        payee_account=body.payee_account,
        payment_method=body.payment_method,
        payee_qrcode_url=body.payee_qrcode_url,
    )
    write_audit(
        db,
        principal=principal,
        action="V12_SUPPLY_WITHDRAWAL_REQUEST",
        resource_type="supply_withdrawal",
        resource_id=item.id,
        company_id=company_id,
        after={"points_requested": item.points_requested, "status": item.status},
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, withdrawal_to_dict(db, item), "提现申请已提交，等待超级管理员审核")


@router.get("/supply-withdrawals")
def list_supply_withdrawals(
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
    status: str | None = Query(default=None),
    page_no: int = Query(default=1, alias="page", ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
):
    reveal = _can_manage_withdrawals(principal)
    filters = []
    if reveal:
        pass
    elif principal.has_any_role("FRANCHISE_OWNER") and principal.company_id:
        filters.append(SupplyPointsWithdrawal.company_id == principal.company_id)
    else:
        raise AppError("FORBIDDEN", "无权查看提现申请", 403)
    if status:
        filters.append(SupplyPointsWithdrawal.status == status.strip().upper())
    total = db.scalar(select(func.count(SupplyPointsWithdrawal.id)).where(*filters)) or 0
    items = db.scalars(
        select(SupplyPointsWithdrawal)
        .where(*filters)
        .order_by(SupplyPointsWithdrawal.created_at.desc(), SupplyPointsWithdrawal.id.desc())
        .offset((page_no - 1) * page_size)
        .limit(page_size)
    ).all()
    data = page(
        [withdrawal_to_dict(db, item, reveal_payee=reveal) for item in items],
        int(total),
        page_no,
        page_size,
    )
    return ok(request, data)


def _load_own_or_manage(db: Session, principal: CurrentPrincipal, withdrawal_id: str):
    item = db.get(SupplyPointsWithdrawal, withdrawal_id)
    if item is None:
        raise AppError("WITHDRAWAL_NOT_FOUND", "提现申请不存在", 404)
    if _can_manage_withdrawals(principal):
        return item, True
    if (
        principal.has_any_role("FRANCHISE_OWNER")
        and principal.company_id == item.company_id
        and (principal.can("points.own.read") or principal.can("*"))
    ):
        return item, False
    raise AppError("FORBIDDEN", "无权查看该提现申请", 403)


@router.get("/supply-withdrawals/{withdrawal_id}")
def supply_withdrawal_detail(
    withdrawal_id: str,
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
):
    item, reveal = _load_own_or_manage(db, principal, withdrawal_id)
    return ok(request, withdrawal_to_dict(db, item, reveal_payee=reveal))


@router.post("/supply-withdrawals/{withdrawal_id}/cancel")
def cancel_supply_withdrawal(
    withdrawal_id: str,
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
):
    company_id = _require_owner(principal)
    item = db.get(SupplyPointsWithdrawal, withdrawal_id)
    if item is None or item.company_id != company_id:
        raise AppError("WITHDRAWAL_NOT_FOUND", "提现申请不存在", 404)
    item = cancel_withdrawal(
        db, withdrawal_id=withdrawal_id, cancelled_by=principal.user_id, note="负责人取消"
    )
    write_audit(
        db,
        principal=principal,
        action="V12_SUPPLY_WITHDRAWAL_CANCEL",
        resource_type="supply_withdrawal",
        resource_id=item.id,
        company_id=item.company_id,
        after={"status": item.status},
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, withdrawal_to_dict(db, item, reveal_payee=True), "提现申请已取消")


@router.post("/supply-withdrawals/{withdrawal_id}/payee-change")
def request_supply_withdrawal_payment_change(
    withdrawal_id: str,
    body: SupplyWithdrawalPaymentChangeBody,
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
):
    company_id = _require_owner(principal)
    item = db.get(SupplyPointsWithdrawal, withdrawal_id)
    if item is None or item.company_id != company_id:
        raise AppError("WITHDRAWAL_NOT_FOUND", "提现申请不存在", 404)
    item = request_payment_change(
        db,
        withdrawal_id=withdrawal_id,
        requested_by=principal.user_id,
        payee_name=body.payee_name,
        payee_account=body.payee_account,
        payment_method=body.payment_method,
        reason=body.reason,
        payee_qrcode_url=body.payee_qrcode_url,
    )
    write_audit(
        db,
        principal=principal,
        action="V12_SUPPLY_WITHDRAWAL_PAYMENT_CHANGE",
        resource_type="supply_withdrawal",
        resource_id=item.id,
        company_id=item.company_id,
        after={"change_status": item.change_status},
        reason=body.reason,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, withdrawal_to_dict(db, item), "收款资料变更申请已提交")


@router.post("/admin/supply-withdrawals/{withdrawal_id}/review")
def review_supply_withdrawal(
    withdrawal_id: str,
    body: SupplyWithdrawalReviewBody,
    request: Request,
    principal=Depends(require_permissions("*")),
    db: Session = Depends(get_db),
):
    item = review_withdrawal(
        db,
        withdrawal_id=withdrawal_id,
        reviewed_by=principal.user_id,
        decision=body.decision,
        review_note=body.review_note,
    )
    write_audit(
        db,
        principal=principal,
        action="V12_SUPPLY_WITHDRAWAL_REVIEW",
        resource_type="supply_withdrawal",
        resource_id=item.id,
        company_id=item.company_id,
        after={"status": item.status, "frozen_points": item.points_requested},
        reason=body.review_note,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, withdrawal_to_dict(db, item, reveal_payee=True), "审核完成")


@router.post("/admin/supply-withdrawals/{withdrawal_id}/record-transfer")
def record_supply_withdrawal_payment(
    withdrawal_id: str,
    body: SupplyWithdrawalPaymentBody,
    request: Request,
    principal=Depends(require_permissions("*")),
    db: Session = Depends(get_db),
):
    item = record_offline_payment(
        db,
        withdrawal_id=withdrawal_id,
        recorded_by=principal.user_id,
        registration_no=body.registration_no,
        registration_no_source=body.registration_no_source,
        external_reference=body.external_reference,
        amount_cents=body.amount_cents,
        note=body.note,
        proof_url=body.proof_url,
    )
    write_audit(
        db,
        principal=principal,
        action="V12_SUPPLY_WITHDRAWAL_PAYMENT_RECORDED",
        resource_type="supply_withdrawal",
        resource_id=item.id,
        company_id=item.company_id,
        after={
            "status": item.status,
            "amount_cents": item.payment_amount_cents,
            "registration_no": item.payment_registration_no,
            "registration_no_source": item.payment_registration_no_source,
            "external_reference": item.payment_external_reference,
        },
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, withdrawal_to_dict(db, item, reveal_payee=True), "线下付款已登记")


@router.patch("/admin/supply-withdrawals/{withdrawal_id}/registration-correction")
def correct_supply_withdrawal_payment_registration(
    withdrawal_id: str,
    body: SupplyWithdrawalPaymentCorrectionBody,
    request: Request,
    principal=Depends(require_permissions("*")),
    db: Session = Depends(get_db),
):
    item = correct_payment_registration(
        db,
        withdrawal_id=withdrawal_id,
        corrected_by=principal.user_id,
        reason=body.reason,
        registration_no=body.registration_no,
        registration_no_source=body.registration_no_source,
        external_reference=body.external_reference,
        note=body.note,
        proof_url=body.proof_url,
        clear_external_reference=(
            "external_reference" in body.model_fields_set and body.external_reference is None
        ),
        clear_proof_url=("proof_url" in body.model_fields_set and body.proof_url is None),
    )
    write_audit(
        db,
        principal=principal,
        action="V12_SUPPLY_WITHDRAWAL_PAYMENT_REGISTRATION_CORRECTED",
        resource_type="supply_withdrawal",
        resource_id=item.id,
        company_id=item.company_id,
        after={
            "status": item.status,
            "registration_no": item.payment_registration_no,
            "registration_no_source": item.payment_registration_no_source,
            "external_reference": item.payment_external_reference,
        },
        reason=body.reason,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(
        request,
        withdrawal_to_dict(db, item, reveal_payee=True),
        "付款登记信息已更正",
    )


@router.post("/admin/supply-withdrawals/{withdrawal_id}/fail")
def fail_supply_withdrawal_payment(
    withdrawal_id: str,
    body: SupplyWithdrawalNoteBody,
    request: Request,
    principal=Depends(require_permissions("*")),
    db: Session = Depends(get_db),
):
    item = fail_offline_payment(
        db, withdrawal_id=withdrawal_id, decided_by=principal.user_id, note=body.note
    )
    write_audit(
        db,
        principal=principal,
        action="V12_SUPPLY_WITHDRAWAL_PAYMENT_FAILED",
        resource_type="supply_withdrawal",
        resource_id=item.id,
        company_id=item.company_id,
        after={"status": item.status},
        reason=body.note,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, withdrawal_to_dict(db, item, reveal_payee=True), "付款失败已登记")


@router.post("/admin/supply-withdrawals/{withdrawal_id}/confirm")
def confirm_supply_withdrawal_payment(
    withdrawal_id: str,
    request: Request,
    principal=Depends(require_permissions("*")),
    db: Session = Depends(get_db),
):
    item = confirm_offline_payment(
        db,
        withdrawal_id=withdrawal_id,
        confirmed_by=principal.user_id,
        idempotency_key=f"confirm-{withdrawal_id}",
    )
    write_audit(
        db,
        principal=principal,
        action="V12_SUPPLY_WITHDRAWAL_CONFIRMED",
        resource_type="supply_withdrawal",
        resource_id=item.id,
        company_id=item.company_id,
        after={"status": item.status, "points": item.points_requested},
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(
        request,
        withdrawal_to_dict(db, item, reveal_payee=True),
        "付款已确认，对应供客积分已核销",
    )


@router.post("/admin/supply-withdrawals/{withdrawal_id}/payee-change/review")
def review_supply_withdrawal_payment_change(
    withdrawal_id: str,
    body: SupplyWithdrawalChangeReviewBody,
    request: Request,
    principal=Depends(require_permissions("*")),
    db: Session = Depends(get_db),
):
    item = review_payment_change(
        db, withdrawal_id=withdrawal_id, reviewed_by=principal.user_id, decision=body.decision
    )
    write_audit(
        db,
        principal=principal,
        action="V12_SUPPLY_WITHDRAWAL_PAYMENT_CHANGE_REVIEWED",
        resource_type="supply_withdrawal",
        resource_id=item.id,
        company_id=item.company_id,
        after={"change_status": item.change_status},
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, withdrawal_to_dict(db, item, reveal_payee=True), "收款资料变更已处理")
