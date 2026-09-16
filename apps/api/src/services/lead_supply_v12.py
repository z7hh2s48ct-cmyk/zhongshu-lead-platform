from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased

from ..core.auth import Principal
from ..core.enums import AssignmentStatus, PointsLedgerType
from ..core.errors import AppError
from ..core.models import (
    Assignment,
    AssignmentEvent,
    Company,
    FollowUp,
    Lead,
    LeadDuplicateRelation,
    LeadImportIssue,
    PointsLedger,
    Region,
    ReturnRequest,
    User,
    VerificationSubmission,
    VerificationTask,
)
from ..core.models_v12 import DedupOverride, LeadDedupEvent, SupplierLeadReward
from ..core.security import decrypt_text, encrypt_text, fingerprint_phone, hash_phone, mask_phone, normalize_phone
from ..core.state_machine_v12 import assert_return_transition, assert_reward_transition
from ..core.v12_enums import (
    CustomerSource,
    DuplicateDecision,
    LeadReviewStatus,
    LeadSourceKind,
    LeadV12Status,
    ReturnV12Status,
    RewardStatus,
    VerificationTaskType,
)
from .company_profile_v12 import require_lead_capability
from .phone_uniqueness import require_unique_lead_phone
from .lead_deletion_v12 import require_lead_not_deleted
from .china_regions import region_by_code
from .dedup_v12 import DedupResult, apply_submission_decision, evaluate_phone
from .dispatch_v12 import (
    existing_receiver_correction_issues,
    route_approved_lead_to_pool,
)
from .lead_correction_guard import (
    require_correction_review_resolved,
    store_lead_correction_issues,
)
from .notification_service import create_station_message, enqueue_outbox
from .points_service import change_points
from .pre_dispatch_v12 import (
    is_pre_dispatch_task_requeueable,
    queue_pre_dispatch_task,
    restart_pre_dispatch_after_correction,
)


EDITABLE_FIELDS = {
    "customer_name",
    "phone",
    "province",
    "city",
    "district",
    "region_code",
    "category_code",
    "brand_code",
    "source_channel",
    "source_detail",
    "need_summary",
    "budget_min",
    "budget_max",
    "acquisition_cost_cents",
    "consent_confirmed",
}


@dataclass(frozen=True, slots=True)
class LeadCorrectionResult:
    lead: Lead
    dedup: DedupResult | None
    had_dispatch_history: bool
    changed_fields: tuple[str, ...]
    issues: tuple[str, ...]
    before: dict[str, Any]
    after: dict[str, Any]
    reward_changes: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class LeadCorrectionRedispatchResult:
    lead: Lead
    assignment: Assignment
    assignment_status_before: str
    refund_ledger: PointsLedger | None
    before: dict[str, Any]
    after: dict[str, Any]
    expired_return_request_id: str | None = None


def _clean_text(value: Any, *, empty_to_none: bool = True) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    if empty_to_none and not cleaned:
        return None
    return cleaned


def _assert_draft(lead: Lead) -> None:
    if lead.status != LeadV12Status.DRAFT.value:
        raise AppError("LEAD_NOT_EDITABLE", "仅草稿状态客资允许编辑", 409)


def _assert_owner(lead: Lead, principal: Principal, *, supplier: bool) -> None:
    if principal.can("*"):
        return
    if supplier:
        if not principal.company_id or lead.supplier_company_id != principal.company_id:
            raise AppError("FORBIDDEN", "无权访问其他公司的供应商客资", 403)
        if principal.has_any_role("FRANCHISE_EMPLOYEE") and lead.submitter_user_id != principal.user_id:
            raise AppError("SUPPLIER_LEAD_NOT_OWNED", "加盟商员工只能编辑本人录入的客资", 403)
    elif not principal.can("lead.manual.manage") and lead.submitter_user_id != principal.user_id:
        raise AppError("FORBIDDEN", "无权修改其他录入人的客资草稿", 403)


def create_draft(
    db: Session,
    *,
    principal: Principal,
    source_kind: LeadSourceKind,
    values: dict[str, Any],
) -> Lead:
    supplier_company_id: str | None = None
    is_test = False
    if source_kind is LeadSourceKind.SUPPLIER_H5:
        require_lead_capability(db, principal.company_id, "LEAD_SUPPLIER")
        supplier_company_id = principal.company_id
    elif source_kind is LeadSourceKind.PLATFORM_MANUAL:
        is_test = values.get("is_test") is True
    placeholder_phone = ""
    lead = Lead(
        source_type=source_kind.value,
        source_kind=source_kind.value,
        submitter_user_id=principal.user_id,
        supplier_company_id=supplier_company_id,
        customer_name="未填写",
        phone_encrypted=encrypt_text(placeholder_phone),
        phone_hash=hash_phone(placeholder_phone),
        phone_fingerprint=None,
        consent_confirmed=False,
        is_test=is_test,
        status=LeadV12Status.DRAFT.value,
        review_status=LeadReviewStatus.DRAFT.value,
        duplicate_status=None,
        raw_payload={},
    )
    db.add(lead)
    db.flush()
    update_draft(db, lead=lead, principal=principal, values=values)
    return lead


def update_draft(
    db: Session,
    *,
    lead: Lead,
    principal: Principal,
    values: dict[str, Any],
) -> Lead:
    _assert_draft(lead)
    supplier = lead.source_kind == LeadSourceKind.SUPPLIER_H5.value
    _assert_owner(lead, principal, supplier=supplier)
    _apply_editable_values(db, lead, values)
    db.flush()
    return lead


def _apply_editable_values(db: Session, lead: Lead, values: dict[str, Any]) -> None:
    for field, raw_value in values.items():
        if field not in EDITABLE_FIELDS:
            continue
        if field == "phone":
            if raw_value is None:
                continue
            normalized = require_unique_lead_phone(
                db,
                phone=str(raw_value),
                exclude_lead_id=lead.id,
            )
            if normalized and (len(normalized) != 11 or not normalized.startswith("1")):
                raise AppError("LEAD_PHONE_INVALID", "手机号格式错误", 422)
            lead.phone_encrypted = encrypt_text(normalized)
            lead.phone_hash = hash_phone(normalized)
            lead.phone_fingerprint = fingerprint_phone(normalized) if normalized else None
            continue
        if field in {"budget_min", "budget_max", "acquisition_cost_cents"}:
            setattr(lead, field, raw_value)
            continue
        if field == "consent_confirmed":
            lead.consent_confirmed = bool(raw_value)
            continue
        setattr(lead, field, _clean_text(raw_value))
    lead.source_channel = _clean_text(lead.source_channel)
    if lead.source_channel:
        lead.source_channel = lead.source_channel.upper()
    if lead.source_channel != "OTHER":
        lead.source_detail = None
    if lead.customer_name is None:
        lead.customer_name = "未填写"


def _editable_fact_snapshot(lead: Lead) -> dict[str, Any]:
    snapshot = {field: getattr(lead, field) for field in EDITABLE_FIELDS if field != "phone"}
    snapshot["phone"] = normalize_phone(decrypt_text(lead.phone_encrypted) or "")
    return snapshot


def _correction_audit_snapshot(lead: Lead) -> dict[str, Any]:
    phone = decrypt_text(lead.phone_encrypted)
    return {
        "id": lead.id,
        "source_kind": lead.source_kind,
        "customer_name": lead.customer_name,
        "phone_masked": mask_phone(phone),
        "contact_fingerprint": lead.phone_fingerprint
        or fingerprint_phone(phone or ""),
        "province": lead.province,
        "city": lead.city,
        "district": lead.district,
        "region_code": lead.region_code,
        "category_code": lead.category_code,
        "brand_code": lead.brand_code,
        "source_channel": lead.source_channel,
        "source_detail": lead.source_detail,
        "need_summary": lead.need_summary,
        "budget_min": lead.budget_min,
        "budget_max": lead.budget_max,
        "acquisition_cost_cents": lead.acquisition_cost_cents,
        "consent_confirmed": lead.consent_confirmed,
        "status": lead.status,
        "review_status": lead.review_status,
        "duplicate_status": lead.duplicate_status,
        "pending_reason": lead.pending_reason,
        "correction_issues": list(
            (lead.raw_payload or {}).get("correction_issues") or []
        ),
        "current_assignment_id": lead.current_assignment_id,
        "snapshot_version": lead.snapshot_version,
    }


def _restore_correction_workflow(lead: Lead) -> None:
    payload = dict(lead.raw_payload or {})
    resume_status = payload.pop("correction_resume_status", None)
    if not resume_status:
        return
    lead.status = resume_status
    lead.review_status = payload.pop(
        "correction_resume_review_status",
        lead.review_status,
    )
    lead.pending_reason = payload.pop("correction_resume_pending_reason", None)
    lead.raw_payload = payload


def restore_unassigned_correction_workflow(db: Session, lead: Lead) -> None:
    """Restore a cleared correction without bypassing location verification."""

    _restore_correction_workflow(lead)
    if (
        lead.current_assignment_id is None
        and lead.status == LeadV12Status.READY_DISPATCH.value
        and not _has_known_location(db, lead)
    ):
        lead.status = LeadV12Status.PENDING_REVIEW.value
        lead.review_status = LeadReviewStatus.PENDING.value
        lead.pending_reason = None
        db.flush()
        queue_pre_dispatch_task(
            db,
            lead_id=lead.id,
            reason="LOCATION_REQUIRES_TELESALES_VERIFY",
        )


_CORRECTION_REWARD_REASON_PREFIX = "CORRECTION_DEDUP_"
_CORRECTION_REWARD_PREVIOUS_MARKER = "|PREVIOUS_STATUS="
_RESTORABLE_REWARD_STATUSES = {
    RewardStatus.WAITING_CLAIM.value,
    RewardStatus.OBSERVING.value,
    RewardStatus.FROZEN.value,
}


def _correction_cancelled_reward_previous_status(
    reward: SupplierLeadReward,
) -> str | None:
    reason = reward.exception_reason or ""
    if not reason.startswith(_CORRECTION_REWARD_REASON_PREFIX):
        return None
    _separator, marker, previous_status = reason.partition(
        _CORRECTION_REWARD_PREVIOUS_MARKER
    )
    if not marker or previous_status not in _RESTORABLE_REWARD_STATUSES:
        return None
    return previous_status


def reconcile_supplier_rewards_after_dedup(
    db: Session,
    *,
    lead: Lead,
    dedup_result: DedupResult | None,
) -> tuple[dict[str, Any], ...]:
    if dedup_result is None:
        return ()
    rewards = list(
        db.scalars(
            select(SupplierLeadReward)
            .where(SupplierLeadReward.lead_id == lead.id)
            .order_by(SupplierLeadReward.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    changes: list[dict[str, Any]] = []
    if dedup_result.reward_eligible:
        for reward in rewards:
            if reward.status != RewardStatus.CANCELLED.value:
                continue
            previous_status = _correction_cancelled_reward_previous_status(reward)
            if previous_status is None:
                continue
            assert_reward_transition(reward.status, previous_status)
            reward.status = previous_status
            reward.cancelled_at = None
            reward.exception_reason = None
            changes.append(
                {
                    "reward_id": reward.id,
                    "before_status": RewardStatus.CANCELLED.value,
                    "after_status": previous_status,
                    "reason": "CORRECTION_DEDUP_CLEARED",
                }
            )
        return tuple(changes)

    settled = [
        reward.id
        for reward in rewards
        if reward.status == RewardStatus.SETTLED.value
    ]
    if settled:
        raise AppError(
            "LEAD_CORRECTION_REWARD_REVERSAL_REQUIRED",
            "该客资已有结算奖励，请先完成奖励冲正后再更正手机号",
            409,
            {"reward_ids": settled, "dedup_decision": dedup_result.decision.value},
        )
    now = datetime.now(timezone.utc)
    for reward in rewards:
        if reward.status not in _RESTORABLE_REWARD_STATUSES:
            continue
        before_status = reward.status
        assert_reward_transition(before_status, RewardStatus.CANCELLED)
        reward.status = RewardStatus.CANCELLED.value
        reward.cancelled_at = now
        reward.exception_reason = (
            f"{_CORRECTION_REWARD_REASON_PREFIX}{dedup_result.decision.value}"
            f"{_CORRECTION_REWARD_PREVIOUS_MARKER}{before_status}"
        )
        changes.append(
            {
                "reward_id": reward.id,
                "before_status": before_status,
                "after_status": reward.status,
                "reason": reward.exception_reason,
            }
        )
    return tuple(changes)


def discard_draft(
    db: Session,
    *,
    lead: Lead,
    principal: Principal,
) -> None:
    _assert_owner(lead, principal, supplier=True)
    if lead.source_kind != LeadSourceKind.SUPPLIER_H5.value:
        raise AppError("LEAD_SOURCE_INVALID", "仅供应商客资草稿支持在此删除", 409)
    _assert_draft(lead)
    db.delete(lead)
    db.flush()


def _test_lead_delete_impact(db: Session, lead: Lead) -> dict[str, int]:
    return {
        "assignment_history": int(
            db.scalar(select(func.count(Assignment.id)).where(Assignment.lead_id == lead.id))
            or 0
        ),
        "import_issues": int(
            db.scalar(select(func.count(LeadImportIssue.id)).where(LeadImportIssue.lead_id == lead.id))
            or 0
        ),
        "duplicate_relations": int(
            db.scalar(
                select(func.count(LeadDuplicateRelation.id)).where(
                    or_(
                        LeadDuplicateRelation.lead_id == lead.id,
                        LeadDuplicateRelation.duplicate_lead_id == lead.id,
                    )
                )
            )
            or 0
        ),
        "verification_tasks": int(
            db.scalar(select(func.count(VerificationTask.id)).where(VerificationTask.lead_id == lead.id))
            or 0
        ),
        "verification_submissions": int(
            db.scalar(
                select(func.count(VerificationSubmission.id)).where(
                    VerificationSubmission.lead_id == lead.id
                )
            )
            or 0
        ),
        "dedup_events": int(
            db.scalar(
                select(func.count(LeadDedupEvent.id)).where(
                    or_(
                        LeadDedupEvent.lead_id == lead.id,
                        LeadDedupEvent.matched_lead_id == lead.id,
                    )
                )
            )
            or 0
        ),
        "dedup_overrides": int(
            db.scalar(select(func.count(DedupOverride.id)).where(DedupOverride.lead_id == lead.id))
            or 0
        ),
    }


def _test_lead_delete_preview(db: Session, lead: Lead) -> dict[str, Any]:
    impact = _test_lead_delete_impact(db, lead)
    blockers: list[str] = []
    if not lead.is_test:
        blockers.append("NOT_TEST_LEAD")
    if lead.current_assignment_id or impact["assignment_history"]:
        blockers.append("DISPATCH_HISTORY_EXISTS")
    return {
        "lead_id": lead.id,
        "customer_name": lead.customer_name,
        "source_kind": lead.source_kind,
        "status": lead.status,
        "is_test": bool(lead.is_test),
        "deletable": not blockers,
        "blockers": blockers,
        "impact": impact,
    }


def preview_test_lead_delete(
    db: Session,
    *,
    lead_id: str,
    principal: Principal,
) -> dict[str, Any]:
    if not principal.has_any_role("SUPER_ADMIN"):
        raise AppError("FORBIDDEN", "仅超级管理员可查看测试客资删除影响", 403)
    lead = db.scalar(select(Lead).where(Lead.id == lead_id))
    if lead is None:
        raise AppError("LEAD_NOT_FOUND", "客资不存在", 404)
    return _test_lead_delete_preview(db, lead)


def delete_test_lead_permanently(
    db: Session,
    *,
    lead_id: str,
    principal: Principal,
    confirmed_lead_id: str,
    confirmed_customer_name: str,
    reason: str,
) -> dict[str, Any]:
    if not principal.has_any_role("SUPER_ADMIN"):
        raise AppError("FORBIDDEN", "仅超级管理员可永久删除测试客资", 403)
    normalized_reason = reason.strip()
    if len(normalized_reason) < 5:
        raise AppError("TEST_LEAD_DELETE_REASON_REQUIRED", "永久删除必须填写原因", 422)
    lead = db.scalar(
        select(Lead)
        .where(Lead.id == lead_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if lead is None:
        raise AppError("LEAD_NOT_FOUND", "客资不存在", 404)
    if confirmed_lead_id.strip() != lead.id:
        raise AppError(
            "TEST_LEAD_ID_CONFIRMATION_MISMATCH",
            "请输入完整客资 ID 确认删除",
            422,
        )
    if confirmed_customer_name.strip() != lead.customer_name:
        raise AppError(
            "TEST_LEAD_CONFIRMATION_MISMATCH",
            "请输入完整客户名称确认删除",
            422,
        )
    preview = _test_lead_delete_preview(db, lead)
    if not lead.is_test:
        raise AppError("TEST_LEAD_REQUIRED", "正式客资不允许永久删除", 409)
    if lead.current_assignment_id or preview["impact"]["assignment_history"]:
        raise AppError(
            "TEST_LEAD_DISPATCH_HISTORY_EXISTS",
            "已进入派发流转的测试客资不允许永久删除",
            409,
        )
    snapshot = {
        "id": lead.id,
        "customer_name": lead.customer_name,
        "source_kind": lead.source_kind,
        "status": lead.status,
        "is_test": True,
        "impact": preview["impact"],
        "reason": normalized_reason,
    }
    db.delete(lead)
    db.flush()
    return snapshot


def reopen_rejected_supplier_lead(
    db: Session,
    *,
    lead: Lead,
    principal: Principal,
) -> Lead:
    locked_lead = db.scalar(
        select(Lead)
        .where(Lead.id == lead.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_lead is None:
        raise AppError("LEAD_NOT_FOUND", "客资不存在", 404)
    lead = locked_lead
    require_correction_review_resolved(lead)
    _assert_owner(lead, principal, supplier=True)
    if lead.source_kind != LeadSourceKind.SUPPLIER_H5.value:
        raise AppError("LEAD_SOURCE_INVALID", "仅供应商上传客资支持修改后重新提交", 409)
    if (
        lead.status != LeadV12Status.INVALID.value
        or lead.review_status != LeadReviewStatus.REJECTED.value
    ):
        raise AppError("LEAD_REVISION_NOT_ALLOWED", "仅平台退回的客资支持修改后重新提交", 409)
    lead.status = LeadV12Status.DRAFT.value
    lead.review_status = LeadReviewStatus.DRAFT.value
    lead.submitted_at = None
    lead.reviewed_at = None
    lead.duplicate_status = None
    lead.pending_reason = None
    db.flush()
    return lead


def reopen_platform_lead_for_correction(
    db: Session,
    *,
    lead: Lead,
    principal: Principal,
) -> Lead:
    locked_lead = db.scalar(
        select(Lead)
        .where(Lead.id == lead.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_lead is None:
        raise AppError("LEAD_NOT_FOUND", "客资不存在", 404)
    lead = locked_lead
    require_correction_review_resolved(lead)
    if lead.source_kind != LeadSourceKind.PLATFORM_MANUAL.value:
        raise AppError("LEAD_SOURCE_INVALID", "仅平台录入客资支持运营纠正", 409)
    if not (principal.can("*") or principal.can("lead.manual.manage")):
        raise AppError("FORBIDDEN", "无权纠正平台客资", 403)
    if lead.status != LeadV12Status.READY_DISPATCH.value:
        raise AppError("LEAD_CORRECTION_NOT_ALLOWED", "仅尚未派发的待派发客资允许纠正", 409)
    has_dispatch_history = bool(
        lead.current_assignment_id
        or db.scalar(select(Assignment.id).where(Assignment.lead_id == lead.id).limit(1))
    )
    if has_dispatch_history:
        raise AppError("LEAD_CORRECTION_NOT_ALLOWED", "已进入派发流转的客资不允许直接改写", 409)
    lead.status = LeadV12Status.DRAFT.value
    lead.review_status = LeadReviewStatus.DRAFT.value
    lead.submitted_at = None
    lead.reviewed_at = None
    lead.duplicate_status = None
    lead.pending_reason = "PLATFORM_CORRECTION"
    db.flush()
    return lead


def correct_platform_lead(
    db: Session,
    *,
    lead_id: str,
    principal: Principal,
    values: dict[str, Any],
    reason: str | None,
    expected_snapshot_version: int | None,
) -> LeadCorrectionResult:
    lead = db.scalar(
        select(Lead)
        .where(Lead.id == lead_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if lead is None:
        raise AppError("LEAD_NOT_FOUND", "客资不存在", 404)
    if not (principal.can("*") or principal.can("lead.manual.manage")):
        raise AppError("FORBIDDEN", "无权更正客资", 403)
    if (
        expected_snapshot_version is not None
        and expected_snapshot_version != lead.snapshot_version
    ):
        raise AppError(
            "LEAD_VERSION_CONFLICT",
            "客资已被其他人更新，请刷新后重试",
            409,
            {
                "expected_snapshot_version": expected_snapshot_version,
                "current_snapshot_version": lead.snapshot_version,
            },
        )

    current_assignment = (
        db.get(Assignment, lead.current_assignment_id)
        if lead.current_assignment_id
        else None
    )
    has_dispatch_history = bool(
        current_assignment
        or db.scalar(select(Assignment.id).where(Assignment.lead_id == lead.id).limit(1))
    )
    clean_reason = _clean_text(reason)
    if has_dispatch_history and not clean_reason:
        raise AppError(
            "LEAD_CORRECTION_REASON_REQUIRED",
            "已派发客资更正必须填写原因",
            422,
        )
    if has_dispatch_history and expected_snapshot_version is None:
        raise AppError(
            "LEAD_CORRECTION_VERSION_REQUIRED",
            "已派发客资更正必须携带当前版本",
            422,
        )
    before = _correction_audit_snapshot(lead)
    before_facts = _editable_fact_snapshot(lead)
    before_phone = normalize_phone(decrypt_text(lead.phone_encrypted) or "")
    before_region_code = lead.region_code
    original_status = lead.status
    _apply_editable_values(db, lead, values)
    after_facts = _editable_fact_snapshot(lead)
    changed_fields = tuple(
        sorted(
            field
            for field in EDITABLE_FIELDS
            if before_facts[field] != after_facts[field]
        )
    )
    if not changed_fields:
        raise AppError(
            "LEAD_CORRECTION_NO_CHANGES",
            "客资事实没有变化，无需提交更正",
            422,
        )
    if original_status != LeadV12Status.DRAFT.value:
        _materialize_nationwide_location(db, lead)
        _validate_submission(db, lead)
    phone = normalize_phone(decrypt_text(lead.phone_encrypted) or "")
    phone_changed = phone != before_phone
    region_changed = lead.region_code != before_region_code
    verification_restarted = False
    if (
        not has_dispatch_history
        and (phone_changed or region_changed)
        and original_status
        in {
            LeadV12Status.PENDING_TELESALES_VERIFY.value,
            LeadV12Status.PENDING_OPERATION_DISPOSITION.value,
        }
    ):
        restart_pre_dispatch_after_correction(db, lead=lead)
        verification_restarted = True
    dedup_result: DedupResult | None = None
    previous_issues = list((lead.raw_payload or {}).get("correction_issues") or [])
    issues: list[str] = (
        []
        if phone_changed
        else [issue for issue in previous_issues if issue.startswith("DEDUP_")]
    )

    if (
        not has_dispatch_history
        and original_status == LeadV12Status.READY_DISPATCH.value
        and lead.source_kind == LeadSourceKind.PLATFORM_MANUAL.value
    ):
        lead.status = LeadV12Status.DRAFT.value
        lead.review_status = LeadReviewStatus.DRAFT.value
        lead.pending_reason = None
        dedup_result = submit_draft(
            db,
            lead=lead,
            principal=principal,
            checkpoint="PLATFORM_CORRECTION",
        )
        if not dedup_result.blocks_dispatch:
            restore_unassigned_correction_workflow(db, lead)
    elif not has_dispatch_history:
        # Unassigned leads may already be in review or telesales workflows. Keep
        # that workflow state intact; only phone changes can introduce a new
        # dedup blocker that must be resolved before processing continues.
        if phone_changed and phone:
            dedup_result = evaluate_phone(
                db,
                lead=lead,
                normalized_phone=phone,
                checkpoint="UNASSIGNED_CORRECTION",
                now=datetime.now(timezone.utc),
            )
            if dedup_result.blocks_dispatch:
                issues.append(f"DEDUP_{dedup_result.decision.value}")
        if issues:
            payload = dict(lead.raw_payload or {})
            if verification_restarted:
                payload["correction_resume_status"] = lead.status
                payload["correction_resume_review_status"] = lead.review_status
                payload["correction_resume_pending_reason"] = lead.pending_reason
            else:
                payload.setdefault("correction_resume_status", original_status)
                payload.setdefault(
                    "correction_resume_review_status",
                    before["review_status"],
                )
                payload.setdefault(
                    "correction_resume_pending_reason",
                    before["pending_reason"],
                )
            lead.raw_payload = payload
        store_lead_correction_issues(lead, issues)
        if not issues:
            restore_unassigned_correction_workflow(db, lead)
    elif has_dispatch_history:
        if phone_changed:
            dedup_result = evaluate_phone(
                db,
                lead=lead,
                normalized_phone=phone,
                checkpoint="POST_DISPATCH_CORRECTION",
                now=datetime.now(timezone.utc),
            )
            if dedup_result.blocks_dispatch:
                issues.append(f"DEDUP_{dedup_result.decision.value}")
        recheck_receiver = current_assignment is not None and (
            phone_changed
            or region_changed
            or any(not issue.startswith("DEDUP_") for issue in previous_issues)
        )
        if current_assignment is not None and recheck_receiver:
            issues.extend(
                existing_receiver_correction_issues(
                    db,
                    lead=lead,
                    assignment=current_assignment,
                )
            )

        if issues and current_assignment is None:
            payload = dict(lead.raw_payload or {})
            payload.setdefault("correction_resume_status", original_status)
            payload.setdefault(
                "correction_resume_review_status",
                before["review_status"],
            )
            payload.setdefault(
                "correction_resume_pending_reason",
                before["pending_reason"],
            )
            lead.raw_payload = payload
        store_lead_correction_issues(
            lead,
            issues,
            require_action=(
                current_assignment is None
                or current_assignment.status != AssignmentStatus.COMPLETED.value
            ),
        )
        if not issues and current_assignment is None:
            restore_unassigned_correction_workflow(db, lead)

    reward_changes = reconcile_supplier_rewards_after_dedup(
        db,
        lead=lead,
        dedup_result=dedup_result,
    )
    lead.snapshot_version += 1
    db.flush()
    after = _correction_audit_snapshot(lead)
    return LeadCorrectionResult(
        lead=lead,
        dedup=dedup_result,
        had_dispatch_history=has_dispatch_history,
        changed_fields=changed_fields,
        issues=tuple(dict.fromkeys(issues)),
        before=before,
        after=after,
        reward_changes=reward_changes,
    )


def recheck_platform_lead_correction(
    db: Session,
    *,
    lead_id: str,
    principal: Principal,
    reason: str,
    expected_snapshot_version: int,
) -> LeadCorrectionResult:
    lead = db.scalar(
        select(Lead)
        .where(Lead.id == lead_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if lead is None:
        raise AppError("LEAD_NOT_FOUND", "客资不存在", 404)
    if not (principal.can("*") or principal.can("lead.manual.manage")):
        raise AppError("FORBIDDEN", "无权重新检查客资", 403)
    if expected_snapshot_version != lead.snapshot_version:
        raise AppError(
            "LEAD_VERSION_CONFLICT",
            "客资已被其他人更新，请刷新后重试",
            409,
            {
                "expected_snapshot_version": expected_snapshot_version,
                "current_snapshot_version": lead.snapshot_version,
            },
        )
    previous_issues = list((lead.raw_payload or {}).get("correction_issues") or [])
    if lead.pending_reason != "CORRECTION_REVIEW_REQUIRED" or not previous_issues:
        raise AppError(
            "LEAD_CORRECTION_RECHECK_NOT_REQUIRED",
            "当前客资没有待重新检查的更正异常",
            409,
        )
    if len(reason.strip()) < 5:
        raise AppError("LEAD_CORRECTION_REASON_REQUIRED", "重新检查必须填写原因", 422)

    before = _correction_audit_snapshot(lead)
    _materialize_nationwide_location(db, lead)
    phone = _validate_submission(db, lead)
    issues: list[str] = []
    dedup_result: DedupResult | None = None
    current_assignment = (
        db.get(Assignment, lead.current_assignment_id)
        if lead.current_assignment_id
        else None
    )
    has_dispatch_history = bool(
        current_assignment
        or db.scalar(select(Assignment.id).where(Assignment.lead_id == lead.id).limit(1))
    )
    if any(issue.startswith("DEDUP_") for issue in previous_issues):
        if lead.duplicate_status != DuplicateDecision.OVERRIDDEN.value:
            dedup_result = evaluate_phone(
                db,
                lead=lead,
                normalized_phone=phone,
                checkpoint=(
                    "POST_DISPATCH_CORRECTION_RECHECK"
                    if has_dispatch_history
                    else "UNASSIGNED_CORRECTION_RECHECK"
                ),
                now=datetime.now(timezone.utc),
            )
            if dedup_result.blocks_dispatch:
                issues.append(f"DEDUP_{dedup_result.decision.value}")
    if current_assignment is not None:
        issues.extend(
            existing_receiver_correction_issues(
                db,
                lead=lead,
                assignment=current_assignment,
            )
        )
    normalized_issues = store_lead_correction_issues(lead, issues)
    if not normalized_issues and current_assignment is None:
        restore_unassigned_correction_workflow(db, lead)
    lead.snapshot_version += 1
    reward_changes = reconcile_supplier_rewards_after_dedup(
        db,
        lead=lead,
        dedup_result=dedup_result,
    )
    db.flush()
    return LeadCorrectionResult(
        lead=lead,
        dedup=dedup_result,
        had_dispatch_history=has_dispatch_history,
        changed_fields=(),
        issues=normalized_issues,
        before=before,
        after=_correction_audit_snapshot(lead),
        reward_changes=reward_changes,
    )


def _lock_current_assignment(db: Session, lead_id: str) -> tuple[Lead, Assignment]:
    # Claiming and follow-up both lock Assignment before Lead. Keep the same
    # order here so a cross-process race cannot deadlock or revive a release.
    observed_lead = db.scalar(
        select(Lead)
        .where(Lead.id == lead_id)
        .execution_options(populate_existing=True)
    )
    if observed_lead is None:
        raise AppError("LEAD_NOT_FOUND", "客资不存在", 404)
    if not observed_lead.current_assignment_id:
        raise AppError("LEAD_ASSIGNMENT_CONFLICT", "客资当前没有可解除的派发单", 409)
    assignment = db.scalar(
        select(Assignment)
        .where(Assignment.id == observed_lead.current_assignment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if assignment is None:
        raise AppError("LEAD_ASSIGNMENT_CONFLICT", "客资当前派发单已变更", 409)
    lead = db.scalar(
        select(Lead)
        .where(Lead.id == lead_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if lead is None:
        raise AppError("LEAD_NOT_FOUND", "客资不存在", 404)
    if lead.current_assignment_id != assignment.id or assignment.lead_id != lead.id:
        raise AppError("LEAD_ASSIGNMENT_CONFLICT", "客资与当前派发单不一致", 409)
    return lead, assignment


def _release_assignment_for_redispatch(
    db: Session,
    *,
    lead: Lead,
    assignment: Assignment,
    principal: Principal,
    reason: str,
    release_code: str,
    event_type: str,
    refund_business_type: str,
    idempotency_prefix: str,
    notification_scene: str,
    notification_reason: str,
    settled_reward_error_code: str,
    event_metadata: dict[str, Any] | None = None,
) -> LeadCorrectionRedispatchResult:
    before = _correction_audit_snapshot(lead)
    assignment_status_before = assignment.status
    now = datetime.now(timezone.utc)
    reward = db.scalar(
        select(SupplierLeadReward)
        .where(SupplierLeadReward.assignment_id == assignment.id)
        .with_for_update()
    )
    if reward is not None and reward.status == RewardStatus.SETTLED.value:
        raise AppError(
            settled_reward_error_code,
            "供客奖励已结算，请先完成奖励冲正再解除派发",
            409,
            {"reward_id": reward.id},
        )

    refund_ledger: PointsLedger | None = None
    metadata = dict(event_metadata or {})
    if assignment.status in {
        AssignmentStatus.CLAIMED.value,
        AssignmentStatus.FOLLOWING.value,
    }:
        claim_ledger = db.scalar(
            select(PointsLedger)
            .where(
                PointsLedger.company_id == assignment.company_id,
                PointsLedger.ledger_type == PointsLedgerType.CLAIM.value,
                PointsLedger.business_id == assignment.id,
                PointsLedger.business_type.in_(("V12_ASSIGNMENT_CLAIM", "ASSIGNMENT")),
            )
            .order_by(PointsLedger.created_at.desc())
            .with_for_update()
        )
        if claim_ledger is None or int(claim_ledger.delta) >= 0:
            raise AppError("CLAIM_LEDGER_MISSING", "派发单已领取但原扣分流水缺失", 500)
        refund_ledger = change_points(
            db,
            company_id=assignment.company_id,
            delta=abs(int(claim_ledger.delta)),
            ledger_type=PointsLedgerType.RETURN.value,
            business_type=refund_business_type,
            business_id=assignment.id,
            idempotency_key=f"{idempotency_prefix}:{assignment.id}:redispatch-refund",
            related_ledger_id=claim_ledger.id,
            created_by=principal.user_id,
            metadata={
                "lead_id": lead.id,
                "assignment_id": assignment.id,
                "reason": reason,
                **metadata,
            },
        )

    if reward is not None and reward.status in {
        RewardStatus.WAITING_CLAIM.value,
        RewardStatus.OBSERVING.value,
        RewardStatus.FROZEN.value,
    }:
        reward.status = RewardStatus.CANCELLED.value
        reward.cancelled_at = now
        reward.exception_reason = release_code

    assignment.status = AssignmentStatus.RELEASED.value
    assignment.released_at = now
    assignment.release_reason = release_code
    lead.current_assignment_id = None
    lead.status = LeadV12Status.READY_DISPATCH.value
    lead.current_follow_status = None
    store_lead_correction_issues(lead, [])
    restore_unassigned_correction_workflow(db, lead)
    lead.snapshot_version += 1
    db.add(
        AssignmentEvent(
            assignment_id=assignment.id,
            event_type=event_type,
            actor_user_id=principal.user_id,
            payload={
                "lead_id": lead.id,
                "refund_ledger_id": refund_ledger.id if refund_ledger else None,
                "refund_points": int(refund_ledger.delta) if refund_ledger else 0,
                "reward_id": reward.id if reward else None,
                "reason": reason,
                **metadata,
            },
        )
    )
    notification_body = (
        f"平台因{notification_reason}已撤回该客资，{int(refund_ledger.delta)} 积分已全额退回。"
        if refund_ledger is not None
        else f"平台因{notification_reason}已撤回该待领取客资。"
    )
    notification = create_station_message(
        db,
        user_id=None,
        company_id=assignment.company_id,
        scene=notification_scene,
        title="客资已由平台撤回",
        body=notification_body,
        deep_link=f"/h5/v12-workbench.html?view=assignments&id={assignment.id}",
    )
    enqueue_outbox(
        db,
        event_key=f"{idempotency_prefix}:{assignment.id}:redispatch-notification",
        event_type=notification_scene,
        aggregate_type="assignment",
        aggregate_id=assignment.id,
        payload={
            "notification_id": notification.id,
            "company_id": assignment.company_id,
            "assignment_id": assignment.id,
            "lead_id": lead.id,
            "deep_link": notification.deep_link,
            "refund_points": int(refund_ledger.delta) if refund_ledger else 0,
        },
    )
    db.flush()
    return LeadCorrectionRedispatchResult(
        lead=lead,
        assignment=assignment,
        assignment_status_before=assignment_status_before,
        refund_ledger=refund_ledger,
        before=before,
        after=_correction_audit_snapshot(lead),
        expired_return_request_id=metadata.get("expired_return_request_id"),
    )


def release_corrected_lead_for_redispatch(
    db: Session,
    *,
    lead_id: str,
    principal: Principal,
    reason: str,
    expected_snapshot_version: int,
) -> LeadCorrectionRedispatchResult:
    """Release an ineligible receiver and return a corrected lead to dispatch."""

    if not (principal.can("*") or principal.can("lead.manual.manage")):
        raise AppError("FORBIDDEN", "无权处理更正后的派发异常", 403)
    normalized_reason = reason.strip()
    if len(normalized_reason) < 5:
        raise AppError("LEAD_CORRECTION_REASON_REQUIRED", "解除派发必须填写原因", 422)

    lead, assignment = _lock_current_assignment(db, lead_id)
    if expected_snapshot_version != lead.snapshot_version:
        raise AppError(
            "LEAD_VERSION_CONFLICT",
            "客资已被其他人更新，请刷新后重试",
            409,
            {
                "expected_snapshot_version": expected_snapshot_version,
                "current_snapshot_version": lead.snapshot_version,
            },
        )
    correction_issues = list((lead.raw_payload or {}).get("correction_issues") or [])
    if lead.pending_reason != "CORRECTION_REVIEW_REQUIRED" or not correction_issues:
        raise AppError(
            "LEAD_CORRECTION_REDISPATCH_NOT_REQUIRED",
            "当前客资没有需要解除派发的更正异常",
            409,
        )
    if any(issue.startswith("DEDUP_") for issue in correction_issues):
        raise AppError(
            "LEAD_CORRECTION_DEDUP_UNRESOLVED",
            "请先完成重复客资处置，再解除原派发",
            409,
        )
    if assignment.status == AssignmentStatus.RETURN_PENDING.value:
        raise AppError(
            "LEAD_CORRECTION_RETURN_IN_PROGRESS",
            "当前派发单正在退回处理，请先完成退回终审",
            409,
        )
    releasable_statuses = {
        AssignmentStatus.PENDING_CLAIM.value,
        AssignmentStatus.CLAIMED.value,
        AssignmentStatus.FOLLOWING.value,
    }
    if assignment.status not in releasable_statuses:
        raise AppError(
            "LEAD_CORRECTION_ASSIGNMENT_NOT_RELEASABLE",
            "当前派发状态不支持自动解除，需人工复核",
            409,
            {"assignment_status": assignment.status},
        )

    return _release_assignment_for_redispatch(
        db,
        lead=lead,
        assignment=assignment,
        principal=principal,
        reason=normalized_reason,
        release_code="CORRECTION_REDISPATCH",
        event_type="V12_CORRECTION_REDISPATCH_RELEASE",
        refund_business_type="V12_CORRECTION_REDISPATCH_REFUND",
        idempotency_prefix="v12-correction",
        notification_scene="V12_CORRECTION_REDISPATCH",
        notification_reason="客资事实更正",
        settled_reward_error_code="LEAD_CORRECTION_REWARD_SETTLED",
        event_metadata={"correction_issues": correction_issues},
    )


def release_misdispatched_lead_for_redispatch(
    db: Session,
    *,
    lead_id: str,
    principal: Principal,
    reason: str,
    expected_snapshot_version: int,
) -> LeadCorrectionRedispatchResult:
    if not (principal.can("*") or principal.can("lead.manual.manage")):
        raise AppError("FORBIDDEN", "无权撤回错误派发", 403)
    normalized_reason = reason.strip()
    if len(normalized_reason) < 5:
        raise AppError("MISDISPATCH_REASON_REQUIRED", "撤回错派必须填写原因", 422)
    lead, assignment = _lock_current_assignment(db, lead_id)
    if expected_snapshot_version != lead.snapshot_version:
        raise AppError(
            "LEAD_VERSION_CONFLICT",
            "客资已被其他人更新，请刷新后重试",
            409,
            {
                "expected_snapshot_version": expected_snapshot_version,
                "current_snapshot_version": lead.snapshot_version,
            },
        )
    if lead.pending_reason == "CORRECTION_REVIEW_REQUIRED" or (
        lead.raw_payload or {}
    ).get("correction_issues"):
        raise AppError(
            "MISDISPATCH_CORRECTION_IN_PROGRESS",
            "当前客资正在处理事实更正，请使用更正流程解除原派发",
            409,
        )
    if assignment.status == AssignmentStatus.RETURN_PENDING.value:
        raise AppError(
            "MISDISPATCH_RETURN_IN_PROGRESS",
            "当前派发单正在退回处理，请先完成退回终审",
            409,
        )
    if assignment.status not in {
        AssignmentStatus.PENDING_CLAIM.value,
        AssignmentStatus.CLAIMED.value,
        AssignmentStatus.FOLLOWING.value,
    }:
        raise AppError(
            "MISDISPATCH_ASSIGNMENT_NOT_RELEASABLE",
            "当前派发状态不允许撤回错派",
            409,
            {"assignment_status": assignment.status},
        )
    return_request = db.scalar(
        select(ReturnRequest)
        .where(ReturnRequest.assignment_id == assignment.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    expired_return_request_id: str | None = None
    if return_request is not None:
        active_return_statuses = {
            ReturnV12Status.SUBMITTED.value,
            ReturnV12Status.VERIFYING.value,
            ReturnV12Status.NEED_MORE_EVIDENCE.value,
            ReturnV12Status.REVIEWING.value,
        }
        if return_request.status in active_return_statuses:
            raise AppError(
                "MISDISPATCH_RETURN_REQUEST_CONFLICT",
                "该派发单已有非草稿退回申请，请先完成退回流程",
                409,
                {"return_status": return_request.status},
            )
        if return_request.status == ReturnV12Status.DRAFT.value:
            assert_return_transition(ReturnV12Status.DRAFT, ReturnV12Status.EXPIRED)
            return_request.status = ReturnV12Status.EXPIRED.value
            return_request.reviewed_by = principal.user_id
            return_request.reviewed_at = datetime.now(timezone.utc)
            return_request.review_note = "平台错派撤回，未提交的退回草稿同步关闭"
            expired_return_request_id = return_request.id
    return _release_assignment_for_redispatch(
        db,
        lead=lead,
        assignment=assignment,
        principal=principal,
        reason=normalized_reason,
        release_code="MISDISPATCH_REDISPATCH",
        event_type="V12_MISDISPATCH_REDISPATCH_RELEASE",
        refund_business_type="V12_MISDISPATCH_REDISPATCH_REFUND",
        idempotency_prefix="v12-misdispatch",
        notification_scene="V12_MISDISPATCH_REDISPATCH",
        notification_reason="运营错误派发",
        settled_reward_error_code="MISDISPATCH_REWARD_SETTLED",
        event_metadata={
            "expired_return_request_id": expired_return_request_id,
        },
    )


def _validate_submission(db: Session, lead: Lead) -> str:
    phone = normalize_phone(decrypt_text(lead.phone_encrypted) or "")
    errors: dict[str, str] = {}
    if len(phone) != 11 or not phone.startswith("1"):
        errors["phone"] = "手机号必填且必须为 11 位有效号码"
    if lead.region_code and not db.scalar(
        select(Region.code).where(Region.code == lead.region_code, Region.active.is_(True))
    ):
        errors["region_code"] = "标准地区编码无效或已停用"
    if not lead.consent_confirmed:
        errors["consent_confirmed"] = "必须确认已获得客户信息授权"
    if lead.source_channel == "OTHER" and not _clean_text(lead.source_detail):
        errors["source_detail"] = "来源选择其他时，必须填写具体来源"
    if lead.budget_min is not None and lead.budget_max is not None and lead.budget_min > lead.budget_max:
        errors["budget_max"] = "预算上限不能低于预算下限"
    if errors:
        raise AppError("LEAD_SUBMISSION_INVALID", "客资提交校验失败", 422, {"fields": errors})
    return phone


def _has_known_location(db: Session, lead: Lead) -> bool:
    if not lead.region_code:
        return False
    return bool(
        db.scalar(
            select(Region.code).where(
                Region.code == lead.region_code,
                Region.active.is_(True),
            )
        )
    )


def _materialize_nationwide_location(db: Session, lead: Lead) -> None:
    """Persist only the selected nationwide location before it enters business flow."""

    if not lead.region_code:
        return
    selected = db.get(Region, lead.region_code)
    if selected is not None:
        district: Region | None = None
        city: Region | None = None
        if selected.level == "TOWNSHIP":
            district = db.get(Region, selected.parent_code) if selected.parent_code else None
            city = db.get(Region, district.parent_code) if district and district.parent_code else None
            valid = bool(
                selected.active
                and district
                and district.active
                and district.level == "DISTRICT"
                and city
                and city.active
                and city.level == "CITY"
            )
        elif selected.level == "DISTRICT":
            district = selected
            city = db.get(Region, selected.parent_code) if selected.parent_code else None
            valid = bool(selected.active and city and city.active and city.level == "CITY")
        elif selected.level == "CITY":
            city = selected
            valid = selected.active
        else:
            valid = False
        if not valid or city is None:
            raise AppError(
                "LEAD_SUBMISSION_INVALID",
                "客资提交校验失败",
                422,
                {"fields": {"region_code": "标准地区编码层级无效或已停用"}},
            )
        province = (
            db.get(Region, city.parent_code)
            if city.parent_code
            else None
        )
        location = region_by_code(district.code if district else city.code)
        if province is not None and province.active and province.level == "PROVINCE":
            lead.province = province.name
        elif location is not None:
            lead.province = str(location["province_name"])
        lead.city = city.name
        lead.district = district.name if district else None
        return
    location = region_by_code(lead.region_code)
    if location is None:
        return
    lead.province = str(location["province_name"])
    city_code = str(location["city_code"])
    city = db.get(Region, city_code)
    if city is None:
        city = Region(
            code=city_code,
            name=str(location["city_name"]),
            level="CITY",
            parent_code=None,
            aliases=[str(location["city_name"])],
            active=True,
        )
        db.add(city)
    district_code = location["district_code"]
    district_name = location["district_name"]
    if district_code and db.get(Region, district_code) is None:
        db.add(
            Region(
                code=district_code,
                name=str(district_name),
                level="DISTRICT",
                parent_code=city_code,
                aliases=[str(district_name)],
                active=True,
            )
        )
    lead.city = str(location["city_name"])
    lead.district = str(district_name) if district_name else None
    db.flush()


def submit_draft(
    db: Session,
    *,
    lead: Lead,
    principal: Principal,
    checkpoint: str = "SUBMIT",
) -> DedupResult:
    _assert_draft(lead)
    supplier = lead.source_kind == LeadSourceKind.SUPPLIER_H5.value
    _assert_owner(lead, principal, supplier=supplier)
    if supplier:
        require_lead_capability(db, principal.company_id, "LEAD_SUPPLIER")
    _materialize_nationwide_location(db, lead)
    phone = _validate_submission(db, lead)
    if supplier:
        lead.review_note = None
        lead.reviewed_at = None
    now = datetime.now(timezone.utc)
    lead.submitted_at = now
    lead.imported_at = lead.imported_at or now
    result = evaluate_phone(db, lead=lead, normalized_phone=phone, checkpoint=checkpoint, now=now)
    apply_submission_decision(lead, result)
    if not result.blocks_dispatch:
        if _has_known_location(db, lead):
            route_approved_lead_to_pool(db, lead)
            lead.review_status = LeadReviewStatus.APPROVED.value
        else:
            lead.status = LeadV12Status.PENDING_REVIEW.value
            lead.review_status = LeadReviewStatus.PENDING.value
            db.flush()
            queue_pre_dispatch_task(
                db,
                lead_id=lead.id,
                reason="LOCATION_REQUIRES_TELESALES_VERIFY",
            )
    db.flush()
    return result


def review_supplier_lead(
    db: Session,
    *,
    lead: Lead,
    reviewer: Principal,
    note: str | None,
    approve: bool | None = None,
    decision: str | None = None,
) -> DedupResult | None:
    locked_lead = db.scalar(
        select(Lead)
        .where(Lead.id == lead.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_lead is None:
        raise AppError("LEAD_NOT_FOUND", "客资不存在", 404)
    lead = locked_lead
    require_correction_review_resolved(lead)
    if lead.source_kind != LeadSourceKind.SUPPLIER_H5.value:
        raise AppError("LEAD_SOURCE_INVALID", "仅供应商上传客资需要资料初审", 409)
    if lead.status not in {LeadV12Status.PENDING_REVIEW.value, LeadV12Status.DUPLICATE.value}:
        raise AppError("LEAD_REVIEW_STATE_INVALID", "当前客资状态不可初审", 409)
    normalized_decision = (decision or ("QUALIFIED" if approve else "INVALID")).strip().upper()
    legacy_decisions = {"APPROVE": "QUALIFIED", "REJECT": "INVALID"}
    normalized_decision = legacy_decisions.get(normalized_decision, normalized_decision)
    if normalized_decision not in {"QUALIFIED", "INFO_INCOMPLETE", "DUPLICATE", "INVALID"}:
        raise AppError("SUPPLIER_REVIEW_DECISION_INVALID", "初审结论无效", 422)

    lead.review_note = _clean_text(note)
    lead.reviewed_at = datetime.now(timezone.utc)
    if normalized_decision in {"INFO_INCOMPLETE", "DUPLICATE", "INVALID"} and not lead.review_note:
        raise AppError("REVIEW_NOTE_REQUIRED", "该初审结论必须填写原因", 422)
    if normalized_decision == "INFO_INCOMPLETE":
        if lead.status != LeadV12Status.PENDING_REVIEW.value:
            raise AppError("LEAD_REVIEW_INFO_INCOMPLETE_INVALID", "仅待初审客资可派发前置电销核验", 409)
        lead.review_status = LeadReviewStatus.PENDING.value
        lead.pending_reason = "SUPPLIER_REVIEW_INFO_INCOMPLETE"
        db.flush()
        return None
    if normalized_decision == "DUPLICATE":
        lead.review_status = LeadReviewStatus.PENDING.value
        lead.status = LeadV12Status.DUPLICATE.value
        lead.pending_reason = "SUPPLIER_REVIEW_DUPLICATE"
        db.flush()
        return None
    if normalized_decision == "INVALID":
        lead.review_status = LeadReviewStatus.REJECTED.value
        lead.status = LeadV12Status.INVALID.value
        lead.pending_reason = "SUPPLIER_REVIEW_INVALID"
        db.flush()
        return None

    phone = _validate_submission(db, lead)
    result = evaluate_phone(
        db,
        lead=lead,
        normalized_phone=phone,
        checkpoint="SUPPLIER_REVIEW",
        now=datetime.now(timezone.utc),
    )
    if result.blocks_dispatch:
        lead.review_status = LeadReviewStatus.PENDING.value
        lead.status = LeadV12Status.DUPLICATE.value
        lead.pending_reason = result.decision.value
    elif _has_known_location(db, lead):
        route_approved_lead_to_pool(db, lead)
        lead.review_status = LeadReviewStatus.APPROVED.value
    else:
        lead.status = LeadV12Status.PENDING_REVIEW.value
        db.flush()
        queue_pre_dispatch_task(
            db,
            lead_id=lead.id,
            reason="LOCATION_REQUIRES_TELESALES_VERIFY",
        )
        lead.review_status = LeadReviewStatus.PENDING.value
    db.flush()
    return result


def get_lead_or_404(db: Session, lead_id: str) -> Lead:
    return require_lead_not_deleted(db.get(Lead, lead_id))


def list_supplier_leads(
    db: Session,
    *,
    company_id: str,
    status: str | None,
    page_no: int,
    page_size: int,
    submitter_user_id: str | None = None,
) -> tuple[list[Lead], int]:
    stmt = select(Lead).where(
        Lead.source_kind == LeadSourceKind.SUPPLIER_H5.value,
        Lead.supplier_company_id == company_id,
        Lead.deleted_at.is_(None),
    )
    count_stmt = select(func.count(Lead.id)).where(
        Lead.source_kind == LeadSourceKind.SUPPLIER_H5.value,
        Lead.supplier_company_id == company_id,
        Lead.deleted_at.is_(None),
    )
    if submitter_user_id:
        stmt = stmt.where(Lead.submitter_user_id == submitter_user_id)
        count_stmt = count_stmt.where(Lead.submitter_user_id == submitter_user_id)
    if status:
        stmt = stmt.where(Lead.status == status)
        count_stmt = count_stmt.where(Lead.status == status)
    total = db.scalar(count_stmt) or 0
    items = list(
        db.scalars(
            stmt.order_by(Lead.created_at.desc()).offset((page_no - 1) * page_size).limit(page_size)
        ).all()
    )
    return items, total


def list_platform_leads(
    db: Session,
    *,
    status: str | None,
    region: str | None,
    page_no: int,
    page_size: int,
) -> tuple[list[Lead], int]:
    filters = [
        Lead.source_kind == LeadSourceKind.PLATFORM_MANUAL.value,
        Lead.deleted_at.is_(None),
    ]
    if status:
        filters.append(Lead.status == status.strip().upper())
    region_keyword = region.strip() if region else ""
    if region_keyword:
        filters.append(
            or_(
                Lead.province.contains(region_keyword, autoescape=True),
                Lead.city.contains(region_keyword, autoescape=True),
                Lead.district.contains(region_keyword, autoescape=True),
                Lead.region_code.contains(region_keyword, autoescape=True),
            )
        )
    total = int(db.scalar(select(func.count(Lead.id)).where(*filters)) or 0)
    items = list(
        db.scalars(
            select(Lead)
            .where(*filters)
            .order_by(Lead.created_at.desc(), Lead.id.desc())
            .offset((page_no - 1) * page_size)
            .limit(page_size)
        ).all()
    )
    return items, total


def latest_dedup_event(db: Session, lead_id: str) -> LeadDedupEvent | None:
    return db.scalar(
        select(LeadDedupEvent)
        .where(LeadDedupEvent.lead_id == lead_id)
        .order_by(LeadDedupEvent.created_at.desc())
    )


def customer_source_for_lead(lead: Lead) -> str:
    if lead.source_kind == LeadSourceKind.SUPPLIER_H5.value:
        return CustomerSource.FRANCHISE_SUPPLIED.value
    return CustomerSource.OPERATION_ENTRY.value


def lead_supply_to_dict(
    lead: Lead,
    principal: Principal | None = None,
    *,
    submitter_name: str | None = None,
    supplier_company_name: str | None = None,
    region_name: str | None = None,
    region_level: str | None = None,
    current_assignment: dict[str, Any] | None = None,
    assignment_history: list[dict[str, Any]] | None = None,
    followup_history: list[dict[str, Any]] | None = None,
    latest_pre_dispatch_task: VerificationTask | None = None,
    supplier_cooperation_status: str | None = None,
) -> dict[str, Any]:
    phone = decrypt_text(lead.phone_encrypted)
    can_view_phone = bool(
        principal
        and (
            principal.can("*")
            or principal.can("lead.phone.read")
            or (principal.company_id and lead.supplier_company_id == principal.company_id)
            or lead.submitter_user_id == principal.user_id
        )
    )
    result = {
        "id": lead.id,
        "source_kind": lead.source_kind,
        "customer_source": customer_source_for_lead(lead),
        "submitter_user_id": lead.submitter_user_id,
        "submitter_name": submitter_name,
        "supplier_company_id": lead.supplier_company_id,
        "supplier_company_name": supplier_company_name,
        "customer_name": lead.customer_name,
        "phone": phone if can_view_phone else None,
        "phone_masked": mask_phone(phone),
        "province": lead.province,
        "city": lead.city,
        "district": lead.district,
        "region_code": lead.region_code,
        "region_name": region_name,
        "region_level": region_level,
        "category_code": lead.category_code,
        "brand_code": lead.brand_code,
        "source_channel": lead.source_channel,
        "source_detail": lead.source_detail,
        "source_display": (
            f"其他（{lead.source_detail}）"
            if lead.source_channel == "OTHER" and lead.source_detail
            else lead.source_channel
        ),
        "need_summary": lead.need_summary,
        "budget_min": lead.budget_min,
        "budget_max": lead.budget_max,
        "acquisition_cost_cents": lead.acquisition_cost_cents,
        "is_test": lead.is_test,
        "consent_confirmed": lead.consent_confirmed,
        "status": lead.status,
        "review_status": lead.review_status,
        "review_note": lead.review_note,
        "duplicate_status": lead.duplicate_status,
        "pending_reason": lead.pending_reason,
        "correction_issues": list((lead.raw_payload or {}).get("correction_issues") or []),
        "public_pool_validation_errors": dict(
            (lead.raw_payload or {}).get("public_pool_validation_errors") or {}
        ),
        "current_assignment_id": lead.current_assignment_id,
        "current_assignment_status": (
            current_assignment.get("status") if current_assignment else None
        ),
        "current_receiver_company_id": (
            current_assignment.get("receiver_company_id") if current_assignment else None
        ),
        "current_receiver_company_name": (
            current_assignment.get("receiver_company_name") if current_assignment else None
        ),
        "assigned_by_user_id": (
            current_assignment.get("assigned_by_user_id") if current_assignment else None
        ),
        "assigned_by_name": (
            current_assignment.get("assigned_by_name") if current_assignment else None
        ),
        "assigned_at": current_assignment.get("assigned_at") if current_assignment else None,
        "snapshot_version": lead.snapshot_version,
        "submitted_at": lead.submitted_at.isoformat() if lead.submitted_at else None,
        "reviewed_at": lead.reviewed_at.isoformat() if lead.reviewed_at else None,
        "created_at": lead.created_at.isoformat(),
        "updated_at": lead.updated_at.isoformat(),
        "latest_pre_dispatch_task_id": (
            latest_pre_dispatch_task.id if latest_pre_dispatch_task else None
        ),
        "latest_pre_dispatch_contact_result": (
            latest_pre_dispatch_task.contact_result
            if latest_pre_dispatch_task
            else None
        ),
        "latest_pre_dispatch_conclusion": (
            latest_pre_dispatch_task.verification_conclusion
            if latest_pre_dispatch_task
            else None
        ),
        "can_requeue_pre_dispatch": bool(
            lead.status
            in {
                LeadV12Status.PENDING_OPERATION_DISPOSITION.value,
                LeadV12Status.CLOSED.value,
                LeadV12Status.INVALID.value,
            }
            and lead.current_assignment_id is None
            and is_pre_dispatch_task_requeueable(latest_pre_dispatch_task)
            and (
                lead.supplier_company_id is None
                or supplier_cooperation_status == "ACTIVE"
            )
        ),
    }
    if assignment_history is not None:
        result["assignment_history"] = assignment_history
    if followup_history is not None:
        result["followup_history"] = followup_history
        result["latest_followup"] = followup_history[-1] if followup_history else None
    return result


def _assignment_projection(
    assignment: Assignment,
    *,
    receiver_company_name: str | None,
    assigned_by_name: str | None,
) -> dict[str, Any]:
    return {
        "id": assignment.id,
        "assignment_id": assignment.id,
        "receiver_company_id": assignment.receiver_company_id or assignment.company_id,
        "receiver_company_name": receiver_company_name,
        "status": assignment.status,
        "assigned_by_user_id": assignment.assigned_by,
        "assigned_by_name": assigned_by_name,
        "assigned_at": assignment.assigned_at.isoformat(),
        "claimed_at": assignment.claimed_at.isoformat() if assignment.claimed_at else None,
        "released_at": assignment.released_at.isoformat() if assignment.released_at else None,
        "release_reason": assignment.release_reason,
    }


def _followup_projection(
    followup: FollowUp,
    *,
    created_by_name: str | None,
) -> dict[str, Any]:
    return {
        "id": followup.id,
        "assignment_id": followup.assignment_id,
        "status": followup.status,
        "note": followup.note,
        "next_followup_at": (
            followup.next_followup_at.isoformat()
            if followup.next_followup_at
            else None
        ),
        "created_by_user_id": followup.created_by,
        "created_by_name": created_by_name,
        "created_at": followup.created_at.isoformat(),
    }


def lead_supply_list_to_dict(
    db: Session,
    leads: list[Lead],
    principal: Principal | None = None,
    *,
    include_assignment_history: bool = False,
) -> list[dict[str, Any]]:
    submitter_ids = {lead.submitter_user_id for lead in leads if lead.submitter_user_id}
    submitter_names = dict(
        db.execute(
            select(User.id, User.display_name).where(User.id.in_(submitter_ids))
        ).all()
    ) if submitter_ids else {}
    supplier_company_ids = {
        lead.supplier_company_id for lead in leads if lead.supplier_company_id
    }
    supplier_company_rows = (
        db.execute(
            select(
                Company.id,
                Company.name,
                Company.supplier_cooperation_status,
            ).where(Company.id.in_(supplier_company_ids))
        ).all()
        if supplier_company_ids
        else []
    )
    supplier_company_names = {
        company_id: company_name
        for company_id, company_name, _ in supplier_company_rows
    }
    supplier_cooperation_statuses = {
        company_id: cooperation_status
        for company_id, _, cooperation_status in supplier_company_rows
    }
    region_codes = {lead.region_code for lead in leads if lead.region_code}
    regions_by_code = {
        region.code: region
        for region in db.scalars(select(Region).where(Region.code.in_(region_codes))).all()
    } if region_codes else {}
    assigned_by_user = aliased(User)
    current_assignment_ids = {
        lead.current_assignment_id for lead in leads if lead.current_assignment_id
    }
    current_assignments: dict[str, dict[str, Any]] = {}
    if current_assignment_ids:
        current_rows = db.execute(
            select(Assignment, Company.name, assigned_by_user.display_name)
            .outerjoin(
                Company,
                Company.id == func.coalesce(
                    Assignment.receiver_company_id,
                    Assignment.company_id,
                ),
            )
            .outerjoin(assigned_by_user, assigned_by_user.id == Assignment.assigned_by)
            .where(Assignment.id.in_(current_assignment_ids))
        ).all()
        current_assignments = {
            assignment.id: _assignment_projection(
                assignment,
                receiver_company_name=company_name,
                assigned_by_name=assigned_by_name,
            )
            for assignment, company_name, assigned_by_name in current_rows
        }

    lead_ids = [lead.id for lead in leads]
    latest_pre_dispatch_tasks: dict[str, VerificationTask] = {}
    if lead_ids:
        ranked_tasks = (
            select(
                VerificationTask.id.label("task_id"),
                func.row_number()
                .over(
                    partition_by=VerificationTask.lead_id,
                    order_by=(
                        VerificationTask.created_at.desc(),
                        VerificationTask.id.desc(),
                    ),
                )
                .label("position"),
            )
            .where(
                VerificationTask.lead_id.in_(lead_ids),
                VerificationTask.task_type
                == VerificationTaskType.PRE_DISPATCH_VERIFY.value,
            )
            .subquery()
        )
        task_rows = db.scalars(
            select(VerificationTask)
            .join(
                ranked_tasks,
                ranked_tasks.c.task_id == VerificationTask.id,
            )
            .where(ranked_tasks.c.position == 1)
        ).all()
        latest_pre_dispatch_tasks = {task.lead_id: task for task in task_rows}

    histories: dict[str, list[dict[str, Any]]] = {}
    followups: dict[str, list[dict[str, Any]]] = {}
    if include_assignment_history and leads:
        history_rows = db.execute(
            select(Assignment, Company.name, assigned_by_user.display_name)
            .outerjoin(
                Company,
                Company.id == func.coalesce(
                    Assignment.receiver_company_id,
                    Assignment.company_id,
                ),
            )
            .outerjoin(assigned_by_user, assigned_by_user.id == Assignment.assigned_by)
            .where(Assignment.lead_id.in_([lead.id for lead in leads]))
            .order_by(Assignment.assigned_at.asc(), Assignment.id.asc())
        ).all()
        for assignment, company_name, assigned_by_name in history_rows:
            histories.setdefault(assignment.lead_id, []).append(
                _assignment_projection(
                    assignment,
                    receiver_company_name=company_name,
                    assigned_by_name=assigned_by_name,
                )
            )
        followup_creator = aliased(User)
        followup_rows = db.execute(
            select(FollowUp, Assignment.lead_id, followup_creator.display_name)
            .join(Assignment, Assignment.id == FollowUp.assignment_id)
            .outerjoin(followup_creator, followup_creator.id == FollowUp.created_by)
            .where(Assignment.lead_id.in_([lead.id for lead in leads]))
            .order_by(FollowUp.created_at.asc(), FollowUp.id.asc())
        ).all()
        for followup, lead_id, created_by_name in followup_rows:
            followups.setdefault(lead_id, []).append(
                _followup_projection(
                    followup,
                    created_by_name=created_by_name,
                )
            )
    return [
        lead_supply_to_dict(
            lead,
            principal,
            submitter_name=submitter_names.get(lead.submitter_user_id),
            supplier_company_name=supplier_company_names.get(
                lead.supplier_company_id
            ),
            region_name=(regions_by_code.get(lead.region_code).name if regions_by_code.get(lead.region_code) else None),
            region_level=(regions_by_code.get(lead.region_code).level if regions_by_code.get(lead.region_code) else None),
            current_assignment=current_assignments.get(lead.current_assignment_id),
            assignment_history=(histories.get(lead.id, []) if include_assignment_history else None),
            followup_history=(followups.get(lead.id, []) if include_assignment_history else None),
            latest_pre_dispatch_task=latest_pre_dispatch_tasks.get(lead.id),
            supplier_cooperation_status=supplier_cooperation_statuses.get(
                lead.supplier_company_id
            ),
        )
        for lead in leads
    ]
