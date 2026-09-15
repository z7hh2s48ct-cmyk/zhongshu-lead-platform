from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..core.auth import CurrentPrincipal, require_permissions
from ..core.database import get_db
from ..core.enums import PointsLedgerType
from ..core.errors import AppError
from ..core.models import Company, LeadPriceRule, PointsLedger, PointsPackage
from ..core.responses import ok, page
from ..core.time import as_utc
from ..schemas.points import ManualAdjustmentBody, PointsPackageBody, PriceRuleBody, RechargeBody, ReverseLedgerBody
from ..services.audit import write_audit
from ..services.notification_service import create_station_message, enqueue_outbox
from ..services.points_service import (
    POINTS_WORKBENCH_DEEP_LINK,
    account_summary,
    change_points,
    ledger_to_dict,
    recharge_points,
    reconcile_points_account,
    reverse_ledger,
)

router = APIRouter(prefix="/points", tags=["points"])


def _package_to_dict(item: PointsPackage) -> dict:
    now = datetime.now(timezone.utc)
    effective_at = as_utc(item.effective_at)
    expires_at = as_utc(item.expires_at)
    effective = (
        item.status == "PUBLISHED"
        and (effective_at is None or effective_at <= now)
        and (expires_at is None or expires_at > now)
    )
    return {
        "id": item.id,
        "code": item.code,
        "name": item.name,
        "cash_amount_cents": item.cash_amount_cents,
        "base_points": item.base_points,
        "bonus_points": item.bonus_points,
        "total_points": item.base_points + item.bonus_points,
        "level_code": item.level_code,
        "entitlements": item.entitlements_json,
        "version": item.version,
        "status": item.status,
        "effective_at": item.effective_at.isoformat() if item.effective_at else None,
        "expires_at": item.expires_at.isoformat() if item.expires_at else None,
        "effective": effective,
    }


@router.get("/packages")
def list_packages(request: Request, principal: CurrentPrincipal, db: Session = Depends(get_db), active_only: bool = Query(default=True)):
    if not active_only and not (principal.can("points.read") or principal.can("*")):
        raise AppError("FORBIDDEN", "无权查看全部充值档位", 403)
    stmt = select(PointsPackage)
    if active_only:
        now = datetime.now(timezone.utc)
        stmt = stmt.where(
            PointsPackage.status == "PUBLISHED",
            or_(PointsPackage.effective_at.is_(None), PointsPackage.effective_at <= now),
            or_(PointsPackage.expires_at.is_(None), PointsPackage.expires_at > now),
        )
    items = db.scalars(stmt.order_by(PointsPackage.cash_amount_cents.asc(), PointsPackage.version.desc())).all()
    return ok(request, [_package_to_dict(item) for item in items])


@router.post("/packages")
def create_package(body: PointsPackageBody, request: Request, principal=Depends(require_permissions("points.package.manage")), db: Session = Depends(get_db)):
    latest = db.scalar(select(func.max(PointsPackage.version)).where(PointsPackage.code == body.code)) or 0
    effective_at = body.effective_at or (datetime.now(timezone.utc) if body.publish else None)
    if body.publish and effective_at:
        previous_versions = db.scalars(
            select(PointsPackage).where(
                PointsPackage.code == body.code,
                PointsPackage.status == "PUBLISHED",
                or_(PointsPackage.expires_at.is_(None), PointsPackage.expires_at > effective_at),
            )
        ).all()
        for previous in previous_versions:
            previous.expires_at = effective_at
    package = PointsPackage(
        code=body.code,
        name=body.name,
        cash_amount_cents=body.cash_amount_cents,
        base_points=body.base_points,
        bonus_points=body.bonus_points,
        level_code=body.level_code,
        entitlements_json=body.entitlements,
        version=latest + 1,
        status="PUBLISHED" if body.publish else "DRAFT",
        effective_at=effective_at,
        expires_at=body.expires_at,
    )
    db.add(package)
    db.flush()
    write_audit(db, principal=principal, action="POINTS_PACKAGE_CREATE", resource_type="points_package", resource_id=package.id, after=_package_to_dict(package), request_id=request.state.request_id)
    db.commit()
    return ok(request, _package_to_dict(package))


@router.get("/price-rules")
def list_price_rules(request: Request, principal=Depends(require_permissions("points.read")), db: Session = Depends(get_db)):
    items = db.scalars(select(LeadPriceRule).order_by(LeadPriceRule.priority, LeadPriceRule.version.desc())).all()
    return ok(request, [{"id": x.id, "region_code": x.region_code, "category_code": x.category_code, "brand_code": x.brand_code, "level_code": x.level_code, "points_cost": x.points_cost, "version": x.version, "priority": x.priority, "status": x.status, "effective_at": x.effective_at.isoformat() if x.effective_at else None, "expires_at": x.expires_at.isoformat() if x.expires_at else None} for x in items])


@router.post("/price-rules")
def create_price_rule(body: PriceRuleBody, request: Request, principal=Depends(require_permissions("points.package.manage")), db: Session = Depends(get_db)):
    latest = db.scalar(select(func.max(LeadPriceRule.version)).where(LeadPriceRule.region_code == body.region_code, LeadPriceRule.category_code == body.category_code, LeadPriceRule.brand_code == body.brand_code, LeadPriceRule.level_code == body.level_code)) or 0
    effective_at = body.effective_at or (datetime.now(timezone.utc) if body.publish else None)
    if body.publish and effective_at:
        previous_versions = db.scalars(
            select(LeadPriceRule).where(
                LeadPriceRule.region_code == body.region_code,
                LeadPriceRule.category_code == body.category_code,
                LeadPriceRule.brand_code == body.brand_code,
                LeadPriceRule.level_code == body.level_code,
                LeadPriceRule.status == "PUBLISHED",
                or_(LeadPriceRule.expires_at.is_(None), LeadPriceRule.expires_at > effective_at),
            )
        ).all()
        for previous in previous_versions:
            previous.expires_at = effective_at
    rule = LeadPriceRule(
        region_code=body.region_code,
        category_code=body.category_code,
        brand_code=body.brand_code,
        level_code=body.level_code,
        points_cost=body.points_cost,
        priority=body.priority,
        version=latest + 1,
        status="PUBLISHED" if body.publish else "DRAFT",
        effective_at=effective_at,
        expires_at=body.expires_at,
    )
    db.add(rule)
    db.flush()
    write_audit(db, principal=principal, action="POINTS_PRICE_RULE_CREATE", resource_type="lead_price_rule", resource_id=rule.id, after={"points_cost": rule.points_cost, "version": rule.version}, request_id=request.state.request_id)
    db.commit()
    return ok(request, {"id": rule.id, "version": rule.version})


@router.get("/accounts/{company_id}")
def get_account(company_id: str, request: Request, principal: CurrentPrincipal, db: Session = Depends(get_db)):
    can_read_platform_accounts = principal.can("points.read") or principal.can("*")
    can_read_own_account = principal.company_id == company_id and principal.can("points.own.read")
    if not (can_read_platform_accounts or can_read_own_account):
        raise AppError("FORBIDDEN", "无权查看积分账户", 403)
    return ok(request, account_summary(db, company_id))


@router.get("/ledgers")
def list_ledgers(
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
    company_id: str | None = Query(default=None),
    ledger_type: str | None = Query(default=None),
    point_kind: str | None = Query(default=None),
    page_no: int = Query(default=1, alias="page", ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
):
    if principal.has_any_role("FRANCHISE_OWNER"):
        company_id = principal.company_id
    elif not (principal.can("points.read") or principal.can("*")):
        raise AppError("FORBIDDEN", "无权查看积分流水", 403)
    stmt = select(PointsLedger)
    count_stmt = select(func.count(PointsLedger.id))
    if company_id:
        stmt = stmt.where(PointsLedger.company_id == company_id)
        count_stmt = count_stmt.where(PointsLedger.company_id == company_id)
    if ledger_type:
        stmt = stmt.where(PointsLedger.ledger_type == ledger_type)
        count_stmt = count_stmt.where(PointsLedger.ledger_type == ledger_type)
    if point_kind:
        normalized_kind = point_kind.strip().upper()
        if normalized_kind not in {"CUSTOMER", "SUPPLY"}:
            raise AppError("POINT_KIND_INVALID", "积分账户类型无效", 422)
        stmt = stmt.where(PointsLedger.point_kind == normalized_kind)
        count_stmt = count_stmt.where(PointsLedger.point_kind == normalized_kind)
    total = db.scalar(count_stmt) or 0
    items = db.scalars(stmt.order_by(PointsLedger.created_at.desc()).offset((page_no - 1) * page_size).limit(page_size)).all()
    return ok(request, page([ledger_to_dict(x) for x in items], total, page_no, page_size))


@router.post("/recharge")
def recharge(body: RechargeBody, request: Request, principal=Depends(require_permissions("points.recharge")), db: Session = Depends(get_db)):
    if not body.confirmed:
        raise AppError("POINTS_RECHARGE_CONFIRM_REQUIRED", "请二次确认线下款项已核实", 422)
    package = db.get(PointsPackage, body.package_id)
    if not package:
        raise AppError("POINTS_PACKAGE_NOT_FOUND", "充值档位不存在", 404)
    existing = db.scalar(
        select(PointsLedger).where(
            PointsLedger.company_id == body.company_id,
            PointsLedger.idempotency_key == body.idempotency_key,
        )
    )
    ledger = recharge_points(db, company_id=body.company_id, package=package, cash_amount_cents=body.cash_amount_cents, external_reference=body.external_reference, idempotency_key=body.idempotency_key, created_by=principal.user_id, note=body.note)
    company = db.get(Company, body.company_id)
    if not existing:
        create_station_message(
            db,
            user_id=company.primary_user_id if company else None,
            company_id=body.company_id,
            scene="POINTS_RECHARGED",
            title="积分充值到账",
            body=f"本次到账{ledger.delta}积分，当前余额{ledger.balance_after}积分。",
            deep_link=POINTS_WORKBENCH_DEEP_LINK,
        )
        enqueue_outbox(
            db,
            event_key=f"points:{ledger.id}:recharged",
            event_type="POINTS_RECHARGED",
            aggregate_type="points_ledger",
            aggregate_id=ledger.id,
            payload={"company_id": body.company_id, "user_id": company.primary_user_id if company else None, "deep_link": POINTS_WORKBENCH_DEEP_LINK},
        )
        write_audit(db, principal=principal, action="POINTS_RECHARGE", resource_type="points_ledger", resource_id=ledger.id, company_id=body.company_id, after=ledger_to_dict(ledger), request_id=request.state.request_id)
    db.commit()
    return ok(request, ledger_to_dict(ledger), "充值积分成功")


@router.post("/adjust")
def adjust(body: ManualAdjustmentBody, request: Request, principal=Depends(require_permissions("points.recharge")), db: Session = Depends(get_db)):
    ledger = change_points(db, company_id=body.company_id, delta=body.delta, ledger_type="ADJUST", business_type="MANUAL_ADJUSTMENT", business_id=body.idempotency_key, idempotency_key=body.idempotency_key, created_by=principal.user_id, metadata={"reason": body.reason, "point_kind": body.point_kind}, point_kind=body.point_kind)
    write_audit(db, principal=principal, action="POINTS_ADJUST", resource_type="points_ledger", resource_id=ledger.id, company_id=body.company_id, after=ledger_to_dict(ledger), metadata={"point_kind": body.point_kind}, request_id=request.state.request_id)
    db.commit()
    return ok(request, ledger_to_dict(ledger))


@router.post("/ledgers/{ledger_id}/reverse")
def reverse(ledger_id: str, body: ReverseLedgerBody, request: Request, principal=Depends(require_permissions("points.reverse")), db: Session = Depends(get_db)):
    original = db.get(PointsLedger, ledger_id)
    if not original:
        raise AppError("POINTS_LEDGER_NOT_FOUND", "积分流水不存在", 404)
    if original.ledger_type not in {PointsLedgerType.RECHARGE, PointsLedgerType.ADJUST}:
        raise AppError("POINTS_REVERSAL_MANUAL_ONLY", "领取、退回和奖励流水必须通过对应业务流程处理，不能直接冲正", 409)
    reversal = reverse_ledger(db, original, reason=body.reason, idempotency_key=body.idempotency_key, created_by=principal.user_id)
    write_audit(db, principal=principal, action="POINTS_REVERSE", resource_type="points_ledger", resource_id=reversal.id, company_id=original.company_id, metadata={"original_ledger_id": original.id, "reason": body.reason}, request_id=request.state.request_id)
    db.commit()
    return ok(request, ledger_to_dict(reversal))


@router.get("/reconciliation/{company_id}")
def reconciliation(
    company_id: str,
    request: Request,
    principal=Depends(require_permissions("points.read")),
    db: Session = Depends(get_db),
    point_kind: str = Query(default="CUSTOMER"),
    start_at: datetime | None = Query(default=None),
    end_at: datetime | None = Query(default=None),
):
    if start_at and end_at and start_at >= end_at:
        raise AppError("POINTS_RECONCILIATION_RANGE_INVALID", "开始时间必须早于结束时间", 422)
    result = reconcile_points_account(
        db,
        company_id,
        point_kind=point_kind,
        start_at=start_at,
        end_at=end_at,
    )
    write_audit(db, principal=principal, action="POINTS_RECONCILE", resource_type="points_account", resource_id=company_id, company_id=company_id, metadata={"point_kind": result["point_kind"], "balanced": result["balanced"], "difference": result["difference"]}, request_id=request.state.request_id)
    db.commit()
    return ok(request, result)
