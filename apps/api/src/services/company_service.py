from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from ..core.errors import AppError
from ..core.models import (
    Assignment,
    AssignmentEvent,
    Company,
    CompanyAccountRequest,
    CompanyCapability,
    CompanyServiceRegion,
    DictionaryItem,
    FollowUp,
    InviteToken,
    Lead,
    LeadDuplicateRelation,
    LeadImportIssue,
    Notification,
    NotificationOutbox,
    PointsAccount,
    PointsLedger,
    Region,
    ReturnEvidence,
    ReturnRequest,
    SupplyTerminationRequest,
    User,
    VerificationSubmission,
    VerificationTask,
    WechatIdentity,
)
from ..core.models_v12 import (
    CompanyLeadCapability,
    CompanyServiceAreaV12,
    DedupOverride,
    LeadDedupEvent,
    SupplierLeadReward,
)
from ..core.security import decrypt_text, encrypt_text, hash_phone, mask_phone
from ..core.time import utcnow
from ..core.v12_enums import CompanyLeadCapabilityCode, LeadSourceKind, LeadV12Status
from ..schemas.company import CompanyCreateBody, CompanySimpleCreateBody, CompanyUpdateBody
from .china_regions import city_by_code, region_by_code
from .dedup_v12 import reevaluate_existing_phone_identity
from .lead_correction_guard import lead_requires_correction_review
from .storage_cleanup_worker import enqueue_storage_cleanup

DEFAULT_RECEIVER_CATEGORIES = ("OLD_RENOVATION", "SELF_BUILD", "INTERIOR")
TEST_COMPANY_PURGE_CONFIRM_PHRASE = "永久删除测试数据"
PURGE_LOCK_CONFLICT_SQLSTATES = {"40P01", "55P03"}
logger = logging.getLogger("zhongshu.company_service")


def company_to_dict(company: Company, include_finance: bool = False) -> dict:
    data = {
        "id": company.id,
        "code": company.code,
        "name": company.name,
        "status": company.status,
        "is_test": company.is_test,
        "owner_name": company.owner_name,
        "contact_phone_masked": mask_phone(decrypt_text(company.contact_phone_encrypted) if company.contact_phone_encrypted else None),
        "level_code": company.level_code,
        "primary_user_id": company.primary_user_id,
        "region_codes": [r.region_code for r in company.service_regions if r.active],
        "capabilities": [
            {"category_code": c.category_code, "brand_code": c.brand_code}
            for c in company.capabilities
            if c.active
        ],
        "notes": company.notes,
        "wechat_bound": bool(company.primary_user_id),
    }
    if include_finance:
        customer_balance = company.points_account.balance if company.points_account else 0
        supply_balance = company.points_account.supply_balance if company.points_account else 0
        data["points_balance"] = customer_balance
        data["customer_points_balance"] = customer_balance
        data["supply_points_balance"] = supply_balance
    return data


def create_company(db: Session, body: CompanyCreateBody) -> Company:
    if db.scalar(select(Company).where(Company.code == body.code)):
        raise AppError("COMPANY_CODE_EXISTS", "公司编码已存在", 409)
    phone = body.contact_phone
    company = Company(
        code=body.code,
        name=body.name,
        owner_name=body.owner_name,
        contact_phone_encrypted=encrypt_text(phone) if phone else None,
        contact_phone_hash=hash_phone(phone) if phone else None,
        level_code=body.level_code,
        is_test=body.is_test,
        notes=body.notes,
    )
    db.add(company)
    db.flush()
    db.add(PointsAccount(company_id=company.id, balance=0, version=1))
    replace_company_scope(db, company, body.region_codes, body.capabilities)
    return company


def _next_company_code(db: Session) -> str:
    for _ in range(5):
        code = f"JM-{uuid4().hex[:10].upper()}"
        if not db.scalar(select(Company.id).where(Company.code == code)):
            return code
    raise AppError("COMPANY_CODE_GENERATION_FAILED", "加盟商编码生成失败，请重试", 409)


def _simple_region_codes(db: Session, body: CompanySimpleCreateBody) -> dict[str, Region]:
    _materialize_selected_regions(db, body)
    requested = [*body.district_codes, *body.region_codes]
    if body.serve_all_districts:
        district_codes = db.scalars(
            select(Region.code).where(
                Region.parent_code == body.primary_city_code,
                Region.level == "DISTRICT",
                Region.active.is_(True),
            )
        ).all()
        requested.extend(district_codes)
    cleaned = list(dict.fromkeys(code.strip() for code in requested if code.strip()))
    if not cleaned:
        raise AppError("SERVICE_AREA_REQUIRED", "至少选择一个服务地区", 422)
    regions = {
        region.code: region
        for region in db.scalars(
            select(Region).where(Region.code.in_(cleaned), Region.active.is_(True))
        ).all()
    }
    missing = sorted(set(cleaned) - set(regions))
    if missing:
        raise AppError("REGION_NOT_FOUND", "存在无效或停用的地区", 422, {"region_codes": missing})
    primary = db.get(Region, body.primary_city_code)
    if primary is None or primary.level != "CITY":
        raise AppError("PRIMARY_CITY_LEVEL_INVALID", "服务城市必须选择城市级地区", 422)
    invalid_legacy_districts = [
        code
        for code in body.district_codes
        if (region := regions.get(code)) is None
        or region.level != "DISTRICT"
        or region.parent_code != body.primary_city_code
    ]
    if invalid_legacy_districts:
        raise AppError(
            "SERVICE_AREA_HIERARCHY_INVALID",
            "服务区县必须隶属于所选服务城市",
            422,
            {
                "region_codes": sorted(invalid_legacy_districts),
                "primary_city_code": body.primary_city_code,
            },
        )
    invalid = [code for code, region in regions.items() if not _valid_service_region(db, region)]
    if invalid:
        raise AppError(
            "SERVICE_AREA_HIERARCHY_INVALID",
            "服务区域必须是有效的城市、区县或乡镇",
            422,
            {"region_codes": sorted(invalid)},
        )
    if not any(service_region_city_code(db, region) == body.primary_city_code for region in regions.values()):
        raise AppError("PRIMARY_CITY_INVALID", "主要城市必须与至少一个已选服务区域一致", 422)
    return regions


def _valid_service_region(db: Session, region: Region) -> bool:
    if region.level == "CITY":
        return True
    parent = db.get(Region, region.parent_code) if region.parent_code else None
    if region.level == "DISTRICT":
        return bool(parent and parent.active and parent.level == "CITY")
    if region.level == "TOWNSHIP":
        grandparent = db.get(Region, parent.parent_code) if parent and parent.parent_code else None
        return bool(
            parent
            and parent.active
            and parent.level == "DISTRICT"
            and grandparent
            and grandparent.active
            and grandparent.level == "CITY"
        )
    return False


def service_region_city_code(db: Session, region: Region) -> str | None:
    """Return the containing city without treating that city as service coverage."""

    if region.level == "CITY":
        return region.code
    parent = db.get(Region, region.parent_code) if region.parent_code else None
    if region.level == "DISTRICT":
        return parent.code if parent and parent.active and parent.level == "CITY" else None
    if region.level == "TOWNSHIP" and parent and parent.active and parent.level == "DISTRICT":
        city = db.get(Region, parent.parent_code) if parent.parent_code else None
        return city.code if city and city.active and city.level == "CITY" else None
    return None


def _materialize_selected_regions(db: Session, body: CompanySimpleCreateBody) -> None:
    """把用户从全国组件选中的地区接入现有派发地区模型。"""
    selected_codes = [body.primary_city_code, *body.district_codes, *body.region_codes]
    primary_city = city_by_code(body.primary_city_code)
    if body.serve_all_districts and primary_city:
        selected_codes.extend(district["code"] for district in primary_city["districts"])
    materialize_nationwide_regions(db, selected_codes)
    db.flush()


def materialize_nationwide_regions(db: Session, region_codes: list[str]) -> None:
    """Materialize city/district snapshot entries; township rows come from master data."""

    pending_codes = {
        item.code for item in db.new if isinstance(item, Region)
    }
    for code in dict.fromkeys(region_codes):
        context = region_by_code(code)
        if context is None:
            continue
        city_code = str(context["city_code"])
        city_name = str(context["city_name"])
        if city_code not in pending_codes and db.get(Region, city_code) is None:
            db.add(
                Region(
                    code=city_code,
                    name=city_name,
                    level="CITY",
                    parent_code=None,
                    aliases=[city_name],
                    active=True,
                )
            )
            pending_codes.add(city_code)
        district_code = context.get("district_code")
        district_name = context.get("district_name")
        if (
            district_code
            and str(district_code) not in pending_codes
            and db.get(Region, district_code) is None
        ):
            db.add(
                Region(
                    code=str(district_code),
                    name=str(district_name),
                    level="DISTRICT",
                    parent_code=city_code,
                    aliases=[str(district_name)],
                    active=True,
                )
            )
            pending_codes.add(str(district_code))


def create_simple_company(
    db: Session,
    body: CompanySimpleCreateBody,
    *,
    approved_by: str | None = None,
) -> tuple[Company, dict[str, str]]:
    regions = _simple_region_codes(db, body)
    company = Company(
        code=_next_company_code(db),
        name=body.name,
        owner_name=body.owner_name,
        contact_phone_encrypted=encrypt_text(body.contact_phone) if body.contact_phone else None,
        contact_phone_hash=hash_phone(body.contact_phone) if body.contact_phone else None,
        level_code=body.level_code,
        is_test=body.is_test,
        notes=body.notes,
    )
    db.add(company)
    db.flush()
    db.add(PointsAccount(company_id=company.id, balance=0, version=1))

    region_codes = sorted(regions)
    primary_marker_code = (
        body.primary_city_code
        if body.primary_city_code in regions
        else next(
            code
            for code in region_codes
            if service_region_city_code(db, regions[code]) == body.primary_city_code
        )
    )
    approved_at = datetime.now(timezone.utc)
    db.add(
        CompanyLeadCapability(
            company_id=company.id,
            capability_code=CompanyLeadCapabilityCode.LEAD_RECEIVER.value,
            active=True,
            review_status="APPROVED",
            reviewed_by=approved_by,
            reviewed_at=approved_at,
            review_note="后台创建加盟商时自动开通接单资格",
        )
    )
    for code in region_codes:
        region = regions[code]
        db.add(
            CompanyServiceAreaV12(
                company_id=company.id,
                region_code=code,
                region_level=region.level,
                is_primary_city=code == primary_marker_code,
                active=True,
                review_status="APPROVED",
                reviewed_by=approved_by,
                reviewed_at=approved_at,
                review_note="后台创建加盟商时自动开通服务地区",
            )
        )
    db.flush()
    return company, {
        "points_account": "READY",
        "receiver_capability": "READY",
        "service_areas": "READY",
    }


def default_receiver_categories(db: Session) -> tuple[str, ...]:
    categories = tuple(
        dict.fromkeys(
            db.scalars(
                select(DictionaryItem.code)
                .where(
                    DictionaryItem.domain == "lead_category",
                    DictionaryItem.active.is_(True),
                )
                .order_by(DictionaryItem.sort_order, DictionaryItem.code)
            ).all()
        )
    )
    return categories or DEFAULT_RECEIVER_CATEGORIES


def update_company(db: Session, company: Company, body: CompanyUpdateBody) -> Company:
    previous_status = company.status
    for field in ["name", "owner_name", "level_code", "status", "notes"]:
        value = getattr(body, field)
        if value is not None:
            setattr(company, field, value)
    if body.contact_phone is not None:
        company.contact_phone_encrypted = encrypt_text(body.contact_phone) if body.contact_phone else None
        company.contact_phone_hash = hash_phone(body.contact_phone) if body.contact_phone else None
    if body.region_codes is not None or body.capabilities is not None:
        replace_company_scope(
            db,
            company,
            body.region_codes if body.region_codes is not None else [x.region_code for x in company.service_regions],
            body.capabilities if body.capabilities is not None else [
                {"category_code": x.category_code, "brand_code": x.brand_code} for x in company.capabilities
            ],
        )
    if body.status == "DISABLED" and previous_status != "DISABLED":
        for user in company.members:
            user.session_version += 1
        _cancel_company_invite_outboxes(db, company.id, reason="加盟商已停用")
        db.execute(
            update(InviteToken)
            .where(
                InviteToken.company_id == company.id,
                InviteToken.revoked_at.is_(None),
                InviteToken.used_at.is_(None),
                InviteToken.expires_at > utcnow(),
            )
            .values(revoked_at=utcnow())
            .execution_options(synchronize_session=False)
        )
    return company


def replace_company_scope(db: Session, company: Company, region_codes: list[str], capabilities: list[dict]) -> None:
    db.execute(delete(CompanyServiceRegion).where(CompanyServiceRegion.company_id == company.id))
    db.execute(delete(CompanyCapability).where(CompanyCapability.company_id == company.id))
    for region_code in sorted(set(region_codes)):
        db.add(CompanyServiceRegion(company_id=company.id, region_code=region_code, active=True))
    seen: set[tuple[str, str | None]] = set()
    for item in capabilities:
        category = str(item.get("category_code") or "").strip()
        brand = item.get("brand_code")
        if not category or (category, brand) in seen:
            continue
        seen.add((category, brand))
        db.add(CompanyCapability(company_id=company.id, category_code=category, brand_code=brand, active=True))


def unbind_company_owner_wechat(
    db: Session,
    company_id: str,
    *,
    confirm_name: str,
) -> dict[str, object]:
    company = db.scalar(select(Company).where(Company.id == company_id).with_for_update())
    if company is None:
        raise AppError("COMPANY_NOT_FOUND", "加盟商公司不存在", 404)
    if company.status != "DISABLED":
        raise AppError("COMPANY_MUST_BE_DISABLED", "请先停用加盟商，再解绑负责人微信", 409)
    if confirm_name != company.name:
        raise AppError("COMPANY_CONFIRMATION_MISMATCH", "输入的加盟商名称不匹配", 409)
    if not company.primary_user_id:
        raise AppError("COMPANY_OWNER_WECHAT_NOT_BOUND", "该加盟商尚未绑定负责人微信", 409)

    owner = db.get(User, company.primary_user_id)
    if owner is None or owner.company_id != company.id:
        raise AppError(
            "COMPANY_BINDING_INCONSISTENT",
            "负责人绑定数据不一致，请先进行数据核对",
            409,
        )

    identity = db.scalar(select(WechatIdentity).where(WechatIdentity.user_id == owner.id))
    if identity is None:
        raise AppError(
            "COMPANY_BINDING_INCONSISTENT",
            "负责人绑定数据不一致，请先进行数据核对",
            409,
        )

    previous_status = owner.status
    previous_session_version = owner.session_version
    company.primary_user_id = None
    owner.status = "DISABLED"
    owner.session_version += 1
    db.delete(identity)
    revoked_count = db.execute(
        update(InviteToken)
        .where(
            InviteToken.company_id == company.id,
            InviteToken.revoked_at.is_(None),
            InviteToken.used_at.is_(None),
        )
        .values(revoked_at=utcnow())
        .execution_options(synchronize_session=False)
    ).rowcount
    cancelled_delivery_count = _cancel_company_invite_outboxes(
        db,
        company.id,
        reason="负责人微信已解绑",
    )
    db.flush()
    return {
        "company_id": company.id,
        "unbound_user_id": owner.id,
        "revoked_invite_count": int(revoked_count or 0),
        "cancelled_invite_delivery_count": cancelled_delivery_count,
        "before": {
            "status": company.status,
            "primary_user_id": owner.id,
            "wechat_bound": True,
            "owner_user_status": previous_status,
            "owner_session_version": previous_session_version,
        },
        "after": {
            "status": company.status,
            "primary_user_id": None,
            "wechat_bound": False,
            "owner_user_status": owner.status,
            "owner_session_version": owner.session_version,
        },
    }


def _test_company_purge_scope(db: Session, company_id: str) -> dict[str, list[str]]:
    lead_ids = list(
        db.scalars(
            select(Lead.id)
            .where(Lead.supplier_company_id == company_id)
        ).all()
    )
    assignment_ids = list(
        db.scalars(
            select(Assignment.id)
            .where(
                or_(
                    Assignment.lead_id.in_(lead_ids),
                    Assignment.company_id == company_id,
                    Assignment.supplier_company_id == company_id,
                    Assignment.receiver_company_id == company_id,
                )
            )
        ).all()
    )
    followup_ids = list(
        db.scalars(
            select(FollowUp.id)
            .where(
                or_(
                    FollowUp.company_id == company_id,
                    FollowUp.assignment_id.in_(assignment_ids),
                )
            )
        ).all()
    )
    return_ids = list(
        db.scalars(
            select(ReturnRequest.id)
            .where(
                or_(
                    ReturnRequest.company_id == company_id,
                    ReturnRequest.assignment_id.in_(assignment_ids),
                    ReturnRequest.lead_id.in_(lead_ids),
                )
            )
        ).all()
    )
    reward_ids = list(
        db.scalars(
            select(SupplierLeadReward.id)
            .where(
                or_(
                    SupplierLeadReward.lead_id.in_(lead_ids),
                    SupplierLeadReward.assignment_id.in_(assignment_ids),
                    SupplierLeadReward.supplier_company_id == company_id,
                    SupplierLeadReward.receiver_company_id == company_id,
                )
            )
        ).all()
    )
    termination_ids = list(
        db.scalars(
            select(SupplyTerminationRequest.id).where(
                SupplyTerminationRequest.company_id == company_id
            )
        ).all()
    )

    return {
        "lead_ids": lead_ids,
        "assignment_ids": assignment_ids,
        "followup_ids": followup_ids,
        "return_ids": return_ids,
        "reward_ids": reward_ids,
        "termination_ids": termination_ids,
    }


def _purge_lock_sqlstate(exc: DBAPIError) -> str | None:
    sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    if sqlstate:
        return str(sqlstate)
    return str(getattr(getattr(exc.orig, "diag", None), "sqlstate", "")) or None


def _lock_rows_for_purge(
    db: Session,
    stmt,
    *,
    resource_type: str,
) -> list:
    is_postgresql = db.get_bind().dialect.name == "postgresql"
    try:
        return list(
            db.scalars(
                stmt.with_for_update(nowait=is_postgresql)
            ).all()
        )
    except DBAPIError as exc:
        if is_postgresql and _purge_lock_sqlstate(exc) in PURGE_LOCK_CONFLICT_SQLSTATES:
            logger.warning(
                "test_company_purge_lock_conflict",
                extra={"resource_type": resource_type},
            )
            raise AppError(
                "COMPANY_PURGE_BUSY_RETRY",
                "测试主体业务数据正在处理，请稍后重试删除",
                409,
            ) from exc
        raise


def _lock_purge_business_scope(db: Session, company_id: str) -> dict[str, list[str]]:
    """Serialize with claim/return/reward writers, then return a stable scope."""

    locked: dict[str, set[str]] = {
        "assignment_ids": set(),
        "lead_ids": set(),
        "return_ids": set(),
        "reward_ids": set(),
        "termination_ids": set(),
    }
    model_by_key = {
        "assignment_ids": Assignment,
        "lead_ids": Lead,
        "return_ids": ReturnRequest,
        "reward_ids": SupplierLeadReward,
        "termination_ids": SupplyTerminationRequest,
    }
    scope = _test_company_purge_scope(db, company_id)
    for _ in range(3):
        changed = False
        for key in ("assignment_ids", "lead_ids", "return_ids", "reward_ids", "termination_ids"):
            new_ids = sorted(set(scope[key]) - locked[key])
            if not new_ids:
                continue
            model = model_by_key[key]
            _lock_rows_for_purge(
                db,
                select(model)
                .where(model.id.in_(new_ids))
                .order_by(model.id),
                resource_type=model.__tablename__,
            )
            locked[key].update(new_ids)
            changed = True
        refreshed = _test_company_purge_scope(db, company_id)
        if not changed and all(set(refreshed[key]) == set(scope[key]) for key in scope):
            return refreshed
        scope = refreshed
    if any(set(scope[key]) - locked.get(key, set()) for key in locked):
        raise AppError(
            "COMPANY_PURGE_SCOPE_CHANGED_RETRY",
            "测试主体业务数据正在变更，请稍后重试删除",
            409,
        )
    return scope


def _direct_purge_ledger_ids(
    db: Session,
    *,
    return_ids: list[str],
    reward_ids: list[str],
) -> set[str]:
    ledger_ids: set[str] = set()
    if return_ids:
        ledger_ids.update(
            ledger_id
            for ledger_id in db.scalars(
                select(ReturnRequest.refund_ledger_id).where(ReturnRequest.id.in_(return_ids))
            ).all()
            if ledger_id
        )
    if reward_ids:
        rewards = list(
            db.scalars(
                select(SupplierLeadReward).where(SupplierLeadReward.id.in_(reward_ids))
            ).all()
        )
        for reward in rewards:
            if reward.ledger_id:
                ledger_ids.add(reward.ledger_id)
            if reward.reversal_ledger_id:
                ledger_ids.add(reward.reversal_ledger_id)
    return ledger_ids


def _discover_purge_ledgers(
    db: Session,
    *,
    company_id: str,
    business_ids: list[str],
    direct_ledger_ids: set[str],
    lock: bool,
) -> dict[str, PointsLedger]:
    filters = [PointsLedger.company_id == company_id]
    if business_ids:
        filters.append(PointsLedger.business_id.in_(business_ids))
    if direct_ledger_ids:
        filters.append(PointsLedger.id.in_(direct_ledger_ids))
    stmt = select(PointsLedger).where(or_(*filters)).order_by(PointsLedger.id)
    rows = (
        _lock_rows_for_purge(db, stmt, resource_type=PointsLedger.__tablename__)
        if lock
        else list(db.scalars(stmt).all())
    )
    ledger_by_id = {ledger.id: ledger for ledger in rows}
    while ledger_by_id:
        known_ids = set(ledger_by_id)
        related_ids = {
            ledger.related_ledger_id
            for ledger in ledger_by_id.values()
            if ledger.related_ledger_id
        }
        related_filters = [PointsLedger.related_ledger_id.in_(known_ids)]
        if related_ids:
            related_filters.append(PointsLedger.id.in_(related_ids))
        related_stmt = (
            select(PointsLedger)
            .where(or_(*related_filters))
            .order_by(PointsLedger.id)
        )
        related_rows = (
            _lock_rows_for_purge(
                db,
                related_stmt,
                resource_type=PointsLedger.__tablename__,
            )
            if lock
            else list(db.scalars(related_stmt).all())
        )
        new_rows = [
            ledger
            for ledger in related_rows
            if ledger.id not in ledger_by_id
        ]
        if not new_rows:
            break
        ledger_by_id.update((ledger.id, ledger) for ledger in new_rows)
    return ledger_by_id


def _purge_scope_token(company_id: str, resources: dict[str, list[str]]) -> str:
    payload = {
        "company_id": company_id,
        "resources": {
            key: sorted(set(values))
            for key, values in sorted(resources.items())
        },
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def preview_test_company_purge(db: Session, company_id: str) -> dict[str, object]:
    company = db.get(Company, company_id)
    if company is None:
        raise AppError("COMPANY_NOT_FOUND", "加盟商公司不存在", 404)
    if company.status != "DISABLED":
        raise AppError("COMPANY_MUST_BE_DISABLED", "请先停用加盟商，再预览测试数据清理范围", 409)

    scope = _test_company_purge_scope(db, company_id)
    verification_task_ids = list(
        db.scalars(
            select(VerificationTask.id).where(
                or_(
                    VerificationTask.lead_id.in_(scope["lead_ids"]),
                    VerificationTask.assignment_id.in_(scope["assignment_ids"]),
                    VerificationTask.return_request_id.in_(scope["return_ids"]),
                )
            )
        ).all()
    )
    evidence_ids = list(
        db.scalars(
            select(ReturnEvidence.id).where(
                ReturnEvidence.return_request_id.in_(scope["return_ids"])
            )
        ).all()
    )
    invite_ids = list(
        db.scalars(select(InviteToken.id).where(InviteToken.company_id == company_id)).all()
    )
    account_request_ids = list(
        db.scalars(
            select(CompanyAccountRequest.id).where(
                CompanyAccountRequest.company_id == company_id
            )
        ).all()
    )
    account_ids = list(
        db.scalars(
            select(PointsAccount.id).where(PointsAccount.company_id == company_id)
        ).all()
    )
    member_ids = list(
        db.scalars(select(User.id).where(User.company_id == company_id)).all()
    )
    business_ids = [
        *scope["lead_ids"],
        *scope["assignment_ids"],
        *scope["return_ids"],
        *scope["reward_ids"],
    ]
    ledger_by_id = _discover_purge_ledgers(
        db,
        company_id=company_id,
        business_ids=business_ids,
        direct_ledger_ids=_direct_purge_ledger_ids(
            db,
            return_ids=scope["return_ids"],
            reward_ids=scope["reward_ids"],
        ),
        lock=False,
    )
    external_company_ids = {
        ledger.company_id
        for ledger in ledger_by_id.values()
        if ledger.company_id != company_id
    }
    external_account_ids = {
        ledger.account_id
        for ledger in ledger_by_id.values()
        if ledger.company_id != company_id
    }
    if scope["assignment_ids"]:
        for row in db.execute(
            select(
                Assignment.company_id,
                Assignment.supplier_company_id,
                Assignment.receiver_company_id,
            ).where(Assignment.id.in_(scope["assignment_ids"]))
        ).all():
            external_company_ids.update(
                company_ref
                for company_ref in row
                if company_ref and company_ref != company_id
            )

    resources = {
        **scope,
        "verification_task_ids": verification_task_ids,
        "evidence_ids": evidence_ids,
        "invite_ids": invite_ids,
        "account_request_ids": account_request_ids,
        "account_ids": account_ids,
        "ledger_ids": list(ledger_by_id),
        "member_ids": member_ids,
    }
    return {
        "company_id": company_id,
        "company_name": company.name,
        "scope_token": _purge_scope_token(company_id, resources),
        "confirm_phrase": TEST_COMPANY_PURGE_CONFIRM_PHRASE,
        "counts": {
            "leads": len(scope["lead_ids"]),
            "assignments": len(scope["assignment_ids"]),
            "followups": len(scope["followup_ids"]),
            "returns": len(scope["return_ids"]),
            "verification_tasks": len(verification_task_ids),
            "evidence_files": len(evidence_ids),
            "supplier_rewards": len(scope["reward_ids"]),
            "supply_terminations": len(scope["termination_ids"]),
            "points_ledgers": len(ledger_by_id),
            "points_accounts": len(account_ids),
            "account_requests": len(account_request_ids),
            "invites": len(invite_ids),
            "members": len(member_ids),
        },
        "cross_company_impact": {
            "companies": len(external_company_ids),
            "points_accounts": len(external_account_ids),
            "points_ledgers": sum(
                1
                for ledger in ledger_by_id.values()
                if ledger.company_id != company_id
            ),
        },
    }


def _deep_link_references_resource(deep_link: str | None, resource_ids: list[str]) -> bool:
    if not deep_link:
        return False
    return any(
        re.search(
            rf"(?<![0-9A-Za-z-]){re.escape(resource_id)}(?![0-9A-Za-z-])",
            deep_link,
        )
        for resource_id in resource_ids
    )


def _restore_dedup_affected_lead(lead: Lead, *, blocks_dispatch: bool, decision: str) -> None:
    if lead.current_assignment_id or lead_requires_correction_review(lead):
        return
    if blocks_dispatch:
        if lead.status in {
            LeadV12Status.READY_DISPATCH.value,
            LeadV12Status.PENDING_REVIEW.value,
            LeadV12Status.DUPLICATE.value,
        }:
            lead.status = LeadV12Status.DUPLICATE.value
            lead.pending_reason = decision
        return
    if lead.status != LeadV12Status.DUPLICATE.value:
        return
    if (
        lead.source_kind == LeadSourceKind.SUPPLIER_H5.value
        and lead.review_status != "APPROVED"
    ):
        lead.status = LeadV12Status.PENDING_REVIEW.value
        lead.review_status = "PENDING"
    else:
        lead.status = LeadV12Status.READY_DISPATCH.value
        if lead.source_kind != LeadSourceKind.SUPPLIER_H5.value:
            lead.review_status = "APPROVED"
    lead.pending_reason = None


def _purge_test_company_data(
    db: Session,
    company: Company,
) -> dict[str, int]:
    scope = _lock_purge_business_scope(db, company.id)
    lead_ids = scope["lead_ids"]
    assignment_ids = scope["assignment_ids"]
    followup_ids = scope["followup_ids"]
    return_ids = scope["return_ids"]
    reward_ids = scope["reward_ids"]
    termination_ids = scope["termination_ids"]
    verification_task_ids: list[str] = []
    if lead_ids or assignment_ids or return_ids:
        verification_task_ids = list(
            db.scalars(
                select(VerificationTask.id).where(
                    or_(
                        VerificationTask.lead_id.in_(lead_ids),
                        VerificationTask.assignment_id.in_(assignment_ids),
                        VerificationTask.return_request_id.in_(return_ids),
                    )
                )
            ).all()
        )
        if verification_task_ids:
            _lock_rows_for_purge(
                db,
                select(VerificationTask)
                .where(VerificationTask.id.in_(verification_task_ids))
                .order_by(VerificationTask.id),
                resource_type=VerificationTask.__tablename__,
            )
    evidence_rows: list[ReturnEvidence] = []
    if return_ids:
        evidence_rows = _lock_rows_for_purge(
            db,
            select(ReturnEvidence)
            .where(ReturnEvidence.return_request_id.in_(return_ids))
            .order_by(ReturnEvidence.id),
            resource_type=ReturnEvidence.__tablename__,
        )
    evidence_ids = [evidence.id for evidence in evidence_rows]
    affected_dedup_lead_ids: set[str] = set()
    if lead_ids or assignment_ids:
        dedup_event_filters = []
        if lead_ids:
            dedup_event_filters.append(LeadDedupEvent.matched_lead_id.in_(lead_ids))
        if assignment_ids:
            dedup_event_filters.append(
                LeadDedupEvent.matched_assignment_id.in_(assignment_ids)
            )
        affected_dedup_lead_ids.update(
            lead_id
            for lead_id in db.scalars(
                select(LeadDedupEvent.lead_id).where(or_(*dedup_event_filters))
            ).all()
            if lead_id not in lead_ids
        )
    if lead_ids:
        affected_dedup_lead_ids.update(
            lead_id
            for lead_id in db.scalars(
                select(LeadDuplicateRelation.lead_id).where(
                    LeadDuplicateRelation.duplicate_lead_id.in_(lead_ids)
                )
            ).all()
            if lead_id not in lead_ids
        )
    invite_ids = list(
        db.scalars(select(InviteToken.id).where(InviteToken.company_id == company.id)).all()
    )
    account_request_ids = list(
        db.scalars(
            select(CompanyAccountRequest.id).where(
                CompanyAccountRequest.company_id == company.id
            )
        ).all()
    )
    account_ids = list(
        db.scalars(
            select(PointsAccount.id)
            .where(PointsAccount.company_id == company.id)
        ).all()
    )
    business_ids = [*lead_ids, *assignment_ids, *return_ids, *reward_ids]
    direct_ledger_ids = _direct_purge_ledger_ids(
        db,
        return_ids=return_ids,
        reward_ids=reward_ids,
    )
    provisional_ledgers = _discover_purge_ledgers(
        db,
        company_id=company.id,
        business_ids=business_ids,
        direct_ledger_ids=direct_ledger_ids,
        lock=False,
    )
    test_account_id_set = set(account_ids)
    account_lock_ids = sorted(
        test_account_id_set
        | {ledger.account_id for ledger in provisional_ledgers.values()}
    )
    locked_accounts: list[PointsAccount] = []
    if account_lock_ids:
        locked_accounts = _lock_rows_for_purge(
            db,
            select(PointsAccount)
            .where(PointsAccount.id.in_(account_lock_ids))
            .order_by(PointsAccount.id),
            resource_type=PointsAccount.__tablename__,
        )
    ledger_by_id = _discover_purge_ledgers(
        db,
        company_id=company.id,
        business_ids=business_ids,
        direct_ledger_ids=_direct_purge_ledger_ids(
            db,
            return_ids=return_ids,
            reward_ids=reward_ids,
        ),
        lock=True,
    )
    locked_account_id_set = {account.id for account in locked_accounts}
    if any(ledger.account_id not in locked_account_id_set for ledger in ledger_by_id.values()):
        raise AppError(
            "COMPANY_PURGE_SCOPE_CHANGED_RETRY",
            "测试主体积分数据正在变更，请稍后重试删除",
            409,
        )
    ledger_ids = sorted(ledger_by_id)
    adjusted_accounts = [
        account for account in locked_accounts if account.id not in test_account_id_set
    ]
    for account in adjusted_accounts:
        running = {"CUSTOMER": 0, "SUPPLY": 0}
        account_ledgers = _lock_rows_for_purge(
            db,
            select(PointsLedger)
            .where(PointsLedger.account_id == account.id)
            .order_by(PointsLedger.created_at, PointsLedger.id),
            resource_type=PointsLedger.__tablename__,
        )
        for ledger in account_ledgers:
            if ledger.id in ledger_by_id:
                continue
            kind = ledger.point_kind if ledger.point_kind in running else "CUSTOMER"
            running[kind] += int(ledger.delta)
            ledger.balance_after = running[kind]
        account.balance = running["CUSTOMER"]
        account.supply_balance = running["SUPPLY"]
        account.version += 1

    resource_ids = [
        company.id,
        *invite_ids,
        *account_request_ids,
        *lead_ids,
        *assignment_ids,
        *return_ids,
        *evidence_ids,
        *verification_task_ids,
        *reward_ids,
        *termination_ids,
        *ledger_ids,
        *account_ids,
    ]
    outbox_rows = list(
        db.scalars(
            select(NotificationOutbox).where(
                NotificationOutbox.aggregate_id.in_(resource_ids)
            )
        ).all()
    )
    outbox_ids = [item.id for item in outbox_rows]
    outbox_notification_ids: set[str] = set()
    for item in outbox_rows:
        if not isinstance(item.payload, dict):
            continue
        notification_id = item.payload.get("notification_id")
        if isinstance(notification_id, str):
            outbox_notification_ids.add(notification_id)
    if outbox_ids:
        db.execute(delete(NotificationOutbox).where(NotificationOutbox.id.in_(outbox_ids)))

    notification_filters = [Notification.company_id == company.id]
    if outbox_notification_ids:
        notification_filters.append(Notification.id.in_(outbox_notification_ids))
    notification_id_set = set(
        db.scalars(select(Notification.id).where(or_(*notification_filters))).all()
    )
    for offset in range(0, len(resource_ids), 200):
        linked_resource_ids = resource_ids[offset : offset + 200]
        linked_notifications = list(
            db.scalars(
                select(Notification).where(
                    or_(
                        *[
                            Notification.deep_link.contains(resource_id)
                            for resource_id in linked_resource_ids
                        ]
                    )
                )
            ).all()
        )
        notification_id_set.update(
            notification.id
            for notification in linked_notifications
            if _deep_link_references_resource(
                notification.deep_link,
                linked_resource_ids,
            )
        )
    notification_ids = list(notification_id_set)
    if notification_ids:
        db.execute(delete(Notification).where(Notification.id.in_(notification_ids)))

    if return_ids:
        db.execute(
            update(ReturnRequest)
            .where(ReturnRequest.id.in_(return_ids))
            .values(verification_task_id=None, refund_ledger_id=None)
        )
    if verification_task_ids:
        db.execute(
            update(VerificationTask)
            .where(VerificationTask.id.in_(verification_task_ids))
            .values(return_request_id=None, assignment_id=None)
        )
        db.execute(
            delete(VerificationSubmission).where(
                VerificationSubmission.task_id.in_(verification_task_ids)
            )
        )
    for evidence in evidence_rows:
        enqueue_storage_cleanup(
            db,
            event_key=f"return-evidence:{evidence.id}:delete-object",
            object_key=evidence.object_key,
            source_type="return_evidence",
            source_id=evidence.id,
            reason=f"删除测试加盟商 {company.id}",
        )
    if return_ids:
        db.execute(delete(ReturnEvidence).where(ReturnEvidence.return_request_id.in_(return_ids)))
    if reward_ids:
        db.execute(delete(SupplierLeadReward).where(SupplierLeadReward.id.in_(reward_ids)))
    if return_ids:
        db.execute(delete(ReturnRequest).where(ReturnRequest.id.in_(return_ids)))
    if verification_task_ids:
        db.execute(delete(VerificationTask).where(VerificationTask.id.in_(verification_task_ids)))
    if followup_ids:
        db.execute(delete(FollowUp).where(FollowUp.id.in_(followup_ids)))
    if assignment_ids:
        db.execute(
            update(Lead)
            .where(
                Lead.current_assignment_id.in_(assignment_ids),
                Lead.id.not_in(lead_ids),
            )
            .values(current_assignment_id=None, status=LeadV12Status.READY_DISPATCH.value)
        )
        db.execute(delete(AssignmentEvent).where(AssignmentEvent.assignment_id.in_(assignment_ids)))
        db.execute(delete(Assignment).where(Assignment.id.in_(assignment_ids)))
    if lead_ids:
        db.execute(delete(DedupOverride).where(DedupOverride.lead_id.in_(lead_ids)))
        db.execute(
            delete(LeadDuplicateRelation).where(
                or_(
                    LeadDuplicateRelation.lead_id.in_(lead_ids),
                    LeadDuplicateRelation.duplicate_lead_id.in_(lead_ids),
                )
            )
        )
        db.execute(delete(LeadDedupEvent).where(LeadDedupEvent.lead_id.in_(lead_ids)))
        db.execute(delete(LeadImportIssue).where(LeadImportIssue.lead_id.in_(lead_ids)))
        db.execute(delete(Lead).where(Lead.id.in_(lead_ids)))
        db.flush()
    for affected_lead_id in sorted(affected_dedup_lead_ids):
        affected_rows = _lock_rows_for_purge(
            db,
            select(Lead).where(Lead.id == affected_lead_id),
            resource_type=Lead.__tablename__,
        )
        if not affected_rows:
            continue
        affected_lead = affected_rows[0]
        result = reevaluate_existing_phone_identity(
            db,
            lead=affected_lead,
            checkpoint="TEST_COMPANY_PURGE",
        )
        _restore_dedup_affected_lead(
            affected_lead,
            blocks_dispatch=result.blocks_dispatch,
            decision=result.decision.value,
        )
    if ledger_ids:
        db.execute(
            update(PointsLedger)
            .where(PointsLedger.related_ledger_id.in_(ledger_ids))
            .values(related_ledger_id=None)
        )
        db.execute(delete(PointsLedger).where(PointsLedger.id.in_(ledger_ids)))

    if termination_ids:
        db.execute(
            delete(SupplyTerminationRequest).where(
                SupplyTerminationRequest.id.in_(termination_ids)
            )
        )

    if account_request_ids:
        db.execute(
            delete(CompanyAccountRequest).where(
                CompanyAccountRequest.id.in_(account_request_ids)
            )
        )
    db.execute(delete(InviteToken).where(InviteToken.company_id == company.id))
    db.execute(delete(CompanyLeadCapability).where(CompanyLeadCapability.company_id == company.id))
    db.execute(delete(CompanyServiceAreaV12).where(CompanyServiceAreaV12.company_id == company.id))
    db.execute(delete(CompanyServiceRegion).where(CompanyServiceRegion.company_id == company.id))
    db.execute(delete(CompanyCapability).where(CompanyCapability.company_id == company.id))
    db.execute(delete(PointsAccount).where(PointsAccount.company_id == company.id))

    return {
        "leads": len(lead_ids),
        "assignments": len(assignment_ids),
        "followups": len(followup_ids),
        "returns": len(return_ids),
        "verification_tasks": len(verification_task_ids),
        "supplier_rewards": len(reward_ids),
        "supply_terminations": len(termination_ids),
        "points_ledgers": len(ledger_ids),
        "points_accounts": len(account_ids),
        "account_requests": len(account_request_ids),
        "adjusted_external_points_accounts": len(adjusted_accounts),
        "notifications": len(notification_ids),
        "notification_outboxes": len(outbox_ids),
        "storage_cleanup_jobs": len(evidence_rows),
    }


def _cancel_company_invite_outboxes(db: Session, company_id: str, *, reason: str) -> int:
    invite_ids = list(
        db.scalars(select(InviteToken.id).where(InviteToken.company_id == company_id)).all()
    )
    if not invite_ids:
        return 0
    result = db.execute(
        update(NotificationOutbox)
        .where(
            NotificationOutbox.aggregate_type == "invite",
            NotificationOutbox.aggregate_id.in_(invite_ids),
            NotificationOutbox.status.in_(["PENDING", "FAILED", "PROCESSING"]),
        )
        .values(status="CANCELLED", next_attempt_at=None, last_error=reason)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


def mark_company_as_test(
    db: Session,
    company_id: str,
    *,
    confirm_name: str,
    confirm_phrase: str,
    scope_token: str,
) -> dict[str, object]:
    company = db.scalar(select(Company).where(Company.id == company_id).with_for_update())
    if company is None:
        raise AppError("COMPANY_NOT_FOUND", "加盟商公司不存在", 404)
    if company.status != "DISABLED":
        raise AppError("COMPANY_MUST_BE_DISABLED", "请先停用加盟商，再标记历史测试数据", 409)
    if company.is_test:
        raise AppError("COMPANY_ALREADY_TEST", "该加盟商已是测试主体", 409)
    if confirm_name != company.name:
        raise AppError("COMPANY_CONFIRMATION_MISMATCH", "输入的加盟商名称不匹配", 409)
    if confirm_phrase != TEST_COMPANY_PURGE_CONFIRM_PHRASE:
        raise AppError("COMPANY_PURGE_CONFIRMATION_MISMATCH", "永久删除确认短语不匹配", 409)
    preview = preview_test_company_purge(db, company_id)
    if scope_token != preview["scope_token"]:
        raise AppError(
            "COMPANY_PURGE_PREVIEW_STALE",
            "清理范围已变化，请重新预览后再标记测试主体",
            409,
        )

    company.is_test = True
    db.flush()
    return {
        "company_id": company.id,
        "before": {"status": company.status, "is_test": False},
        "after": {"status": company.status, "is_test": True},
        "preview": {
            "counts": preview["counts"],
            "cross_company_impact": preview["cross_company_impact"],
            "scope_token": preview["scope_token"],
        },
    }


def delete_test_company(
    db: Session,
    company_id: str,
    *,
    confirm_name: str,
) -> dict[str, object]:
    companies = _lock_rows_for_purge(
        db,
        select(Company).where(Company.id == company_id),
        resource_type=Company.__tablename__,
    )
    if not companies:
        raise AppError("COMPANY_NOT_FOUND", "加盟商公司不存在", 404)
    company = companies[0]
    if company.status != "DISABLED":
        raise AppError("COMPANY_MUST_BE_DISABLED", "请先停用加盟商，再清理测试数据", 409)
    if not company.is_test:
        raise AppError("COMPANY_DELETE_TEST_ONLY", "正常加盟商只能停用，不允许删除", 409)
    if confirm_name != company.name:
        raise AppError("COMPANY_CONFIRMATION_MISMATCH", "输入的加盟商名称不匹配", 409)

    members = list(db.scalars(select(User).where(User.company_id == company.id)).all())
    purged = _purge_test_company_data(db, company)
    detached_user_ids: list[str] = []
    for user in members:
        db.execute(delete(WechatIdentity).where(WechatIdentity.user_id == user.id))
        user.status = "DISABLED"
        user.session_version += 1
        user.company_id = None
        detached_user_ids.append(user.id)

    snapshot = {
        "id": company.id,
        "code": company.code,
        "name": company.name,
        "status": company.status,
        "is_test": company.is_test,
        "primary_user_id": company.primary_user_id,
        "member_count": len(members),
        "detached_user_ids": detached_user_ids,
        "purged": purged,
    }
    company.primary_user_id = None
    db.delete(company)
    db.flush()
    return snapshot
