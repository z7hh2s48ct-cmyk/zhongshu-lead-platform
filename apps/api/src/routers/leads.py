from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.auth import CurrentPrincipal, require_permissions
from ..core.config import get_settings
from ..core.database import get_db
from ..core.enums import LeadStatus
from ..core.errors import AppError
from ..core.models import Assignment, Lead
from ..core.responses import ok, page
from ..integrations.feishu import FeishuClient, FeishuRecord
from ..schemas.leads import DuplicateDecisionBody, FeishuMockSyncBody, LeadStagingUpdateBody
from ..services.audit import write_audit
from ..services.company_assignment_v12 import require_company_assignment_access
from ..services.dispatch_v12 import CLAIMED_CONTACT_STATUSES
from ..services.feishu_sync_service import configured_mapping, fetch_and_import_feishu, writeback_feishu_results
from ..services.lead_service import import_records, lead_to_dict, update_staging_lead
from ..services.staging_cleanup_service import preview_feishu_staging_cleanup

router = APIRouter(prefix="/leads", tags=["leads"])
settings = get_settings()


def _ensure_feishu_enabled() -> None:
    FeishuClient.ensure_enabled()


def _ensure_feishu_mock_allowed() -> None:
    if settings.app_env.lower() == "production" or not settings.feishu_dev_mock:
        raise AppError("FEISHU_MOCK_DISABLED", "飞书模拟同步仅允许在已启用模拟的非生产环境使用", 404)


def _legacy_mutation_lead_or_raise(db: Session, lead_id: str) -> Lead:
    lead = db.scalar(
        select(Lead)
        .where(Lead.id == lead_id, Lead.deleted_at.is_(None))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if lead is None:
        raise AppError("LEAD_NOT_FOUND", "客资不存在", 404)
    if lead.source_kind is not None:
        raise AppError(
            "LEAD_CORRECTION_API_REQUIRED",
            "V1.2 客资请使用统一客资更正入口",
            409,
        )
    return lead


@router.post("/feishu/mock-sync")
def mock_sync(
    body: FeishuMockSyncBody,
    request: Request,
    principal=Depends(require_permissions("lead.edit")),
    db: Session = Depends(get_db),
):
    _ensure_feishu_mock_allowed()
    records = [FeishuRecord(record_id=item.record_id, fields=item.fields) for item in body.records]
    batch = import_records(
        db,
        records,
        body.field_mapping,
        app_token=settings.feishu_app_token or "dev-app",
        table_id=settings.feishu_table_id or "dev-table",
        requested_by=principal.user_id,
    )
    write_audit(db, principal=principal, action="FEISHU_SYNC_MOCK", resource_type="sync_batch", resource_id=batch.id, after={"total": batch.total_count, "success": batch.success_count, "error": batch.error_count}, request_id=request.state.request_id)
    db.commit()
    return ok(request, {"batch_id": batch.id, "total": batch.total_count, "success": batch.success_count, "errors": batch.error_count})


@router.post("/feishu/sync")
def real_sync(
    request: Request,
    principal=Depends(require_permissions("lead.edit")),
    db: Session = Depends(get_db),
):
    _ensure_feishu_enabled()
    client = FeishuClient()
    batch, records = fetch_and_import_feishu(db, requested_by=principal.user_id, client=client)
    write_audit(
        db,
        principal=principal,
        action="FEISHU_SYNC",
        resource_type="sync_batch",
        resource_id=batch.id,
        after={"total": batch.total_count, "success": batch.success_count, "error": batch.error_count},
        request_id=request.state.request_id,
    )
    db.commit()
    writeback = writeback_feishu_results(db, records, client=client)
    if writeback["failed"]:
        batch.error_message = f"飞书回写失败 {writeback['failed']} 条"
    db.commit()
    return ok(request, {"batch_id": batch.id, "total": batch.total_count, "success": batch.success_count, "errors": batch.error_count, "writeback": writeback})


@router.get("/feishu/diagnostics")
def feishu_diagnostics(request: Request, principal=Depends(require_permissions("lead.read"))):
    return ok(request, {**FeishuClient().diagnostics(), "field_mapping": configured_mapping()})


@router.get("/staging")
def staging_list(
    request: Request,
    principal=Depends(require_permissions("lead.read")),
    db: Session = Depends(get_db),
    status: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    page_no: int = Query(default=1, alias="page", ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
):
    statuses = [LeadStatus.IMPORTED, LeadStatus.IMPORT_ERROR, LeadStatus.DUPLICATE_REVIEW]
    stmt = select(Lead).where(Lead.status.in_(statuses), Lead.deleted_at.is_(None))
    count_stmt = select(func.count(Lead.id)).where(
        Lead.status.in_(statuses),
        Lead.deleted_at.is_(None),
    )
    if status:
        stmt = stmt.where(Lead.status == status)
        count_stmt = count_stmt.where(Lead.status == status)
    if keyword:
        stmt = stmt.where(Lead.customer_name.contains(keyword) | Lead.city.contains(keyword))
        count_stmt = count_stmt.where(Lead.customer_name.contains(keyword) | Lead.city.contains(keyword))
    total = db.scalar(count_stmt) or 0
    items = db.scalars(stmt.order_by(Lead.imported_at.desc()).offset((page_no - 1) * page_size).limit(page_size)).all()
    return ok(request, page([lead_to_dict(x, principal) for x in items], total, page_no, page_size))


@router.get("/staging-cleanup-preview")
def staging_cleanup_preview(
    request: Request,
    principal=Depends(require_permissions("*")),
    db: Session = Depends(get_db),
):
    preview = preview_feishu_staging_cleanup(db)
    return ok(
        request,
        {
            **preview,
            "scope": "仅清理飞书来源、仍处于暂存状态、且未进入核验、派发或退回流程的客资",
        },
    )


@router.get("/{lead_id}")
def get_lead(lead_id: str, request: Request, principal: CurrentPrincipal, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead or lead.deleted_at is not None:
        raise AppError("LEAD_NOT_FOUND", "客资不存在", 404)
    reveal_phone: bool | None = None
    if principal.has_any_role("FRANCHISE_OWNER", "FRANCHISE_EMPLOYEE"):
        assignment_query = select(Assignment).where(
            Assignment.lead_id == lead.id,
            Assignment.company_id == principal.company_id,
        )
        if lead.current_assignment_id:
            assignment_query = assignment_query.where(
                Assignment.id == lead.current_assignment_id
            )
        else:
            assignment_query = assignment_query.order_by(Assignment.assigned_at.desc())
        assignment = db.scalar(assignment_query)
        if not assignment:
            raise AppError("FORBIDDEN", "无权查看该客资", 403)
        require_company_assignment_access(principal, assignment)
        reveal_phone = assignment.status in CLAIMED_CONTACT_STATUSES
    elif not (principal.can("lead.read") or principal.can("*")):
        raise AppError("FORBIDDEN", "无权查看该客资", 403)
    return ok(
        request,
        lead_to_dict(
            lead,
            principal,
            include_raw=True,
            reveal_phone=reveal_phone,
        ),
    )


@router.patch("/{lead_id}/staging")
def update_staging(
    lead_id: str,
    body: LeadStagingUpdateBody,
    request: Request,
    principal=Depends(require_permissions("lead.edit")),
    db: Session = Depends(get_db),
):
    lead = _legacy_mutation_lead_or_raise(db, lead_id)
    before = lead_to_dict(lead, principal)
    update_staging_lead(db, lead, body.model_dump(exclude_unset=True))
    write_audit(db, principal=principal, action="LEAD_STAGING_UPDATE", resource_type="lead", resource_id=lead.id, before=before, after=lead_to_dict(lead, principal), request_id=request.state.request_id)
    db.commit()
    return ok(request, lead_to_dict(lead, principal))


@router.post("/{lead_id}/duplicate-decision")
def decide_duplicate(
    lead_id: str,
    body: DuplicateDecisionBody,
    request: Request,
    principal=Depends(require_permissions("lead.edit")),
    db: Session = Depends(get_db),
):
    lead = _legacy_mutation_lead_or_raise(db, lead_id)
    relation = db.scalar(select(LeadDuplicateRelation).where(LeadDuplicateRelation.lead_id == lead_id, LeadDuplicateRelation.duplicate_lead_id == body.duplicate_lead_id))
    if not relation:
        raise AppError("DUPLICATE_RELATION_NOT_FOUND", "疑似重复关系不存在", 404)
    relation.decision = body.decision
    relation.decided_by = principal.user_id
    from datetime import datetime, timezone
    relation.decided_at = datetime.now(timezone.utc)
    if body.decision in {"CONFIRMED", "KEEP_FIRST"}:
        lead.status = LeadStatus.INVALID
        lead.pending_reason = "DUPLICATE_CONFIRMED"
    else:
        lead.status = LeadStatus.IMPORTED
        lead.pending_reason = None
    write_audit(db, principal=principal, action="LEAD_DUPLICATE_DECISION", resource_type="lead", resource_id=lead_id, after={"decision": body.decision, "duplicate_lead_id": body.duplicate_lead_id}, request_id=request.state.request_id)
    db.commit()
    return ok(request)


@router.get("/{lead_id}/issues")
def lead_issues(lead_id: str, request: Request, principal=Depends(require_permissions("lead.read")), db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if lead is None or lead.deleted_at is not None:
        raise AppError("LEAD_NOT_FOUND", "客资不存在", 404)
    items = db.scalars(select(LeadImportIssue).where(LeadImportIssue.lead_id == lead_id).order_by(LeadImportIssue.created_at.desc())).all()
    return ok(request, [{"id": x.id, "type": x.issue_type, "field": x.field_name, "message": x.message, "resolved_at": x.resolved_at.isoformat() if x.resolved_at else None} for x in items])
