from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..core import models_v12  # noqa: F401 - registers source and submitter columns on Lead
from ..core.auth import Principal
from ..core.enums import AssignmentStatus, VerificationTaskStatus
from ..core.errors import AppError
from ..core.models import (
    Assignment,
    AssignmentEvent,
    Lead,
    Notification,
    NotificationOutbox,
    ReturnRequest,
    VerificationTask,
)
from ..core.models_v12 import SupplierLeadReward
from ..core.state_machine_v12 import assert_return_transition, assert_reward_transition
from ..core.time import as_utc
from ..core.v12_enums import ReturnV12Status, RewardStatus
from .supplier_reward_v12 import resolve_effective_confirmation, settle_supplier_reward

_ACTIVE_VERIFICATION_STATUSES = (
    VerificationTaskStatus.PENDING.value,
    VerificationTaskStatus.ASSIGNED.value,
    VerificationTaskStatus.IN_PROGRESS.value,
    VerificationTaskStatus.SUBMITTED.value,
)
_ACTIVE_RETURN_STATUSES = (
    ReturnV12Status.DRAFT.value,
    ReturnV12Status.SUBMITTED.value,
    ReturnV12Status.VERIFYING.value,
    ReturnV12Status.REVIEWING.value,
    ReturnV12Status.NEED_MORE_EVIDENCE.value,
)
_UNSETTLED_REWARD_STATUSES = (
    RewardStatus.WAITING_CLAIM.value,
    RewardStatus.OBSERVING.value,
    RewardStatus.FROZEN.value,
)
_ACTIVE_ASSIGNMENT_STATUSES = (
    AssignmentStatus.PENDING_CLAIM.value,
    AssignmentStatus.CLAIMED.value,
    AssignmentStatus.FOLLOWING.value,
    AssignmentStatus.RETURN_PENDING.value,
)
_CANCELLABLE_OUTBOX_STATUSES = (
    "PENDING",
    "FAILED",
    "PROCESSING",
    "DEAD",
    "MANUAL_ACTION_REQUIRED",
)
_BLOCKER_MESSAGES = {"LEAD_ALREADY_DELETED": "该客资已经删除"}


@dataclass(frozen=True)
class LeadDeletionResult:
    lead: Lead
    idempotent: bool


def require_lead_not_deleted(lead: Lead | None) -> Lead:
    if lead is None or lead.deleted_at is not None:
        raise AppError("LEAD_NOT_FOUND", "客资不存在", 404)
    return lead


def _operation_lead(
    db: Session, *, lead_id: str, principal: Principal, lock: bool = False
) -> Lead:
    if not principal.has_any_role("OPERATION"):
        raise AppError("FORBIDDEN", "仅运营人员可删除客资", 403)
    statement = select(Lead).where(Lead.id == lead_id)
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    lead = db.scalar(statement)
    if lead is None:
        raise AppError("LEAD_NOT_FOUND", "客资不存在", 404)
    return lead


def _deletion_preview(db: Session, lead: Lead) -> dict[str, Any]:
    impact = {
        "assignment_history": int(
            db.scalar(select(func.count(Assignment.id)).where(Assignment.lead_id == lead.id)) or 0
        ),
        "active_verification_tasks": int(
            db.scalar(
                select(func.count(VerificationTask.id)).where(
                    VerificationTask.lead_id == lead.id,
                    VerificationTask.status.in_(_ACTIVE_VERIFICATION_STATUSES),
                )
            )
            or 0
        ),
        "active_return_requests": int(
            db.scalar(
                select(func.count(ReturnRequest.id)).where(
                    ReturnRequest.lead_id == lead.id,
                    ReturnRequest.status.in_(_ACTIVE_RETURN_STATUSES),
                )
            )
            or 0
        ),
    }
    blockers: list[str] = []
    if lead.deleted_at is not None:
        blockers.append("LEAD_ALREADY_DELETED")
    return {
        "lead_id": lead.id,
        "customer_name": lead.customer_name,
        "source_kind": lead.source_kind,
        "status": lead.status,
        "deleted": lead.deleted_at is not None,
        "deletable": not blockers,
        "blockers": blockers,
        "blocker_messages": [_BLOCKER_MESSAGES[code] for code in blockers],
        "impact": impact,
    }


def preview_lead_deletion(
    db: Session, *, lead_id: str, principal: Principal
) -> dict[str, Any]:
    return _deletion_preview(db, _operation_lead(db, lead_id=lead_id, principal=principal))


def delete_operation_lead(
    db: Session, *, lead_id: str, principal: Principal, reason: str
) -> LeadDeletionResult:
    normalized_reason = reason.strip()
    if not 2 <= len(normalized_reason) <= 500:
        raise AppError("LEAD_DELETE_REASON_REQUIRED", "删除原因必须为 2 至 500 个字符", 422)

    if not principal.has_any_role("OPERATION"):
        raise AppError("FORBIDDEN", "仅运营人员可删除客资", 403)
    # Claim, return, and reward settlement lock assignment before lead. Follow the
    # same order so deletion cannot deadlock those customer-facing transactions.
    assignments = db.scalars(
        select(Assignment)
        .where(Assignment.lead_id == lead_id)
        .with_for_update()
    ).all()
    assignments_by_id = {item.id: item for item in assignments}
    lead = _operation_lead(db, lead_id=lead_id, principal=principal, lock=True)
    if (
        lead.current_assignment_id is not None
        and lead.current_assignment_id not in assignments_by_id
    ):
        raise AppError(
            "LEAD_STATE_CHANGED_RETRY",
            "客资状态刚刚发生变化，请刷新后重试",
            409,
        )
    if lead.deleted_at is not None:
        return LeadDeletionResult(lead=lead, idempotent=True)
    preview = _deletion_preview(db, lead)
    if preview["blockers"]:
        code = preview["blockers"][0]
        raise AppError(code, _BLOCKER_MESSAGES[code], 409, preview)

    # Keep every record, but close unfinished child workflows in the same transaction.
    # This prevents deleted leads from producing new tasks, notifications or points.
    deleted_at = datetime.now(timezone.utc)
    active_returns = db.scalars(
        select(ReturnRequest)
        .where(
            ReturnRequest.lead_id == lead.id,
            ReturnRequest.status.in_(_ACTIVE_RETURN_STATUSES),
        )
        .with_for_update()
    ).all()
    active_tasks = db.scalars(
        select(VerificationTask)
        .where(
            VerificationTask.lead_id == lead.id,
            VerificationTask.status.in_(_ACTIVE_VERIFICATION_STATUSES),
        )
        .with_for_update()
    ).all()
    unsettled_rewards = db.scalars(
        select(SupplierLeadReward)
        .where(
            SupplierLeadReward.lead_id == lead.id,
            SupplierLeadReward.status.in_(_UNSETTLED_REWARD_STATUSES),
        )
        .with_for_update()
    ).all()
    confirmations = {
        reward.id: resolve_effective_confirmation(
            db,
            assignments_by_id[reward.assignment_id],
            as_of=deleted_at,
        )
        for reward in unsettled_rewards
        if reward.assignment_id in assignments_by_id
    }

    for item in active_returns:
        assert_return_transition(item.status, ReturnV12Status.CANCELLED)
        item.status = ReturnV12Status.CANCELLED.value
        item.reviewed_by = principal.user_id
        item.reviewed_at = deleted_at
        item.review_note = normalized_reason
        item.final_decision_reason = f"客资已删除：{normalized_reason}"

    for task in active_tasks:
        task.status = VerificationTaskStatus.CANCELLED.value
        task.lock_version += 1

    # A cancelled return no longer freezes an already-earned reward. Restore the
    # assignment while settlement evaluates the same deletion timestamp.
    for assignment in assignments:
        if assignment.status == AssignmentStatus.RETURN_PENDING.value:
            assignment.status = AssignmentStatus.FOLLOWING.value

    for reward in unsettled_rewards:
        confirmation = confirmations.get(reward.id)
        if (
            confirmation is not None
            and confirmation.effective_at is not None
            and lead.current_assignment_id == reward.assignment_id
        ):
            if reward.status == RewardStatus.FROZEN.value:
                assert_reward_transition(RewardStatus.FROZEN, RewardStatus.OBSERVING)
                reward.status = RewardStatus.OBSERVING.value
                reward.frozen_at = None
            settle_supplier_reward(
                db,
                reward_id=reward.id,
                as_of=deleted_at,
                settled_by=principal.user_id,
            )
            continue
        assert_reward_transition(reward.status, RewardStatus.CANCELLED)
        reward.status = RewardStatus.CANCELLED.value
        reward.cancelled_at = deleted_at
        reward.exception_reason = f"LEAD_DELETED: {normalized_reason}"

    for assignment in assignments:
        if assignment.status not in _ACTIVE_ASSIGNMENT_STATUSES:
            continue
        assignment.status = AssignmentStatus.RELEASED.value
        assignment.released_at = deleted_at
        assignment.release_reason = "LEAD_DELETED"
        db.add(
            AssignmentEvent(
                assignment_id=assignment.id,
                event_type="V12_ASSIGNMENT_RELEASED_LEAD_DELETED",
                actor_user_id=principal.user_id,
                occurred_at=deleted_at,
                payload={"reason": normalized_reason},
            )
        )
    lead.current_assignment_id = None

    related_ids = {
        lead.id,
        *(item.id for item in assignments),
        *db.scalars(select(ReturnRequest.id).where(ReturnRequest.lead_id == lead.id)).all(),
        *db.scalars(select(VerificationTask.id).where(VerificationTask.lead_id == lead.id)).all(),
        *db.scalars(select(SupplierLeadReward.id).where(SupplierLeadReward.lead_id == lead.id)).all(),
    }
    pending_outboxes = db.scalars(
        select(NotificationOutbox)
        .where(
            NotificationOutbox.aggregate_id.in_(related_ids),
            NotificationOutbox.status.in_(_CANCELLABLE_OUTBOX_STATUSES),
        )
        .with_for_update()
    ).all()
    live_delivery = next(
        (
            item
            for item in pending_outboxes
            if item.status == "PROCESSING"
            and (as_utc(item.next_attempt_at) or deleted_at) > deleted_at
        ),
        None,
    )
    if live_delivery is not None:
        raise AppError(
            "LEAD_NOTIFICATION_DELIVERY_IN_PROGRESS",
            "客资提醒正在发送，请稍后重试删除",
            409,
            {"outbox_id": live_delivery.id},
        )
    notification_ids: set[str] = set()
    notification_links: set[str] = set()
    for outbox in pending_outboxes:
        outbox.status = "CANCELLED"
        outbox.next_attempt_at = None
        outbox.last_error = f"LEAD_DELETED: {normalized_reason}"
        notification_id = (outbox.payload or {}).get("notification_id")
        if notification_id:
            notification_ids.add(str(notification_id))
        deep_link = (outbox.payload or {}).get("deep_link")
        if deep_link:
            notification_links.add(str(deep_link))
    notification_conditions = []
    if notification_ids:
        notification_conditions.append(Notification.id.in_(notification_ids))
    if notification_links:
        notification_conditions.append(Notification.deep_link.in_(notification_links))
    notification_conditions.extend(
        Notification.deep_link.contains(resource_id) for resource_id in related_ids
    )
    if notification_conditions:
        notifications = db.scalars(
            select(Notification)
            .where(or_(*notification_conditions))
            .with_for_update()
        ).all()
        for notification in notifications:
            notification.status = "CANCELLED"
            notification.read_at = notification.read_at or deleted_at

    # Keep source identifiers and all historical records so reimports remain idempotent.
    lead.deleted_at = deleted_at
    lead.deleted_by = principal.user_id
    lead.delete_reason = normalized_reason
    db.flush()
    return LeadDeletionResult(lead=lead, idempotent=False)
