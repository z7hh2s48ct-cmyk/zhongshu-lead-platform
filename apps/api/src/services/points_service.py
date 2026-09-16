from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..core.enums import AssignmentStatus, PointsLedgerType
from ..core.errors import AppError
from ..core.models import (
    Assignment,
    Company,
    Lead,
    LeadPriceRule,
    NotificationOutbox,
    PointsAccount,
    PointsLedger,
    PointsPackage,
    SupplyTerminationRequest,
)
from ..core.time import as_utc
from .lead_points_v12 import LeadPointsSettings, get_lead_points_settings, operation_claim_points_for_lead
from .notification_service import create_station_message, enqueue_outbox

settings = get_settings()
POINTS_WORKBENCH_DEEP_LINK = "/h5/v12-workbench.html?view=points"


def get_or_create_account(db: Session, company_id: str) -> PointsAccount:
    account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == company_id))
    if account:
        return account
    if not db.get(Company, company_id):
        raise AppError("COMPANY_NOT_FOUND", "加盟商公司不存在", 404)
    account = PointsAccount(company_id=company_id, balance=0, version=1)
    db.add(account)
    db.flush()
    return account


def pending_claim_points(
    db: Session,
    company_id: str,
    *,
    points_settings: LeadPointsSettings | None = None,
) -> int:
    current_settings = points_settings or get_lead_points_settings(db)
    pending_price = Assignment.points_price
    if current_settings.configured and current_settings.operation_claim_points is not None:
        pending_price = case(
            (Assignment.claimed_at.is_(None), current_settings.operation_claim_points),
            else_=Assignment.points_price,
        )
    return int(
        db.scalar(
            select(func.coalesce(func.sum(pending_price), 0))
            .join(Lead, Lead.id == Assignment.lead_id)
            .where(
                Assignment.company_id == company_id,
                Assignment.status == AssignmentStatus.PENDING_CLAIM,
                Lead.deleted_at.is_(None),
            )
        )
        or 0
    )


def points_available_for_dispatch(
    db: Session,
    company_id: str,
    *,
    points_settings: LeadPointsSettings | None = None,
) -> tuple[int, int, int]:
    account = get_or_create_account(db, company_id)
    reserved = pending_claim_points(db, company_id, points_settings=points_settings)
    available = max(0, int(account.balance - reserved - account.frozen_customer_points))
    return int(account.balance), int(reserved), available


def _effective_package_stmt(now: datetime):
    return select(PointsPackage).where(
        PointsPackage.status == "PUBLISHED",
        or_(PointsPackage.effective_at.is_(None), PointsPackage.effective_at <= now),
        or_(PointsPackage.expires_at.is_(None), PointsPackage.expires_at > now),
    )


def resolve_level_entitlements(db: Session, level_code: str) -> tuple[dict[str, Any], PointsPackage | None]:
    now = datetime.now(timezone.utc)
    package = db.scalar(
        _effective_package_stmt(now)
        .where(PointsPackage.level_code == level_code)
        .order_by(PointsPackage.version.desc(), PointsPackage.effective_at.desc())
        .limit(1)
    )
    return (dict(package.entitlements_json), package) if package else ({}, None)


def account_summary(db: Session, company_id: str) -> dict[str, Any]:
    company = db.get(Company, company_id)
    if not company:
        raise AppError("COMPANY_NOT_FOUND", "加盟商公司不存在", 404)
    balance, reserved, available = points_available_for_dispatch(db, company_id)
    entitlements, package = resolve_level_entitlements(db, company.level_code)
    threshold = int(settings.low_points_warning_threshold)
    return {
        "company_id": company_id,
        "company_name": company.name,
        "level_code": company.level_code,
        "balance": balance,
        "customer_balance": balance,
        "supply_balance": int(get_or_create_account(db, company_id).supply_balance),
        "pending_claim_points": reserved,
        "available_for_dispatch": available,
        "low_points_threshold": threshold,
        "low_points": balance < threshold,
        "level_entitlements": entitlements,
        "level_package": {
            "id": package.id,
            "code": package.code,
            "name": package.name,
            "version": package.version,
        }
        if package
        else None,
    }


def resolve_price(
    db: Session,
    lead: Lead,
    company: Company,
    *,
    points_settings: LeadPointsSettings | None = None,
) -> tuple[int, LeadPriceRule | None]:
    fixed_price = operation_claim_points_for_lead(
        db,
        source_kind=lead.source_kind,
        source_type=lead.source_type,
        settings=points_settings,
    )
    if fixed_price is not None:
        return fixed_price[0], None
    now = datetime.now(timezone.utc)
    rules = db.scalars(
        select(LeadPriceRule)
        .where(
            LeadPriceRule.status == "PUBLISHED",
            or_(LeadPriceRule.effective_at.is_(None), LeadPriceRule.effective_at <= now),
            or_(LeadPriceRule.expires_at.is_(None), LeadPriceRule.expires_at > now),
            or_(LeadPriceRule.region_code.is_(None), LeadPriceRule.region_code == lead.region_code),
            or_(LeadPriceRule.category_code.is_(None), LeadPriceRule.category_code == lead.category_code),
            or_(LeadPriceRule.brand_code.is_(None), LeadPriceRule.brand_code == lead.brand_code),
            or_(LeadPriceRule.level_code.is_(None), LeadPriceRule.level_code == company.level_code),
        )
        .order_by(
            LeadPriceRule.priority.asc(),
            LeadPriceRule.region_code.is_(None).asc(),
            LeadPriceRule.category_code.is_(None).asc(),
            LeadPriceRule.brand_code.is_(None).asc(),
            LeadPriceRule.level_code.is_(None).asc(),
            LeadPriceRule.version.desc(),
        )
    ).all()
    if rules:
        return rules[0].points_cost, rules[0]
    return 100, None


def _points_idempotent_ledger(db: Session, company_id: str, idempotency_key: str) -> PointsLedger | None:
    return db.scalar(
        select(PointsLedger).where(
            PointsLedger.company_id == company_id,
            PointsLedger.idempotency_key == idempotency_key,
        )
    )


def _validate_idempotent_ledger(
    existing: PointsLedger,
    *,
    delta: int,
    ledger_type: str,
    business_type: str,
    business_id: str,
    point_kind: str,
    external_reference: str | None,
    related_ledger_id: str | None,
) -> PointsLedger:
    expected = (
        int(delta), str(ledger_type), business_type, business_id,
        point_kind, external_reference, related_ledger_id,
    )
    actual = (
        int(existing.delta), existing.ledger_type, existing.business_type,
        existing.business_id, existing.point_kind, existing.external_reference,
        existing.related_ledger_id,
    )
    if actual != expected:
        raise AppError(
            "POINTS_IDEMPOTENCY_CONFLICT",
            "幂等键已被另一笔积分业务使用",
            409,
            {"ledger_id": existing.id},
        )
    return existing


def _serialize_points_account(db: Session, company_id: str) -> PointsAccount:
    """Acquire the account write serialization boundary before changing balance."""

    if db.get_bind().dialect.name == "sqlite":
        # SQLite ignores FOR UPDATE. Start with a no-op write so concurrent writers
        # serialize before either rechecks the idempotency ledger or reads balance.
        result = db.execute(
            update(PointsAccount)
            .where(PointsAccount.company_id == company_id)
            .values(version=PointsAccount.version)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 0:
            account = get_or_create_account(db, company_id)
            db.flush()
            return account
        db.expire_all()
        account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == company_id))
        assert account is not None
        return account

    account = db.scalar(
        select(PointsAccount)
        .where(PointsAccount.company_id == company_id)
        .with_for_update()
    )
    if account is None:
        account = get_or_create_account(db, company_id)
    return account


def lock_points_account(db: Session, company_id: str) -> PointsAccount:
    """Expose the wallet lock for multi-step financial workflows."""

    return _serialize_points_account(db, company_id)


def change_points(
    db: Session,
    *,
    company_id: str,
    delta: int,
    ledger_type: str,
    business_type: str,
    business_id: str,
    idempotency_key: str,
    created_by: str | None,
    external_reference: str | None = None,
    related_ledger_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    point_kind: str = "CUSTOMER",
    allow_frozen: bool = False,
    allow_negative: bool = False,
) -> PointsLedger:
    if delta == 0:
        raise AppError("POINTS_DELTA_ZERO", "积分变动不能为0", 422)
    normalized_kind = point_kind.strip().upper()
    if normalized_kind not in {"CUSTOMER", "SUPPLY"}:
        raise AppError("POINT_KIND_INVALID", "积分账户类型无效", 422)
    normalized_ledger_type = (
        ledger_type.value if isinstance(ledger_type, PointsLedgerType) else str(ledger_type).strip().upper()
    )
    required_kind = {
        PointsLedgerType.REWARD.value: "SUPPLY",
        PointsLedgerType.RECHARGE.value: "CUSTOMER",
        PointsLedgerType.CLAIM.value: "CUSTOMER",
        PointsLedgerType.RETURN.value: "CUSTOMER",
    }.get(normalized_ledger_type)
    if required_kind and normalized_kind != required_kind:
        raise AppError(
            "POINT_KIND_BUSINESS_MISMATCH",
            "积分流水类型与积分账户不一致",
            422,
            {"ledger_type": normalized_ledger_type, "point_kind": normalized_kind},
        )
    if normalized_ledger_type == PointsLedgerType.REVERSAL.value:
        original = db.get(PointsLedger, related_ledger_id) if related_ledger_id else None
        if original is None or original.point_kind != normalized_kind:
            raise AppError(
                "POINT_KIND_BUSINESS_MISMATCH",
                "冲正流水必须关联同一积分账户的原流水",
                422,
                {"ledger_type": normalized_ledger_type, "point_kind": normalized_kind},
            )
    if allow_negative and normalized_ledger_type != PointsLedgerType.REVERSAL.value:
        raise AppError(
            "POINTS_NEGATIVE_BALANCE_NOT_ALLOWED",
            "仅异常冲正允许形成负积分余额",
            422,
        )

    # Fast path avoids unnecessary locking for normal retries.
    existing = _points_idempotent_ledger(db, company_id, idempotency_key)
    if existing:
        return _validate_idempotent_ledger(
            existing,
            delta=delta,
            ledger_type=normalized_ledger_type,
            business_type=business_type,
            business_id=business_id,
            point_kind=normalized_kind,
            external_reference=external_reference,
            related_ledger_id=related_ledger_id,
        )

    account = _serialize_points_account(db, company_id)

    # Required concurrency recheck: another request may have completed the same
    # idempotency key while this transaction waited for the account write lock.
    existing = _points_idempotent_ledger(db, company_id, idempotency_key)
    if existing:
        return _validate_idempotent_ledger(
            existing,
            delta=delta,
            ledger_type=normalized_ledger_type,
            business_type=business_type,
            business_id=business_id,
            point_kind=normalized_kind,
            external_reference=external_reference,
            related_ledger_id=related_ledger_id,
        )

    if account.points_split_status != "READY":
        raise AppError(
            "POINTS_SPLIT_RECONCILIATION_REQUIRED",
            "历史积分分账存在异常，完成对账前不能变更积分",
            409,
            {"points_split_status": account.points_split_status},
        )

    balance_attr = "balance" if normalized_kind == "CUSTOMER" else "supply_balance"
    frozen_attr = "frozen_customer_points" if normalized_kind == "CUSTOMER" else "frozen_supply_points"
    current_balance = int(getattr(account, balance_attr))
    frozen_points = int(getattr(account, frozen_attr))
    active_supply_settlement = False
    if normalized_kind == "SUPPLY" and not allow_frozen:
        active_supply_settlement = db.scalar(
            select(SupplyTerminationRequest.id)
            .where(
                SupplyTerminationRequest.company_id == company_id,
                SupplyTerminationRequest.status.in_(
                    ("APPROVED_PENDING_PAYMENT", "PAID_PENDING_WRITE_OFF", "PAYMENT_FAILED")
                ),
            )
            .limit(1)
        ) is not None
    if normalized_kind == "SUPPLY" and not allow_frozen and (frozen_points > 0 or active_supply_settlement):
        raise AppError(
            "POINTS_FROZEN_FOR_TERMINATION",
            "终止客资合作结算中的供客积分已冻结",
            409,
            {"balance": current_balance, "frozen_points": frozen_points},
        )
    new_balance = current_balance + int(delta)
    if delta < 0 and new_balance < 0 and not allow_negative:
        raise AppError("POINTS_INSUFFICIENT", "积分不足", 409, {"balance": current_balance, "required": abs(delta)})
    if delta < 0 and not allow_frozen and not allow_negative and new_balance < frozen_points:
        raise AppError(
            "POINTS_FROZEN_FOR_TERMINATION",
            "终止客资合作结算中的积分已冻结",
            409,
            {"balance": current_balance, "frozen_points": frozen_points},
        )
    setattr(account, balance_attr, new_balance)
    account.version += 1
    ledger = PointsLedger(
        account_id=account.id,
        company_id=company_id,
        ledger_type=normalized_ledger_type,
        point_kind=normalized_kind,
        delta=delta,
        balance_after=new_balance,
        business_type=business_type,
        business_id=business_id,
        idempotency_key=idempotency_key,
        external_reference=external_reference,
        related_ledger_id=related_ledger_id,
        metadata_json=metadata or {},
        created_by=created_by,
    )
    db.add(ledger)
    db.flush()
    return ledger


def recharge_points(
    db: Session,
    *,
    company_id: str,
    package: PointsPackage,
    cash_amount_cents: int,
    external_reference: str,
    idempotency_key: str,
    created_by: str,
    note: str | None,
) -> PointsLedger:
    existing = db.scalar(
        select(PointsLedger).where(
            PointsLedger.company_id == company_id,
            PointsLedger.idempotency_key == idempotency_key,
        )
    )
    if existing:
        return existing
    duplicate_ref = db.scalar(select(PointsLedger).where(PointsLedger.external_reference == external_reference))
    if duplicate_ref:
        raise AppError("POINTS_EXTERNAL_REFERENCE_EXISTS", "该付款流水号已使用", 409)
    now = datetime.now(timezone.utc)
    effective_at = as_utc(package.effective_at)
    expires_at = as_utc(package.expires_at)
    active = (
        package.status == "PUBLISHED"
        and (effective_at is None or effective_at <= now)
        and (expires_at is None or expires_at > now)
    )
    if not active:
        raise AppError("POINTS_PACKAGE_INACTIVE", "充值档位不可用", 409)
    if cash_amount_cents != package.cash_amount_cents:
        raise AppError("POINTS_CASH_AMOUNT_MISMATCH", "实收金额与档位不一致", 422)
    total = package.base_points + package.bonus_points
    company = db.get(Company, company_id)
    if not company:
        raise AppError("COMPANY_NOT_FOUND", "加盟商公司不存在", 404)
    company.level_code = package.level_code
    return change_points(
        db,
        company_id=company_id,
        delta=total,
        ledger_type=PointsLedgerType.RECHARGE,
        business_type="POINTS_PACKAGE",
        business_id=package.id,
        idempotency_key=idempotency_key,
        external_reference=external_reference,
        created_by=created_by,
        metadata={
            "package_code": package.code,
            "package_version": package.version,
            "level_code": package.level_code,
            "entitlements": package.entitlements_json,
            "cash_amount_cents": cash_amount_cents,
            "base_points": package.base_points,
            "bonus_points": package.bonus_points,
            "note": note,
        },
    )


def reverse_ledger(db: Session, original: PointsLedger, *, reason: str, idempotency_key: str, created_by: str) -> PointsLedger:
    existing_reversal = db.scalar(select(PointsLedger).where(PointsLedger.related_ledger_id == original.id, PointsLedger.ledger_type == PointsLedgerType.REVERSAL))
    if existing_reversal:
        raise AppError("POINTS_ALREADY_REVERSED", "该流水已经冲正", 409)
    return change_points(
        db,
        company_id=original.company_id,
        delta=-original.delta,
        ledger_type=PointsLedgerType.REVERSAL,
        business_type="LEDGER_REVERSAL",
        business_id=original.id,
        idempotency_key=idempotency_key,
        related_ledger_id=original.id,
        created_by=created_by,
        metadata={"reason": reason},
        point_kind=original.point_kind,
    )


def run_low_points_warnings(
    db: Session,
    *,
    threshold: int | None = None,
    as_of: datetime | None = None,
) -> dict[str, int]:
    resolved_threshold = max(1, int(threshold if threshold is not None else settings.low_points_warning_threshold))
    now = as_of or datetime.now(timezone.utc)
    bucket = now.date().isoformat()
    warned = 0
    skipped = 0
    rows = db.execute(
        select(PointsAccount, Company)
        .join(Company, Company.id == PointsAccount.company_id)
        .where(Company.status == "ACTIVE")
    ).all()
    for account, company in rows:
        if int(account.balance) >= resolved_threshold or not company.primary_user_id:
            skipped += 1
            continue
        event_key = f"points:{company.id}:low:{bucket}"
        if db.scalar(select(NotificationOutbox).where(NotificationOutbox.event_key == event_key)):
            skipped += 1
            continue
        create_station_message(
            db,
            user_id=company.primary_user_id,
            company_id=company.id,
            scene="LOW_POINTS",
            title="积分余额不足提醒",
            body=f"当前积分为{int(account.balance)}，已低于预警值{resolved_threshold}，请联系平台完成线下充值。",
            deep_link=POINTS_WORKBENCH_DEEP_LINK,
        )
        enqueue_outbox(
            db,
            event_key=event_key,
            event_type="POINTS_LOW_BALANCE",
            aggregate_type="points_account",
            aggregate_id=account.id,
            payload={
                "company_id": company.id,
                "user_id": company.primary_user_id,
                "balance": int(account.balance),
                "threshold": resolved_threshold,
                "deep_link": POINTS_WORKBENCH_DEEP_LINK,
            },
        )
        warned += 1
    return {"warned": warned, "skipped": skipped}


def reconcile_points_account(
    db: Session,
    company_id: str,
    *,
    point_kind: str = "CUSTOMER",
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> dict[str, Any]:
    normalized_kind = point_kind.strip().upper()
    if normalized_kind not in {"CUSTOMER", "SUPPLY"}:
        raise AppError("POINT_KIND_INVALID", "积分账户类型无效", 422)
    account = get_or_create_account(db, company_id)
    ledgers = db.scalars(
        select(PointsLedger)
        .where(PointsLedger.company_id == company_id, PointsLedger.point_kind == normalized_kind)
        .order_by(PointsLedger.created_at.asc(), PointsLedger.id.asc())
    ).all()
    normalized_start = as_utc(start_at)
    normalized_end = as_utc(end_at)
    running = 0
    sequence_errors: list[dict[str, Any]] = []
    opening_balance = 0
    period_delta = 0
    closing_balance = 0
    period_count = 0
    for ledger in ledgers:
        created_at = as_utc(ledger.created_at) or ledger.created_at
        before_period = bool(normalized_start and created_at < normalized_start)
        after_period = bool(normalized_end and created_at >= normalized_end)
        running += int(ledger.delta)
        if int(ledger.balance_after) != running:
            sequence_errors.append(
                {"ledger_id": ledger.id, "expected": running, "actual": int(ledger.balance_after)}
            )
        if before_period:
            opening_balance = running
        elif not after_period:
            period_delta += int(ledger.delta)
            closing_balance = running
            period_count += 1
    if period_count == 0:
        closing_balance = opening_balance
    expected_closing = opening_balance + period_delta
    current_scope = end_at is None
    account_balance = account.balance if normalized_kind == "CUSTOMER" else account.supply_balance
    snapshot_balance = int(account_balance) if current_scope else closing_balance
    difference = snapshot_balance - expected_closing
    return {
        "company_id": company_id,
        "point_kind": normalized_kind,
        "start_at": start_at.isoformat() if start_at else None,
        "end_at": end_at.isoformat() if end_at else None,
        "opening_balance": opening_balance,
        "period_delta": period_delta,
        "expected_closing_balance": expected_closing,
        "snapshot_balance": snapshot_balance,
        "difference": difference,
        "ledger_count": period_count,
        "sequence_error_count": len(sequence_errors),
        "sequence_errors": sequence_errors[:50],
        "balanced": difference == 0 and not sequence_errors,
    }


def ledger_to_dict(ledger: PointsLedger) -> dict[str, Any]:
    return {
        "id": ledger.id,
        "company_id": ledger.company_id,
        "type": ledger.ledger_type,
        "point_kind": ledger.point_kind,
        "delta": ledger.delta,
        "balance_after": ledger.balance_after,
        "business_type": ledger.business_type,
        "business_id": ledger.business_id,
        "external_reference": ledger.external_reference,
        "related_ledger_id": ledger.related_ledger_id,
        "metadata": ledger.metadata_json,
        "created_at": ledger.created_at.isoformat(),
    }
