from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from threading import Lock
from typing import Any

from sqlalchemy import Index, and_, case, func, literal, or_, select
from sqlalchemy.orm import Session, aliased

from ..core.config import get_settings
from ..core.enums import ACTIVE_ASSIGNMENT_STATUSES, AssignmentStatus, PointsLedgerType
from ..core.errors import AppError
from ..core.models import Assignment, AssignmentEvent, Company, Lead, LeadPriceRule, PointsAccount, PointsLedger, Region
from ..core.models_v12 import CompanyLeadCapability, CompanyServiceAreaV12, SupplierLeadReward
from ..core.security import decrypt_text, mask_phone
from ..core.time import as_utc
from ..core.v12_enums import (
    DuplicateDecision,
    LeadSourceKind,
    LeadV12Status,
    RewardStatus,
)
from .company_profile_v12 import REMOVAL_REQUEST_PREFIX, has_lead_capability, require_lead_capability
from .company_assignment_v12 import resolve_direct_dispatch_recipients
from .lead_correction_guard import require_correction_review_resolved
from .lead_deletion_v12 import require_lead_not_deleted
from .points_service import change_points, resolve_price
from .reward_rule_v12 import (
    SupplierRewardRule,
    calculate_reward_points,
    resolve_supplier_reward_rule,
)
from .workday_calendar import WorkdayCalendarService

settings = get_settings()

ACTIVE_ASSIGNMENT_STATUS_VALUES = tuple(item.value for item in ACTIVE_ASSIGNMENT_STATUSES)
CLAIMED_CONTACT_STATUSES = {
    AssignmentStatus.CLAIMED.value,
    AssignmentStatus.FOLLOWING.value,
    AssignmentStatus.RETURN_PENDING.value,
    AssignmentStatus.COMPLETED.value,
}
RECEIVER_HISTORY_STATUSES = CLAIMED_CONTACT_STATUSES


@dataclass
class _DispatchLockEntry:
    lock: Any
    users: int = 0


@dataclass(frozen=True)
class ManualDispatchOutcome:
    assignment: Assignment
    created: bool


_dispatch_locks_guard = Lock()
_dispatch_locks: dict[str, _DispatchLockEntry] = {}


@contextmanager
def manual_dispatch_idempotency_guard(idempotency_key: str) -> Iterator[None]:
    """Collapse same-key replays inside one worker while PostgreSQL remains authoritative."""

    with _dispatch_locks_guard:
        entry = _dispatch_locks.get(idempotency_key)
        if entry is None:
            entry = _DispatchLockEntry(lock=Lock())
            _dispatch_locks[idempotency_key] = entry
        entry.users += 1
    entry.lock.acquire()
    try:
        yield
    finally:
        entry.lock.release()
        with _dispatch_locks_guard:
            entry.users -= 1
            if entry.users == 0:
                _dispatch_locks.pop(idempotency_key, None)


def acquire_manual_dispatch_idempotency_lock(db: Session, idempotency_key: str) -> None:
    """Serialize same-key quick dispatches across PostgreSQL API processes."""

    if db.get_bind().dialect.name != "postgresql":
        return
    digest = hashlib.sha256(f"v12-manual-dispatch:{idempotency_key}".encode("utf-8")).digest()
    lock_id = int.from_bytes(digest[:8], byteorder="big", signed=True)
    db.execute(select(func.pg_advisory_xact_lock(lock_id)))

# Base.metadata.create_all is still used in development and tests. Register the
# same partial unique index that migration 0003 creates for PostgreSQL/SQLite.
_active_assignment_predicate = Assignment.__table__.c.status.in_(ACTIVE_ASSIGNMENT_STATUS_VALUES)
if not any(index.name == "uq_assignments_active_lead_v12" for index in Assignment.__table__.indexes):
    Index(
        "uq_assignments_active_lead_v12",
        Assignment.__table__.c.lead_id,
        unique=True,
        sqlite_where=_active_assignment_predicate,
        postgresql_where=_active_assignment_predicate,
    )


@dataclass(frozen=True, slots=True)
class CandidateResult:
    company_id: str
    company_name: str
    eligible: bool
    exclusion_reasons: tuple[str, ...]
    points_price: int
    price_rule_id: str | None
    price_version: int
    points_balance: int
    points_reserved: int
    points_available: int
    region_match: bool
    duplicate_to_receiver: bool


@dataclass(frozen=True, slots=True)
class ClaimResult:
    assignment: Assignment
    ledger: PointsLedger
    reward: SupplierLeadReward | None
    phone: str | None
    idempotent: bool


def get_dispatch_lead(db: Session, lead_id: str, *, lock: bool = False) -> Lead:
    stmt = select(Lead).where(Lead.id == lead_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    lead = db.scalar(stmt)
    return require_lead_not_deleted(lead)


def _region_matches(db: Session, company_id: str, lead: Lead) -> bool:
    if not lead.region_code:
        return False
    direct_match = db.scalar(
        select(CompanyServiceAreaV12.id).where(
            CompanyServiceAreaV12.company_id == company_id,
            _service_area_region_clause(lead.region_code),
            CompanyServiceAreaV12.active.is_(True),
            _service_area_dispatchable(),
        )
    )
    if direct_match is not None:
        return True

    lead_region = db.get(Region, lead.region_code)
    if lead_region is None or lead_region.level != "CITY":
        return False
    active_district_count = int(
        db.scalar(
            select(func.count(Region.code)).where(
                Region.parent_code == lead_region.code,
                Region.level == "DISTRICT",
                Region.active.is_(True),
            )
        )
        or 0
    )
    if active_district_count == 0:
        return False
    covered_district_count = int(
        db.scalar(
            select(func.count(func.distinct(CompanyServiceAreaV12.region_code)))
            .join(Region, Region.code == CompanyServiceAreaV12.region_code)
            .where(
                CompanyServiceAreaV12.company_id == company_id,
                CompanyServiceAreaV12.active.is_(True),
                _service_area_dispatchable(),
                Region.parent_code == lead_region.code,
                Region.level == "DISTRICT",
                Region.active.is_(True),
            )
        )
        or 0
    )
    return covered_district_count == active_district_count


def _service_area_region_clause(lead_region_code: str):
    parent = aliased(Region)
    grandparent = aliased(Region)
    parent_code = select(parent.parent_code).where(parent.code == lead_region_code).scalar_subquery()
    grandparent_code = (
        select(grandparent.parent_code).where(grandparent.code == parent_code).scalar_subquery()
    )
    return or_(
        CompanyServiceAreaV12.region_code == lead_region_code,
        CompanyServiceAreaV12.region_code == parent_code,
        CompanyServiceAreaV12.region_code == grandparent_code,
    )


def _service_area_dispatchable():
    """Keep approved coverage active while its removal request is pending."""

    return or_(
        CompanyServiceAreaV12.review_status == "APPROVED",
        and_(
            CompanyServiceAreaV12.review_status == "PENDING",
            CompanyServiceAreaV12.review_note.like(f"{REMOVAL_REQUEST_PREFIX}%"),
        ),
    )


def has_receiver_coverage(db: Session, lead: Lead) -> bool:
    """Check stable local coverage without transient dispatch conditions."""

    if not lead.region_code:
        return False
    base_conditions = [
        Company.status == "ACTIVE",
        CompanyLeadCapability.capability_code == "LEAD_RECEIVER",
        CompanyLeadCapability.active.is_(True),
        CompanyLeadCapability.review_status == "APPROVED",
    ]
    if lead.supplier_company_id:
        base_conditions.append(Company.id != lead.supplier_company_id)

    direct = (
        select(Company.id)
        .join(
            CompanyLeadCapability,
            CompanyLeadCapability.company_id == Company.id,
        )
        .join(
            CompanyServiceAreaV12,
            CompanyServiceAreaV12.company_id == Company.id,
        )
        .where(
            *base_conditions,
            CompanyServiceAreaV12.active.is_(True),
            _service_area_dispatchable(),
            _service_area_region_clause(lead.region_code),
        )
        .limit(1)
    )
    if db.scalar(direct) is not None:
        return True

    lead_region = db.get(Region, lead.region_code)
    if lead_region is None or lead_region.level != "CITY":
        return False
    active_district_count = int(
        db.scalar(
            select(func.count(Region.code)).where(
                Region.parent_code == lead_region.code,
                Region.level == "DISTRICT",
                Region.active.is_(True),
            )
        )
        or 0
    )
    if active_district_count == 0:
        return False
    full_city_coverage = (
        select(Company.id)
        .join(
            CompanyLeadCapability,
            CompanyLeadCapability.company_id == Company.id,
        )
        .join(
            CompanyServiceAreaV12,
            CompanyServiceAreaV12.company_id == Company.id,
        )
        .join(Region, Region.code == CompanyServiceAreaV12.region_code)
        .where(
            *base_conditions,
            CompanyServiceAreaV12.active.is_(True),
            _service_area_dispatchable(),
            Region.parent_code == lead_region.code,
            Region.level == "DISTRICT",
            Region.active.is_(True),
        )
        .group_by(Company.id)
        .having(
            func.count(func.distinct(CompanyServiceAreaV12.region_code))
            == active_district_count
        )
        .limit(1)
    )
    return db.scalar(full_city_coverage) is not None


def approved_lead_pool_target(db: Session, lead: Lead) -> LeadV12Status:
    if (
        lead.source_kind == LeadSourceKind.SUPPLIER_H5.value
        and not has_receiver_coverage(db, lead)
    ):
        return LeadV12Status.PUBLIC_POOL
    return LeadV12Status.READY_DISPATCH


def route_approved_lead_to_pool(db: Session, lead: Lead) -> LeadV12Status:
    target = approved_lead_pool_target(db, lead)
    lead.status = target.value
    lead.pending_reason = (
        "PUBLIC_POOL_NO_LOCAL_RECEIVER"
        if target is LeadV12Status.PUBLIC_POOL
        else None
    )
    return target


def _points_snapshot(
    db: Session,
    company_id: str,
    *,
    lock_account: bool = False,
) -> tuple[int, int, int]:
    """Read balance/reservations without creating an account during a GET.

    Dispatch calls this while the company row and points account are locked, so
    two concurrent manual dispatches cannot reserve the same points twice.
    """

    stmt = select(PointsAccount).where(PointsAccount.company_id == company_id)
    if lock_account:
        stmt = stmt.with_for_update()
    account = db.scalar(stmt)
    balance = int(account.balance) if account is not None else 0
    reserved = db.scalar(
        select(func.coalesce(func.sum(Assignment.points_price), 0)).where(
            Assignment.company_id == company_id,
            Assignment.status == AssignmentStatus.PENDING_CLAIM.value,
        )
    ) or 0
    return balance, int(reserved), balance - int(reserved)


def _receiver_duplicate_assignment(
    db: Session,
    *,
    lead: Lead,
    company_id: str,
    exclude_assignment_id: str | None = None,
    now: datetime | None = None,
    reward_rule: SupplierRewardRule | None = None,
) -> Assignment | None:
    current = as_utc(now) or datetime.now(timezone.utc)
    rule = reward_rule or resolve_supplier_reward_rule(db, as_of=current)
    cutoff = current - timedelta(days=rule.historical_suspect_days)
    match_clauses = [Lead.phone_hash == lead.phone_hash]
    if lead.phone_fingerprint:
        match_clauses.insert(0, Lead.phone_fingerprint == lead.phone_fingerprint)
    filters = [
        Assignment.company_id == company_id,
        Assignment.status.in_(RECEIVER_HISTORY_STATUSES),
        Assignment.claimed_at.is_not(None),
        Assignment.claimed_at >= cutoff,
        Assignment.lead_id != lead.id,
        or_(*match_clauses),
    ]
    if exclude_assignment_id:
        filters.append(Assignment.id != exclude_assignment_id)
    return db.scalar(
        select(Assignment)
        .join(Lead, Lead.id == Assignment.lead_id)
        .where(*filters)
        .order_by(Assignment.claimed_at.desc())
        .limit(1)
    )


def _returned_receiver_company_ids(db: Session, lead_id: str) -> set[str]:
    return {
        str(company_id)
        for company_id in db.scalars(
            select(func.coalesce(Assignment.receiver_company_id, Assignment.company_id)).where(
                Assignment.lead_id == lead_id,
                Assignment.status == AssignmentStatus.RETURNED.value,
            )
        ).all()
        if company_id
    }


def evaluate_candidate(
    db: Session,
    *,
    lead: Lead,
    company: Company,
    lock_account: bool = False,
    reward_rule: SupplierRewardRule | None = None,
    returned_receiver_company_ids: set[str] | None = None,
    allow_returned_receiver: bool = False,
) -> CandidateResult:
    reasons: list[str] = []
    if company.status != "ACTIVE":
        reasons.append("COMPANY_INACTIVE")
    if not has_lead_capability(db, company.id, "LEAD_RECEIVER"):
        reasons.append("RECEIVER_CAPABILITY_REQUIRED")
    if lead.supplier_company_id and lead.supplier_company_id == company.id:
        reasons.append("SELF_SUPPLY_FORBIDDEN")
    returned_receiver_company_ids = (
        _returned_receiver_company_ids(db, lead.id)
        if returned_receiver_company_ids is None
        else returned_receiver_company_ids
    )
    if company.id in returned_receiver_company_ids and not allow_returned_receiver:
        reasons.append("RETURNED_RECEIVER_EXCLUDED")
    region_match = _region_matches(db, company.id, lead)
    if not region_match:
        reasons.append("SERVICE_REGION_MISMATCH")
    duplicate_assignment = _receiver_duplicate_assignment(
        db,
        lead=lead,
        company_id=company.id,
        reward_rule=reward_rule,
    )
    if duplicate_assignment is not None:
        reasons.append("DUPLICATE_TO_RECEIVER")
    points_price, price_rule = resolve_price(db, lead, company)
    balance, reserved, available = _points_snapshot(
        db,
        company.id,
        lock_account=lock_account,
    )
    if available < points_price:
        reasons.append("POINTS_INSUFFICIENT")
    return CandidateResult(
        company_id=company.id,
        company_name=company.name,
        eligible=not reasons,
        exclusion_reasons=tuple(reasons),
        points_price=points_price,
        price_rule_id=price_rule.id if price_rule else None,
        price_version=price_rule.version if price_rule else 1,
        points_balance=balance,
        points_reserved=reserved,
        points_available=available,
        region_match=region_match,
        duplicate_to_receiver=duplicate_assignment is not None,
    )


def existing_receiver_correction_issues(
    db: Session,
    *,
    lead: Lead,
    assignment: Assignment,
) -> list[str]:
    """Recheck facts that can invalidate the current receiver after correction.

    Historical pricing and points are intentionally excluded: a factual correction
    must not rewrite an already-created assignment's commercial snapshot.
    """

    company_id = assignment.receiver_company_id or assignment.company_id
    company = db.get(Company, company_id)
    issues: list[str] = []
    if company is None:
        return ["RECEIVER_COMPANY_MISSING"]
    if company.status != "ACTIVE":
        issues.append("COMPANY_INACTIVE")
    if not has_lead_capability(db, company.id, "LEAD_RECEIVER"):
        issues.append("RECEIVER_CAPABILITY_REQUIRED")
    if lead.supplier_company_id and lead.supplier_company_id == company.id:
        issues.append("SELF_SUPPLY_FORBIDDEN")
    if not _region_matches(db, company.id, lead):
        issues.append("SERVICE_REGION_MISMATCH")
    if _receiver_duplicate_assignment(
        db,
        lead=lead,
        company_id=company.id,
        exclude_assignment_id=assignment.id,
    ) is not None:
        issues.append("DUPLICATE_TO_RECEIVER")
    return issues


def count_candidates(db: Session, *, keyword: str | None = None) -> int:
    stmt = select(func.count(Company.id))
    normalized_keyword = (keyword or "").strip()
    if normalized_keyword:
        stmt = stmt.where(
            or_(
                Company.name.contains(normalized_keyword, autoescape=True),
                Company.code.contains(normalized_keyword, autoescape=True),
            )
        )
    return int(db.scalar(stmt) or 0)


def list_candidates(
    db: Session,
    *,
    lead: Lead,
    keyword: str | None = None,
    page_no: int | None = None,
    page_size: int | None = None,
) -> list[CandidateResult]:
    capable_companies = (
        select(CompanyLeadCapability.company_id)
        .where(
            CompanyLeadCapability.capability_code == "LEAD_RECEIVER",
            CompanyLeadCapability.active.is_(True),
            CompanyLeadCapability.review_status == "APPROVED",
        )
        .distinct()
        .subquery()
    )
    reserved_by_company = (
        select(
            Assignment.company_id,
            func.coalesce(func.sum(Assignment.points_price), 0).label("points_reserved"),
        )
        .where(Assignment.status == AssignmentStatus.PENDING_CLAIM.value)
        .group_by(Assignment.company_id)
        .subquery()
    )
    returned_receiver_companies = (
        select(
            func.coalesce(Assignment.receiver_company_id, Assignment.company_id).label(
                "company_id"
            )
        )
        .where(
            Assignment.lead_id == lead.id,
            Assignment.status == AssignmentStatus.RETURNED.value,
        )
        .distinct()
        .subquery()
    )
    reward_rule = resolve_supplier_reward_rule(db)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=reward_rule.historical_suspect_days)
    match_clauses = [Lead.phone_hash == lead.phone_hash]
    if lead.phone_fingerprint:
        match_clauses.insert(0, Lead.phone_fingerprint == lead.phone_fingerprint)
    duplicate_receiver_companies = (
        select(Assignment.company_id)
        .join(Lead, Lead.id == Assignment.lead_id)
        .where(
            Assignment.status.in_(RECEIVER_HISTORY_STATUSES),
            Assignment.claimed_at.is_not(None),
            Assignment.claimed_at >= cutoff,
            Assignment.lead_id != lead.id,
            or_(*match_clauses),
        )
        .distinct()
        .subquery()
    )
    candidate_price = (
        select(LeadPriceRule.points_cost)
        .where(
            LeadPriceRule.status == "PUBLISHED",
            or_(LeadPriceRule.effective_at.is_(None), LeadPriceRule.effective_at <= now),
            or_(LeadPriceRule.expires_at.is_(None), LeadPriceRule.expires_at > now),
            or_(LeadPriceRule.region_code.is_(None), LeadPriceRule.region_code == lead.region_code),
            or_(LeadPriceRule.category_code.is_(None), LeadPriceRule.category_code == lead.category_code),
            or_(LeadPriceRule.brand_code.is_(None), LeadPriceRule.brand_code == lead.brand_code),
            or_(LeadPriceRule.level_code.is_(None), LeadPriceRule.level_code == Company.level_code),
        )
        .order_by(
            LeadPriceRule.priority.asc(),
            LeadPriceRule.region_code.is_(None).asc(),
            LeadPriceRule.category_code.is_(None).asc(),
            LeadPriceRule.brand_code.is_(None).asc(),
            LeadPriceRule.level_code.is_(None).asc(),
            LeadPriceRule.version.desc(),
        )
        .limit(1)
        .correlate(Company)
        .scalar_subquery()
    )
    company_stmt = (
        select(
            Company,
            capable_companies.c.company_id.label("capable_company_id"),
            PointsAccount.balance.label("points_balance"),
            func.coalesce(reserved_by_company.c.points_reserved, 0).label("points_reserved"),
            returned_receiver_companies.c.company_id.label("returned_receiver_company_id"),
            duplicate_receiver_companies.c.company_id.label("duplicate_company_id"),
        )
        .outerjoin(capable_companies, capable_companies.c.company_id == Company.id)
        .outerjoin(PointsAccount, PointsAccount.company_id == Company.id)
        .outerjoin(reserved_by_company, reserved_by_company.c.company_id == Company.id)
        .outerjoin(
            returned_receiver_companies,
            returned_receiver_companies.c.company_id == Company.id,
        )
        .outerjoin(
            duplicate_receiver_companies,
            duplicate_receiver_companies.c.company_id == Company.id,
        )
    )
    normalized_keyword = (keyword or "").strip()
    if normalized_keyword:
        company_stmt = company_stmt.where(
            or_(
                Company.name.contains(normalized_keyword, autoescape=True),
                Company.code.contains(normalized_keyword, autoescape=True),
            )
        )
    if lead.region_code:
        direct_region_companies = (
            select(CompanyServiceAreaV12.company_id)
            .where(
                _service_area_region_clause(lead.region_code),
                CompanyServiceAreaV12.active.is_(True),
                _service_area_dispatchable(),
            )
        )
        lead_region = db.get(Region, lead.region_code)
        active_district_count = 0
        if lead_region is not None and lead_region.level == "CITY":
            active_district_count = int(
                db.scalar(
                    select(func.count(Region.code)).where(
                        Region.parent_code == lead_region.code,
                        Region.level == "DISTRICT",
                        Region.active.is_(True),
                    )
                )
                or 0
            )
        if active_district_count:
            all_district_region_companies = (
                select(CompanyServiceAreaV12.company_id)
                .join(Region, Region.code == CompanyServiceAreaV12.region_code)
                .where(
                    CompanyServiceAreaV12.active.is_(True),
                    _service_area_dispatchable(),
                    Region.parent_code == lead.region_code,
                    Region.level == "DISTRICT",
                    Region.active.is_(True),
                )
                .group_by(CompanyServiceAreaV12.company_id)
                .having(
                    func.count(func.distinct(CompanyServiceAreaV12.region_code))
                    == active_district_count
                )
            )
            region_companies = direct_region_companies.union(
                all_district_region_companies
            ).subquery()
        else:
            region_companies = direct_region_companies.distinct().subquery()
        company_stmt = company_stmt.add_columns(
            region_companies.c.company_id.label("region_company_id")
        ).outerjoin(region_companies, region_companies.c.company_id == Company.id)
        eligible_conditions = [
            Company.status == "ACTIVE",
            capable_companies.c.company_id.is_not(None),
            returned_receiver_companies.c.company_id.is_(None),
            duplicate_receiver_companies.c.company_id.is_(None),
            region_companies.c.company_id.is_not(None),
            (
                func.coalesce(PointsAccount.balance, 0)
                - func.coalesce(reserved_by_company.c.points_reserved, 0)
            )
            >= func.coalesce(candidate_price, 100),
        ]
        if lead.supplier_company_id:
            eligible_conditions.append(Company.id != lead.supplier_company_id)
        company_stmt = company_stmt.order_by(
            region_companies.c.company_id.is_(None).asc(),
            case((and_(*eligible_conditions), 0), else_=1).asc(),
            func.lower(Company.name).asc(),
            Company.name.asc(),
            Company.id.asc(),
        )
    else:
        company_stmt = company_stmt.add_columns(literal(None).label("region_company_id"))
        company_stmt = company_stmt.order_by(
            func.lower(Company.name).asc(),
            Company.name.asc(),
            Company.id.asc(),
        )
    if page_no is not None and page_size is not None:
        company_stmt = company_stmt.offset((page_no - 1) * page_size).limit(page_size)
    company_rows = db.execute(company_stmt).all()
    companies = [row[0] for row in company_rows]
    if not companies:
        return []
    capable_company_ids = {row.capable_company_id for row in company_rows if row.capable_company_id}
    region_company_ids = {row.region_company_id for row in company_rows if row.region_company_id}
    duplicate_company_ids = {
        row.duplicate_company_id for row in company_rows if row.duplicate_company_id
    }
    returned_receiver_company_ids = {
        row.returned_receiver_company_id
        for row in company_rows
        if row.returned_receiver_company_id
    }
    price_rules = db.scalars(
        select(LeadPriceRule)
        .where(
            LeadPriceRule.status == "PUBLISHED",
            or_(LeadPriceRule.effective_at.is_(None), LeadPriceRule.effective_at <= now),
            or_(LeadPriceRule.expires_at.is_(None), LeadPriceRule.expires_at > now),
            or_(LeadPriceRule.region_code.is_(None), LeadPriceRule.region_code == lead.region_code),
            or_(LeadPriceRule.category_code.is_(None), LeadPriceRule.category_code == lead.category_code),
            or_(LeadPriceRule.brand_code.is_(None), LeadPriceRule.brand_code == lead.brand_code),
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
    balances = {row[0].id: int(row.points_balance or 0) for row in company_rows}
    reserved = {row[0].id: int(row.points_reserved or 0) for row in company_rows}
    results: list[CandidateResult] = []
    for company in companies:
        reasons: list[str] = []
        if company.status != "ACTIVE":
            reasons.append("COMPANY_INACTIVE")
        if company.id not in capable_company_ids:
            reasons.append("RECEIVER_CAPABILITY_REQUIRED")
        if lead.supplier_company_id and lead.supplier_company_id == company.id:
            reasons.append("SELF_SUPPLY_FORBIDDEN")
        if company.id in returned_receiver_company_ids:
            reasons.append("RETURNED_RECEIVER_EXCLUDED")
        region_match = company.id in region_company_ids
        if not region_match:
            reasons.append("SERVICE_REGION_MISMATCH")
        duplicate_to_receiver = company.id in duplicate_company_ids
        if duplicate_to_receiver:
            reasons.append("DUPLICATE_TO_RECEIVER")
        price_rule = next(
            (rule for rule in price_rules if rule.level_code is None or rule.level_code == company.level_code),
            None,
        )
        points_price = int(price_rule.points_cost) if price_rule else 100
        points_balance = int(balances.get(company.id, 0))
        points_reserved = int(reserved.get(company.id, 0))
        points_available = points_balance - points_reserved
        if points_available < points_price:
            reasons.append("POINTS_INSUFFICIENT")
        results.append(
            CandidateResult(
                company_id=company.id,
                company_name=company.name,
                eligible=not reasons,
                exclusion_reasons=tuple(reasons),
                points_price=points_price,
                price_rule_id=price_rule.id if price_rule else None,
                price_version=price_rule.version if price_rule else 1,
                points_balance=points_balance,
                points_reserved=points_reserved,
                points_available=points_available,
                region_match=region_match,
                duplicate_to_receiver=duplicate_to_receiver,
            )
        )
    return results


def list_dispatch_pool(
    db: Session,
    *,
    region_code: str | None = None,
    source_kind: str | None = None,
    page_no: int = 1,
    page_size: int = 20,
) -> tuple[list[Lead], int]:
    filters = [
        Lead.status == LeadV12Status.READY_DISPATCH.value,
        Lead.current_assignment_id.is_(None),
        Lead.deleted_at.is_(None),
    ]
    if region_code:
        filters.append(Lead.region_code == region_code)
    if source_kind:
        filters.append(Lead.source_kind == source_kind)
    total = db.scalar(select(func.count(Lead.id)).where(*filters)) or 0
    items = db.scalars(
        select(Lead)
        .where(*filters)
        .order_by(Lead.submitted_at.asc(), Lead.created_at.asc())
        .offset((page_no - 1) * page_size)
        .limit(page_size)
    ).all()
    return list(items), int(total)


def dispatch_manually_with_outcome(
    db: Session,
    *,
    lead_id: str,
    company_id: str,
    employee_user_id: str | None = None,
    assigned_by: str,
    idempotency_key: str,
    note: str | None = None,
    return_receiver_override: bool = False,
    return_receiver_override_reason: str | None = None,
) -> ManualDispatchOutcome:
    existing = db.scalar(select(Assignment).where(Assignment.idempotency_key == idempotency_key))
    if existing:
        if (
            existing.lead_id != lead_id
            or existing.company_id != company_id
            or existing.internal_assignee_user_id != employee_user_id
        ):
            raise AppError("IDEMPOTENCY_CONFLICT", "幂等键已被其他派发请求使用", 409)
        return ManualDispatchOutcome(assignment=existing, created=False)

    lead = get_dispatch_lead(db, lead_id, lock=True)

    # A same-key request may have completed while this transaction waited for
    # the lead lock. Recheck before validating the now-transitioned lead state.
    existing = db.scalar(select(Assignment).where(Assignment.idempotency_key == idempotency_key))
    if existing:
        if (
            existing.lead_id != lead_id
            or existing.company_id != company_id
            or existing.internal_assignee_user_id != employee_user_id
        ):
            raise AppError("IDEMPOTENCY_CONFLICT", "幂等键已被其他派发请求使用", 409)
        return ManualDispatchOutcome(assignment=existing, created=False)

    if lead.status != LeadV12Status.READY_DISPATCH.value or lead.current_assignment_id:
        raise AppError(
            "LEAD_NOT_READY_DISPATCH",
            "客资当前不在待派发池",
            409,
            {"status": lead.status, "current_assignment_id": lead.current_assignment_id},
        )
    active = db.scalar(
        select(Assignment).where(
            Assignment.lead_id == lead.id,
            Assignment.status.in_(ACTIVE_ASSIGNMENT_STATUS_VALUES),
        )
    )
    if active:
        raise AppError("LEAD_ALREADY_ASSIGNED", "客资已有有效派发单", 409, {"assignment_id": active.id})

    # Serialize point reservations for this receiver. Company creation normally
    # creates the points account; locking the company also covers missing legacy
    # accounts without introducing a read-side mutation.
    company = db.scalar(select(Company).where(Company.id == company_id).with_for_update())
    if company is None:
        raise AppError("COMPANY_NOT_FOUND", "目标公司不存在", 404)
    recipients = (
        resolve_direct_dispatch_recipients(
            db,
            company_id=company.id,
            employee_user_id=employee_user_id,
        )
        if employee_user_id
        else None
    )
    returned_receiver_company_ids = _returned_receiver_company_ids(db, lead.id)
    is_returned_receiver = company.id in returned_receiver_company_ids
    if return_receiver_override and not is_returned_receiver:
        raise AppError(
            "RETURN_RECEIVER_OVERRIDE_NOT_APPLICABLE",
            "仅再次派发给原领取公司时可使用例外派发",
            409,
        )
    if is_returned_receiver and return_receiver_override and not (
        return_receiver_override_reason and return_receiver_override_reason.strip()
    ):
        raise AppError(
            "RETURN_RECEIVER_OVERRIDE_REASON_REQUIRED",
            "再次派发给原领取公司必须填写例外原因",
            422,
        )
    candidate = evaluate_candidate(
        db,
        lead=lead,
        company=company,
        lock_account=True,
        returned_receiver_company_ids=returned_receiver_company_ids,
        allow_returned_receiver=return_receiver_override,
    )
    if not candidate.eligible:
        raise AppError(
            "DISPATCH_CANDIDATE_INELIGIBLE",
            "目标公司不符合本次派发条件",
            409,
            {"reasons": list(candidate.exclusion_reasons)},
        )

    now = datetime.now(timezone.utc)
    phone = decrypt_text(lead.phone_encrypted)
    assignment = Assignment(
        lead_id=lead.id,
        company_id=company.id,
        receiver_company_id=company.id,
        supplier_company_id=lead.supplier_company_id,
        status=AssignmentStatus.PENDING_CLAIM.value,
        points_price=candidate.points_price,
        claim_points=candidate.points_price,
        price_rule_id=candidate.price_rule_id,
        price_version=candidate.price_version,
        lead_snapshot={
            "customer_name": lead.customer_name,
            "phone_masked": mask_phone(phone),
            "region_code": lead.region_code,
            "city": lead.city,
            "district": lead.district,
            "category_code": lead.category_code,
            "brand_code": lead.brand_code,
            "need_summary": lead.need_summary,
            "source_kind": lead.source_kind,
            "duplicate_status": lead.duplicate_status,
            "note": note.strip() if note else None,
        },
        assigned_by=assigned_by,
        assigned_at=now,
        internal_assignee_user_id=recipients.employee.id if recipients else None,
        internal_assigned_by=assigned_by if recipients else None,
        internal_assigned_at=now if recipients else None,
        expires_at=now + timedelta(hours=settings.assignment_expire_hours),
        idempotency_key=idempotency_key,
    )
    db.add(assignment)
    db.flush()
    lead.status = LeadV12Status.DISPATCHED.value
    lead.current_assignment_id = assignment.id
    db.add(
        AssignmentEvent(
            assignment_id=assignment.id,
            event_type="V12_MANUAL_DISPATCH",
            actor_user_id=assigned_by,
            payload={
                "lead_id": lead.id,
                "company_id": company.id,
                "employee_user_id": recipients.employee.id if recipients else None,
                "owner_user_id": recipients.owner.id if recipients else None,
                "points_price": candidate.points_price,
                "price_rule_id": candidate.price_rule_id,
                "manual": True,
                "return_receiver_override": is_returned_receiver,
                "return_receiver_override_reason": (
                    return_receiver_override_reason.strip()
                    if is_returned_receiver and return_receiver_override_reason
                    else None
                ),
            },
        )
    )
    db.flush()
    return ManualDispatchOutcome(assignment=assignment, created=True)


def dispatch_manually(
    db: Session,
    *,
    lead_id: str,
    company_id: str,
    employee_user_id: str | None = None,
    assigned_by: str,
    idempotency_key: str,
    note: str | None = None,
) -> Assignment:
    return dispatch_manually_with_outcome(
        db,
        lead_id=lead_id,
        company_id=company_id,
        employee_user_id=employee_user_id,
        assigned_by=assigned_by,
        idempotency_key=idempotency_key,
        note=note,
    ).assignment


def _reward_for_claim(
    db: Session,
    *,
    lead: Lead,
    assignment: Assignment,
    now: datetime,
) -> SupplierLeadReward | None:
    if not lead.supplier_company_id or lead.supplier_company_id == assignment.company_id:
        return None
    existing = db.scalar(
        select(SupplierLeadReward).where(SupplierLeadReward.assignment_id == assignment.id)
    )
    if existing:
        return existing
    eligible = lead.duplicate_status not in {
        DuplicateDecision.HARD_DUPLICATE.value,
        DuplicateDecision.REWARD_DUPLICATE.value,
    }
    rule = resolve_supplier_reward_rule(db, as_of=now)
    reward_points = calculate_reward_points(int(assignment.points_price), rule) if eligible else 0
    reward_status = (
        RewardStatus.WAITING_CLAIM.value
        if eligible and reward_points > 0
        else RewardStatus.NOT_ELIGIBLE.value
    )
    reward = SupplierLeadReward(
        lead_id=lead.id,
        assignment_id=assignment.id,
        supplier_company_id=lead.supplier_company_id,
        receiver_company_id=assignment.company_id,
        status=reward_status,
        claim_points=int(assignment.points_price),
        reward_ratio_bps=rule.ratio_bps,
        reward_points=reward_points,
        rule_version=rule.version,
        rule_snapshot_json=rule.snapshot(),
        # 领取只代表接收方取得了客资；供资奖励要等有效确认后才开始结算。
        observed_at=None,
        appeal_deadline_at=None,
        reward_due_at=None,
        exception_reason=(
            None
            if reward_status == RewardStatus.WAITING_CLAIM.value
            else "REWARD_DUPLICATE" if not eligible else "ZERO_REWARD_POINTS"
        ),
    )
    db.add(reward)
    db.flush()
    return reward


def claim_assignment(
    db: Session,
    *,
    assignment_id: str,
    company_id: str,
    claimed_by: str,
) -> ClaimResult:
    assignment = db.scalar(
        select(Assignment)
        .where(Assignment.id == assignment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if assignment is None:
        raise AppError("ASSIGNMENT_NOT_FOUND", "派发单不存在", 404)
    if assignment.company_id != company_id:
        raise AppError("ASSIGNMENT_FORBIDDEN", "无权领取其他公司的派发单", 403)
    if (
        assignment.internal_assignee_user_id
        and assignment.internal_assignee_user_id != claimed_by
    ):
        raise AppError("ASSIGNMENT_EMPLOYEE_FORBIDDEN", "该客资仅限被指定员工领取", 403)

    lead = get_dispatch_lead(db, assignment.lead_id, lock=True)
    require_correction_review_resolved(lead)
    existing_ledger = db.scalar(
        select(PointsLedger).where(
            PointsLedger.company_id == company_id,
            PointsLedger.idempotency_key == f"v12-claim:{assignment.id}",
        )
    )
    if assignment.status in CLAIMED_CONTACT_STATUSES:
        if existing_ledger is None:
            raise AppError("CLAIM_LEDGER_MISSING", "派发单已领取但积分流水缺失", 500)
        reward = db.scalar(
            select(SupplierLeadReward).where(SupplierLeadReward.assignment_id == assignment.id)
        )
        return ClaimResult(
            assignment=assignment,
            ledger=existing_ledger,
            reward=reward,
            phone=decrypt_text(lead.phone_encrypted),
            idempotent=True,
        )
    if assignment.status != AssignmentStatus.PENDING_CLAIM.value:
        raise AppError(
            "ASSIGNMENT_NOT_CLAIMABLE",
            "派发单当前不可领取",
            409,
            {"status": assignment.status},
        )

    now = datetime.now(timezone.utc)
    expires_at = as_utc(assignment.expires_at)
    if expires_at and expires_at <= now:
        raise AppError("ASSIGNMENT_EXPIRED", "派发单已过期", 409)
    require_lead_capability(db, company_id, "LEAD_RECEIVER")
    if lead.current_assignment_id != assignment.id or lead.status != LeadV12Status.DISPATCHED.value:
        raise AppError("LEAD_ASSIGNMENT_CONFLICT", "客资与派发单状态不一致", 409)
    if lead.supplier_company_id and lead.supplier_company_id == company_id:
        raise AppError("SELF_SUPPLY_FORBIDDEN", "供应商不得领取自己上传的客资", 409)
    if not _region_matches(db, company_id, lead):
        raise AppError("SERVICE_REGION_MISMATCH", "公司当前服务区域已不匹配该客资", 409)
    duplicate = _receiver_duplicate_assignment(
        db,
        lead=lead,
        company_id=company_id,
        exclude_assignment_id=assignment.id,
        now=now,
    )
    if duplicate:
        raise AppError(
            "DUPLICATE_TO_RECEIVER",
            "该客户已由当前公司历史领取",
            409,
            {"assignment_id": duplicate.id},
        )

    account = db.scalar(
        select(PointsAccount).where(PointsAccount.company_id == company_id).with_for_update()
    )
    if account is None or int(account.balance) < int(assignment.points_price):
        raise AppError(
            "POINTS_INSUFFICIENT",
            "积分不足，无法领取客资",
            409,
            {"balance": int(account.balance) if account else 0, "required": int(assignment.points_price)},
        )
    ledger = change_points(
        db,
        company_id=company_id,
        delta=-int(assignment.points_price),
        ledger_type=PointsLedgerType.CLAIM.value,
        business_type="V12_ASSIGNMENT_CLAIM",
        business_id=assignment.id,
        idempotency_key=f"v12-claim:{assignment.id}",
        created_by=claimed_by,
        metadata={"lead_id": lead.id, "points_price": int(assignment.points_price)},
    )

    deadline = WorkdayCalendarService(db).add_workdays(now, 3)
    assignment.status = AssignmentStatus.CLAIMED.value
    assignment.claimed_at = now
    assignment.internal_assignee_user_id = claimed_by
    assignment.internal_assigned_by = claimed_by
    assignment.internal_assigned_at = now
    assignment.claim_points = int(assignment.points_price)
    assignment.appeal_deadline_at = deadline
    assignment.reward_due_at = deadline
    assignment.receiver_company_id = company_id
    assignment.supplier_company_id = lead.supplier_company_id
    assignment.first_followup_due_at = now + timedelta(hours=settings.first_followup_hours)
    lead.status = LeadV12Status.CLAIMED.value
    lead.current_follow_status = "UNCONTACTED"
    reward = _reward_for_claim(db, lead=lead, assignment=assignment, now=now)
    db.add(
        AssignmentEvent(
            assignment_id=assignment.id,
            event_type="V12_CLAIMED",
            actor_user_id=claimed_by,
            payload={
                "lead_id": lead.id,
                "company_id": company_id,
                "points": int(assignment.points_price),
                "ledger_id": ledger.id,
                "appeal_deadline_at": deadline.isoformat(),
                "reward_id": reward.id if reward else None,
                "internal_assignee_user_id": claimed_by,
            },
        )
    )
    db.flush()
    return ClaimResult(
        assignment=assignment,
        ledger=ledger,
        reward=reward,
        phone=decrypt_text(lead.phone_encrypted),
        idempotent=False,
    )


def refuse_pending_assignment(
    db: Session,
    *,
    assignment_id: str,
    company_id: str,
    refused_by: str,
    reason: str,
) -> tuple[Assignment, Lead]:
    assignment = db.scalar(
        select(Assignment)
        .where(Assignment.id == assignment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if assignment is None:
        raise AppError("ASSIGNMENT_NOT_FOUND", "派发单不存在", 404)
    if assignment.company_id != company_id:
        raise AppError("ASSIGNMENT_FORBIDDEN", "无权拒绝其他公司的派发单", 403)
    if (
        assignment.internal_assignee_user_id
        and assignment.internal_assignee_user_id != refused_by
    ):
        raise AppError("ASSIGNMENT_EMPLOYEE_FORBIDDEN", "该客资仅限被指定员工拒绝领取", 403)
    if assignment.status != AssignmentStatus.PENDING_CLAIM.value:
        raise AppError(
            "ASSIGNMENT_NOT_REFUSABLE",
            "仅待领取派发单可拒绝领取",
            409,
            {"status": assignment.status},
        )
    lead = get_dispatch_lead(db, assignment.lead_id, lock=True)
    require_correction_review_resolved(lead)
    if lead.current_assignment_id != assignment.id or lead.status != LeadV12Status.DISPATCHED.value:
        raise AppError("LEAD_ASSIGNMENT_CONFLICT", "客资与派发单状态不一致", 409)

    now = datetime.now(timezone.utc)
    assignment.status = AssignmentStatus.RELEASED.value
    assignment.released_at = now
    assignment.release_reason = "REFUSED_CLAIM"
    lead.status = LeadV12Status.READY_DISPATCH.value
    lead.current_assignment_id = None
    db.add(
        AssignmentEvent(
            assignment_id=assignment.id,
            event_type="V12_ASSIGNMENT_REFUSED",
            actor_user_id=refused_by,
            payload={
                "lead_id": lead.id,
                "company_id": company_id,
                "reason": reason,
            },
        )
    )
    db.flush()
    return assignment, lead


def lead_pool_item(
    lead: Lead,
    *,
    reveal_phone: bool = False,
    supplier_company_name: str | None = None,
    submitter_name: str | None = None,
) -> dict[str, Any]:
    phone = decrypt_text(lead.phone_encrypted)
    return {
        "id": lead.id,
        "customer_name": lead.customer_name,
        "phone": phone if reveal_phone else None,
        "phone_masked": mask_phone(phone),
        "city": lead.city,
        "district": lead.district,
        "region_code": lead.region_code,
        "category_code": lead.category_code,
        "brand_code": lead.brand_code,
        "need_summary": lead.need_summary,
        "source_kind": lead.source_kind,
        "supplier_company_id": lead.supplier_company_id,
        "supplier_company_name": supplier_company_name,
        "submitter_user_id": lead.submitter_user_id,
        "submitter_name": submitter_name,
        "duplicate_status": lead.duplicate_status,
        "status": lead.status,
        "submitted_at": lead.submitted_at.isoformat() if lead.submitted_at else None,
        "created_at": lead.created_at.isoformat(),
    }


def candidate_to_dict(item: CandidateResult, *, include_financials: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "company_id": item.company_id,
        "company_name": item.company_name,
        "eligible": item.eligible,
        "exclusion_reasons": list(item.exclusion_reasons),
        "points_price": item.points_price,
        "price_rule_id": item.price_rule_id,
        "price_version": item.price_version,
        "region_match": item.region_match,
        "duplicate_to_receiver": item.duplicate_to_receiver,
    }
    if include_financials:
        data.update(
            {
                "points_balance": item.points_balance,
                "points_reserved": item.points_reserved,
                "points_available": item.points_available,
            }
        )
    return data
