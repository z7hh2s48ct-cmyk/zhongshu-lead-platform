"""供客积分日常提现（2026-09-17 S1/S3 确认口径）。

流程固定：申请 -> 审核冻结 -> 超级管理员线下付款 -> 确认付款并扣分。
- 仅负责人可申请；员工无提现权。
- 支持在可提现额度内部分申请；兑换比例取后台已发布配置（不默认 1 积分 = 1 元），
  审核锁定积分、比例与应付金额形成快照；比例未配置时不能审核放款。
- 可提现额排除未决退回、冻结、在途申请及仍可能冲回的供客积分；
  与既有终止合作结算互斥占用同一批积分。
- 收款资料随申请保存独立快照；已审核申请更换收款资料必须提交变更申请给超级管理员。
- 最低提现额度与手续费客户暂未确定（待定），本期不设置、不默认零费用。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.errors import AppError
from ..core.models import (
    Company,
    PointsAccount,
    SupplyPointsWithdrawal,
    SystemConfig,
    User,
)
from ..core.security import decrypt_text, encrypt_text, mask_phone
from .points_service import change_points, get_or_create_account, lock_points_account
from .supply_termination import _published_rate

logger = logging.getLogger("zhongshu.supply_withdrawal")

ACTIVE_WITHDRAWAL_STATUSES = {
    "PENDING_REVIEW",
    "APPROVED_PENDING_PAYMENT",
    "PAID_PENDING_WRITE_OFF",
}
REVIEWABLE_STATUSES = {"PENDING_REVIEW"}
PAYEE_METHODS = {"BANK", "WECHAT_QR", "ALIPAY_QR"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require_owner(db: Session, company_id: str) -> Company:
    company = db.get(Company, company_id)
    if company is None or company.status != "ACTIVE":
        raise AppError("COMPANY_INACTIVE", "加盟商主体未启用", 403)
    return company


def at_risk_supply_points(db: Session, *, company_id: str, now: datetime) -> int:
    """已入账但退回申诉窗口未关闭、仍可能被冲回的供客积分。"""

    from ..core.models_v12 import SupplierLeadReward

    rows = db.execute(
        select(SupplierLeadReward.reward_points).where(
            SupplierLeadReward.supplier_company_id == company_id,
            SupplierLeadReward.ledger_id.is_not(None),
            SupplierLeadReward.reversal_ledger_id.is_(None),
            SupplierLeadReward.appeal_deadline_at.is_not(None),
            SupplierLeadReward.appeal_deadline_at > now,
        )
    ).all()
    return sum(int(row[0] or 0) for row in rows)


def in_flight_withdrawal_points(db: Session, *, company_id: str) -> int:
    total = db.scalar(
        select(func.sum(SupplyPointsWithdrawal.points_requested)).where(
            SupplyPointsWithdrawal.company_id == company_id,
            SupplyPointsWithdrawal.status.in_(ACTIVE_WITHDRAWAL_STATUSES),
        )
    )
    return int(total or 0)


def available_supply_points(db: Session, *, company_id: str) -> dict[str, Any]:
    """可提现额 = 供客积分余额 - 冻结 - 在途申请 - 仍可能冲回部分。"""

    account = db.scalar(
        select(PointsAccount).where(PointsAccount.company_id == company_id)
    )
    balance = int(account.supply_balance or 0) if account else 0
    frozen = int(account.frozen_supply_points or 0) if account else 0
    in_flight = in_flight_withdrawal_points(db, company_id=company_id)
    at_risk = at_risk_supply_points(db, company_id=company_id, now=_now())
    available = max(0, balance - frozen - in_flight - at_risk)
    return {
        "supply_balance": balance,
        "frozen": frozen,
        "in_flight": in_flight,
        "at_risk": at_risk,
        "available": available,
    }


def create_withdrawal(
    db: Session,
    *,
    company_id: str,
    requested_by: str,
    points_requested: int,
    payee_name: str,
    payee_account: str,
    payment_method: str,
    payee_qrcode_url: str | None = None,
) -> SupplyPointsWithdrawal:
    _require_owner(db, company_id)
    if payment_method not in PAYEE_METHODS:
        raise AppError("WITHDRAWAL_PAYEE_METHOD_INVALID", "收款方式无效", 422)
    if not payee_name.strip() or not payee_account.strip():
        raise AppError("WITHDRAWAL_PAYEE_REQUIRED", "收款人与收款账号必填", 422)
    if payment_method in {"WECHAT_QR", "ALIPAY_QR"} and not (payee_qrcode_url or "").strip():
        raise AppError("WITHDRAWAL_QRCODE_REQUIRED", "二维码收款必须上传收款二维码", 422)
    if not isinstance(points_requested, int) or points_requested <= 0:
        raise AppError("WITHDRAWAL_AMOUNT_INVALID", "提现积分必须为正整数", 422)
    availability = available_supply_points(db, company_id=company_id)
    if points_requested > availability["available"]:
        raise AppError(
            "WITHDRAWAL_AMOUNT_EXCEEDS_AVAILABLE",
            "超出可提现额度",
            422,
            availability,
        )
    item = SupplyPointsWithdrawal(
        company_id=company_id,
        status="PENDING_REVIEW",
        points_requested=points_requested,
        requested_by=requested_by,
        payee_name_encrypted=encrypt_text(payee_name.strip()),
        payee_account_encrypted=encrypt_text(payee_account.strip()),
        payment_method=payment_method,
        payee_qrcode_url=(payee_qrcode_url or "").strip() or None,
    )
    db.add(item)
    db.flush()
    return item


def _lock_withdrawal(db: Session, withdrawal_id: str) -> SupplyPointsWithdrawal:
    item = db.scalar(
        select(SupplyPointsWithdrawal)
        .where(SupplyPointsWithdrawal.id == withdrawal_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if item is None:
        raise AppError("WITHDRAWAL_NOT_FOUND", "提现申请不存在", 404)
    return item


def review_withdrawal(
    db: Session,
    *,
    withdrawal_id: str,
    reviewed_by: str,
    decision: str,
    review_note: str,
) -> SupplyPointsWithdrawal:
    item = _lock_withdrawal(db, withdrawal_id)
    normalized = decision.strip().upper()
    if item.status == "APPROVED_PENDING_PAYMENT" and normalized == "APPROVE":
        return item
    if item.status != "PENDING_REVIEW":
        raise AppError("WITHDRAWAL_NOT_REVIEWABLE", "当前申请不可审核", 409)
    if normalized == "REJECT":
        item.status = "REJECTED"
        item.review_note = review_note.strip()
        item.reviewed_by = reviewed_by
        item.approved_at = _now()
        db.flush()
        return item
    account = lock_points_account(db, item.company_id)
    availability = available_supply_points(db, company_id=item.company_id)
    if item.points_requested > availability["available"]:
        raise AppError(
            "WITHDRAWAL_AMOUNT_EXCEEDS_AVAILABLE",
            "可提现额度已变化，不能审核通过",
            409,
            availability,
        )
    config, rate = _published_rate(db, as_of=_now())
    item.cash_cents_per_point_snapshot = rate
    item.cash_amount_cents_snapshot = item.points_requested * rate
    item.rate_config_id = config.id
    item.review_note = review_note.strip()
    item.reviewed_by = reviewed_by
    item.approved_at = _now()
    item.status = "APPROVED_PENDING_PAYMENT"
    # 审核冻结：不提前扣账，只锁定额度，防止重复提现。
    account.frozen_supply_points = int(account.frozen_supply_points or 0) + item.points_requested
    db.flush()
    return item


def cancel_withdrawal(
    db: Session,
    *,
    withdrawal_id: str,
    cancelled_by: str,
    note: str,
) -> SupplyPointsWithdrawal:
    item = _lock_withdrawal(db, withdrawal_id)
    if item.status not in {"PENDING_REVIEW", "APPROVED_PENDING_PAYMENT"}:
        raise AppError("WITHDRAWAL_NOT_CANCELLABLE", "申请已进入付款或完成状态，不能取消", 409)
    if item.status == "APPROVED_PENDING_PAYMENT":
        account = lock_points_account(db, item.company_id)
        account.frozen_supply_points = max(
            0, int(account.frozen_supply_points or 0) - item.points_requested
        )
    item.status = "CANCELLED"
    item.cancel_note = note.strip()
    item.cancelled_at = _now()
    db.flush()
    return item


def record_offline_payment(
    db: Session,
    *,
    withdrawal_id: str,
    recorded_by: str,
    external_reference: str,
    amount_cents: int,
    note: str | None = None,
    proof_url: str | None = None,
) -> SupplyPointsWithdrawal:
    item = _lock_withdrawal(db, withdrawal_id)
    if item.status != "APPROVED_PENDING_PAYMENT":
        raise AppError("WITHDRAWAL_NOT_PAYABLE", "申请未处于待线下付款状态", 409)
    if item.change_status == "PENDING":
        raise AppError(
            "WITHDRAWAL_PAYMENT_CHANGE_PENDING",
            "收款资料变更待审核，先处理变更再付款",
            409,
        )
    if int(amount_cents) != int(item.cash_amount_cents_snapshot or 0):
        raise AppError(
            "WITHDRAWAL_PAYMENT_AMOUNT_MISMATCH",
            "付款金额与审批快照不一致",
            409,
        )
    item.payment_external_reference = external_reference.strip()
    item.payment_amount_cents = int(amount_cents)
    item.payment_note = (note or "").strip() or None
    item.payment_proof_url = (proof_url or "").strip() or None
    item.payment_recorded_by = recorded_by
    item.paid_at = _now()
    item.status = "PAID_PENDING_WRITE_OFF"
    history = list(item.payment_history_json or [])
    history.append(
        {
            "at": item.paid_at.isoformat(),
            "external_reference": item.payment_external_reference,
            "amount_cents": int(amount_cents),
            "recorded_by": recorded_by,
        }
    )
    item.payment_history_json = history
    db.flush()
    return item


def fail_offline_payment(
    db: Session,
    *,
    withdrawal_id: str,
    decided_by: str,
    note: str,
) -> SupplyPointsWithdrawal:
    item = _lock_withdrawal(db, withdrawal_id)
    if item.status != "PAID_PENDING_WRITE_OFF":
        raise AppError("WITHDRAWAL_FAIL_NOT_ALLOWED", "当前状态不能登记付款失败", 409)
    # 付款失败不核销积分、不显示为已完成；释放冻结回到待处理，可重新发起付款或取消。
    item.status = "APPROVED_PENDING_PAYMENT"
    item.payment_external_reference = None
    item.payment_amount_cents = None
    item.payment_note = f"付款失败：{note.strip()}"
    item.paid_at = None
    item.payment_recorded_by = decided_by
    db.flush()
    return item


def confirm_offline_payment(
    db: Session,
    *,
    withdrawal_id: str,
    confirmed_by: str,
    idempotency_key: str,
) -> SupplyPointsWithdrawal:
    item = _lock_withdrawal(db, withdrawal_id)
    if item.status == "PAID":
        return item
    if item.status != "PAID_PENDING_WRITE_OFF" or not item.payment_external_reference or item.paid_at is None:
        raise AppError("WITHDRAWAL_PAYMENT_NOT_RECORDED", "请先登记并核实线下付款", 409)
    if int(item.payment_amount_cents or 0) != int(item.cash_amount_cents_snapshot or 0):
        raise AppError("WITHDRAWAL_PAYMENT_AMOUNT_MISMATCH", "付款金额与审批快照不一致", 409)
    account = db.scalar(
        select(PointsAccount).where(PointsAccount.company_id == item.company_id).with_for_update()
    ) or get_or_create_account(db, item.company_id)
    writeoff_key = f"supply-withdrawal:{item.id}"
    if int(account.supply_balance or 0) < item.points_requested:
        raise AppError("WITHDRAWAL_SNAPSHOT_BALANCE_CHANGED", "供客积分余额低于已付款快照，禁止核销", 409)
    change_points(
        db,
        company_id=item.company_id,
        delta=-item.points_requested,
        ledger_type="WITHDRAWAL_WRITEOFF",
        business_type="SUPPLY_WITHDRAWAL",
        business_id=item.id,
        idempotency_key=f"{writeoff_key}:supply",
        external_reference=item.payment_external_reference,
        created_by=confirmed_by,
        metadata={
            "withdrawal_id": item.id,
            "cash_amount_cents": item.cash_amount_cents_snapshot,
            "client_idempotency_key": idempotency_key,
        },
        point_kind="SUPPLY",
        allow_frozen=True,
    )
    account.frozen_supply_points = max(
        0, int(account.frozen_supply_points or 0) - item.points_requested
    )
    item.writeoff_idempotency_key = writeoff_key
    item.status = "PAID"
    db.flush()
    return item


def request_payment_change(
    db: Session,
    *,
    withdrawal_id: str,
    requested_by: str,
    payee_name: str,
    payee_account: str,
    payment_method: str,
    reason: str,
    payee_qrcode_url: str | None = None,
) -> SupplyPointsWithdrawal:
    item = _lock_withdrawal(db, withdrawal_id)
    if item.status != "APPROVED_PENDING_PAYMENT":
        raise AppError(
            "WITHDRAWAL_CHANGE_NOT_ALLOWED",
            "仅已审核且未付款的申请可申请更换收款资料",
            409,
        )
    if payment_method not in PAYEE_METHODS:
        raise AppError("WITHDRAWAL_PAYEE_METHOD_INVALID", "收款方式无效", 422)
    item.change_payee_name_encrypted = encrypt_text(payee_name.strip())
    item.change_payee_account_encrypted = encrypt_text(payee_account.strip())
    item.change_payment_method = payment_method
    item.change_reason = reason.strip()
    item.change_status = "PENDING"
    item.change_requested_by = requested_by
    db.flush()
    return item


def review_payment_change(
    db: Session,
    *,
    withdrawal_id: str,
    reviewed_by: str,
    decision: str,
) -> SupplyPointsWithdrawal:
    item = _lock_withdrawal(db, withdrawal_id)
    if item.change_status != "PENDING":
        raise AppError("WITHDRAWAL_CHANGE_NOT_PENDING", "没有待审的收款资料变更", 409)
    if item.status != "APPROVED_PENDING_PAYMENT":
        raise AppError("WITHDRAWAL_CHANGE_NOT_ALLOWED", "该申请已完成付款，不能更换收款资料", 409)
    normalized = decision.strip().upper()
    if normalized == "APPROVE":
        item.payee_name_encrypted = item.change_payee_name_encrypted
        item.payee_account_encrypted = item.change_payee_account_encrypted
        item.payment_method = item.change_payment_method or item.payment_method
    item.change_status = "APPROVED" if normalized == "APPROVE" else "REJECTED"
    item.change_reviewed_by = reviewed_by
    item.change_reviewed_at = _now()
    db.flush()
    return item


def withdrawal_to_dict(
    db: Session,
    item: SupplyPointsWithdrawal,
    *,
    reveal_payee: bool = False,
) -> dict[str, Any]:
    payee_name = decrypt_text(item.payee_name_encrypted)
    payee_account = decrypt_text(item.payee_account_encrypted)
    change_payee_name = (
        decrypt_text(item.change_payee_name_encrypted)
        if item.change_payee_name_encrypted
        else None
    )
    change_payee_account = (
        decrypt_text(item.change_payee_account_encrypted)
        if item.change_payee_account_encrypted
        else None
    )
    requester = db.get(User, item.requested_by)
    return {
        "id": item.id,
        "company_id": item.company_id,
        "status": item.status,
        "points_requested": int(item.points_requested),
        "requested_by": item.requested_by,
        "requested_by_name": requester.display_name if requester else None,
        "payee_name": payee_name,
        "payee_account": payee_account if reveal_payee else None,
        "payee_account_masked": mask_phone(payee_account) if reveal_payee else None,
        "payment_method": item.payment_method,
        "payee_qrcode_url": item.payee_qrcode_url if reveal_payee else None,
        "cash_cents_per_point_snapshot": item.cash_cents_per_point_snapshot,
        "cash_amount_cents_snapshot": item.cash_amount_cents_snapshot,
        "review_note": item.review_note,
        "approved_at": item.approved_at.isoformat() if item.approved_at else None,
        "payment_external_reference": item.payment_external_reference,
        "payment_amount_cents": item.payment_amount_cents,
        "payment_proof_url": item.payment_proof_url if reveal_payee else None,
        "paid_at": item.paid_at.isoformat() if item.paid_at else None,
        "change_status": item.change_status,
        "change_reason": item.change_reason,
        "change_payee_name": change_payee_name if reveal_payee else None,
        "change_payee_account": change_payee_account if reveal_payee else None,
        "change_payment_method": item.change_payment_method,
        "cancel_note": item.cancel_note,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }
