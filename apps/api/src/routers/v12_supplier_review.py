from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.auth import require_permissions
from ..core.database import get_db
from ..core.errors import AppError
from ..core.models import Lead
from ..core.responses import ok, page
from ..core.v12_enums import LeadSourceKind
from ..services.lead_supply_v12 import (
    get_lead_or_404,
    lead_supply_list_to_dict,
)

router = APIRouter(prefix="/v1.2/admin/supplier-leads", tags=["v1.2-supplier-review"])


@router.get("")
def supplier_review_queue(
    request: Request,
    principal=Depends(require_permissions("lead.supplier.review")),
    db: Session = Depends(get_db),
    status: str | None = Query(default=None),
    review_status: str | None = Query(default=None),
    page_no: int = Query(default=1, alias="page", ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
):
    filters = [Lead.source_kind == LeadSourceKind.SUPPLIER_H5.value]
    if status:
        filters.append(Lead.status == status.strip().upper())
    if review_status:
        filters.append(Lead.review_status == review_status.strip().upper())
    total = db.scalar(select(func.count(Lead.id)).where(*filters)) or 0
    items = db.scalars(
        select(Lead)
        .where(*filters)
        .order_by(Lead.submitted_at.desc(), Lead.created_at.desc())
        .offset((page_no - 1) * page_size)
        .limit(page_size)
    ).all()
    return ok(
        request,
        page(
            lead_supply_list_to_dict(db, list(items), principal),
            total,
            page_no,
            page_size,
        ),
    )


@router.get("/{lead_id}")
def supplier_review_detail(
    lead_id: str,
    request: Request,
    principal=Depends(require_permissions("lead.supplier.review")),
    db: Session = Depends(get_db),
):
    lead = get_lead_or_404(db, lead_id)
    if lead.source_kind != LeadSourceKind.SUPPLIER_H5.value:
        raise AppError("LEAD_SOURCE_INVALID", "该客资不是供应商上传客资", 409)
    return ok(request, lead_supply_list_to_dict(db, [lead], principal)[0])
