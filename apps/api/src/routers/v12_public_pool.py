from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.auth import require_permissions
from ..core.database import get_db
from ..core.errors import AppError
from ..core.models import Lead
from ..core.responses import ok, page
from ..core.security import hash_phone
from ..core.v12_enums import CustomerSource
from ..integrations.feishu import FeishuClient
from ..schemas.v12_lead_supply import LeadDraftBody, LeadDraftUpdateBody
from ..schemas.v12_public_pool import PublicPoolFeishuImportBody
from ..schemas.v12_reports import normalize_exact_phone
from ..services.audit import write_audit
from ..services.lead_supply_v12 import lead_supply_list_to_dict, lead_supply_to_dict
from ..services.public_pool_v12 import (
    create_public_pool_lead,
    import_feishu_customer_view,
    list_public_pool_leads,
    public_pool_validation_errors,
    require_public_pool_lead,
    transfer_public_pool_lead,
    update_public_pool_lead,
)


router = APIRouter(prefix="/v1.2/public-pool", tags=["v1.2-public-pool"])


def _validated_created_range(
    created_from: datetime | None,
    created_to: datetime | None,
) -> tuple[datetime | None, datetime | None]:
    for value in (created_from, created_to):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise AppError(
                "DATE_TIMEZONE_REQUIRED",
                "日期时间必须包含时区，例如 +08:00",
                422,
            )
    normalized_from = created_from.astimezone(timezone.utc) if created_from else None
    normalized_to = created_to.astimezone(timezone.utc) if created_to else None
    if normalized_from and normalized_to and normalized_from > normalized_to:
        raise AppError("DATE_RANGE_INVALID", "起始时间不能晚于结束时间", 422)
    return normalized_from, normalized_to


@router.get("/leads")
def public_pool_list(
    request: Request,
    principal=Depends(require_permissions("lead.manual.manage")),
    db: Session = Depends(get_db),
    keyword: str | None = Query(default=None, max_length=128),
    phone: str | None = Query(default=None, max_length=32),
    customer_source: CustomerSource | None = Query(default=None),
    source_kind: str | None = Query(default=None, max_length=32),
    completeness: str | None = Query(default=None, max_length=32),
    duplicate_status: str | None = Query(default=None, max_length=32),
    created_from: datetime | None = Query(default=None),
    created_to: datetime | None = Query(default=None),
    submitter_user_id: str | None = Query(default=None, max_length=36),
    page_no: int = Query(default=1, alias="page", ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
):
    created_from, created_to = _validated_created_range(created_from, created_to)
    try:
        normalized_phone = normalize_exact_phone(phone)
    except ValueError as exc:
        raise AppError("PHONE_INVALID", str(exc), 422) from exc
    items, total = list_public_pool_leads(
        db,
        keyword=keyword,
        phone_hash=hash_phone(normalized_phone) if normalized_phone else None,
        customer_source=customer_source.value if customer_source else None,
        source_kind=source_kind,
        completeness=completeness,
        duplicate_status=duplicate_status,
        created_from=created_from,
        created_to=created_to,
        submitter_user_id=submitter_user_id,
        page_no=page_no,
        page_size=page_size,
    )
    data = lead_supply_list_to_dict(db, items, principal)
    for item, serialized in zip(items, data, strict=True):
        serialized["public_pool_validation_errors"] = public_pool_validation_errors(
            db,
            item,
        )
    return ok(request, page(data, total, page_no, page_size))


@router.post("/leads")
def public_pool_create(
    body: LeadDraftBody,
    request: Request,
    principal=Depends(require_permissions("lead.manual.manage")),
    db: Session = Depends(get_db),
):
    lead = create_public_pool_lead(
        db,
        principal=principal,
        values=body.model_dump(exclude_none=True),
    )
    write_audit(
        db,
        principal=principal,
        action="V12_PUBLIC_POOL_LEAD_CREATE",
        resource_type="lead",
        resource_id=lead.id,
        after={"status": lead.status, "source_kind": lead.source_kind},
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, lead_supply_to_dict(lead, principal), "客户已保存到公海池")


@router.patch("/leads/{lead_id}")
def public_pool_update(
    lead_id: str,
    body: LeadDraftUpdateBody,
    request: Request,
    principal=Depends(require_permissions("lead.manual.manage")),
    db: Session = Depends(get_db),
):
    lead = require_public_pool_lead(
        db.scalar(select(Lead).where(Lead.id == lead_id).with_for_update())
    )
    before = lead_supply_to_dict(lead, principal)
    update_public_pool_lead(
        db,
        lead=lead,
        principal=principal,
        values=body.model_dump(exclude_unset=True),
    )
    after = lead_supply_to_dict(lead, principal)
    write_audit(
        db,
        principal=principal,
        action="V12_PUBLIC_POOL_LEAD_UPDATE",
        resource_type="lead",
        resource_id=lead.id,
        before=before,
        after=after,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, after, "公海池客户已更新")


@router.post("/leads/{lead_id}/transfer-to-dispatch")
def public_pool_transfer(
    lead_id: str,
    request: Request,
    principal=Depends(require_permissions("lead.manual.manage")),
    db: Session = Depends(get_db),
):
    lead = require_public_pool_lead(
        db.scalar(select(Lead).where(Lead.id == lead_id).with_for_update())
    )
    before = lead_supply_to_dict(lead, principal)
    result = transfer_public_pool_lead(db, lead=lead, principal=principal)
    after = lead_supply_to_dict(lead, principal)
    write_audit(
        db,
        principal=principal,
        action=(
            "V12_PUBLIC_POOL_TRANSFER"
            if result.transferred
            else "V12_PUBLIC_POOL_TRANSFER_BLOCKED"
        ),
        resource_type="lead",
        resource_id=lead.id,
        before=before,
        after=after,
        metadata={
            "result": "TRANSFERRED" if result.transferred else "BLOCKED",
            "customer_source": after["customer_source"],
            "region_code": lead.region_code,
            "supplier_company_id": lead.supplier_company_id,
            "pending_reason": lead.pending_reason,
            "validation_errors": result.validation_errors,
            "dedup_decision": result.dedup.decision.value if result.dedup else None,
        },
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(
        request,
        {
            "lead": after,
            "transferred": result.transferred,
            "validation_errors": result.validation_errors,
            "dedup_decision": result.dedup.decision.value if result.dedup else None,
        },
        "客户已进入派发池" if result.transferred else "客户仍留在公海池，请处理提示项",
    )


@router.get("/feishu/diagnostics")
def public_pool_feishu_diagnostics(
    request: Request,
    principal=Depends(require_permissions("lead.manual.manage")),
):
    return ok(request, FeishuClient().diagnostics())


@router.post("/feishu/import")
def public_pool_feishu_import(
    body: PublicPoolFeishuImportBody,
    request: Request,
    principal=Depends(require_permissions("lead.manual.manage")),
    db: Session = Depends(get_db),
):
    result = import_feishu_customer_view(
        db,
        principal=principal,
        target=body.target_pool,
    )
    summary = {
        "batch_id": result.batch.id,
        "target_pool": body.target_pool.value,
        "total_count": result.total_count,
        "created_count": result.created_count,
        "skipped_count": result.skipped_count,
        "public_pool_count": result.public_pool_count,
        "dispatch_pool_count": result.dispatch_pool_count,
        "duplicate_count": result.duplicate_count,
        "incomplete_count": result.incomplete_count,
        "error_count": result.error_count,
    }
    write_audit(
        db,
        principal=principal,
        action="V12_PUBLIC_POOL_FEISHU_IMPORT",
        resource_type="sync_batch",
        resource_id=result.batch.id,
        after=summary,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, summary, "飞书客户视图导入完成")
