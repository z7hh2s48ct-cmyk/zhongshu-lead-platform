from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..core.enums import AssignmentStatus
from ..core.errors import AppError
from ..core.models import Assignment, Lead, LeadDuplicateRelation
from ..core.models_v12 import DedupOverride, LeadDedupEvent
from ..core.security import fingerprint_phone, hash_phone
from ..core.v12_enums import DuplicateDecision, LeadSourceKind, LeadV12Status
from .reward_rule_v12 import SupplierRewardRule, resolve_supplier_reward_rule
from .lead_correction_guard import store_lead_correction_issues
from .phone_uniqueness import acquire_phone_identity_lock as _acquire_phone_identity_lock

settings = get_settings()

REVIEWABLE_DUPLICATE_DECISIONS = {
    DuplicateDecision.HARD_DUPLICATE.value,
    DuplicateDecision.REWARD_DUPLICATE.value,
    DuplicateDecision.HISTORICAL_SUSPECT.value,
}


@dataclass(frozen=True, slots=True)
class DedupResult:
    decision: DuplicateDecision
    matched_lead_id: str | None = None
    window_days: int | None = None
    age_days: int | None = None
    event_id: str | None = None

    @property
    def blocks_dispatch(self) -> bool:
        return self.decision in {
            DuplicateDecision.HARD_DUPLICATE,
            DuplicateDecision.HISTORICAL_SUSPECT,
        }

    @property
    def reward_eligible(self) -> bool:
        return self.decision not in {
            DuplicateDecision.HARD_DUPLICATE,
            DuplicateDecision.REWARD_DUPLICATE,
        }


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _lead_business_time(lead: Lead) -> datetime:
    return _as_utc(lead.submitted_at or lead.imported_at or lead.created_at)


def classify_age(
    age_days: int,
    rule: SupplierRewardRule | None = None,
) -> tuple[DuplicateDecision, int | None]:
    hard_days = rule.hard_duplicate_days if rule else settings.lead_hard_duplicate_days
    reward_days = rule.reward_duplicate_days if rule else settings.lead_reward_duplicate_days
    historical_days = (
        rule.historical_suspect_days if rule else settings.lead_historical_suspect_days
    )
    if age_days <= hard_days:
        return DuplicateDecision.HARD_DUPLICATE, hard_days
    if age_days <= reward_days:
        return DuplicateDecision.REWARD_DUPLICATE, reward_days
    if age_days <= historical_days:
        return DuplicateDecision.HISTORICAL_SUSPECT, historical_days
    return DuplicateDecision.CLEAR, None


def evaluate_phone(
    db: Session,
    *,
    lead: Lead,
    normalized_phone: str,
    checkpoint: str,
    now: datetime | None = None,
) -> DedupResult:
    fingerprint = fingerprint_phone(normalized_phone)
    legacy_hash = hash_phone(normalized_phone)
    return _evaluate_phone_identity(
        db,
        lead=lead,
        fingerprint=fingerprint,
        legacy_hash=legacy_hash,
        checkpoint=checkpoint,
        now=now,
    )


def reevaluate_existing_phone_identity(
    db: Session,
    *,
    lead: Lead,
    checkpoint: str,
    now: datetime | None = None,
) -> DedupResult:
    """Re-run dedup after a matched historical record has been removed."""

    return _evaluate_phone_identity(
        db,
        lead=lead,
        fingerprint=lead.phone_fingerprint or lead.phone_hash,
        legacy_hash=lead.phone_hash,
        checkpoint=checkpoint,
        now=now,
    )


def _evaluate_phone_identity(
    db: Session,
    *,
    lead: Lead,
    fingerprint: str,
    legacy_hash: str,
    checkpoint: str,
    now: datetime | None,
) -> DedupResult:
    _acquire_phone_identity_lock(db, fingerprint)
    now = _as_utc(now or datetime.now(timezone.utc))
    rule = resolve_supplier_reward_rule(db, as_of=now)
    cutoff = now - timedelta(days=rule.historical_suspect_days)
    candidates = db.scalars(
        select(Lead)
        .where(
            Lead.id != lead.id,
            Lead.status != LeadV12Status.DRAFT.value,
            or_(
                Lead.phone_fingerprint == fingerprint,
                Lead.phone_hash == legacy_hash,
            ),
        )
        .order_by(Lead.imported_at.desc())
    ).all()
    matched: Lead | None = None
    matched_age: int | None = None
    for candidate in candidates:
        business_time = _lead_business_time(candidate)
        if business_time < cutoff:
            continue
        age = max(0, (now - business_time).days)
        if matched is None or age < (matched_age if matched_age is not None else 10**9):
            matched = candidate
            matched_age = age
    if matched is None or matched_age is None:
        decision, window_days = DuplicateDecision.CLEAR, None
    else:
        decision, window_days = classify_age(matched_age, rule)

    event = LeadDedupEvent(
        lead_id=lead.id,
        phone_fingerprint=fingerprint,
        checkpoint=checkpoint.strip().upper(),
        decision=decision.value,
        matched_lead_id=matched.id if matched else None,
        window_days=window_days,
        details_json={
            "age_days": matched_age,
            "reward_eligible": decision not in {
                DuplicateDecision.HARD_DUPLICATE,
                DuplicateDecision.REWARD_DUPLICATE,
            },
            "rule_version": rule.version,
            "rule_config_id": rule.config_id,
            "dedup_windows": {
                "hard_duplicate_days": rule.hard_duplicate_days,
                "reward_duplicate_days": rule.reward_duplicate_days,
                "historical_suspect_days": rule.historical_suspect_days,
            },
        },
    )
    db.add(event)
    if matched is not None:
        relation = db.scalar(
            select(LeadDuplicateRelation).where(
                LeadDuplicateRelation.lead_id == lead.id,
                LeadDuplicateRelation.duplicate_lead_id == matched.id,
            )
        )
        if relation is None:
            db.add(
                LeadDuplicateRelation(
                    lead_id=lead.id,
                    duplicate_lead_id=matched.id,
                    reason=f"V12_{decision.value}",
                )
            )
    lead.phone_fingerprint = fingerprint
    lead.duplicate_status = decision.value
    db.flush()
    return DedupResult(
        decision=decision,
        matched_lead_id=matched.id if matched else None,
        window_days=window_days,
        age_days=matched_age,
        event_id=event.id,
    )


def apply_submission_decision(lead: Lead, result: DedupResult) -> None:
    if result.blocks_dispatch:
        lead.status = LeadV12Status.DUPLICATE.value
        lead.pending_reason = result.decision.value
        return
    if lead.source_kind == LeadSourceKind.SUPPLIER_H5.value:
        lead.status = LeadV12Status.PENDING_REVIEW.value
        lead.review_status = "PENDING"
    else:
        lead.status = LeadV12Status.READY_DISPATCH.value
        lead.review_status = "APPROVED"
    lead.pending_reason = None


def override_duplicate(
    db: Session,
    *,
    lead: Lead,
    event_id: str | None,
    reason: str,
    approved_by: str,
) -> DedupOverride:
    clean_reason = reason.strip()
    if len(clean_reason) < 5:
        raise ValueError("去重覆盖原因至少 5 个字符")
    locked_lead = db.scalar(
        select(Lead)
        .where(Lead.id == lead.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_lead is None:
        raise AppError("LEAD_NOT_FOUND", "客资不存在", 404)
    lead = locked_lead
    latest_event = db.scalar(
        select(LeadDedupEvent)
        .where(LeadDedupEvent.lead_id == lead.id)
        .order_by(LeadDedupEvent.created_at.desc(), LeadDedupEvent.id.desc())
        .limit(1)
    )
    event = db.get(LeadDedupEvent, event_id) if event_id else latest_event
    if event is None or event.lead_id != lead.id:
        raise ValueError("未找到当前客资的去重结论")
    if (
        latest_event is None
        or event.id != latest_event.id
        or event.phone_fingerprint != lead.phone_fingerprint
    ):
        raise AppError(
            "DEDUP_OVERRIDE_STALE",
            "去重结论已因客资更正而变更，请刷新后覆核当前结论",
            409,
        )
    if event.decision not in REVIEWABLE_DUPLICATE_DECISIONS:
        raise ValueError("仅重复或历史疑似结论允许人工覆盖")
    if lead.duplicate_status not in REVIEWABLE_DUPLICATE_DECISIONS:
        raise ValueError("当前客资不存在可覆盖的重复状态")
    override = DedupOverride(
        lead_id=lead.id,
        dedup_event_id=event.id,
        reason=clean_reason,
        approved_by=approved_by,
    )
    db.add(override)
    event.decision = DuplicateDecision.OVERRIDDEN.value
    details: dict[str, Any] = dict(event.details_json or {})
    details["override_reason"] = clean_reason
    details["overridden_by"] = approved_by
    event.details_json = details
    lead.duplicate_status = DuplicateDecision.OVERRIDDEN.value
    current_assignment = (
        db.get(Assignment, lead.current_assignment_id)
        if lead.current_assignment_id
        else None
    )
    post_dispatch_correction = (
        event.checkpoint.startswith("POST_DISPATCH_CORRECTION")
        or current_assignment is not None
    )
    resume_payload = dict(lead.raw_payload or {})
    resume_status = resume_payload.get("correction_resume_status")
    correction_checkpoint = event.checkpoint.startswith(
        ("UNASSIGNED_CORRECTION", "POST_DISPATCH_CORRECTION")
    )
    if correction_checkpoint and resume_status:
        store_lead_correction_issues(lead, [])
        resume_payload = dict(lead.raw_payload or {})
        resume_review_status = resume_payload.pop(
            "correction_resume_review_status",
            lead.review_status,
        )
        resume_pending_reason = resume_payload.pop(
            "correction_resume_pending_reason",
            None,
        )
        resume_payload.pop("correction_resume_status", None)
        lead.raw_payload = resume_payload
        lead.status = resume_status
        lead.review_status = resume_review_status
        lead.pending_reason = resume_pending_reason
        from .lead_supply_v12 import restore_unassigned_correction_workflow

        restore_unassigned_correction_workflow(db, lead)
    elif post_dispatch_correction:
        from .dispatch_v12 import existing_receiver_correction_issues

        issues: list[str] = []
        if current_assignment is not None:
            issues.extend(
                existing_receiver_correction_issues(
                    db,
                    lead=lead,
                    assignment=current_assignment,
                )
            )
        store_lead_correction_issues(
            lead,
            issues,
            require_action=(
                current_assignment is None
                or current_assignment.status != AssignmentStatus.COMPLETED.value
            ),
        )
    elif lead.source_kind == LeadSourceKind.SUPPLIER_H5.value and lead.review_status != "APPROVED":
        lead.pending_reason = None
        lead.status = LeadV12Status.PENDING_REVIEW.value
        lead.review_status = "PENDING"
    else:
        from .dispatch_v12 import route_approved_lead_to_pool

        route_approved_lead_to_pool(db, lead)
    from .lead_supply_v12 import reconcile_supplier_rewards_after_dedup

    reward_changes = reconcile_supplier_rewards_after_dedup(
        db,
        lead=lead,
        dedup_result=DedupResult(decision=DuplicateDecision.OVERRIDDEN),
    )
    setattr(override, "reward_changes", reward_changes)
    db.flush()
    return override
