from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..core.errors import AppError
from ..core.models import (
    Assignment,
    Company,
    Lead,
    PointsAccount,
    PointsLedger,
    ReturnRequest,
    SupplyTerminationRequest,
    SystemConfig,
)
from ..core.models_v12 import SupplierLeadReward
from ..core.security import decrypt_text, encrypt_text
from ..core.time import as_utc
from ..core.v12_enums import LeadV12Status, ReturnV12Status, RewardStatus
from .points_service import change_points, get_or_create_account, lock_points_account

logger = logging.getLogger("zhongshu.supply_termination")
RATE_DOMAIN = "supply_termination"
RATE_KEY = "cashout_rate"
ACTIVE_REQUEST_STATUSES = {
    "REQUESTED", "NEED_MORE", "APPROVED_PENDING_PAYMENT", "PAID_PENDING_WRITE_OFF", "PAYMENT_FAILED"
}
TERMINAL_LEAD_STATUSES = {
    LeadV12Status.COMPLETED.value,
    LeadV12Status.INVALID.value,
    LeadV12Status.DUPLICATE.value,
    LeadV12Status.CLOSED.value,
}
TERMINAL_RETURN_STATUSES = {
    ReturnV12Status.APPROVED.value,
    ReturnV12Status.REJECTED.value,
    ReturnV12Status.EXPIRED.value,
    ReturnV12Status.CANCELLED.value,
}
TERMINAL_REWARD_STATUSES = {
    RewardStatus.NOT_ELIGIBLE.value,
    RewardStatus.SETTLED.value,
    RewardStatus.CANCELLED.value,
    RewardStatus.REVERSED.value,
}


def _now(value: datetime | None = None) -> datetime:
    return as_utc(value) or datetime.now(timezone.utc)


def _lock_company(db: Session, company_id: str) -> Company:
    company = db.scalar(
        select(Company)
        .where(Company.id == company_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if company is None:
        raise AppError("COMPANY_NOT_FOUND", "加盟商公司不存在", 404)
    return company


def _lock_request_with_company(
    db: Session, request_id: str
) -> tuple[SupplyTerminationRequest, Company]:
    company_id = db.scalar(select(SupplyTerminationRequest.company_id).where(
        SupplyTerminationRequest.id == request_id
    ))
    if company_id is None:
        raise AppError("SUPPLY_TERMINATION_NOT_FOUND", "终止合作申请不存在", 404)
    company = _lock_company(db, company_id)
    item = db.scalar(
        select(SupplyTerminationRequest)
        .where(SupplyTerminationRequest.id == request_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if item is None:
        raise AppError("SUPPLY_TERMINATION_NOT_FOUND", "终止合作申请不存在", 404)
    return item, company


def require_supply_write_enabled(db: Session, company_id: str | None) -> Company:
    if not company_id:
        raise AppError("COMPANY_REQUIRED", "当前账号未关联加盟商公司", 409)
    company = db.scalar(
        select(Company)
        .where(Company.id == company_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if company is None or company.status != "ACTIVE":
        raise AppError("COMPANY_INACTIVE", "加盟商公司未启用", 409)
    status = company.supplier_cooperation_status
    if status == "TERMINATED":
        raise AppError("SUPPLY_COOPERATION_TERMINATED", "客资提供合作已终止，不能新增、修改或提交客资", 409)
    if status != "ACTIVE":
        raise AppError("SUPPLY_COOPERATION_PENDING", "客资提供合作正在终止结算，不能新增、修改或提交客资", 409)
    return company


def termination_blockers(db: Session, company_id: str) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    account = get_or_create_account(db, company_id)
    if account.points_split_status != "READY" or int(account.balance) < 0 or int(account.supply_balance) < 0:
        blockers.append({"code": "POINTS_SPLIT_RECONCILIATION", "count": 1, "record_ids": [account.id], "truncated": False, "message": "历史积分分账存在异常，需先完成对账"})

    customer_sum = int(db.scalar(select(func.coalesce(func.sum(PointsLedger.delta), 0)).where(
        PointsLedger.company_id == company_id, PointsLedger.point_kind == "CUSTOMER"
    )) or 0)
    supply_sum = int(db.scalar(select(func.coalesce(func.sum(PointsLedger.delta), 0)).where(
        PointsLedger.company_id == company_id, PointsLedger.point_kind == "SUPPLY"
    )) or 0)
    if customer_sum != int(account.balance) or supply_sum != int(account.supply_balance):
        blockers.append({"code": "POINTS_RECONCILIATION", "count": 1, "record_ids": [account.id], "truncated": False, "message": "积分账户与流水不一致，需先完成对账"})

    lead_ids = list(db.scalars(select(Lead.id).where(
        Lead.supplier_company_id == company_id,
        Lead.deleted_at.is_(None),
        Lead.status.not_in(TERMINAL_LEAD_STATUSES),
    ).order_by(Lead.id).limit(21)).all())
    lead_count = int(db.scalar(select(func.count(Lead.id)).where(Lead.supplier_company_id == company_id, Lead.deleted_at.is_(None), Lead.status.not_in(TERMINAL_LEAD_STATUSES))) or 0)
    if lead_count:
        blockers.append({"code": "SUPPLIED_LEADS_UNFINISHED", "count": lead_count, "record_ids": lead_ids[:20], "truncated": lead_count > 20, "message": "仍有供资客资未完成审核或业务流转"})

    return_ids = list(db.scalars(select(ReturnRequest.id).join(Lead, Lead.id == ReturnRequest.lead_id).where(
        Lead.supplier_company_id == company_id, ReturnRequest.status.not_in(TERMINAL_RETURN_STATUSES)
    ).order_by(ReturnRequest.id).limit(21)).all())
    return_count = int(db.scalar(select(func.count(ReturnRequest.id)).join(
        Lead, Lead.id == ReturnRequest.lead_id
    ).where(
        Lead.supplier_company_id == company_id,
        ReturnRequest.status.not_in(TERMINAL_RETURN_STATUSES),
    )) or 0)
    if return_count:
        blockers.append({"code": "RETURNS_UNFINISHED", "count": return_count, "record_ids": return_ids[:20], "truncated": return_count > 20, "message": "仍有退回申请待处理"})

    reward_ids = list(db.scalars(select(SupplierLeadReward.id).where(
        SupplierLeadReward.supplier_company_id == company_id,
        SupplierLeadReward.status.not_in(TERMINAL_REWARD_STATUSES),
    ).order_by(SupplierLeadReward.id).limit(21)).all())
    reward_count = int(db.scalar(select(func.count(SupplierLeadReward.id)).where(
        SupplierLeadReward.supplier_company_id == company_id,
        SupplierLeadReward.status.not_in(TERMINAL_REWARD_STATUSES),
    )) or 0)
    if reward_count:
        blockers.append({"code": "REWARDS_UNFINISHED", "count": reward_count, "record_ids": reward_ids[:20], "truncated": reward_count > 20, "message": "仍有供客奖励待确认或结算"})
    early_ids = list(db.scalars(select(SupplierLeadReward.id).join(
        Assignment, Assignment.id == SupplierLeadReward.assignment_id
    ).where(
        SupplierLeadReward.supplier_company_id == company_id,
        SupplierLeadReward.status == RewardStatus.SETTLED.value,
        Assignment.claimed_at.is_not(None),
        Assignment.claimed_at > _now() - timedelta(hours=48),
    ).order_by(SupplierLeadReward.id).limit(21)).all())
    early_count = int(db.scalar(select(func.count(SupplierLeadReward.id)).join(
        Assignment, Assignment.id == SupplierLeadReward.assignment_id
    ).where(
        SupplierLeadReward.supplier_company_id == company_id,
        SupplierLeadReward.status == RewardStatus.SETTLED.value,
        Assignment.claimed_at.is_not(None),
        Assignment.claimed_at > _now() - timedelta(hours=48),
    )) or 0)
    if early_count:
        blockers.append({"code": "EARLY_REWARD_RETURN_WINDOW", "count": early_count, "record_ids": early_ids[:20], "truncated": early_count > 20, "message": "提前确认的奖励仍在48小时退回窗口内"})
    return blockers


def _published_rate(db: Session, *, as_of: datetime) -> tuple[SystemConfig, int]:
    item = db.scalar(select(SystemConfig).where(
        SystemConfig.domain == RATE_DOMAIN,
        SystemConfig.key == RATE_KEY,
        SystemConfig.status == "PUBLISHED",
        or_(SystemConfig.effective_at.is_(None), SystemConfig.effective_at <= as_of),
    ).order_by(SystemConfig.version.desc(), SystemConfig.effective_at.desc()).limit(1))
    raw = item.value_json.get("cash_cents_per_point") if item else None
    if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
        raise AppError(
            "SUPPLY_TERMINATION_RATE_NOT_CONFIGURED",
            "终止合作积分兑换比例尚未发布，不能生成结算金额快照",
            409,
        )
    return item, raw


def create_termination_request(
    db: Session,
    *,
    company_id: str,
    requested_by: str,
    reason: str,
    payee_name: str,
    payee_account: str,
    payment_method: str,
) -> SupplyTerminationRequest:
    company = _lock_company(db, company_id)
    existing = db.scalar(select(SupplyTerminationRequest).where(
        SupplyTerminationRequest.company_id == company_id,
        SupplyTerminationRequest.status.in_(ACTIVE_REQUEST_STATUSES),
    ).order_by(SupplyTerminationRequest.created_at.desc()).limit(1).with_for_update().execution_options(populate_existing=True))
    if existing:
        if existing.status == "NEED_MORE":
            existing.reason = reason.strip()
            existing.payee_name_encrypted = encrypt_text(payee_name.strip())
            existing.payee_account_encrypted = encrypt_text(payee_account.strip())
            existing.payment_method = payment_method.strip().upper()
            existing.blockers_json = termination_blockers(db, company_id)
            existing.status = "REQUESTED"
            db.flush()
        return existing
    if company.supplier_cooperation_status == "TERMINATED":
        raise AppError("SUPPLY_COOPERATION_TERMINATED", "客资提供合作已经终止", 409)
    item = SupplyTerminationRequest(
        company_id=company_id,
        status="REQUESTED",
        reason=reason.strip(),
        requested_by=requested_by,
        payee_name_encrypted=encrypt_text(payee_name.strip()),
        payee_account_encrypted=encrypt_text(payee_account.strip()),
        payment_method=payment_method.strip().upper(),
        blockers_json=termination_blockers(db, company_id),
    )
    company.supplier_cooperation_status = "TERMINATION_PENDING"
    db.add(item)
    db.flush()
    return item


def refresh_termination(db: Session, *, request_id: str) -> SupplyTerminationRequest:
    item, _company = _lock_request_with_company(db, request_id)
    if item.status in {"REQUESTED", "NEED_MORE"}:
        item.blockers_json = termination_blockers(db, item.company_id)
        if item.status == "NEED_MORE" and not item.blockers_json:
            item.status = "REQUESTED"
        db.flush()
    return item


def cancel_termination(
    db: Session, *, company_id: str, cancelled_by: str, note: str
) -> SupplyTerminationRequest:
    company = _lock_company(db, company_id)
    item = db.scalar(select(SupplyTerminationRequest).where(
        SupplyTerminationRequest.company_id == company_id,
        SupplyTerminationRequest.status.in_(("REQUESTED", "NEED_MORE", "APPROVED_PENDING_PAYMENT", "PAYMENT_FAILED")),
    ).order_by(SupplyTerminationRequest.created_at.desc()).with_for_update().execution_options(populate_existing=True))
    if item is None:
        raise AppError("SUPPLY_TERMINATION_NOT_CANCELLABLE", "没有可取消的终止合作申请", 409)
    item.status = "CANCELLED"
    item.review_note = note.strip()
    item.reviewed_by = cancelled_by
    account = lock_points_account(db, company_id)
    account.frozen_customer_points = 0
    account.frozen_supply_points = 0
    company.supplier_cooperation_status = "ACTIVE"
    db.flush()
    return item


def approve_termination(
    db: Session, *, request_id: str, approved_by: str, review_note: str
) -> SupplyTerminationRequest:
    item, company = _lock_request_with_company(db, request_id)
    if item.status == "APPROVED_PENDING_PAYMENT":
        return item
    if item.status not in {"REQUESTED", "NEED_MORE"}:
        raise AppError("SUPPLY_TERMINATION_NOT_REVIEWABLE", "当前申请不可审核通过", 409)
    now = _now()
    account = lock_points_account(db, item.company_id)
    blockers = termination_blockers(db, item.company_id)
    item.blockers_json = blockers
    if blockers:
        raise AppError("SUPPLY_TERMINATION_BLOCKED", "仍有未结事项，不能进入结算", 409, {"blockers": blockers})
    config, rate = _published_rate(db, as_of=now)
    customer_points = int(account.balance)
    supply_points = int(account.supply_balance)
    if customer_points < 0 or supply_points < 0:
        raise AppError("SUPPLY_TERMINATION_POINTS_INVALID", "积分余额异常，需先完成对账", 409)
    item.customer_points_snapshot = customer_points
    item.supply_points_snapshot = supply_points
    item.general_points_snapshot = customer_points + supply_points
    item.cash_cents_per_point_snapshot = rate
    item.cash_amount_cents_snapshot = (customer_points + supply_points) * rate
    item.rate_config_id = config.id
    item.review_note = review_note.strip()
    item.reviewed_by = approved_by
    item.approved_at = now
    item.status = "APPROVED_PENDING_PAYMENT"
    account.frozen_customer_points = customer_points
    account.frozen_supply_points = supply_points
    company.supplier_cooperation_status = "TERMINATION_PENDING"
    db.flush()
    return item


def decide_termination(
    db: Session, *, request_id: str, decision: str, decided_by: str, note: str
) -> SupplyTerminationRequest:
    normalized = decision.strip().upper()
    if normalized == "APPROVE":
        return approve_termination(db, request_id=request_id, approved_by=decided_by, review_note=note)
    item, company = _lock_request_with_company(db, request_id)
    if item.status not in {"REQUESTED", "NEED_MORE"}:
        raise AppError("SUPPLY_TERMINATION_NOT_REVIEWABLE", "当前申请不可审核", 409)
    if normalized not in {"REJECT", "NEED_MORE"}:
        raise AppError("SUPPLY_TERMINATION_DECISION_INVALID", "审核决定无效", 422)
    item.review_note = note.strip()
    item.reviewed_by = decided_by
    item.status = "REJECTED" if normalized == "REJECT" else "NEED_MORE"
    if normalized == "REJECT":
        company.supplier_cooperation_status = "ACTIVE"
    db.flush()
    return item


def record_offline_payment(
    db: Session,
    *,
    request_id: str,
    recorded_by: str,
    external_reference: str,
    paid_at: datetime,
    payment_amount_cents: int,
    note: str,
    proof_url: str | None = None,
) -> SupplyTerminationRequest:
    item, _company = _lock_request_with_company(db, request_id)
    if item.status not in {"APPROVED_PENDING_PAYMENT", "PAID_PENDING_WRITE_OFF", "PAYMENT_FAILED"}:
        raise AppError("SUPPLY_TERMINATION_PAYMENT_NOT_ALLOWED", "当前申请不能登记付款", 409)
    normalized_reference = external_reference.strip()
    if not normalized_reference:
        raise AppError("SUPPLY_TERMINATION_PAYMENT_REFERENCE_REQUIRED", "付款流水号不能为空", 422)
    duplicate = db.scalar(select(SupplyTerminationRequest.id).where(
        SupplyTerminationRequest.payment_external_reference == normalized_reference,
        SupplyTerminationRequest.id != request_id,
    ))
    if duplicate:
        raise AppError("SUPPLY_TERMINATION_PAYMENT_REFERENCE_EXISTS", "付款流水号已使用", 409)
    expected_amount = int(item.cash_amount_cents_snapshot or 0)
    if int(payment_amount_cents) != expected_amount:
        raise AppError("SUPPLY_TERMINATION_PAYMENT_AMOUNT_MISMATCH", "付款金额与审批快照不一致", 422, {"expected_amount_cents": expected_amount})
    normalized_paid_at = _now(paid_at)
    history = list(item.payment_history_json or [])
    history.append({
        "event": "RECORDED" if item.status in {"APPROVED_PENDING_PAYMENT", "PAYMENT_FAILED"} else "CORRECTED",
        "external_reference": normalized_reference,
        "paid_at": normalized_paid_at.isoformat(),
        "payment_amount_cents": int(payment_amount_cents),
        "note": note.strip(),
        "proof_url": proof_url.strip() if proof_url else None,
        "actor_user_id": recorded_by,
        "recorded_at": _now().isoformat(),
    })
    item.payment_history_json = history
    item.payment_external_reference = normalized_reference
    item.paid_at = normalized_paid_at
    item.payment_amount_cents = int(payment_amount_cents)
    item.payment_note = note.strip()
    item.payment_proof_url = proof_url.strip() if proof_url else None
    item.payment_recorded_by = recorded_by
    item.status = "PAID_PENDING_WRITE_OFF"
    db.flush()
    return item


def fail_offline_payment(
    db: Session, *, request_id: str, failed_by: str, note: str
) -> SupplyTerminationRequest:
    item, company = _lock_request_with_company(db, request_id)
    if item.status not in {"APPROVED_PENDING_PAYMENT", "PAID_PENDING_WRITE_OFF"}:
        raise AppError("SUPPLY_TERMINATION_PAYMENT_FAILURE_NOT_ALLOWED", "当前申请不能回退付款", 409)
    lock_points_account(db, item.company_id)
    history = list(item.payment_history_json or [])
    history.append({
        "event": "FAILED",
        "external_reference": item.payment_external_reference,
        "paid_at": item.paid_at.isoformat() if item.paid_at else None,
        "payment_amount_cents": item.payment_amount_cents,
        "note": note.strip(),
        "proof_url": item.payment_proof_url,
        "actor_user_id": failed_by,
        "recorded_at": _now().isoformat(),
    })
    item.payment_history_json = history
    item.status = "PAYMENT_FAILED"
    item.payment_external_reference = None
    item.paid_at = None
    item.payment_amount_cents = None
    item.payment_note = note.strip()
    item.payment_proof_url = None
    company.supplier_cooperation_status = "TERMINATION_PENDING"
    db.flush()
    return item


def prepare_supplier_reward_reversal(
    db: Session, *, company_id: str, reward_id: str
) -> None:
    """Invalidate an unpaid snapshot, or reject reversal after payment evidence."""

    has_active_request = db.scalar(select(SupplyTerminationRequest.id).where(
        SupplyTerminationRequest.company_id == company_id,
        SupplyTerminationRequest.status.in_(("APPROVED_PENDING_PAYMENT", "PAID_PENDING_WRITE_OFF", "PAYMENT_FAILED")),
    ).order_by(SupplyTerminationRequest.created_at.desc()).limit(1))
    if has_active_request is None:
        return
    _lock_company(db, company_id)
    item = db.scalar(
        select(SupplyTerminationRequest)
        .where(
            SupplyTerminationRequest.company_id == company_id,
            SupplyTerminationRequest.status.in_(("APPROVED_PENDING_PAYMENT", "PAID_PENDING_WRITE_OFF", "PAYMENT_FAILED")),
        )
        .order_by(SupplyTerminationRequest.created_at.desc())
        .limit(1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if item is None:
        return
    account = lock_points_account(db, company_id)
    if item.status == "PAID_PENDING_WRITE_OFF":
        logger.error(
            "supplier reward reversal blocked after payment request_id=%s reward_id=%s",
            item.id,
            reward_id,
        )
        raise AppError(
            "REWARD_REVERSAL_PAYMENT_RECORDED",
            "终止合作付款已登记，奖励冲回需先回退付款",
            409,
            {"termination_request_id": item.id, "reward_id": reward_id},
        )
    blockers = list(item.blockers_json or [])
    blockers.append({
        "code": "REWARD_REVERSED_AFTER_APPROVAL", "count": 1,
        "record_ids": [reward_id], "truncated": False,
        "message": "审批后发生奖励冲回，需重新清算并审核",
    })
    item.blockers_json = blockers
    item.status = "NEED_MORE"
    item.customer_points_snapshot = None
    item.supply_points_snapshot = None
    item.general_points_snapshot = None
    item.cash_cents_per_point_snapshot = None
    item.cash_amount_cents_snapshot = None
    item.rate_config_id = None
    item.approved_at = None
    account.frozen_customer_points = 0
    account.frozen_supply_points = 0
    db.flush()


def confirm_offline_payment(
    db: Session,
    *,
    request_id: str,
    confirmed_by: str,
    idempotency_key: str,
) -> SupplyTerminationRequest:
    item, company = _lock_request_with_company(db, request_id)
    if item.status == "TERMINATED":
        return item
    if item.status != "PAID_PENDING_WRITE_OFF" or not item.payment_external_reference or item.paid_at is None:
        raise AppError("SUPPLY_TERMINATION_PAYMENT_NOT_RECORDED", "请先登记并核实线下付款", 409)
    if item.payment_amount_cents is None or int(item.payment_amount_cents) != int(item.cash_amount_cents_snapshot or 0):
        raise AppError("SUPPLY_TERMINATION_PAYMENT_AMOUNT_MISMATCH", "付款金额与审批快照不一致", 409)
    account = db.scalar(select(PointsAccount).where(
        PointsAccount.company_id == item.company_id
    ).with_for_update()) or get_or_create_account(db, item.company_id)
    customer_points = int(item.customer_points_snapshot or 0)
    supply_points = int(item.supply_points_snapshot or 0)
    writeoff_key = f"supply-termination:{item.id}"
    if int(account.balance) < customer_points or int(account.supply_balance) < supply_points:
        raise AppError("SUPPLY_TERMINATION_SNAPSHOT_BALANCE_CHANGED", "积分余额低于已付款快照，禁止核销", 409)
    if customer_points:
        change_points(
            db, company_id=item.company_id, delta=-customer_points,
            ledger_type="TERMINATION_WRITEOFF", business_type="SUPPLY_TERMINATION",
            business_id=item.id, idempotency_key=f"{writeoff_key}:customer",
            external_reference=item.payment_external_reference, created_by=confirmed_by,
            metadata={"request_id": item.id, "cash_amount_cents": item.cash_amount_cents_snapshot, "client_idempotency_key": idempotency_key},
            point_kind="CUSTOMER", allow_frozen=True,
        )
    if supply_points:
        change_points(
            db, company_id=item.company_id, delta=-supply_points,
            ledger_type="TERMINATION_WRITEOFF", business_type="SUPPLY_TERMINATION",
            business_id=item.id, idempotency_key=f"{writeoff_key}:supply",
            external_reference=item.payment_external_reference, created_by=confirmed_by,
            metadata={"request_id": item.id, "cash_amount_cents": item.cash_amount_cents_snapshot, "client_idempotency_key": idempotency_key},
            point_kind="SUPPLY", allow_frozen=True,
        )
    account.frozen_customer_points = 0
    account.frozen_supply_points = 0
    item.writeoff_idempotency_key = writeoff_key
    item.terminated_at = _now()
    item.status = "TERMINATED"
    company.supplier_cooperation_status = "TERMINATED"
    db.flush()
    return item


def reopen_supply_cooperation(
    db: Session, *, company_id: str, reopened_by: str, note: str
) -> Company:
    company = _lock_company(db, company_id)
    if company.supplier_cooperation_status == "ACTIVE":
        return company
    if company.supplier_cooperation_status != "TERMINATED":
        raise AppError("SUPPLY_COOPERATION_REOPEN_NOT_ALLOWED", "终止流程尚未完成，不能恢复合作", 409)
    company.supplier_cooperation_status = "ACTIVE"
    db.flush()
    return company


def _masked_account(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 4:
        return "*" * len(value)
    return ("*" * max(0, len(value) - 4)) + value[-4:]


def termination_to_dict(
    db: Session,
    item: SupplyTerminationRequest,
    *,
    include_sensitive: bool = True,
    company: Company | None = None,
) -> dict[str, Any]:
    company = company or db.get(Company, item.company_id)
    payee_name = decrypt_text(item.payee_name_encrypted)
    payee_account = decrypt_text(item.payee_account_encrypted)
    if payee_name is None or payee_account is None:
        logger.error("supply termination payee decrypt failed request_id=%s", item.id)
    return {
        "id": item.id,
        "company_id": item.company_id,
        "company_name": company.name if company else None,
        "cooperation_status": company.supplier_cooperation_status if company else None,
        "status": item.status,
        "reason": item.reason,
        "blockers": list(item.blockers_json or []),
        "customer_points_snapshot": item.customer_points_snapshot,
        "supply_points_snapshot": item.supply_points_snapshot,
        "general_points_snapshot": item.general_points_snapshot,
        "cash_cents_per_point_snapshot": item.cash_cents_per_point_snapshot,
        "cash_amount_cents_snapshot": item.cash_amount_cents_snapshot,
        "payment_method": item.payment_method,
        "payee_name": payee_name if include_sensitive else None,
        "payee_account_masked": _masked_account(payee_account),
        "payee_account": payee_account if include_sensitive else None,
        "review_note": item.review_note,
        "payment_external_reference": item.payment_external_reference if include_sensitive else None,
        "payment_amount_cents": item.payment_amount_cents if include_sensitive else None,
        "payment_note": item.payment_note if include_sensitive else None,
        "payment_proof_url": item.payment_proof_url if include_sensitive else None,
        "payment_history": list(item.payment_history_json or []) if include_sensitive else [],
        "paid_at": item.paid_at.isoformat() if item.paid_at else None,
        "approved_at": item.approved_at.isoformat() if item.approved_at else None,
        "terminated_at": item.terminated_at.isoformat() if item.terminated_at else None,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }
