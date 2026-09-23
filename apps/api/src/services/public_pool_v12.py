from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core.auth import Principal
from ..core.config import get_settings
from ..core.errors import AppError
from ..core.models import Lead, LeadImportIssue, Region, SyncBatch
from ..core.security import decrypt_text, normalize_phone
from ..core.v12_enums import (
    CustomerSource,
    DuplicateDecision,
    LeadSourceKind,
    LeadV12Status,
)
from ..integrations.feishu import FeishuClient, FeishuRecord
from .china_regions import region_by_code
from .dedup_v12 import DedupResult, evaluate_phone
from .dispatch_v12 import has_receiver_coverage
from .feishu_sync_service import configured_mapping
from .lead_service import _field, _resolve_region_code
from .lead_supply_v12 import create_draft, submit_draft, update_draft


settings = get_settings()
logger = logging.getLogger("zhongshu.public_pool")

PUBLIC_POOL_OPERATION_SOURCE_KINDS = frozenset(
    {
        LeadSourceKind.PLATFORM_MANUAL.value,
        LeadSourceKind.FEISHU_IMPORT.value,
    }
)
PUBLIC_POOL_OPERATION_STATUSES = frozenset(
    {
        LeadV12Status.DRAFT.value,
        LeadV12Status.DUPLICATE.value,
    }
)
KNOWN_SOURCE_CHANNELS = frozenset(
    {"MANUAL", "DOUYIN", "WECHAT_VIDEO", "XIAOHONGSHU", "OTHER"}
)


class PublicPoolTarget(StrEnum):
    PUBLIC_POOL = "PUBLIC_POOL"
    DISPATCH_POOL = "DISPATCH_POOL"


@dataclass(frozen=True, slots=True)
class PublicPoolTransferResult:
    lead: Lead
    transferred: bool
    validation_errors: dict[str, str]
    dedup: DedupResult | None = None


@dataclass(frozen=True, slots=True)
class PublicPoolImportResult:
    batch: SyncBatch
    total_count: int
    created_count: int
    skipped_count: int
    public_pool_count: int
    dispatch_pool_count: int
    duplicate_count: int
    incomplete_count: int
    error_count: int
    view_id: str


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    return normalized in {"1", "true", "yes", "y", "是", "已授权", "已确认"}


def _source_values(raw_channel: Any, raw_detail: Any) -> tuple[str | None, str | None]:
    channel = _clean_text(raw_channel)
    detail = _clean_text(raw_detail)
    if channel is None:
        return None, detail
    normalized = channel.upper()
    aliases = {
        "人工录入": "MANUAL",
        "抖音": "DOUYIN",
        "抖音/信息流": "DOUYIN",
        "视频号": "WECHAT_VIDEO",
        "小红书": "XIAOHONGSHU",
        "其他": "OTHER",
    }
    normalized = aliases.get(channel, normalized)
    if normalized in KNOWN_SOURCE_CHANNELS:
        return normalized, detail if normalized == "OTHER" else None
    return "OTHER", detail or channel


def _feishu_values(
    db: Session,
    record: FeishuRecord,
    mapping: dict[str, str],
) -> dict[str, Any]:
    fields = record.fields
    province = _clean_text(_field(fields, mapping, "province"))
    city = _clean_text(_field(fields, mapping, "city"))
    district = _clean_text(_field(fields, mapping, "district"))
    explicit_region = _clean_text(_field(fields, mapping, "region_code"))
    source_channel, source_detail = _source_values(
        _field(fields, mapping, "source_channel"),
        _field(fields, mapping, "source_detail"),
    )
    return {
        "customer_name": _clean_text(_field(fields, mapping, "customer_name")),
        "phone": _clean_text(_field(fields, mapping, "phone")),
        "province": province,
        "city": city,
        "district": district,
        "region_code": _resolve_region_code(db, explicit_region, city, district),
        "category_code": _clean_text(_field(fields, mapping, "category_code")),
        "brand_code": _clean_text(_field(fields, mapping, "brand_code")),
        "source_channel": source_channel,
        "source_detail": source_detail,
        "need_summary": _clean_text(_field(fields, mapping, "need_summary")),
        "budget_min": _as_int(_field(fields, mapping, "budget_min")),
        "budget_max": _as_int(_field(fields, mapping, "budget_max")),
        "acquisition_cost_cents": _as_int(
            _field(fields, mapping, "acquisition_cost")
        )
        or 0,
        "consent_confirmed": _as_bool(
            _field(fields, mapping, "consent_confirmed")
        ),
    }


def public_pool_validation_errors(db: Session, lead: Lead) -> dict[str, str]:
    errors: dict[str, str] = {}
    phone = normalize_phone(decrypt_text(lead.phone_encrypted) or "")
    if len(phone) != 11 or not phone.startswith("1"):
        errors["phone"] = "手机号必填且必须为 11 位有效号码"
    if not lead.region_code:
        errors["region_code"] = "必须选择标准地区"
    else:
        active_region = db.scalar(
            select(Region.code).where(
                Region.code == lead.region_code,
                Region.active.is_(True),
            )
        )
        if active_region is None and region_by_code(lead.region_code) is None:
            errors["region_code"] = "标准地区无效或已停用"
        # 2026-09-23 口径：缺县不再阻断转入派发池（运营自行担责，前端软提醒）。
    if lead.source_kind != LeadSourceKind.SUPPLIER_H5.value:
        if not _clean_text(lead.source_channel):
            errors["source_channel"] = "必须选择客资来源"
        elif lead.source_channel == "OTHER" and not _clean_text(lead.source_detail):
            errors["source_detail"] = "来源选择其他时必须填写具体来源"
    if not lead.consent_confirmed:
        errors["consent_confirmed"] = "必须确认已获得客户信息授权"
    if (
        lead.budget_min is not None
        and lead.budget_max is not None
        and lead.budget_min > lead.budget_max
    ):
        errors["budget_max"] = "预算上限不能低于预算下限"
    if (
        lead.source_kind == LeadSourceKind.SUPPLIER_H5.value
        and not has_receiver_coverage(db, lead)
    ):
        errors["receiver_coverage"] = "当地暂无可接收加盟商"
    return errors


def _store_validation_errors(lead: Lead, errors: dict[str, str]) -> None:
    payload = dict(lead.raw_payload or {})
    if errors:
        payload["public_pool_validation_errors"] = errors
    else:
        payload.pop("public_pool_validation_errors", None)
    lead.raw_payload = payload


def _refresh_public_pool_validation(db: Session, lead: Lead) -> dict[str, str]:
    errors = public_pool_validation_errors(db, lead)
    _store_validation_errors(lead, errors)
    lead.pending_reason = "PUBLIC_POOL_INCOMPLETE" if errors else None
    return errors


def create_public_pool_lead(
    db: Session,
    *,
    principal: Principal,
    values: dict[str, Any],
) -> Lead:
    lead = create_draft(
        db,
        principal=principal,
        source_kind=LeadSourceKind.PLATFORM_MANUAL,
        values=values,
    )
    _refresh_public_pool_validation(db, lead)
    db.flush()
    return lead


def update_public_pool_lead(
    db: Session,
    *,
    lead: Lead,
    principal: Principal,
    values: dict[str, Any],
) -> Lead:
    require_public_pool_lead(lead)
    if lead.status != LeadV12Status.DRAFT.value:
        raise AppError("PUBLIC_POOL_LEAD_NOT_EDITABLE", "当前公海池客资不可直接编辑", 409)
    preserve_rework_reason = (
        lead.source_kind in PUBLIC_POOL_OPERATION_SOURCE_KINDS
        and lead.pending_reason == "PRE_DISPATCH_REWORK_REQUIRED"
    )
    updated = update_draft(db, lead=lead, principal=principal, values=values)
    _refresh_public_pool_validation(db, updated)
    if preserve_rework_reason:
        updated.pending_reason = "PRE_DISPATCH_REWORK_REQUIRED"
    db.flush()
    return updated


def require_public_pool_lead(lead: Lead | None) -> Lead:
    if lead is None:
        raise AppError("LEAD_NOT_FOUND", "客资不存在", 404)
    operation_entry = (
        lead.source_kind in PUBLIC_POOL_OPERATION_SOURCE_KINDS
        and lead.status in PUBLIC_POOL_OPERATION_STATUSES
    )
    supplier_waiting = (
        lead.source_kind == LeadSourceKind.SUPPLIER_H5.value
        and lead.status == LeadV12Status.PUBLIC_POOL.value
    )
    if not operation_entry and not supplier_waiting:
        raise AppError("LEAD_NOT_IN_PUBLIC_POOL", "客资当前不在公海池", 409)
    return lead


def transfer_public_pool_lead(
    db: Session,
    *,
    lead: Lead,
    principal: Principal,
) -> PublicPoolTransferResult:
    require_public_pool_lead(lead)
    if lead.source_kind == LeadSourceKind.SUPPLIER_H5.value:
        errors = public_pool_validation_errors(db, lead)
        _store_validation_errors(lead, errors)
        if errors:
            lead.pending_reason = (
                "PUBLIC_POOL_NO_LOCAL_RECEIVER"
                if "receiver_coverage" in errors
                else "PUBLIC_POOL_INCOMPLETE"
            )
            db.flush()
            return PublicPoolTransferResult(
                lead=lead,
                transferred=False,
                validation_errors=errors,
            )
        dedup = _check_draft_duplicate(
            db,
            lead,
            checkpoint="PUBLIC_POOL_SUPPLIER_RECHECK",
        )
        if dedup and dedup.decision in {
            DuplicateDecision.HARD_DUPLICATE,
            DuplicateDecision.HISTORICAL_SUSPECT,
        }:
            lead.pending_reason = dedup.decision.value
            db.flush()
            return PublicPoolTransferResult(
                lead=lead,
                transferred=False,
                validation_errors={
                    "duplicate_status": "手机号查重结论阻止进入派发池"
                },
                dedup=dedup,
            )
        lead.status = LeadV12Status.READY_DISPATCH.value
        lead.review_status = "APPROVED"
        lead.pending_reason = None
        _store_validation_errors(lead, {})
        db.flush()
        return PublicPoolTransferResult(
            lead=lead,
            transferred=True,
            validation_errors={},
            dedup=dedup,
        )
    if lead.status != LeadV12Status.DRAFT.value:
        return PublicPoolTransferResult(
            lead=lead,
            transferred=False,
            validation_errors={"duplicate_status": "手机号查重结论尚未处理"},
        )
    errors = public_pool_validation_errors(db, lead)
    _store_validation_errors(lead, errors)
    if errors:
        if lead.pending_reason != "PRE_DISPATCH_REWORK_REQUIRED":
            lead.pending_reason = "PUBLIC_POOL_INCOMPLETE"
        db.flush()
        return PublicPoolTransferResult(
            lead=lead,
            transferred=False,
            validation_errors=errors,
        )
    dedup = submit_draft(
        db,
        lead=lead,
        principal=principal,
        checkpoint="PUBLIC_POOL_TRANSFER",
    )
    transferred = lead.status == LeadV12Status.READY_DISPATCH.value
    if transferred:
        _store_validation_errors(lead, {})
    db.flush()
    return PublicPoolTransferResult(
        lead=lead,
        transferred=transferred,
        validation_errors=(
            {}
            if transferred
            else {"duplicate_status": "手机号查重结论阻止进入派发池"}
        ),
        dedup=dedup,
    )


def _check_draft_duplicate(db: Session, lead: Lead, *, checkpoint: str) -> DedupResult | None:
    phone = normalize_phone(decrypt_text(lead.phone_encrypted) or "")
    if len(phone) != 11 or not phone.startswith("1"):
        return None
    return evaluate_phone(
        db,
        lead=lead,
        normalized_phone=phone,
        checkpoint=checkpoint,
    )


def _record_import_issue(
    db: Session,
    *,
    batch: SyncBatch,
    lead: Lead | None,
    field_name: str | None,
    message: str,
) -> None:
    db.add(
        LeadImportIssue(
            lead_id=lead.id if lead else None,
            sync_batch_id=batch.id,
            issue_type="PUBLIC_POOL_IMPORT",
            field_name=field_name,
            message=message,
        )
    )


def _configured_view_id(client: FeishuClient) -> str:
    configured_view_id = settings.feishu_view_id.strip()
    resolved_view_id = client.resolve_view_id(settings.feishu_view_name)
    if configured_view_id and configured_view_id != resolved_view_id:
        raise AppError(
            "FEISHU_VIEW_CONFIG_MISMATCH",
            "配置的飞书视图 ID 与“客户视图”不一致",
            409,
        )
    return configured_view_id or resolved_view_id


def import_feishu_customer_view(
    db: Session,
    *,
    principal: Principal,
    target: PublicPoolTarget,
    client: FeishuClient | None = None,
) -> PublicPoolImportResult:
    app_token = settings.feishu_app_token.strip()
    table_id = settings.feishu_table_id.strip()
    if not app_token or not table_id:
        raise AppError("FEISHU_TABLE_NOT_CONFIGURED", "飞书多维表格尚未配置", 503)
    adapter = client or FeishuClient()
    view_id = _configured_view_id(adapter)
    records = list(
        adapter.iter_records(
            view_id=view_id,
            page_size=settings.feishu_sync_page_size,
            max_pages=settings.feishu_sync_max_pages,
        )
    )
    batch = SyncBatch(
        source="FEISHU_PUBLIC_POOL",
        status="RUNNING",
        total_count=len(records),
        requested_by=principal.user_id,
    )
    db.add(batch)
    db.flush()

    created_count = 0
    skipped_count = 0
    public_pool_count = 0
    dispatch_pool_count = 0
    duplicate_count = 0
    incomplete_count = 0
    error_count = 0
    mapping = configured_mapping()

    for record in records:
        existing = db.scalar(
            select(Lead.id).where(
                Lead.source_app_token == app_token,
                Lead.source_table_id == table_id,
                Lead.source_record_id == record.record_id,
            )
        )
        if existing:
            skipped_count += 1
            batch.success_count += 1
            continue
        lead: Lead | None = None
        try:
            with db.begin_nested():
                values = _feishu_values(db, record, mapping)
                lead = create_draft(
                    db,
                    principal=principal,
                    source_kind=LeadSourceKind.FEISHU_IMPORT,
                    values=values,
                )
                lead.source_app_token = app_token
                lead.source_table_id = table_id
                lead.source_record_id = record.record_id
                lead.raw_payload = {
                    "feishu_view_id": view_id,
                    "feishu_imported_field_names": sorted(
                        str(name) for name in record.fields
                    ),
                }
                db.flush()

                if target is PublicPoolTarget.DISPATCH_POOL:
                    transfer = transfer_public_pool_lead(
                        db,
                        lead=lead,
                        principal=principal,
                    )
                    if transfer.transferred:
                        dispatch_pool_count += 1
                    else:
                        public_pool_count += 1
                        if "duplicate_status" in transfer.validation_errors:
                            duplicate_count += 1
                        else:
                            incomplete_count += 1
                            for field_name, message in transfer.validation_errors.items():
                                _record_import_issue(
                                    db,
                                    batch=batch,
                                    lead=lead,
                                    field_name=field_name,
                                    message=message,
                                )
                            duplicate = _check_draft_duplicate(
                                db,
                                lead,
                                checkpoint="FEISHU_IMPORT_PUBLIC_POOL",
                            )
                            if duplicate and duplicate.decision is not DuplicateDecision.CLEAR:
                                duplicate_count += 1
                else:
                    errors = _refresh_public_pool_validation(db, lead)
                    if errors:
                        incomplete_count += 1
                    duplicate = _check_draft_duplicate(
                        db,
                        lead,
                        checkpoint="FEISHU_IMPORT_PUBLIC_POOL",
                    )
                    if duplicate and duplicate.decision is not DuplicateDecision.CLEAR:
                        duplicate_count += 1
                    public_pool_count += 1
                created_count += 1
                batch.success_count += 1
        except IntegrityError:
            skipped_count += 1
            batch.success_count += 1
        except Exception as exc:
            error_count += 1
            batch.error_count += 1
            logger.error(
                "feishu public-pool record import failed batch_id=%s record_id=%s error_type=%s",
                batch.id,
                record.record_id,
                type(exc).__name__,
            )
            safe_error = (
                f"{exc.code}: {exc.message}"
                if isinstance(exc, AppError)
                else f"处理失败（{type(exc).__name__}）"
            )
            _record_import_issue(
                db,
                batch=batch,
                lead=None,
                field_name=None,
                message=f"飞书记录 {record.record_id} 导入失败：{safe_error}",
            )

    batch.error_count = error_count
    batch.status = "COMPLETED" if error_count == 0 else "PARTIAL"
    batch.finished_at = datetime.now(timezone.utc)
    db.flush()
    return PublicPoolImportResult(
        batch=batch,
        total_count=len(records),
        created_count=created_count,
        skipped_count=skipped_count,
        public_pool_count=public_pool_count,
        dispatch_pool_count=dispatch_pool_count,
        duplicate_count=duplicate_count,
        incomplete_count=incomplete_count,
        error_count=error_count,
        view_id=view_id,
    )


def public_pool_lead_conditions(
    *,
    keyword: str | None = None,
    phone_hash: str | None = None,
    customer_source: str | None = None,
    source_kind: str | None = None,
    completeness: str | None = None,
    duplicate_status: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    submitter_user_id: str | None = None,
) -> list[Any]:
    incomplete_condition = or_(
        Lead.phone_fingerprint.is_(None),
        Lead.region_code.is_(None),
        Lead.region_code == "",
        and_(
            Lead.source_kind != LeadSourceKind.SUPPLIER_H5.value,
            or_(
                Lead.source_channel.is_(None),
                Lead.source_channel == "",
                and_(
                    Lead.source_channel == "OTHER",
                    or_(Lead.source_detail.is_(None), Lead.source_detail == ""),
                ),
            ),
        ),
        Lead.consent_confirmed.is_not(True),
        and_(
            Lead.budget_min.is_not(None),
            Lead.budget_max.is_not(None),
            Lead.budget_min > Lead.budget_max,
        ),
        and_(
            Lead.pending_reason.is_not(None),
            Lead.pending_reason == "PUBLIC_POOL_INCOMPLETE",
        ),
    )
    public_pool_membership = or_(
        and_(
            Lead.source_kind.in_(PUBLIC_POOL_OPERATION_SOURCE_KINDS),
            Lead.status.in_(PUBLIC_POOL_OPERATION_STATUSES),
        ),
        and_(
            Lead.source_kind == LeadSourceKind.SUPPLIER_H5.value,
            Lead.status == LeadV12Status.PUBLIC_POOL.value,
        ),
    )
    filters = [
        public_pool_membership,
        Lead.current_assignment_id.is_(None),
        Lead.deleted_at.is_(None),
    ]
    normalized_keyword = (keyword or "").strip()
    if normalized_keyword:
        filters.append(
            or_(
                Lead.customer_name.contains(normalized_keyword),
                Lead.city.contains(normalized_keyword),
                Lead.district.contains(normalized_keyword),
                Lead.source_detail.contains(normalized_keyword),
            )
        )
    normalized_phone_hash = (phone_hash or "").strip()
    if normalized_phone_hash:
        filters.append(Lead.phone_hash == normalized_phone_hash)
    normalized_source = (source_kind or "").strip().upper()
    if normalized_source:
        filters.append(Lead.source_kind == normalized_source)
    normalized_customer_source = (customer_source or "").strip().upper()
    if normalized_customer_source == CustomerSource.OPERATION_ENTRY.value:
        filters.append(Lead.source_kind.in_(PUBLIC_POOL_OPERATION_SOURCE_KINDS))
    elif normalized_customer_source == CustomerSource.FRANCHISE_SUPPLIED.value:
        filters.append(Lead.source_kind == LeadSourceKind.SUPPLIER_H5.value)
    normalized_completeness = (completeness or "").strip().upper()
    if normalized_completeness == "INCOMPLETE":
        filters.append(incomplete_condition)
    elif normalized_completeness == "COMPLETE":
        filters.append(~incomplete_condition)
    normalized_duplicate = (duplicate_status or "").strip().upper()
    if normalized_duplicate:
        filters.append(Lead.duplicate_status == normalized_duplicate)
    if created_from:
        filters.append(Lead.created_at >= created_from)
    if created_to:
        filters.append(Lead.created_at < created_to)
    normalized_submitter = (submitter_user_id or "").strip()
    if normalized_submitter:
        filters.append(Lead.submitter_user_id == normalized_submitter)
    return filters


def list_public_pool_leads(
    db: Session,
    *,
    keyword: str | None = None,
    phone_hash: str | None = None,
    customer_source: str | None = None,
    source_kind: str | None = None,
    completeness: str | None = None,
    duplicate_status: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    submitter_user_id: str | None = None,
    page_no: int = 1,
    page_size: int = 20,
) -> tuple[list[Lead], int]:
    filters = public_pool_lead_conditions(
        keyword=keyword,
        phone_hash=phone_hash,
        customer_source=customer_source,
        source_kind=source_kind,
        completeness=completeness,
        duplicate_status=duplicate_status,
        created_from=created_from,
        created_to=created_to,
        submitter_user_id=submitter_user_id,
    )

    stmt = select(Lead).where(*filters)
    count_stmt = select(func.count(Lead.id)).where(*filters)
    total = int(db.scalar(count_stmt) or 0)
    items = list(
        db.scalars(
            stmt.order_by(Lead.created_at.desc(), Lead.id.desc())
            .offset((page_no - 1) * page_size)
            .limit(page_size)
        ).all()
    )
    return items, total
