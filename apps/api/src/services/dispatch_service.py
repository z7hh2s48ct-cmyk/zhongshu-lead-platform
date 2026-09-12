from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import Session, selectinload

from ..core.auth import Principal
from ..core.config import get_settings
from ..core.enums import AssignmentStatus, CompanyStatus, LeadStatus
from ..core.errors import AppError
from ..core.models import (
    Assignment,
    AssignmentEvent,
    Company,
    CompanyCapability,
    CompanyServiceRegion,
    Lead,
    ReturnRequest,
)
from .lead_points_v12 import get_lead_points_settings, operation_claim_points_for_lead
from .notification_service import create_station_message, enqueue_outbox
from .points_service import points_available_for_dispatch, resolve_price

settings = get_settings()


def candidate_companies(db: Session, lead: Lead, *, include_balance: bool = False) -> list[dict[str, Any]]:
    companies = db.scalars(
        select(Company)
        .options(selectinload(Company.service_regions), selectinload(Company.capabilities), selectinload(Company.points_account))
        .where(Company.status == CompanyStatus.ACTIVE)
        .order_by(Company.name)
    ).all()
    points_settings = get_lead_points_settings(db)
    fixed_price = operation_claim_points_for_lead(
        db,
        source_kind=lead.source_kind,
        source_type=lead.source_type,
        settings=points_settings,
    )
    candidates: list[dict[str, Any]] = []
    for company in companies:
        reasons: list[str] = []
        region_match = any(r.active and r.region_code == lead.region_code for r in company.service_regions)
        if not region_match:
            reasons.append("REGION_MISMATCH")
        capability_match = any(
            c.active
            and c.category_code == lead.category_code
            and (not c.brand_code or not lead.brand_code or c.brand_code == lead.brand_code)
            for c in company.capabilities
        )
        if not capability_match:
            reasons.append("CAPABILITY_MISMATCH")
        if not company.primary_user_id:
            reasons.append("WECHAT_UNBOUND")
        excluded = db.scalar(
            select(func.count(ReturnRequest.id)).where(
                ReturnRequest.lead_id == lead.id,
                ReturnRequest.company_id == company.id,
                ReturnRequest.status == "APPROVED",
            )
        ) or 0
        if excluded:
            reasons.append("HISTORICAL_RETURN_EXCLUDED")
        price, rule = resolve_price(
            db,
            lead,
            company,
            points_settings=points_settings,
        )
        balance, reserved, available = points_available_for_dispatch(db, company.id)
        if available < price:
            reasons.append("POINTS_INSUFFICIENT")
        item: dict[str, Any] = {
            "company_id": company.id,
            "company_code": company.code,
            "company_name": company.name,
            "level_code": company.level_code,
            "wechat_bound": bool(company.primary_user_id),
            "points_price": price,
            "price_rule_id": rule.id if rule else None,
            "price_version": (
                fixed_price[1]
                if fixed_price
                else rule.version if rule else 1
            ),
            "eligible": not reasons,
            "reason_codes": reasons,
            "eligibility_label": "可派" if not reasons else _label(reasons[0]),
        }
        if include_balance:
            item.update({"points_balance": balance, "pending_claim_points": reserved, "available_points": available})
        candidates.append(item)
    return sorted(candidates, key=lambda item: (not item["eligible"], item["company_name"]))


def dispatch_lead(
    db: Session,
    *,
    lead_id: str,
    company_id: str,
    principal: Principal,
    idempotency_key: str,
    reason: str | None = None,
) -> Assignment:
    existing = db.scalar(select(Assignment).where(Assignment.idempotency_key == idempotency_key))
    if existing:
        return existing
    lead = db.scalar(select(Lead).where(Lead.id == lead_id).with_for_update())
    if not lead:
        raise AppError("LEAD_NOT_FOUND", "客资不存在", 404)
    if lead.status != LeadStatus.QUALIFIED or lead.current_assignment_id:
        raise AppError("LEAD_NOT_DISPATCHABLE", "客资当前不可派发", 409)
    company = db.get(Company, company_id)
    if not company:
        raise AppError("COMPANY_NOT_FOUND", "加盟商公司不存在", 404)
    candidates = {item["company_id"]: item for item in candidate_companies(db, lead, include_balance=True)}
    candidate = candidates.get(company_id)
    if not candidate or not candidate["eligible"]:
        raise AppError("COMPANY_NOT_ELIGIBLE", "目标加盟商暂不可派发", 409, candidate or {})
    now = datetime.now(timezone.utc)
    assignment = Assignment(
        lead_id=lead.id,
        company_id=company.id,
        status=AssignmentStatus.PENDING_CLAIM,
        points_price=candidate["points_price"],
        price_rule_id=candidate["price_rule_id"],
        price_version=candidate["price_version"],
        lead_snapshot={
            "customer_name": lead.customer_name,
            "city": lead.city,
            "district": lead.district,
            "region_code": lead.region_code,
            "category_code": lead.category_code,
            "brand_code": lead.brand_code,
            "source_channel": lead.source_channel,
            "need_summary": lead.need_summary,
            "budget_min": lead.budget_min,
            "budget_max": lead.budget_max,
        },
        assigned_by=principal.user_id,
        assigned_at=now,
        expires_at=now + timedelta(hours=settings.assignment_expire_hours),
        idempotency_key=idempotency_key,
    )
    db.add(assignment)
    db.flush()
    lead.current_assignment_id = assignment.id
    lead.status = LeadStatus.ASSIGNED
    db.add(
        AssignmentEvent(
            assignment_id=assignment.id,
            event_type="DISPATCHED",
            actor_user_id=principal.user_id,
            payload={"reason": reason, "points_price": assignment.points_price},
        )
    )
    deep_link = f"/h5/v12-workbench.html?view=assignments&id={assignment.id}"
    create_station_message(
        db,
        user_id=company.primary_user_id,
        company_id=company.id,
        scene="NEW_LEAD",
        title="新客资已派发",
        body=f"您收到一条{lead.city or ''}客资，领取需{assignment.points_price}积分。",
        deep_link=deep_link,
    )
    enqueue_outbox(
        db,
        event_key=f"assignment:{assignment.id}:dispatched",
        event_type="ASSIGNMENT_DISPATCHED",
        aggregate_type="assignment",
        aggregate_id=assignment.id,
        payload={"company_id": company.id, "user_id": company.primary_user_id, "deep_link": deep_link},
    )
    return assignment


def release_assignment(db: Session, assignment: Assignment, *, principal: Principal | None, reason: str, event_type: str = "RELEASED") -> None:
    if assignment.status not in {AssignmentStatus.PENDING_CLAIM, AssignmentStatus.CLAIMED, AssignmentStatus.FOLLOWING}:
        raise AppError("ASSIGNMENT_NOT_RELEASABLE", "订单当前不可释放", 409)
    lead = db.scalar(select(Lead).where(Lead.id == assignment.lead_id).with_for_update())
    assignment.status = AssignmentStatus.RELEASED if event_type != "EXPIRED" else AssignmentStatus.EXPIRED
    assignment.released_at = datetime.now(timezone.utc)
    assignment.release_reason = reason
    if lead and lead.current_assignment_id == assignment.id:
        lead.current_assignment_id = None
        lead.status = LeadStatus.QUALIFIED
        lead.pending_reason = reason
    db.add(
        AssignmentEvent(
            assignment_id=assignment.id,
            event_type=event_type,
            actor_user_id=principal.user_id if principal else None,
            payload={"reason": reason},
        )
    )


def assignment_to_dict(assignment: Assignment, *, include_snapshot: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": assignment.id,
        "lead_id": assignment.lead_id,
        "company_id": assignment.company_id,
        "status": assignment.status,
        "points_price": assignment.points_price,
        "price_version": assignment.price_version,
        "assigned_at": assignment.assigned_at.isoformat(),
        "claimed_at": assignment.claimed_at.isoformat() if assignment.claimed_at else None,
        "expires_at": assignment.expires_at.isoformat() if assignment.expires_at else None,
        "first_followup_due_at": assignment.first_followup_due_at.isoformat() if assignment.first_followup_due_at else None,
        "released_at": assignment.released_at.isoformat() if assignment.released_at else None,
        "release_reason": assignment.release_reason,
    }
    if include_snapshot:
        data["lead_snapshot"] = assignment.lead_snapshot
    return data


def _label(reason: str) -> str:
    return {
        "REGION_MISMATCH": "地区不匹配",
        "CAPABILITY_MISMATCH": "类目不匹配",
        "WECHAT_UNBOUND": "未绑定微信",
        "HISTORICAL_RETURN_EXCLUDED": "历史退回排除",
        "POINTS_INSUFFICIENT": "积分不足",
    }.get(reason, "不可派")
