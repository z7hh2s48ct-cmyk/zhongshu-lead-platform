from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.errors import AppError
from ..core.models import Company, CompanyCapability, CompanyServiceRegion, Region
from ..core.models_v12 import CompanyLeadCapability, CompanyServiceAreaV12
from ..core.v12_enums import CompanyLeadCapabilityCode
from .company_service import (
    default_receiver_categories,
    materialize_nationwide_regions,
    service_region_city_code,
)


VALID_CAPABILITIES = {item.value for item in CompanyLeadCapabilityCode}
VALID_REVIEW_STATUSES = {"PENDING", "APPROVED", "REJECTED"}
REMOVAL_REQUEST_PREFIX = "[REMOVE_REQUEST]"


def require_active_company(db: Session, company_id: str | None) -> Company:
    if not company_id:
        raise AppError("COMPANY_REQUIRED", "当前账号未绑定公司", 403)
    company = db.get(Company, company_id)
    if not company or company.status != "ACTIVE":
        raise AppError("COMPANY_INACTIVE", "公司不存在或已停用", 403)
    return company


def has_lead_capability(db: Session, company_id: str, capability_code: str) -> bool:
    return db.scalar(
        select(CompanyLeadCapability.id).where(
            CompanyLeadCapability.company_id == company_id,
            CompanyLeadCapability.capability_code == capability_code,
            CompanyLeadCapability.active.is_(True),
            CompanyLeadCapability.review_status == "APPROVED",
        )
    ) is not None


def require_lead_capability(db: Session, company_id: str | None, capability_code: str) -> None:
    require_active_company(db, company_id)
    if not has_lead_capability(db, company_id or "", capability_code):
        raise AppError(
            "COMPANY_CAPABILITY_REQUIRED",
            "当前公司尚未获得对应客资能力",
            403,
            {"capability_code": capability_code},
        )


def list_capabilities(db: Session, company_id: str) -> list[CompanyLeadCapability]:
    return list(
        db.scalars(
            select(CompanyLeadCapability)
            .where(CompanyLeadCapability.company_id == company_id)
            .order_by(CompanyLeadCapability.capability_code)
        ).all()
    )


def request_capability(db: Session, company_id: str, capability_code: str) -> CompanyLeadCapability:
    require_active_company(db, company_id)
    code = capability_code.strip().upper()
    if code not in VALID_CAPABILITIES:
        raise AppError("CAPABILITY_INVALID", "公司客资能力编码无效", 422)
    item = db.scalar(
        select(CompanyLeadCapability).where(
            CompanyLeadCapability.company_id == company_id,
            CompanyLeadCapability.capability_code == code,
        )
    )
    if item is None:
        item = CompanyLeadCapability(
            company_id=company_id,
            capability_code=code,
            active=False,
            review_status="PENDING",
        )
        db.add(item)
    elif item.review_status != "APPROVED" or not item.active:
        item.review_status = "PENDING"
        item.active = False
        item.reviewed_by = None
        item.reviewed_at = None
        item.review_note = None
    db.flush()
    return item


def review_capability(
    db: Session,
    *,
    company_id: str,
    capability_code: str,
    approve: bool,
    reviewed_by: str,
    note: str | None = None,
) -> CompanyLeadCapability:
    item = db.scalar(
        select(CompanyLeadCapability).where(
            CompanyLeadCapability.company_id == company_id,
            CompanyLeadCapability.capability_code == capability_code.strip().upper(),
        )
    )
    if item is None:
        raise AppError("CAPABILITY_REQUEST_NOT_FOUND", "公司客资能力申请不存在", 404)
    item.review_status = "APPROVED" if approve else "REJECTED"
    item.active = approve
    item.reviewed_by = reviewed_by
    item.reviewed_at = datetime.now(timezone.utc)
    item.review_note = note.strip() if note else None
    _sync_legacy_receiver_capability(db, item)
    db.flush()
    return item


def configure_capability(
    db: Session,
    *,
    company_id: str,
    capability_code: str,
    active: bool,
    reviewed_by: str,
    note: str | None = None,
) -> CompanyLeadCapability:
    """Set a franchise capability from the platform configuration page.

    New companies have receiving enabled by the creation flow. Supplier access is
    intentionally opt-in here so the platform can verify the commercial setup
    before the company uploads leads.
    """

    require_active_company(db, company_id)
    code = capability_code.strip().upper()
    if code not in VALID_CAPABILITIES:
        raise AppError("CAPABILITY_INVALID", "公司客资能力编码无效", 422)
    item = db.scalar(
        select(CompanyLeadCapability).where(
            CompanyLeadCapability.company_id == company_id,
            CompanyLeadCapability.capability_code == code,
        )
    )
    if item is None:
        item = CompanyLeadCapability(
            company_id=company_id,
            capability_code=code,
        )
        db.add(item)
    item.active = active
    item.review_status = "APPROVED"
    item.reviewed_by = reviewed_by
    item.reviewed_at = datetime.now(timezone.utc)
    item.review_note = note or "平台后台配置"
    _sync_legacy_receiver_capability(db, item)
    db.flush()
    return item


def approve_pending_profile(
    db: Session,
    *,
    company_id: str,
    reviewed_by: str,
    note: str | None = None,
) -> tuple[list[CompanyLeadCapability], list[CompanyServiceAreaV12]]:
    """Approve every pending opening item for one company in one transaction.

    A removal request is deliberately excluded: approving it turns off an
    already-live dispatch region and therefore still needs a separate review.
    """

    capabilities = list(
        db.scalars(
            select(CompanyLeadCapability)
            .where(
                CompanyLeadCapability.company_id == company_id,
                CompanyLeadCapability.review_status == "PENDING",
            )
            .order_by(CompanyLeadCapability.capability_code)
        ).all()
    )
    pending_areas = list(
        db.scalars(
            select(CompanyServiceAreaV12)
            .where(
                CompanyServiceAreaV12.company_id == company_id,
                CompanyServiceAreaV12.review_status == "PENDING",
            )
            .order_by(CompanyServiceAreaV12.is_primary_city.desc(), CompanyServiceAreaV12.region_code)
        ).all()
    )
    opening_areas = [
        item
        for item in pending_areas
        if not str(item.review_note or "").startswith(REMOVAL_REQUEST_PREFIX)
    ]
    if not capabilities and not opening_areas:
        raise AppError(
            "COMPANY_PROFILE_NOT_PENDING",
            "该公司没有可一键通过的待开通申请",
            409,
        )

    reviewed_capabilities = [
        review_capability(
            db,
            company_id=company_id,
            capability_code=item.capability_code,
            approve=True,
            reviewed_by=reviewed_by,
            note=note,
        )
        for item in capabilities
    ]
    reviewed_areas = [
        review_service_area(
            db,
            area_id=item.id,
            approve=True,
            reviewed_by=reviewed_by,
            note=note,
        )
        for item in opening_areas
    ]
    return reviewed_capabilities, reviewed_areas


def _sync_legacy_receiver_capability(db: Session, item: CompanyLeadCapability) -> None:
    if item.capability_code != CompanyLeadCapabilityCode.LEAD_RECEIVER.value:
        return
    existing = list(
        db.scalars(
            select(CompanyCapability).where(
                CompanyCapability.company_id == item.company_id
            )
        ).all()
    )
    if item.review_status != "APPROVED" or not item.active:
        for capability in existing:
            capability.active = False
        return
    generic_by_category = {
        capability.category_code: capability
        for capability in existing
        if capability.brand_code is None
    }
    for category_code in default_receiver_categories(db):
        capability = generic_by_category.get(category_code)
        if capability is None:
            db.add(
                CompanyCapability(
                    company_id=item.company_id,
                    category_code=category_code,
                    brand_code=None,
                    active=True,
                )
            )
        else:
            capability.active = True


def list_service_areas(db: Session, company_id: str) -> list[CompanyServiceAreaV12]:
    return list(
        db.scalars(
            select(CompanyServiceAreaV12)
            .where(CompanyServiceAreaV12.company_id == company_id)
            .order_by(CompanyServiceAreaV12.is_primary_city.desc(), CompanyServiceAreaV12.region_code)
        ).all()
    )


def _validate_region_hierarchy(db: Session, regions: dict[str, Region], primary_city_code: str) -> None:
    primary = db.get(Region, primary_city_code)
    # 主要地区支持城市或区县级（含县级市），主体归属可落到县（2026-09-19 S12）。
    if primary is None or not primary.active or primary.level not in {"CITY", "DISTRICT"}:
        raise AppError("PRIMARY_CITY_LEVEL_INVALID", "主要城市必须选择城市或区县级地区", 422)
    invalid_codes: list[str] = []
    for code, region in regions.items():
        if region.level == "CITY":
            continue
        parent = regions.get(region.parent_code or "") or db.get(Region, region.parent_code)
        if region.level == "DISTRICT" and parent and parent.active and parent.level == "CITY":
            continue
        if region.level == "TOWNSHIP":
            grandparent = (
                regions.get(parent.parent_code or "") or db.get(Region, parent.parent_code)
                if parent and parent.parent_code
                else None
            )
            if (
                parent
                and parent.active
                and parent.level == "DISTRICT"
                and grandparent
                and grandparent.active
                and grandparent.level == "CITY"
            ):
                continue
            invalid_codes.append(code)
            continue
        invalid_codes.append(code)
    if invalid_codes:
        raise AppError(
            "SERVICE_AREA_HIERARCHY_INVALID",
            "服务区域必须是有效的城市、区县或乡镇",
            422,
            {"region_codes": sorted(invalid_codes), "primary_city_code": primary_city_code},
        )


def replace_service_areas(
    db: Session,
    *,
    company_id: str,
    region_codes: list[str],
    primary_city_code: str | None,
) -> list[CompanyServiceAreaV12]:
    """Submit a reviewed replacement without interrupting approved coverage.

    New or reactivated regions remain inactive until approved. Removal requests
    retain their currently approved coverage until the platform approves the
    removal, preventing an unreviewed profile edit from changing dispatch
    eligibility immediately.
    """

    require_active_company(db, company_id)
    cleaned = list(dict.fromkeys(code.strip() for code in region_codes if code.strip()))
    if not cleaned:
        raise AppError("SERVICE_AREA_REQUIRED", "至少配置一个服务区域", 422)
    if not primary_city_code:
        raise AppError("PRIMARY_CITY_REQUIRED", "必须配置一个主要城市", 422)
    materialize_nationwide_regions(db, [primary_city_code, *cleaned])
    db.flush()
    regions = {
        region.code: region
        for region in db.scalars(select(Region).where(Region.code.in_(cleaned), Region.active.is_(True))).all()
    }
    missing = sorted(set(cleaned) - set(regions))
    if missing:
        raise AppError("REGION_NOT_FOUND", "存在无效或停用的地区编码", 422, {"region_codes": missing})
    _validate_region_hierarchy(db, regions, primary_city_code)
    primary_marker_code = (
        primary_city_code
        if primary_city_code in regions
        else next(
            (
                code
                for code in cleaned
                if service_region_city_code(db, regions[code]) == primary_city_code
            ),
            None,
        )
    )
    if primary_marker_code is None:
        raise AppError("PRIMARY_CITY_INVALID", "主要城市必须与至少一个已选服务区域一致", 422)

    existing_items = list(
        db.scalars(
            select(CompanyServiceAreaV12).where(CompanyServiceAreaV12.company_id == company_id)
        ).all()
    )
    existing_by_code = {item.region_code: item for item in existing_items}
    desired_codes = set(cleaned)
    result: list[CompanyServiceAreaV12] = []

    for code in cleaned:
        region = regions[code]
        item = existing_by_code.get(code)
        if item is None:
            item = CompanyServiceAreaV12(
                company_id=company_id,
                region_code=code,
                region_level=region.level,
                is_primary_city=code == primary_marker_code,
                active=False,
                review_status="PENDING",
            )
            db.add(item)
        else:
            item.region_level = region.level
            item.is_primary_city = code == primary_marker_code
            removal_pending = bool(item.review_note and item.review_note.startswith(REMOVAL_REQUEST_PREFIX))
            if removal_pending and item.active:
                item.review_status = "APPROVED"
                item.review_note = None
                item.reviewed_by = None
                item.reviewed_at = None
            elif item.review_status == "REJECTED" or (item.review_status == "APPROVED" and not item.active):
                item.review_status = "PENDING"
                item.active = False
                item.review_note = None
                item.reviewed_by = None
                item.reviewed_at = None
        result.append(item)

    for item in existing_items:
        if item.region_code in desired_codes:
            continue
        removal_pending = bool(item.review_note and item.review_note.startswith(REMOVAL_REQUEST_PREFIX))
        if item.active and item.review_status == "APPROVED":
            item.review_status = "PENDING"
            item.review_note = f"{REMOVAL_REQUEST_PREFIX} 公司申请移除服务区域"
            item.reviewed_by = None
            item.reviewed_at = None
            result.append(item)
        elif removal_pending and item.active:
            result.append(item)
        else:
            if item.review_status == "PENDING" and not item.active:
                item.review_status = "REJECTED"
                item.review_note = "公司已撤回该服务区域申请"
                item.reviewed_by = None
                item.reviewed_at = datetime.now(timezone.utc)
            result.append(item)

    db.flush()
    return sorted(result, key=lambda item: (not item.is_primary_city, item.region_code))


def review_service_area(
    db: Session,
    *,
    area_id: str,
    approve: bool,
    reviewed_by: str,
    note: str | None = None,
) -> CompanyServiceAreaV12:
    item = db.get(CompanyServiceAreaV12, area_id)
    if item is None:
        raise AppError("SERVICE_AREA_NOT_FOUND", "服务区域申请不存在", 404)
    removal_request = bool(item.review_note and item.review_note.startswith(REMOVAL_REQUEST_PREFIX))
    clean_note = note.strip() if note else None
    if removal_request:
        item.review_status = "APPROVED"
        item.active = not approve
        item.review_note = clean_note or ("移除申请已批准" if approve else "移除申请已驳回")
    else:
        item.review_status = "APPROVED" if approve else "REJECTED"
        item.active = approve
        item.review_note = clean_note
    item.reviewed_by = reviewed_by
    item.reviewed_at = datetime.now(timezone.utc)
    _sync_legacy_service_area(db, item)
    db.flush()
    return item


def _sync_legacy_service_area(db: Session, item: CompanyServiceAreaV12) -> None:
    """Keep the legacy candidate path aligned with the approved V1.2 profile."""

    legacy = db.scalar(
        select(CompanyServiceRegion).where(
            CompanyServiceRegion.company_id == item.company_id,
            CompanyServiceRegion.region_code == item.region_code,
        )
    )
    should_be_active = item.review_status == "APPROVED" and item.active
    if legacy is None and should_be_active:
        db.add(
            CompanyServiceRegion(
                company_id=item.company_id,
                region_code=item.region_code,
                active=True,
            )
        )
    elif legacy is not None:
        legacy.active = should_be_active
