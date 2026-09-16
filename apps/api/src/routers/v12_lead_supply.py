from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core.auth import CurrentPrincipal, require_permissions
from ..core.database import get_db
from ..core.errors import AppError
from ..core.models import Assignment, Company, Lead, Region, VerificationTask
from ..core.models_v12 import CompanyLeadCapability, CompanyServiceAreaV12
from ..core.responses import ok, page
from ..core.v12_enums import LeadSourceKind, VerificationTaskType
from ..schemas.v12_lead_supply import (
    CapabilityRequestBody,
    CapabilityReviewBody,
    CompanyCapabilityConfigureBody,
    CompanyProfileBulkApproveBody,
    DedupOverrideBody,
    LeadDraftBody,
    PlatformLeadDraftBody,
    PlatformLeadPreDispatchBody,
    LeadCorrectionBody,
    LeadCorrectionRedispatchBody,
    LeadDeleteBody,
    LeadCorrectionRecheckBody,
    LeadDraftUpdateBody,
    LeadQuickDispatchBody,
    ServiceAreaReplaceBody,
    ServiceAreaReviewBody,
    SupplierReviewBody,
    TestLeadDeleteBody,
)
from ..services.lead_deletion_v12 import delete_operation_lead, preview_lead_deletion, require_lead_not_deleted
from ..services.supply_termination import require_supply_write_enabled
from ..services.audit import write_audit
from ..services.company_profile_v12 import (
    list_capabilities,
    list_service_areas,
    approve_pending_profile,
    configure_capability,
    replace_service_areas,
    request_capability,
    require_active_company,
    review_capability,
    review_service_area,
)
from ..services.company_service import company_to_dict
from ..services.dedup_v12 import override_duplicate
from ..services.china_regions import region_by_code
from ..services.dispatch_v12 import (
    acquire_manual_dispatch_idempotency_lock,
    candidate_to_dict,
    count_candidates,
    dispatch_manually_with_outcome,
    list_candidates,
    manual_dispatch_idempotency_guard,
)
from ..services.lead_supply_v12 import (
    create_draft,
    correct_platform_lead,
    delete_test_lead_permanently,
    discard_draft,
    get_lead_or_404,
    lead_supply_list_to_dict,
    lead_supply_to_dict,
    list_platform_leads as list_platform_leads_service,
    list_supplier_leads,
    preview_test_lead_delete,
    recheck_platform_lead_correction,
    release_corrected_lead_for_redispatch,
    release_misdispatched_lead_for_redispatch,
    reopen_platform_lead_for_correction,
    reopen_rejected_supplier_lead,
    review_supplier_lead,
    submit_draft,
    update_draft,
)
from ..services.pre_dispatch_v12 import assign_pre_dispatch_task
from ..services.points_service import ledger_to_dict

router = APIRouter(prefix="/v1.2", tags=["v1.2-lead-supply"])

QUICK_DISPATCH_HASH_KEY = "quick_dispatch_request_hash"
PRE_DISPATCH_CREATE_HASH_KEY = "pre_dispatch_create_request_hash"
PRE_DISPATCH_CREATE_TASK_KEY = "pre_dispatch_create_task_id"
PRE_DISPATCH_CREATE_SOURCE_APP = "zhongshu-internal"
PRE_DISPATCH_CREATE_SOURCE_TABLE = "platform-pre-dispatch"


def _dedup_dict(result) -> dict | None:
    if result is None:
        return None
    return {
        "decision": result.decision.value,
        "matched_lead_id": result.matched_lead_id,
        "window_days": result.window_days,
        "age_days": result.age_days,
        "event_id": result.event_id,
        "blocks_dispatch": result.blocks_dispatch,
        "reward_eligible": result.reward_eligible,
    }


def _quick_dispatch_hash(body: LeadQuickDispatchBody) -> str:
    payload = body.model_dump(exclude={"idempotency_key"}, mode="json")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _pre_dispatch_create_hash(body: PlatformLeadPreDispatchBody) -> str:
    payload = body.model_dump(exclude={"idempotency_key"}, mode="json")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _pre_dispatch_create_record_id(user_id: str, idempotency_key: str) -> str:
    return hashlib.sha256(f"{user_id}:{idempotency_key}".encode("utf-8")).hexdigest()


def _existing_pre_dispatch_create(
    db: Session,
    *,
    user_id: str,
    body: PlatformLeadPreDispatchBody,
    request_hash: str,
) -> tuple[Lead, VerificationTask] | None:
    lead = db.scalar(
        select(Lead).where(
            Lead.source_app_token == PRE_DISPATCH_CREATE_SOURCE_APP,
            Lead.source_table_id == PRE_DISPATCH_CREATE_SOURCE_TABLE,
            Lead.source_record_id
            == _pre_dispatch_create_record_id(user_id, body.idempotency_key),
        )
    )
    if lead is None:
        return None
    raw_payload = lead.raw_payload or {}
    if raw_payload.get(PRE_DISPATCH_CREATE_HASH_KEY) != request_hash:
        raise AppError("IDEMPOTENCY_CONFLICT", "幂等键已被其他直派电销请求使用", 409)
    task_id = raw_payload.get(PRE_DISPATCH_CREATE_TASK_KEY)
    task = db.get(VerificationTask, task_id) if task_id else None
    if (
        task is None
        or task.lead_id != lead.id
        or task.task_type != VerificationTaskType.PRE_DISPATCH_VERIFY.value
    ):
        raise AppError(
            "IDEMPOTENCY_STATE_INVALID",
            "直派电销请求已存在但任务状态不完整，请联系管理员处理",
            409,
        )
    return lead, task


def _quick_assignment_dict(assignment) -> dict:
    return {
        "id": assignment.id,
        "lead_id": assignment.lead_id,
        "company_id": assignment.company_id,
        "receiver_company_id": assignment.receiver_company_id,
        "status": assignment.status,
        "points_price": assignment.points_price,
        "assigned_by_user_id": assignment.assigned_by,
        "internal_assignee_user_id": assignment.internal_assignee_user_id,
        "assigned_at": assignment.assigned_at.isoformat(),
    }


def _existing_quick_dispatch(
    db: Session,
    *,
    body: LeadQuickDispatchBody,
    request_hash: str,
) -> tuple[Assignment, Lead] | None:
    assignment = db.scalar(
        select(Assignment).where(Assignment.idempotency_key == body.idempotency_key)
    )
    if assignment is None:
        return None
    lead = db.get(Lead, assignment.lead_id)
    stored_hash = (lead.raw_payload or {}).get(QUICK_DISPATCH_HASH_KEY) if lead else None
    if (
        lead is None
        or assignment.company_id != body.company_id
        or stored_hash != request_hash
    ):
        raise AppError("IDEMPOTENCY_CONFLICT", "幂等键已被其他快捷派发请求使用", 409)
    return assignment, lead


def _pre_dispatch_task_dict(task) -> dict:
    return {
        "id": task.id,
        "task_type": task.task_type,
        "status": task.status,
        "assignee_user_id": task.assignee_user_id,
        "due_at": task.due_at.isoformat() if task.due_at else None,
    }


def _capability_dict(
    item: CompanyLeadCapability,
    *,
    company_name: str | None = None,
    company_code: str | None = None,
) -> dict:
    result = {
        "id": item.id,
        "company_id": item.company_id,
        "capability_code": item.capability_code,
        "active": item.active,
        "review_status": item.review_status,
        "review_note": item.review_note,
        "reviewed_by": item.reviewed_by,
        "reviewed_at": item.reviewed_at.isoformat() if item.reviewed_at else None,
    }
    if company_name is not None:
        result["company_name"] = company_name
    if company_code is not None:
        result["company_code"] = company_code
    return result


def _area_dict(
    item: CompanyServiceAreaV12,
    *,
    db: Session | None = None,
    company_name: str | None = None,
    company_code: str | None = None,
) -> dict:
    region = region_by_code(item.region_code)
    region_name = None
    if region is not None:
        region_name = " · ".join(
            part for part in (region["city_name"], region["district_name"]) if part
        )
    elif db is not None:
        current = db.get(Region, item.region_code)
        names: list[str] = []
        visited: set[str] = set()
        while current is not None and current.code not in visited:
            visited.add(current.code)
            names.append(current.name)
            current = db.get(Region, current.parent_code) if current.parent_code else None
        if names:
            region_name = " · ".join(reversed(names))
    result = {
        "id": item.id,
        "company_id": item.company_id,
        "region_code": item.region_code,
        "region_name": region_name,
        "region_level": item.region_level,
        "is_primary_city": item.is_primary_city,
        "active": item.active,
        "review_status": item.review_status,
        "review_note": item.review_note,
        "reviewed_by": item.reviewed_by,
        "reviewed_at": item.reviewed_at.isoformat() if item.reviewed_at else None,
    }
    if company_name is not None:
        result["company_name"] = company_name
    if company_code is not None:
        result["company_code"] = company_code
    return result


def _platform_lead_or_raise(db: Session, lead_id: str, *, lock: bool = False) -> Lead:
    if lock:
        lead = db.scalar(
            select(Lead)
            .where(Lead.id == lead_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        require_lead_not_deleted(lead)
    else:
        lead = get_lead_or_404(db, lead_id)
    if lead.source_kind != LeadSourceKind.PLATFORM_MANUAL.value:
        raise AppError("LEAD_SOURCE_INVALID", "仅平台来源客资可从此入口查看", 409)
    return lead


def _lead_detail_dict(db: Session, lead: Lead, principal) -> dict:
    return lead_supply_list_to_dict(
        db,
        [lead],
        principal,
        include_assignment_history=True,
    )[0]


@router.post("/platform/leads")
def create_platform_lead(
    body: PlatformLeadDraftBody,
    request: Request,
    principal=Depends(require_permissions("lead.manual.manage")),
    db: Session = Depends(get_db),
):
    lead = create_draft(
        db,
        principal=principal,
        source_kind=LeadSourceKind.PLATFORM_MANUAL,
        values=body.model_dump(exclude_none=True),
    )
    write_audit(
        db,
        principal=principal,
        action="V12_PLATFORM_LEAD_DRAFT_CREATE",
        resource_type="lead",
        resource_id=lead.id,
        after={"status": lead.status, "source_kind": lead.source_kind},
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, lead_supply_to_dict(lead, principal), "客资草稿已创建")


@router.post("/platform/leads/pre-dispatch-verification")
def create_platform_lead_for_pre_dispatch(
    body: PlatformLeadPreDispatchBody,
    request: Request,
    principal=Depends(
        require_permissions("lead.manual.manage", "lead.supplier.review")
    ),
    db: Session = Depends(get_db),
):
    request_hash = _pre_dispatch_create_hash(body)
    idempotency_scope = (
        f"platform-pre-dispatch:{principal.user_id}:{body.idempotency_key}"
    )
    try:
        with manual_dispatch_idempotency_guard(idempotency_scope):
            acquire_manual_dispatch_idempotency_lock(db, idempotency_scope)
            replay = _existing_pre_dispatch_create(
                db,
                user_id=principal.user_id,
                body=body,
                request_hash=request_hash,
            )
            if replay is not None:
                lead, task = replay
                return ok(
                    request,
                    {
                        "lead": lead_supply_to_dict(lead, principal),
                        "task": _pre_dispatch_task_dict(task),
                        "idempotent": True,
                    },
                    "客资已保存并派发电销核验",
                )

            lead_values = body.model_dump(
                exclude={"assignee_user_id", "reason", "idempotency_key"},
                exclude_none=True,
            )
            lead = create_draft(
                db,
                principal=principal,
                source_kind=LeadSourceKind.PLATFORM_MANUAL,
                values=lead_values,
            )
            lead.source_app_token = PRE_DISPATCH_CREATE_SOURCE_APP
            lead.source_table_id = PRE_DISPATCH_CREATE_SOURCE_TABLE
            lead.source_record_id = _pre_dispatch_create_record_id(
                principal.user_id,
                body.idempotency_key,
            )
            lead.raw_payload = {
                **(lead.raw_payload or {}),
                PRE_DISPATCH_CREATE_HASH_KEY: request_hash,
            }
            db.flush()
            write_audit(
                db,
                principal=principal,
                action="V12_PLATFORM_LEAD_DRAFT_CREATE",
                resource_type="lead",
                resource_id=lead.id,
                after={
                    "status": lead.status,
                    "source_kind": lead.source_kind,
                    "pre_dispatch": True,
                },
                request_id=request.state.request_id,
            )
            assignment = assign_pre_dispatch_task(
                db,
                lead_id=lead.id,
                assignee_user_id=body.assignee_user_id,
                assigned_by=principal.user_id,
                reason=body.reason,
            )
            task = assignment.task
            lead.raw_payload = {
                **(lead.raw_payload or {}),
                PRE_DISPATCH_CREATE_TASK_KEY: task.id,
            }
            write_audit(
                db,
                principal=principal,
                action="V12_PRE_DISPATCH_VERIFY_ASSIGN",
                resource_type="verification_task",
                resource_id=task.id,
                before=assignment.before,
                after=assignment.after,
                reason=body.reason,
                request_id=request.state.request_id,
            )
            db.commit()
            return ok(
                request,
                {
                    "lead": lead_supply_to_dict(lead, principal),
                    "task": _pre_dispatch_task_dict(task),
                    "idempotent": False,
                },
                "客资已保存并派发电销核验",
            )
    except IntegrityError:
        db.rollback()
        replay = _existing_pre_dispatch_create(
            db,
            user_id=principal.user_id,
            body=body,
            request_hash=request_hash,
        )
        if replay is None:
            raise
        lead, task = replay
        return ok(
            request,
            {
                "lead": lead_supply_to_dict(lead, principal),
                "task": _pre_dispatch_task_dict(task),
                "idempotent": True,
            },
            "客资已保存并派发电销核验",
        )
    except Exception:
        db.rollback()
        raise


@router.post("/platform/leads/quick-dispatch/candidates")
def preview_platform_lead_quick_dispatch_candidates(
    body: PlatformLeadDraftBody,
    request: Request,
    principal=Depends(require_permissions("lead.manual.manage", "lead.dispatch")),
    db: Session = Depends(get_db),
    keyword: str | None = Query(default=None, max_length=128),
    page_no: int = Query(default=1, alias="page", ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
):
    try:
        lead = create_draft(
            db,
            principal=principal,
            source_kind=LeadSourceKind.PLATFORM_MANUAL,
            values=body.model_dump(exclude_none=True),
        )
        dedup = submit_draft(
            db,
            lead=lead,
            principal=principal,
            checkpoint="QUICK_DISPATCH_PREVIEW",
        )
        if lead.status != "READY_DISPATCH" or lead.current_assignment_id:
            raise AppError(
                "LEAD_NOT_READY_DISPATCH",
                "当前客资无法直接派发",
                409,
                {
                    "status": lead.status,
                    "duplicate_status": lead.duplicate_status,
                    "dedup": _dedup_dict(dedup),
                },
            )
        candidates = list_candidates(
            db,
            lead=lead,
            keyword=keyword,
            page_no=page_no,
            page_size=page_size,
        )
        include_financials = principal.can("points.read") or principal.can("*")
        eligible = [item for item in candidates if item.eligible]
        data = {
            "candidates": [
                candidate_to_dict(item, include_financials=include_financials)
                for item in eligible
            ],
            "page": page_no,
            "page_size": page_size,
            "page_eligible_count": len(eligible),
            "total_companies": count_candidates(db, keyword=keyword),
        }
    finally:
        db.rollback()
    return ok(request, data)


@router.post("/platform/leads/quick-dispatch")
def quick_dispatch_platform_lead(
    body: LeadQuickDispatchBody,
    request: Request,
    principal=Depends(require_permissions("lead.manual.manage", "lead.dispatch")),
    db: Session = Depends(get_db),
):
    request_hash = _quick_dispatch_hash(body)
    try:
        with manual_dispatch_idempotency_guard(body.idempotency_key):
            acquire_manual_dispatch_idempotency_lock(db, body.idempotency_key)
            replay = _existing_quick_dispatch(
                db,
                body=body,
                request_hash=request_hash,
            )
            if replay is not None:
                assignment, lead = replay
                return ok(
                    request,
                    {
                        "lead": _lead_detail_dict(db, lead, principal),
                        "assignment": _quick_assignment_dict(assignment),
                        "idempotent": True,
                    },
                    "客资已完成快捷派发",
                )

            lead_values = body.model_dump(
                exclude={"company_id", "employee_user_id", "idempotency_key", "note"},
                exclude_none=True,
            )
            lead = create_draft(
                db,
                principal=principal,
                source_kind=LeadSourceKind.PLATFORM_MANUAL,
                values=lead_values,
            )
            lead.raw_payload = {
                **(lead.raw_payload or {}),
                QUICK_DISPATCH_HASH_KEY: request_hash,
            }
            write_audit(
                db,
                principal=principal,
                action="V12_PLATFORM_LEAD_DRAFT_CREATE",
                resource_type="lead",
                resource_id=lead.id,
                after={
                    "status": lead.status,
                    "source_kind": lead.source_kind,
                    "quick_dispatch": True,
                },
                request_id=request.state.request_id,
            )
            dedup = submit_draft(
                db,
                lead=lead,
                principal=principal,
                checkpoint="QUICK_DISPATCH",
            )
            if lead.status != "READY_DISPATCH" or lead.current_assignment_id:
                raise AppError(
                    "LEAD_NOT_READY_DISPATCH",
                    "当前客资无法直接派发",
                    409,
                    {
                        "status": lead.status,
                        "duplicate_status": lead.duplicate_status,
                        "dedup": _dedup_dict(dedup),
                    },
                )
            write_audit(
                db,
                principal=principal,
                action="V12_PLATFORM_LEAD_SUBMIT",
                resource_type="lead",
                resource_id=lead.id,
                after={
                    "status": lead.status,
                    "duplicate_status": lead.duplicate_status,
                    "quick_dispatch": True,
                },
                reason=dedup.decision.value,
                request_id=request.state.request_id,
            )
            outcome = dispatch_manually_with_outcome(
                db,
                lead_id=lead.id,
                company_id=body.company_id,
                employee_user_id=body.employee_user_id,
                assigned_by=principal.user_id,
                idempotency_key=body.idempotency_key,
                note=body.note,
            )
            assignment = outcome.assignment
            write_audit(
                db,
                principal=principal,
                action="V12_MANUAL_DISPATCH",
                resource_type="assignment",
                resource_id=assignment.id,
                company_id=assignment.company_id,
                after={
                    "lead_id": lead.id,
                    "company_id": assignment.company_id,
                    "employee_user_id": body.employee_user_id,
                    "recipient_user_id": assignment.internal_assignee_user_id,
                    "recipient_role_code": (assignment.lead_snapshot or {}).get(
                        "direct_recipient_role_code"
                    ),
                    "status": assignment.status,
                    "points_price": assignment.points_price,
                    "manual": True,
                    "quick_dispatch": True,
                },
                reason=body.note,
                request_id=request.state.request_id,
            )
            db.commit()
            return ok(
                request,
                {
                    "lead": _lead_detail_dict(db, lead, principal),
                    "assignment": _quick_assignment_dict(assignment),
                    "idempotent": False,
                },
                "客资已创建并派发给所选加盟商",
            )
    except IntegrityError:
        db.rollback()
        replay = _existing_quick_dispatch(
            db,
            body=body,
            request_hash=request_hash,
        )
        if replay is None:
            raise
        assignment, lead = replay
        return ok(
            request,
            {
                "lead": _lead_detail_dict(db, lead, principal),
                "assignment": _quick_assignment_dict(assignment),
                "idempotent": True,
            },
            "客资已完成快捷派发",
        )
    except Exception:
        db.rollback()
        raise


@router.patch("/platform/leads/{lead_id}")
def update_platform_lead(
    lead_id: str,
    body: LeadDraftUpdateBody,
    request: Request,
    principal=Depends(require_permissions("lead.manual.manage")),
    db: Session = Depends(get_db),
):
    lead = _platform_lead_or_raise(db, lead_id, lock=True)
    before = lead_supply_to_dict(lead, principal)
    update_draft(db, lead=lead, principal=principal, values=body.model_dump(exclude_unset=True))
    write_audit(
        db,
        principal=principal,
        action="V12_PLATFORM_LEAD_DRAFT_UPDATE",
        resource_type="lead",
        resource_id=lead.id,
        before=before,
        after=lead_supply_to_dict(lead, principal),
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, lead_supply_to_dict(lead, principal))


@router.get("/platform/leads/{lead_id}")
def get_platform_lead(
    lead_id: str,
    request: Request,
    principal=Depends(require_permissions("lead.manual.manage")),
    db: Session = Depends(get_db),
):
    lead = _platform_lead_or_raise(db, lead_id)
    return ok(request, _lead_detail_dict(db, lead, principal))


@router.get("/admin/leads/{lead_id}")
def get_admin_lead(
    lead_id: str,
    request: Request,
    principal=Depends(require_permissions("lead.manual.manage")),
    db: Session = Depends(get_db),
):
    lead = get_lead_or_404(db, lead_id)
    return ok(request, _lead_detail_dict(db, lead, principal))


@router.post("/platform/leads/{lead_id}/correction")
def reopen_platform_lead_correction(
    lead_id: str,
    request: Request,
    principal=Depends(require_permissions("lead.manual.manage")),
    db: Session = Depends(get_db),
):
    lead = _platform_lead_or_raise(db, lead_id, lock=True)
    before = _lead_detail_dict(db, lead, principal)
    reopen_platform_lead_for_correction(db, lead=lead, principal=principal)
    after = _lead_detail_dict(db, lead, principal)
    write_audit(
        db,
        principal=principal,
        action="V12_PLATFORM_LEAD_CORRECTION_OPEN",
        resource_type="lead",
        resource_id=lead.id,
        before=before,
        after=after,
        reason="运营纠正尚未派发的客资关键信息",
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, after, "客资已进入纠正状态")


@router.patch("/platform/leads/{lead_id}/correction")
def correct_platform_lead_facts(
    lead_id: str,
    body: LeadCorrectionBody,
    request: Request,
    principal=Depends(require_permissions("lead.manual.manage")),
    db: Session = Depends(get_db),
):
    values = body.model_dump(
        exclude_unset=True,
        exclude={"reason", "expected_snapshot_version"},
    )
    result = correct_platform_lead(
        db,
        lead_id=lead_id,
        principal=principal,
        values=values,
        reason=body.reason,
        expected_snapshot_version=body.expected_snapshot_version,
    )
    after = _lead_detail_dict(db, result.lead, principal)
    write_audit(
        db,
        principal=principal,
        action="V12_PLATFORM_LEAD_FACT_CORRECTION",
        resource_type="lead",
        resource_id=result.lead.id,
        before=result.before,
        after=result.after,
        metadata={
            "changed_fields": list(result.changed_fields),
            "had_dispatch_history": result.had_dispatch_history,
            "correction_issues": list(result.issues),
            "dedup": _dedup_dict(result.dedup),
            "reward_changes": list(result.reward_changes),
        },
        reason=body.reason,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(
        request,
        after,
        "客资信息已更正"
        if not result.issues
        else "客资信息已更正，当前接收资格需运营处理",
    )


@router.post("/platform/leads/{lead_id}/correction/recheck")
def recheck_platform_lead_correction_facts(
    lead_id: str,
    body: LeadCorrectionRecheckBody,
    request: Request,
    principal=Depends(require_permissions("lead.manual.manage")),
    db: Session = Depends(get_db),
):
    result = recheck_platform_lead_correction(
        db,
        lead_id=lead_id,
        principal=principal,
        reason=body.reason,
        expected_snapshot_version=body.expected_snapshot_version,
    )
    response_data = _lead_detail_dict(db, result.lead, principal)
    write_audit(
        db,
        principal=principal,
        action="V12_PLATFORM_LEAD_CORRECTION_RECHECK",
        resource_type="lead",
        resource_id=result.lead.id,
        before=result.before,
        after=result.after,
        metadata={
            "correction_issues_before": result.before["correction_issues"],
            "correction_issues_after": list(result.issues),
            "dedup": _dedup_dict(result.dedup),
            "reward_changes": list(result.reward_changes),
        },
        reason=body.reason,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(
        request,
        response_data,
        "接收资格重新检查通过"
        if not result.issues
        else "重新检查完成，仍有异常需处理",
    )


@router.post("/platform/leads/{lead_id}/correction/release-for-redispatch")
def release_platform_lead_correction_for_redispatch(
    lead_id: str,
    body: LeadCorrectionRedispatchBody,
    request: Request,
    principal=Depends(require_permissions("lead.manual.manage")),
    db: Session = Depends(get_db),
):
    result = release_corrected_lead_for_redispatch(
        db,
        lead_id=lead_id,
        principal=principal,
        reason=body.reason,
        expected_snapshot_version=body.expected_snapshot_version,
    )
    response_data = {
        "lead": _lead_detail_dict(db, result.lead, principal),
        "assignment": _quick_assignment_dict(result.assignment),
        "refund_ledger": (
            ledger_to_dict(result.refund_ledger) if result.refund_ledger else None
        ),
    }
    write_audit(
        db,
        principal=principal,
        action="V12_PLATFORM_LEAD_CORRECTION_REDISPATCH",
        resource_type="lead",
        resource_id=result.lead.id,
        company_id=result.assignment.company_id,
        before=result.before,
        after=result.after,
        metadata={
            "assignment_id": result.assignment.id,
            "assignment_status_before": result.assignment_status_before,
            "assignment_status_after": result.assignment.status,
            "refund_ledger_id": (
                result.refund_ledger.id if result.refund_ledger else None
            ),
            "refund_points": (
                int(result.refund_ledger.delta) if result.refund_ledger else 0
            ),
        },
        reason=body.reason,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, response_data, "原派发已解除，客资已重新进入待派发池")


@router.post("/platform/leads/{lead_id}/misdispatch/release-for-redispatch")
def release_platform_lead_misdispatch_for_redispatch(
    lead_id: str,
    body: LeadCorrectionRedispatchBody,
    request: Request,
    principal=Depends(require_permissions("lead.manual.manage")),
    db: Session = Depends(get_db),
):
    result = release_misdispatched_lead_for_redispatch(
        db,
        lead_id=lead_id,
        principal=principal,
        reason=body.reason,
        expected_snapshot_version=body.expected_snapshot_version,
    )
    response_data = {
        "lead": _lead_detail_dict(db, result.lead, principal),
        "assignment": _quick_assignment_dict(result.assignment),
        "refund_ledger": (
            ledger_to_dict(result.refund_ledger) if result.refund_ledger else None
        ),
    }
    write_audit(
        db,
        principal=principal,
        action="V12_PLATFORM_LEAD_MISDISPATCH_REDISPATCH",
        resource_type="lead",
        resource_id=result.lead.id,
        company_id=result.assignment.company_id,
        before=result.before,
        after=result.after,
        metadata={
            "assignment_id": result.assignment.id,
            "assignment_status_before": result.assignment_status_before,
            "assignment_status_after": result.assignment.status,
            "refund_ledger_id": (
                result.refund_ledger.id if result.refund_ledger else None
            ),
            "refund_points": (
                int(result.refund_ledger.delta) if result.refund_ledger else 0
            ),
            "expired_return_request_id": result.expired_return_request_id,
        },
        reason=body.reason,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, response_data, "错误派发已撤回，客资已重新进入待派发池")


@router.delete("/platform/leads/{lead_id}/test-record")
def delete_platform_test_lead(
    lead_id: str,
    body: TestLeadDeleteBody,
    request: Request,
    principal=Depends(require_permissions("lead.manual.manage")),
    db: Session = Depends(get_db),
):
    snapshot = delete_test_lead_permanently(
        db,
        lead_id=lead_id,
        principal=principal,
        confirmed_lead_id=body.confirmed_lead_id,
        confirmed_customer_name=body.confirmed_customer_name,
        reason=body.reason,
    )
    write_audit(
        db,
        principal=principal,
        action="V12_TEST_LEAD_PERMANENT_DELETE",
        resource_type="lead",
        resource_id=lead_id,
        before=snapshot,
        after={"deleted": True},
        reason=body.reason,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, snapshot, "测试客资已永久删除")


@router.get("/platform/leads/{lead_id}/test-record/impact")
def preview_platform_test_lead_delete(
    lead_id: str,
    request: Request,
    principal=Depends(require_permissions("lead.manual.manage")),
    db: Session = Depends(get_db),
):
    return ok(
        request,
        preview_test_lead_delete(db, lead_id=lead_id, principal=principal),
    )


@router.post("/platform/leads/{lead_id}/submit")
def submit_platform_lead(
    lead_id: str,
    request: Request,
    principal=Depends(require_permissions("lead.manual.manage")),
    db: Session = Depends(get_db),
):
    lead = _platform_lead_or_raise(db, lead_id)
    result = submit_draft(db, lead=lead, principal=principal)
    write_audit(
        db,
        principal=principal,
        action="V12_PLATFORM_LEAD_SUBMIT",
        resource_type="lead",
        resource_id=lead.id,
        after={"status": lead.status, "duplicate_status": lead.duplicate_status},
        reason=result.decision.value,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, {"lead": _lead_detail_dict(db, lead, principal), "dedup": _dedup_dict(result)}, "客资已提交")


@router.get("/platform/leads")
def list_platform_leads(
    request: Request,
    principal=Depends(require_permissions("lead.manual.manage")),
    db: Session = Depends(get_db),
    status: str | None = Query(default=None),
    region: str | None = Query(default=None, max_length=64),
    page_no: int = Query(default=1, alias="page", ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
):
    items, total = list_platform_leads_service(
        db,
        status=status,
        region=region,
        page_no=page_no,
        page_size=page_size,
    )
    return ok(
        request,
        page(lead_supply_list_to_dict(db, items, principal), total, page_no, page_size),
    )


@router.get("/operation/leads/{lead_id}/deletion-preview")
def preview_operation_lead_deletion(
    lead_id: str,
    request: Request,
    principal=Depends(require_permissions("lead.own.delete")),
    db: Session = Depends(get_db),
):
    return ok(request, preview_lead_deletion(db, lead_id=lead_id, principal=principal))


@router.delete("/operation/leads/{lead_id}")
def delete_operation_lead_endpoint(
    lead_id: str,
    body: LeadDeleteBody,
    request: Request,
    principal=Depends(require_permissions("lead.own.delete")),
    db: Session = Depends(get_db),
):
    result = delete_operation_lead(
        db,
        lead_id=lead_id,
        principal=principal,
        reason=body.reason,
    )
    if not result.idempotent:
        write_audit(
            db,
            principal=principal,
            action="V12_OPERATION_LEAD_DELETE",
            resource_type="lead",
            resource_id=result.lead.id,
            before={"deleted_at": None},
            after={
                "deleted_at": result.lead.deleted_at.isoformat(),
                "deleted_by": result.lead.deleted_by,
            },
            reason=body.reason,
            request_id=request.state.request_id,
        )
        db.commit()
    return ok(
        request,
        {"id": result.lead.id, "deleted": True, "idempotent": result.idempotent},
        "客资已删除",
    )


@router.post("/supplier/leads")
def create_supplier_lead(
    body: LeadDraftBody,
    request: Request,
    principal=Depends(require_permissions("supplier.lead.manage")),
    db: Session = Depends(get_db),
):
    require_supply_write_enabled(db, principal.company_id)
    lead = create_draft(
        db,
        principal=principal,
        source_kind=LeadSourceKind.SUPPLIER_H5,
        values=body.model_dump(exclude_none=True),
    )
    write_audit(
        db,
        principal=principal,
        action="V12_SUPPLIER_LEAD_DRAFT_CREATE",
        resource_type="lead",
        resource_id=lead.id,
        after={"company_id": principal.company_id, "status": lead.status},
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, lead_supply_to_dict(lead, principal), "供应商客资草稿已创建")


@router.patch("/supplier/leads/{lead_id}")
def update_supplier_lead(
    lead_id: str,
    body: LeadDraftUpdateBody,
    request: Request,
    principal=Depends(require_permissions("supplier.lead.manage")),
    db: Session = Depends(get_db),
):
    require_supply_write_enabled(db, principal.company_id)
    lead = get_lead_or_404(db, lead_id)
    before = lead_supply_to_dict(lead, principal)
    update_draft(db, lead=lead, principal=principal, values=body.model_dump(exclude_unset=True))
    write_audit(
        db,
        principal=principal,
        action="V12_SUPPLIER_LEAD_DRAFT_UPDATE",
        resource_type="lead",
        resource_id=lead.id,
        before=before,
        after=lead_supply_to_dict(lead, principal),
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, lead_supply_to_dict(lead, principal))


@router.delete("/supplier/leads/{lead_id}")
def delete_supplier_lead_draft(
    lead_id: str,
    request: Request,
    principal=Depends(require_permissions("supplier.lead.manage")),
    db: Session = Depends(get_db),
):
    lead = get_lead_or_404(db, lead_id)
    before = lead_supply_to_dict(lead, principal)
    discard_draft(db, lead=lead, principal=principal)
    write_audit(
        db,
        principal=principal,
        action="V12_SUPPLIER_LEAD_DRAFT_DELETE",
        resource_type="lead",
        resource_id=lead_id,
        company_id=principal.company_id,
        before=before,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, {"id": lead_id}, "草稿已删除")


@router.post("/supplier/leads/{lead_id}/revise")
def revise_rejected_supplier_lead(
    lead_id: str,
    request: Request,
    principal=Depends(require_permissions("supplier.lead.manage")),
    db: Session = Depends(get_db),
):
    require_supply_write_enabled(db, principal.company_id)
    lead = get_lead_or_404(db, lead_id)
    before = lead_supply_to_dict(lead, principal)
    reopen_rejected_supplier_lead(db, lead=lead, principal=principal)
    write_audit(
        db,
        principal=principal,
        action="V12_SUPPLIER_LEAD_REVISE",
        resource_type="lead",
        resource_id=lead.id,
        company_id=principal.company_id,
        before=before,
        after=lead_supply_to_dict(lead, principal),
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, lead_supply_to_dict(lead, principal), "请修改后重新提交")


@router.post("/supplier/leads/{lead_id}/submit")
def submit_supplier_lead(
    lead_id: str,
    request: Request,
    principal=Depends(require_permissions("supplier.lead.manage")),
    db: Session = Depends(get_db),
):
    require_supply_write_enabled(db, principal.company_id)
    lead = get_lead_or_404(db, lead_id)
    result = submit_draft(db, lead=lead, principal=principal)
    write_audit(
        db,
        principal=principal,
        action="V12_SUPPLIER_LEAD_SUBMIT",
        resource_type="lead",
        resource_id=lead.id,
        after={
            "company_id": principal.company_id,
            "status": lead.status,
            "duplicate_status": lead.duplicate_status,
            "submission_snapshot": lead_supply_to_dict(lead, principal),
        },
        reason=result.decision.value,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, {"lead": _lead_detail_dict(db, lead, principal), "dedup": _dedup_dict(result)}, "供应商客资已提交")


@router.get("/supplier/leads")
def supplier_lead_list(
    request: Request,
    principal=Depends(require_permissions("supplier.lead.manage")),
    db: Session = Depends(get_db),
    status: str | None = Query(default=None),
    page_no: int = Query(default=1, alias="page", ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
):
    require_active_company(db, principal.company_id)
    items, total = list_supplier_leads(
        db,
        company_id=principal.company_id or "",
        status=status,
        page_no=page_no,
        page_size=page_size,
        submitter_user_id=principal.user_id if principal.has_any_role("FRANCHISE_EMPLOYEE") else None,
    )
    return ok(
        request,
        page(lead_supply_list_to_dict(db, list(items), principal), total, page_no, page_size),
    )


@router.get("/supplier/leads/{lead_id}")
def supplier_lead_detail(
    lead_id: str,
    request: Request,
    principal=Depends(require_permissions("supplier.lead.manage")),
    db: Session = Depends(get_db),
):
    lead = get_lead_or_404(db, lead_id)
    if not principal.can("*") and lead.supplier_company_id != principal.company_id:
        raise AppError("FORBIDDEN", "无权查看其他公司的供应商客资", 403)
    if (
        principal.has_any_role("FRANCHISE_EMPLOYEE")
        and lead.submitter_user_id != principal.user_id
    ):
        raise AppError("SUPPLIER_LEAD_NOT_OWNED", "加盟商员工只能查看本人录入的客资", 403)
    return ok(request, _lead_detail_dict(db, lead, principal))


@router.get("/admin/supplier-leads/{lead_id}")
def admin_supplier_lead_detail(
    lead_id: str,
    request: Request,
    principal=Depends(require_permissions("lead.supplier.review")),
    db: Session = Depends(get_db),
):
    lead = get_lead_or_404(db, lead_id)
    if lead.source_kind != LeadSourceKind.SUPPLIER_H5.value:
        raise AppError("LEAD_SOURCE_INVALID", "仅加盟商来源客资可从此入口查看", 409)
    return ok(request, _lead_detail_dict(db, lead, principal))


@router.post("/admin/supplier-leads/{lead_id}/review")
def admin_review_supplier_lead(
    lead_id: str,
    body: SupplierReviewBody,
    request: Request,
    principal=Depends(require_permissions("lead.supplier.review")),
    db: Session = Depends(get_db),
):
    lead = get_lead_or_404(db, lead_id)
    before = lead_supply_to_dict(lead, principal)
    review_decision = {"APPROVE": "QUALIFIED", "REJECT": "INVALID"}.get(
        body.decision,
        body.decision,
    )
    requires_telesales = review_decision == "INFO_INCOMPLETE"
    result = review_supplier_lead(
        db,
        lead=lead,
        reviewer=principal,
        decision="INFO_INCOMPLETE" if requires_telesales else review_decision,
        note=body.note,
    )
    task = None
    if requires_telesales:
        assignment = assign_pre_dispatch_task(
            db,
            lead_id=lead.id,
            assignee_user_id=body.assignee_user_id or "",
            assigned_by=principal.user_id,
            reason=body.pre_dispatch_reason or "",
            template_code=body.template_code,
        )
        task = assignment.task
    after = lead_supply_to_dict(lead, principal)
    after["review_decision"] = review_decision
    after["submission_snapshot"] = before
    write_audit(
        db,
        principal=principal,
        action="V12_SUPPLIER_LEAD_REVIEW",
        resource_type="lead",
        resource_id=lead.id,
        before=before,
        after=after,
        reason=body.note or review_decision,
        request_id=request.state.request_id,
    )
    if task is not None:
        write_audit(
            db,
            principal=principal,
            action="V12_PRE_DISPATCH_VERIFY_ASSIGN",
            resource_type="verification_task",
            resource_id=task.id,
            before=assignment.before,
            after=assignment.after,
            reason=body.pre_dispatch_reason,
            request_id=request.state.request_id,
        )
    db.commit()
    return ok(
        request,
        {
            "lead": lead_supply_to_dict(lead, principal),
            "dedup": _dedup_dict(result),
            "task": _pre_dispatch_task_dict(task) if task else None,
        },
        "已派发电销核实" if task is not None else "资料初审已完成",
    )


@router.post("/admin/leads/{lead_id}/dedup-override")
def admin_override_dedup(
    lead_id: str,
    body: DedupOverrideBody,
    request: Request,
    principal=Depends(require_permissions("lead.dedup.override")),
    db: Session = Depends(get_db),
):
    lead = get_lead_or_404(db, lead_id)
    before = lead_supply_to_dict(lead, principal)
    try:
        item = override_duplicate(
            db,
            lead=lead,
            event_id=body.event_id,
            reason=body.reason,
            approved_by=principal.user_id,
        )
    except ValueError as exc:
        raise AppError("DEDUP_OVERRIDE_INVALID", str(exc), 422) from exc
    write_audit(
        db,
        principal=principal,
        action="V12_DEDUP_OVERRIDE",
        resource_type="lead",
        resource_id=lead.id,
        before=before,
        after=lead_supply_to_dict(lead, principal),
        metadata={"reward_changes": list(getattr(item, "reward_changes", ()))},
        reason=body.reason,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, {"override_id": item.id, "lead": lead_supply_to_dict(lead, principal)}, "去重覆盖已生效")


@router.get("/company/capabilities")
def own_capabilities(
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
):
    company = require_active_company(db, principal.company_id)
    return ok(request, [_capability_dict(item) for item in list_capabilities(db, company.id)])


@router.post("/company/capabilities", deprecated=True)
def request_own_capability(
    body: CapabilityRequestBody,
    request: Request,
    principal=Depends(require_permissions("company.profile.manage")),
    db: Session = Depends(get_db),
):
    company = require_active_company(db, principal.company_id)
    item = request_capability(db, company.id, body.capability_code)
    write_audit(
        db,
        principal=principal,
        action="V12_COMPANY_CAPABILITY_REQUEST",
        resource_type="company_lead_capability",
        resource_id=item.id,
        company_id=company.id,
        after=_capability_dict(item),
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, _capability_dict(item), "公司能力申请已提交")


@router.get("/company/service-areas")
def own_service_areas(
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
):
    company = require_active_company(db, principal.company_id)
    return ok(request, [_area_dict(item, db=db) for item in list_service_areas(db, company.id)])


@router.put("/company/service-areas", deprecated=True)
def replace_own_service_areas(
    body: ServiceAreaReplaceBody,
    request: Request,
    principal=Depends(require_permissions("company.profile.manage")),
    db: Session = Depends(get_db),
):
    company = require_active_company(db, principal.company_id)
    items = replace_service_areas(
        db,
        company_id=company.id,
        region_codes=body.region_codes,
        primary_city_code=body.primary_city_code,
    )
    write_audit(
        db,
        principal=principal,
        action="V12_COMPANY_SERVICE_AREAS_REPLACE",
        resource_type="company",
        resource_id=company.id,
        company_id=company.id,
        after={"region_codes": [item.region_code for item in items], "review_status": "PENDING"},
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, [_area_dict(item, db=db) for item in items], "服务区域已提交审核")


@router.get("/admin/company-capabilities", deprecated=True)
def admin_capability_list(
    request: Request,
    principal=Depends(require_permissions("company.profile.review")),
    db: Session = Depends(get_db),
    review_status: str | None = Query(default="PENDING"),
    page_no: int = Query(default=1, alias="page", ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
):
    filters = []
    if review_status:
        filters.append(CompanyLeadCapability.review_status == review_status.upper())
    total = db.scalar(select(func.count(CompanyLeadCapability.id)).where(*filters)) or 0
    rows = db.execute(
        select(CompanyLeadCapability, Company.name, Company.code)
        .join(Company, Company.id == CompanyLeadCapability.company_id)
        .where(*filters)
        .order_by(CompanyLeadCapability.created_at.desc())
        .offset((page_no - 1) * page_size)
        .limit(page_size)
    ).all()
    items = [
        _capability_dict(item, company_name=company_name, company_code=company_code)
        for item, company_name, company_code in rows
    ]
    return ok(request, page(items, int(total), page_no, page_size))


@router.post(
    "/admin/companies/{company_id}/capabilities/{capability_code}/review",
    deprecated=True,
)
def admin_review_capability(
    company_id: str,
    capability_code: str,
    body: CapabilityReviewBody,
    request: Request,
    principal=Depends(require_permissions("company.profile.review")),
    db: Session = Depends(get_db),
):
    company = db.get(Company, company_id)
    if company is None:
        raise AppError("COMPANY_NOT_FOUND", "公司不存在", 404)
    item = review_capability(
        db,
        company_id=company_id,
        capability_code=capability_code,
        approve=body.decision == "APPROVE",
        reviewed_by=principal.user_id,
        note=body.note,
    )
    write_audit(
        db,
        principal=principal,
        action="V12_COMPANY_CAPABILITY_REVIEW",
        resource_type="company_lead_capability",
        resource_id=item.id,
        company_id=company_id,
        after=_capability_dict(item, company_name=company.name, company_code=company.code),
        reason=body.note or body.decision,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(
        request,
        _capability_dict(item, company_name=company.name, company_code=company.code),
        "公司能力审核已完成",
    )


@router.get("/admin/companies/{company_id}/profile")
def admin_company_profile(
    company_id: str,
    request: Request,
    principal=Depends(require_permissions("company.profile.review")),
    db: Session = Depends(get_db),
):
    company = db.get(Company, company_id)
    if company is None:
        raise AppError("COMPANY_NOT_FOUND", "公司不存在", 404)
    company_data = company_to_dict(company)
    return ok(
        request,
        {
            "company": {
                "id": company.id,
                "name": company.name,
                "code": company.code,
                "status": company.status,
                "is_test": company.is_test,
                "owner_name": company.owner_name,
                "contact_phone_masked": company_data["contact_phone_masked"],
                "level_code": company.level_code,
                "notes": company.notes,
                "primary_user_id": company.primary_user_id,
                "wechat_bound": bool(company.primary_user_id),
            },
            "capabilities": [
                _capability_dict(item, company_name=company.name, company_code=company.code)
                for item in list_capabilities(db, company_id)
            ],
            "service_areas": [
                _area_dict(item, db=db, company_name=company.name, company_code=company.code)
                for item in list_service_areas(db, company_id)
            ],
        },
    )


@router.put("/admin/companies/{company_id}/service-areas")
def admin_configure_service_areas(
    company_id: str,
    body: ServiceAreaReplaceBody,
    request: Request,
    principal=Depends(require_permissions("company.profile.review")),
    db: Session = Depends(get_db),
):
    company = db.get(Company, company_id)
    if company is None:
        raise AppError("COMPANY_NOT_FOUND", "公司不存在", 404)
    items = replace_service_areas(
        db,
        company_id=company_id,
        region_codes=body.region_codes,
        primary_city_code=body.primary_city_code,
    )
    for item in items:
        removal_pending = bool(item.review_note and item.review_note.startswith("[REMOVE_REQUEST]"))
        if removal_pending or item.review_status != "APPROVED" or not item.active:
            review_service_area(
                db,
                area_id=item.id,
                approve=True,
                reviewed_by=principal.user_id,
                note="平台直接配置服务区域",
            )
    configured = list_service_areas(db, company_id)
    active_codes = [item.region_code for item in configured if item.active]
    write_audit(
        db,
        principal=principal,
        action="V12_COMPANY_SERVICE_AREAS_CONFIGURE",
        resource_type="company",
        resource_id=company_id,
        company_id=company_id,
        after={
            "region_codes": active_codes,
            "primary_city_code": body.primary_city_code,
        },
        reason="平台直接配置服务区域",
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(
        request,
        [
            _area_dict(item, db=db, company_name=company.name, company_code=company.code)
            for item in configured
        ],
        "服务区域已更新",
    )


@router.put("/admin/companies/{company_id}/capabilities/{capability_code}")
def admin_configure_capability(
    company_id: str,
    capability_code: str,
    body: CompanyCapabilityConfigureBody,
    request: Request,
    principal=Depends(require_permissions("company.profile.review")),
    db: Session = Depends(get_db),
):
    company = db.get(Company, company_id)
    if company is None:
        raise AppError("COMPANY_NOT_FOUND", "公司不存在", 404)
    item = configure_capability(
        db,
        company_id=company_id,
        capability_code=capability_code,
        active=body.active,
        reviewed_by=principal.user_id,
        note=body.note,
    )
    payload = _capability_dict(item, company_name=company.name, company_code=company.code)
    write_audit(
        db,
        principal=principal,
        action="V12_COMPANY_CAPABILITY_CONFIGURE",
        resource_type="company_lead_capability",
        resource_id=item.id,
        company_id=company_id,
        after=payload,
        reason=body.note or ("平台启用" if body.active else "平台停用"),
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, payload, "公司客资能力已更新")


@router.post("/admin/companies/{company_id}/profile/approve-pending")
def admin_approve_pending_company_profile(
    company_id: str,
    body: CompanyProfileBulkApproveBody,
    request: Request,
    principal=Depends(require_permissions("company.profile.review")),
    db: Session = Depends(get_db),
):
    company = db.get(Company, company_id)
    if company is None:
        raise AppError("COMPANY_NOT_FOUND", "公司不存在", 404)
    capabilities, areas = approve_pending_profile(
        db,
        company_id=company_id,
        reviewed_by=principal.user_id,
        note=body.note,
    )
    capability_items = [
        _capability_dict(item, company_name=company.name, company_code=company.code)
        for item in capabilities
    ]
    area_items = [
        _area_dict(item, db=db, company_name=company.name, company_code=company.code)
        for item in areas
    ]
    for item in capability_items:
        write_audit(
            db,
            principal=principal,
            action="V12_COMPANY_CAPABILITY_REVIEW",
            resource_type="company_lead_capability",
            resource_id=item["id"],
            company_id=company_id,
            after=item,
            metadata={"bulk_profile_approval": True},
            reason=body.note or "APPROVE",
            request_id=request.state.request_id,
        )
    for item in area_items:
        write_audit(
            db,
            principal=principal,
            action="V12_COMPANY_SERVICE_AREA_REVIEW",
            resource_type="company_service_area_v12",
            resource_id=item["id"],
            company_id=company_id,
            after=item,
            metadata={"bulk_profile_approval": True},
            reason=body.note or "APPROVE",
            request_id=request.state.request_id,
        )
    write_audit(
        db,
        principal=principal,
        action="V12_COMPANY_PROFILE_BULK_APPROVE",
        resource_type="company",
        resource_id=company_id,
        company_id=company_id,
        after={
            "capability_codes": [item["capability_code"] for item in capability_items],
            "service_area_codes": [item["region_code"] for item in area_items],
        },
        reason=body.note or "APPROVE",
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(
        request,
        {"company_id": company_id, "capabilities": capability_items, "service_areas": area_items},
        "加盟商待开通申请已一次通过",
    )


@router.get("/admin/service-areas", deprecated=True)
def admin_service_area_list(
    request: Request,
    principal=Depends(require_permissions("company.profile.review")),
    db: Session = Depends(get_db),
    review_status: str | None = Query(default="PENDING"),
    page_no: int = Query(default=1, alias="page", ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
):
    filters = []
    if review_status:
        filters.append(CompanyServiceAreaV12.review_status == review_status.upper())
    total = db.scalar(select(func.count(CompanyServiceAreaV12.id)).where(*filters)) or 0
    rows = db.execute(
        select(CompanyServiceAreaV12, Company.name, Company.code)
        .join(Company, Company.id == CompanyServiceAreaV12.company_id)
        .where(*filters)
        .order_by(CompanyServiceAreaV12.created_at.desc())
        .offset((page_no - 1) * page_size)
        .limit(page_size)
    ).all()
    items = [
        _area_dict(item, db=db, company_name=company_name, company_code=company_code)
        for item, company_name, company_code in rows
    ]
    return ok(request, page(items, int(total), page_no, page_size))


@router.post("/admin/service-areas/{area_id}/review", deprecated=True)
def admin_review_area(
    area_id: str,
    body: ServiceAreaReviewBody,
    request: Request,
    principal=Depends(require_permissions("company.profile.review")),
    db: Session = Depends(get_db),
):
    area = db.get(CompanyServiceAreaV12, area_id)
    if area is None:
        raise AppError("SERVICE_AREA_NOT_FOUND", "服务区域申请不存在", 404)
    company = db.get(Company, area.company_id)
    if company is None:
        raise AppError("COMPANY_NOT_FOUND", "公司不存在", 404)
    removal_request = str(area.review_note or "").startswith("[REMOVE_REQUEST]")
    item = review_service_area(
        db,
        area_id=area_id,
        approve=body.decision == "APPROVE",
        reviewed_by=principal.user_id,
        note=body.note,
    )
    write_audit(
        db,
        principal=principal,
        action="V12_COMPANY_SERVICE_AREA_REVIEW",
        resource_type="company_service_area_v12",
        resource_id=item.id,
        company_id=item.company_id,
        after={
            **_area_dict(item, db=db, company_name=company.name, company_code=company.code),
            "request_type": "REMOVE" if removal_request else "OPEN",
        },
        reason=body.note or body.decision,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(
        request,
        _area_dict(item, db=db, company_name=company.name, company_code=company.code),
        "服务区域审核已完成",
    )
