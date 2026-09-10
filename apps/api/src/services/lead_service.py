from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..core.auth import Principal
from ..core.enums import ImportIssueType, LeadStatus
from ..core.errors import AppError
from ..core.models import Lead, LeadDuplicateRelation, LeadImportIssue, Region, SyncBatch
from ..core.security import decrypt_text, encrypt_text, hash_phone, mask_phone, normalize_phone
from ..integrations.feishu import FeishuRecord
from .phone_uniqueness import require_unique_lead_phone


def _field(fields: dict[str, Any], mapping: dict[str, str], key: str) -> Any:
    value = fields.get(mapping.get(key, key))
    if isinstance(value, list):
        if len(value) == 1:
            value = value[0]
        else:
            value = ", ".join(str(item.get("text", item) if isinstance(item, dict) else item) for item in value)
    if isinstance(value, dict) and "text" in value:
        value = value["text"]
    return value


def _resolve_region_code(db: Session, explicit_code: str | None, city: str | None, district: str | None) -> str | None:
    if explicit_code and db.get(Region, explicit_code):
        return explicit_code
    candidates = [district, city]
    for candidate in candidates:
        if not candidate:
            continue
        region = db.scalar(select(Region).where(Region.active.is_(True), or_(Region.name == candidate, Region.aliases.contains([candidate]))))
        if region:
            return region.code
    return None


def lead_to_dict(
    lead: Lead,
    principal: Principal | None = None,
    *,
    include_raw: bool = False,
    reveal_phone: bool | None = None,
) -> dict[str, Any]:
    phone = decrypt_text(lead.phone_encrypted)
    can_view_phone = (
        reveal_phone
        if reveal_phone is not None
        else bool(
            principal
            and (
                principal.can("lead.phone.read")
                or principal.can("lead.own.phone.read")
                or principal.can("*")
            )
        )
    )
    data: dict[str, Any] = {
        "id": lead.id,
        "customer_name": lead.customer_name,
        "phone": phone if can_view_phone else None,
        "phone_masked": mask_phone(phone),
        "province": lead.province,
        "city": lead.city,
        "district": lead.district,
        "region_code": lead.region_code,
        "category_code": lead.category_code,
        "brand_code": lead.brand_code,
        "source_channel": lead.source_channel,
        "need_summary": lead.need_summary,
        "budget_min": lead.budget_min,
        "budget_max": lead.budget_max,
        "status": lead.status,
        "pending_reason": lead.pending_reason,
        "current_assignment_id": lead.current_assignment_id,
        "current_follow_status": lead.current_follow_status,
        "imported_at": lead.imported_at.isoformat(),
        "verified_at": lead.verified_at.isoformat() if lead.verified_at else None,
    }
    if principal and (principal.can("*") or principal.can("dashboard.finance.read")):
        data["acquisition_cost_cents"] = lead.acquisition_cost_cents
    if include_raw and principal and principal.can("*"):
        data["raw_payload"] = lead.raw_payload
    return data


def import_records(
    db: Session,
    records: list[FeishuRecord],
    field_mapping: dict[str, str],
    *,
    app_token: str,
    table_id: str,
    requested_by: str | None = None,
    duplicate_window_days: int = 30,
) -> SyncBatch:
    batch = SyncBatch(status="RUNNING", total_count=len(records), requested_by=requested_by)
    db.add(batch)
    db.flush()
    for record in records:
        try:
            existing = db.scalar(
                select(Lead).where(
                    Lead.source_app_token == app_token,
                    Lead.source_table_id == table_id,
                    Lead.source_record_id == record.record_id,
                )
            )
            if existing:
                batch.success_count += 1
                continue
            fields = record.fields
            name = str(_field(fields, field_mapping, "customer_name") or "").strip()
            raw_phone = str(_field(fields, field_mapping, "phone") or "").strip()
            normalized_phone = normalize_phone(raw_phone)
            province = str(_field(fields, field_mapping, "province") or "").strip() or None
            city = str(_field(fields, field_mapping, "city") or "").strip() or None
            district = str(_field(fields, field_mapping, "district") or "").strip() or None
            explicit_region = str(_field(fields, field_mapping, "region_code") or "").strip() or None
            region_code = _resolve_region_code(db, explicit_region, city, district)
            category = str(_field(fields, field_mapping, "category_code") or "").strip() or None
            brand = str(_field(fields, field_mapping, "brand_code") or "").strip() or None
            source_channel = str(_field(fields, field_mapping, "source_channel") or "").strip() or None
            need_summary = str(_field(fields, field_mapping, "need_summary") or "").strip() or None
            budget_min = _as_int(_field(fields, field_mapping, "budget_min"))
            budget_max = _as_int(_field(fields, field_mapping, "budget_max"))
            cost = _as_int(_field(fields, field_mapping, "acquisition_cost")) or 0

            if normalized_phone:
                normalized_phone = require_unique_lead_phone(db, phone=normalized_phone)
            placeholder_phone = normalized_phone or f"missing-{record.record_id}"
            lead = Lead(
                source_type="FEISHU",
                source_app_token=app_token,
                source_table_id=table_id,
                source_record_id=record.record_id,
                source_channel=source_channel,
                customer_name=name or "未填写",
                phone_encrypted=encrypt_text(placeholder_phone),
                phone_hash=hash_phone(placeholder_phone),
                province=province,
                city=city,
                district=district,
                region_code=region_code,
                category_code=category,
                brand_code=brand,
                need_summary=need_summary,
                budget_min=budget_min,
                budget_max=budget_max,
                acquisition_cost_cents=cost,
                status=LeadStatus.IMPORTED,
                raw_payload=fields,
            )
            db.add(lead)
            db.flush()
            blocking_issues = 0
            if not name:
                _issue(db, lead, batch, ImportIssueType.MISSING_FIELD, "customer_name", "缺少客户姓名")
                blocking_issues += 1
            if len(normalized_phone) != 11:
                _issue(db, lead, batch, ImportIssueType.INVALID_PHONE, "phone", "手机号缺失或格式错误")
                blocking_issues += 1
            if not city:
                _issue(db, lead, batch, ImportIssueType.MISSING_FIELD, "city", "缺少城市")
                blocking_issues += 1
            elif not region_code:
                _issue(db, lead, batch, ImportIssueType.UNKNOWN_REGION, "region_code", "城市无法匹配标准地区")
                blocking_issues += 1

            if len(normalized_phone) == 11:
                window = datetime.now(timezone.utc) - timedelta(days=duplicate_window_days)
                duplicate = db.scalar(
                    select(Lead)
                    .where(Lead.phone_hash == lead.phone_hash, Lead.id != lead.id, Lead.imported_at >= window)
                    .order_by(Lead.imported_at.asc())
                )
                if duplicate:
                    db.add(LeadDuplicateRelation(lead_id=lead.id, duplicate_lead_id=duplicate.id, reason="PHONE_WITHIN_WINDOW"))
                    _issue(db, lead, batch, ImportIssueType.DUPLICATE_SUSPECTED, "phone", "最近窗口内存在相同手机号客资")
                    lead.status = LeadStatus.DUPLICATE_REVIEW
                    lead.pending_reason = "DUPLICATE_SUSPECTED"
            if blocking_issues:
                lead.status = LeadStatus.IMPORT_ERROR
                lead.pending_reason = "FIELD_VALIDATION_FAILED"
                batch.error_count += 1
            else:
                batch.success_count += 1
        except Exception as exc:
            batch.error_count += 1
            db.add(
                LeadImportIssue(
                    sync_batch_id=batch.id,
                    issue_type=ImportIssueType.SOURCE_ERROR,
                    message=f"record {record.record_id}: {exc}",
                )
            )
    batch.status = "COMPLETED" if batch.error_count == 0 else "PARTIAL"
    batch.finished_at = datetime.now(timezone.utc)
    return batch


def update_staging_lead(db: Session, lead: Lead, changes: dict[str, Any]) -> Lead:
    allowed = {
        "customer_name",
        "province",
        "city",
        "district",
        "region_code",
        "category_code",
        "brand_code",
        "source_channel",
        "need_summary",
        "budget_min",
        "budget_max",
        "acquisition_cost_cents",
    }
    for field, value in changes.items():
        if field in allowed and value is not None:
            setattr(lead, field, value)
    if changes.get("phone"):
        normalized = require_unique_lead_phone(
            db,
            phone=str(changes["phone"]),
            exclude_lead_id=lead.id,
        )
        if len(normalized) != 11:
            raise AppError("LEAD_PHONE_INVALID", "手机号格式错误", 422)
        lead.phone_encrypted = encrypt_text(normalized)
        lead.phone_hash = hash_phone(normalized)
    unresolved = db.scalar(
        select(func.count(LeadImportIssue.id)).where(LeadImportIssue.lead_id == lead.id, LeadImportIssue.resolved_at.is_(None))
    ) or 0
    if unresolved:
        now = datetime.now(timezone.utc)
        issues = db.scalars(select(LeadImportIssue).where(LeadImportIssue.lead_id == lead.id, LeadImportIssue.resolved_at.is_(None))).all()
        for issue in issues:
            if issue.issue_type != ImportIssueType.DUPLICATE_SUSPECTED:
                issue.resolved_at = now
    if lead.status == LeadStatus.IMPORT_ERROR:
        lead.status = LeadStatus.IMPORTED
        lead.pending_reason = None
    return lead


def _issue(db: Session, lead: Lead, batch: SyncBatch, issue_type: str, field_name: str, message: str) -> None:
    db.add(LeadImportIssue(lead_id=lead.id, sync_batch_id=batch.id, issue_type=issue_type, field_name=field_name, message=message))


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).replace(",", "")))
    except (ValueError, TypeError):
        return None
