from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, case, func, or_, select, text
from sqlalchemy.orm import Session

from ..core.enums import PointsLedgerType
from ..core.errors import AppError
from ..core.models import (
    Assignment,
    AssignmentEvent,
    Company,
    FollowUp,
    Lead,
    PointsAccount,
    PointsLedger,
    ReturnRequest,
)
from ..core.models_v12 import SupplierLeadReward
from ..core.state_machine_v12 import assert_reward_transition
from ..core.time import as_utc
from ..core.v12_enums import ReturnV12Status, RewardStatus
from .lead_correction_guard import CORRECTION_REVIEW_REASON
from .points_service import change_points
from .return_clock import appeal_deadline

logger = logging.getLogger(__name__)

VALID_REVERSAL_REASONS = {"FRAUD", "SYSTEM_ERROR", "ADMIN_ERROR"}
ACTIVE_APPEAL_STATUSES = {
    ReturnV12Status.SUBMITTED.value,
    ReturnV12Status.VERIFYING.value,
    ReturnV12Status.REVIEWING.value,
    ReturnV12Status.NEED_MORE_EVIDENCE.value,
    ReturnV12Status.APPROVED.value,
}
SETTLEABLE_REWARD_STATUSES = {
    RewardStatus.WAITING_CLAIM.value,
    RewardStatus.OBSERVING.value,
}
SETTLEABLE_ASSIGNMENT_STATUSES = {"CLAIMED", "FOLLOWING", "COMPLETED"}
REWARD_SETTLEMENT_BLOCKED_CODES = {
    "REWARD_NOT_DUE",
    "REWARD_NOT_SETTLEABLE",
    "REWARD_ASSIGNMENT_INVALID",
    "REWARD_ASSIGNMENT_INACTIVE",
    "REWARD_CORRECTION_PENDING",
    "REWARD_SETTLEMENT_BUSY",
    "REWARD_LEAD_DELETED",
    "POINTS_SPLIT_RECONCILIATION_REQUIRED",
}


@dataclass(frozen=True, slots=True)
class RewardSettlementResult:
    reward: SupplierLeadReward
    ledger: PointsLedger | None
    idempotent: bool = False
    frozen: bool = False


@dataclass(frozen=True, slots=True)
class RewardReversalResult:
    reward: SupplierLeadReward
    ledger: PointsLedger
    idempotent: bool = False


@dataclass(frozen=True, slots=True)
class EffectiveConfirmation:
    effective_at: datetime | None
    policy: str | None
    deadline_at: datetime | None


@dataclass(frozen=True, slots=True)
class ConfirmationSqlExpressions:
    effective_at: Any
    policy: Any
    manual_confirmed_at: Any
    formal_return_submitted_at: Any
    rejected_return_at: Any
    deadline_at: Any


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return as_utc(current) or current


def get_reward(
    db: Session, reward_id: str, *, lock: bool = False
) -> SupplierLeadReward:
    stmt = select(SupplierLeadReward).where(SupplierLeadReward.id == reward_id)
    if lock:
        stmt = stmt.with_for_update()
        stmt = stmt.execution_options(populate_existing=True)
    reward = db.scalar(stmt)
    if reward is None:
        raise AppError("SUPPLIER_REWARD_NOT_FOUND", "供应商奖励不存在", 404)
    return reward


def _active_appeal_exists(db: Session, assignment_id: str) -> bool:
    return (
        db.scalar(
            select(ReturnRequest.id)
            .where(
                ReturnRequest.assignment_id == assignment_id,
                ReturnRequest.submitted_at.is_not(None),
                ReturnRequest.status.in_(ACTIVE_APPEAL_STATUSES),
            )
            .limit(1)
        )
        is not None
    )


def activate_supplier_reward_after_effective_confirmation(
    db: Session,
    *,
    assignment_id: str,
    confirmed_at: datetime | None = None,
) -> SupplierLeadReward | None:
    """A recorded manual completion credits the supplier immediately."""

    db.flush()
    reward = db.scalar(
        select(SupplierLeadReward).where(
            SupplierLeadReward.assignment_id == assignment_id
        )
    )
    if reward is None or reward.status not in SETTLEABLE_REWARD_STATUSES:
        return reward
    if _manual_confirmation_at(db, assignment_id) is not None:
        settle_supplier_reward(db, reward_id=reward.id, as_of=confirmed_at)
    return reward


def _manual_confirmation_at(db: Session, assignment_id: str) -> datetime | None:
    return as_utc(
        db.scalar(
            select(func.min(FollowUp.created_at)).where(
                FollowUp.assignment_id == assignment_id,
                FollowUp.status == "DEAL",
            )
        )
    )


def resolve_effective_confirmation(
    db: Session,
    assignment: Assignment,
    *,
    as_of: datetime,
) -> EffectiveConfirmation:
    """Return the earliest time at which this assignment legally became effective."""

    now = _now(as_of)
    claimed_at = as_utc(assignment.claimed_at)
    if claimed_at is None:
        return EffectiveConfirmation(None, None, None)
    deadline_at = appeal_deadline(assignment)
    manual_at = _manual_confirmation_at(db, assignment.id)
    return_request = db.scalar(
        select(ReturnRequest)
        .where(
            ReturnRequest.assignment_id == assignment.id,
            ReturnRequest.submitted_at.is_not(None),
        )
        .order_by(ReturnRequest.submitted_at.asc(), ReturnRequest.id.asc())
        .limit(1)
    )
    submitted_at = as_utc(return_request.submitted_at) if return_request else None
    if (
        manual_at is not None
        and (deadline_at is None or manual_at <= deadline_at)
        and (submitted_at is None or manual_at <= submitted_at
             or (assignment.appeal_resumed_at is not None
                 and return_request.status == ReturnV12Status.REJECTED.value
                 and as_utc(return_request.reviewed_at) <= manual_at))
        and manual_at <= now
    ):
        return EffectiveConfirmation(manual_at, "MANUAL_CONFIRMED", deadline_at)
    if (
        deadline_at is not None
        and deadline_at <= now
        and assignment.appeal_paused_at is None
        and (submitted_at is None or submitted_at > deadline_at
             or (assignment.appeal_resumed_at is not None
                 and return_request.status == ReturnV12Status.REJECTED.value))
    ):
        return EffectiveConfirmation(deadline_at, "CLAIM_48H", deadline_at)
    rejected_at = (
        as_utc(return_request.reviewed_at)
        if return_request
        and return_request.status == ReturnV12Status.REJECTED.value
        else None
    )
    if rejected_at is not None and rejected_at <= now and assignment.appeal_resumed_at is None:
        return EffectiveConfirmation(rejected_at, "RETURN_REJECTED", deadline_at)
    return EffectiveConfirmation(None, None, deadline_at)


def confirmation_sql_expressions(*, dialect_name: str) -> ConfirmationSqlExpressions:
    """Build the report form of ``resolve_effective_confirmation``."""

    manual_at = (
        select(func.min(FollowUp.created_at))
        .where(FollowUp.assignment_id == Assignment.id, FollowUp.status == "DEAL")
        .correlate(Assignment)
        .scalar_subquery()
    )
    submitted_at = (
        select(func.min(ReturnRequest.submitted_at))
        .where(
            ReturnRequest.assignment_id == Assignment.id,
            ReturnRequest.submitted_at.is_not(None),
        )
        .correlate(Assignment)
        .scalar_subquery()
    )
    rejected_at = (
        select(func.min(ReturnRequest.reviewed_at))
        .where(
            ReturnRequest.assignment_id == Assignment.id,
            ReturnRequest.status == ReturnV12Status.REJECTED.value,
        )
        .correlate(Assignment)
        .scalar_subquery()
    )
    original_deadline_at = (
        func.datetime(
            Assignment.claimed_at,
            "+48 hours",
            type_=Assignment.claimed_at.type,
        )
        if dialect_name == "sqlite"
        else Assignment.claimed_at + text("INTERVAL '48 hours'")
    )
    deadline_at = case(
        (Assignment.appeal_resumed_at.is_not(None), Assignment.appeal_deadline_at),
        else_=original_deadline_at,
    )
    manual_is_first = and_(
        manual_at.is_not(None),
        or_(deadline_at.is_(None), manual_at <= deadline_at),
        or_(submitted_at.is_(None), manual_at <= submitted_at,
            and_(Assignment.appeal_resumed_at.is_not(None), rejected_at <= manual_at)),
    )
    deadline_is_effective = and_(
        deadline_at.is_not(None),
        Assignment.appeal_paused_at.is_(None),
        or_(submitted_at.is_(None), submitted_at > deadline_at,
            and_(Assignment.appeal_resumed_at.is_not(None), rejected_at.is_not(None))),
    )
    legacy_rejection = and_(rejected_at.is_not(None), Assignment.appeal_resumed_at.is_(None))
    effective_at = case(
        (manual_is_first, manual_at),
        (deadline_is_effective, deadline_at),
        (legacy_rejection, rejected_at),
        else_=None,
    )
    policy = case(
        (manual_is_first, "MANUAL_CONFIRMED"),
        (deadline_is_effective, "CLAIM_48H"),
        (legacy_rejection, "RETURN_REJECTED"),
        else_=None,
    )
    return ConfirmationSqlExpressions(
        effective_at=effective_at,
        policy=policy,
        manual_confirmed_at=manual_at,
        formal_return_submitted_at=submitted_at,
        rejected_return_at=rejected_at,
        deadline_at=deadline_at,
    )


def _lock_reward_context(
    db: Session, reward_id: str, *, skip_locked: bool = False
) -> tuple[Assignment, Lead, SupplierLeadReward]:
    # Match return submission and manual confirmation: assignment -> lead -> reward.
    # Flush first because production sessions disable autoflush.
    db.flush()
    reference = get_reward(db, reward_id)
    assignment = db.scalar(
        select(Assignment)
        .where(Assignment.id == reference.assignment_id)
        .with_for_update(skip_locked=skip_locked)
        .execution_options(populate_existing=True)
    )
    if assignment is None:
        if skip_locked and db.get(Assignment, reference.assignment_id) is not None:
            raise AppError(
                "REWARD_SETTLEMENT_BUSY", "派发单正在处理中，下轮重试结算", 409
            )
        raise AppError("REWARD_ASSIGNMENT_INVALID", "奖励关联派发单不存在", 409)
    lead = db.scalar(
        select(Lead)
        .where(Lead.id == assignment.lead_id)
        .with_for_update(skip_locked=skip_locked)
        .execution_options(populate_existing=True)
    )
    if lead is None:
        if skip_locked and db.get(Lead, assignment.lead_id) is not None:
            raise AppError(
                "REWARD_SETTLEMENT_BUSY", "客资正在处理中，下轮重试结算", 409
            )
        raise AppError("REWARD_ASSIGNMENT_INVALID", "奖励关联客资不存在", 409)
    if lead.deleted_at is not None:
        raise AppError("REWARD_LEAD_DELETED", "客资已删除，奖励不能继续结算", 409)
    reward = db.scalar(
        select(SupplierLeadReward)
        .where(SupplierLeadReward.id == reward_id)
        .with_for_update(skip_locked=skip_locked)
        .execution_options(populate_existing=True)
    )
    if reward is None:
        raise AppError("REWARD_SETTLEMENT_BUSY", "奖励正在处理中，下轮重试结算", 409)
    return assignment, lead, reward


def _lock_settlement_account(
    db: Session, company_id: str, *, skip_locked: bool
) -> None:
    # A batch retains earlier account locks until commit. Never wait on a later
    # account in that batch: a return may be locking the same accounts in order.
    statement = select(PointsAccount).where(PointsAccount.company_id == company_id)
    account = db.scalar(
        statement.with_for_update(skip_locked=skip_locked).execution_options(
            populate_existing=True
        )
    )
    if account is not None:
        return
    if db.scalar(
        select(PointsAccount.id).where(PointsAccount.company_id == company_id)
    ):
        raise AppError(
            "REWARD_SETTLEMENT_BUSY", "积分账户正在处理中，下轮重试结算", 409
        )
    # The first reward may create the supplier's account. Serialize that missing
    # row through its company so two assignments cannot both create an account.
    company = db.scalar(
        select(Company)
        .where(Company.id == company_id)
        .with_for_update(skip_locked=skip_locked)
    )
    if company is None:
        raise AppError(
            "REWARD_SETTLEMENT_BUSY", "加盟商账户正在初始化，下轮重试结算", 409
        )
    account = db.scalar(
        statement.with_for_update(skip_locked=skip_locked).execution_options(
            populate_existing=True
        )
    )
    if account is None and db.scalar(statement.with_only_columns(PointsAccount.id)):
        raise AppError(
            "REWARD_SETTLEMENT_BUSY", "积分账户正在处理中，下轮重试结算", 409
        )


def _record_automatic_confirmation(
    db: Session,
    assignment: Assignment,
    *,
    effective_at: datetime,
    policy: str,
    now: datetime,
) -> None:
    # An automatic validity decision is not a telephone call or a sales follow-up.
    existing = db.scalar(
        select(AssignmentEvent.id)
        .where(
            AssignmentEvent.assignment_id == assignment.id,
            AssignmentEvent.event_type == "V12_ASSIGNMENT_AUTO_CONFIRMED",
        )
        .limit(1)
    )
    if existing:
        return
    db.add(
        AssignmentEvent(
            assignment_id=assignment.id,
            event_type="V12_ASSIGNMENT_AUTO_CONFIRMED",
            actor_user_id=None,
            occurred_at=effective_at,
            payload={
                "effective_at": effective_at.isoformat(),
                "processed_at": now.isoformat(),
                "claimed_at": as_utc(assignment.claimed_at).isoformat(),
                "reason": policy,
            },
        )
    )


def settle_supplier_reward(
    db: Session,
    *,
    reward_id: str,
    as_of: datetime | None = None,
    settled_by: str | None = None,
    require_due: bool = True,
    skip_locked: bool = False,
) -> RewardSettlementResult:
    assignment, lead, reward = _lock_reward_context(
        db, reward_id, skip_locked=skip_locked
    )
    existing_ledger = (
        db.get(PointsLedger, reward.ledger_id) if reward.ledger_id else None
    )
    if reward.status == RewardStatus.SETTLED.value:
        if existing_ledger is None:
            raise AppError("REWARD_LEDGER_MISSING", "奖励已结算但积分流水缺失", 500)
        return RewardSettlementResult(
            reward=reward, ledger=existing_ledger, idempotent=True
        )
    if reward.status == RewardStatus.FROZEN.value:
        return RewardSettlementResult(reward=reward, ledger=None, frozen=True)
    if reward.status not in SETTLEABLE_REWARD_STATUSES:
        raise AppError(
            "REWARD_NOT_SETTLEABLE",
            "奖励当前不可结算",
            409,
            {"status": reward.status},
        )

    now = _now(as_of)
    claimed_at = as_utc(assignment.claimed_at)
    if claimed_at is None:
        raise AppError("REWARD_ASSIGNMENT_INVALID", "奖励缺少有效领取时间", 409)
    confirmation = resolve_effective_confirmation(db, assignment, as_of=now)
    active_appeal = _active_appeal_exists(db, reward.assignment_id)
    due_at = confirmation.effective_at
    if due_at is None and active_appeal:
        deadline_at = confirmation.deadline_at
        if deadline_at is not None and deadline_at <= now:
            due_at = deadline_at
    if due_at is None:
        raise AppError(
            "REWARD_NOT_DUE",
            "奖励尚未到结算时间",
            409,
            {
                "reward_due_at": confirmation.deadline_at.isoformat()
                if confirmation.deadline_at
                else None
            },
        )
    if (
        reward.lead_id != lead.id
        or reward.assignment_id != assignment.id
        or reward.receiver_company_id != assignment.company_id
        or assignment.receiver_company_id != reward.receiver_company_id
        or reward.supplier_company_id != lead.supplier_company_id
        or reward.supplier_company_id != assignment.supplier_company_id
        or reward.supplier_company_id == reward.receiver_company_id
    ):
        raise AppError("REWARD_ASSIGNMENT_INVALID", "奖励与客资或接收方不一致", 409)
    if (
        lead.current_assignment_id != assignment.id
        or assignment.status not in SETTLEABLE_ASSIGNMENT_STATUSES | {"RETURN_PENDING"}
    ):
        raise AppError(
            "REWARD_ASSIGNMENT_INACTIVE", "奖励对应的派发已解除或不是当前派发", 409
        )
    if lead.pending_reason == CORRECTION_REVIEW_REASON:
        raise AppError(
            "REWARD_CORRECTION_PENDING", "客资更正尚待运营处理，暂不结算", 409
        )
    if reward.status == RewardStatus.WAITING_CLAIM.value:
        assert_reward_transition(RewardStatus.WAITING_CLAIM, RewardStatus.OBSERVING)
        reward.status = RewardStatus.OBSERVING.value
    reward.observed_at = reward.observed_at or claimed_at
    reward.appeal_deadline_at = confirmation.deadline_at
    reward.reward_due_at = due_at
    if assignment.status == "RETURN_PENDING" or active_appeal:
        assert_reward_transition(RewardStatus.OBSERVING, RewardStatus.FROZEN)
        reward.status = RewardStatus.FROZEN.value
        reward.frozen_at = reward.frozen_at or now
        db.flush()
        from .notification_v12 import _notify_reward_state

        _notify_reward_state(db, reward)
        return RewardSettlementResult(reward=reward, ledger=None, frozen=True)
    if int(reward.reward_points) <= 0:
        raise AppError("REWARD_POINTS_INVALID", "奖励积分必须大于 0 才能结算", 409)

    _lock_settlement_account(db, reward.supplier_company_id, skip_locked=skip_locked)
    if confirmation.policy != "MANUAL_CONFIRMED":
        _record_automatic_confirmation(
            db,
            assignment,
            effective_at=due_at,
            policy=(
                "RETURN_REJECTED"
                if confirmation.policy == "RETURN_REJECTED"
                else "NO_RETURN_WITHIN_48H"
            ),
            now=now,
        )
    db.flush()
    ledger = change_points(
        db,
        company_id=reward.supplier_company_id,
        delta=int(reward.reward_points),
        ledger_type=PointsLedgerType.REWARD.value,
        business_type="V12_SUPPLIER_REWARD",
        business_id=reward.id,
        idempotency_key=f"v12-reward:{reward.id}:settle",
        created_by=settled_by,
        point_kind="SUPPLY",
        metadata={
            "assignment_id": reward.assignment_id,
            "lead_id": reward.lead_id,
            "receiver_company_id": reward.receiver_company_id,
            "claim_points": int(reward.claim_points),
            "reward_ratio_bps": int(reward.reward_ratio_bps),
            "reward_points": int(reward.reward_points),
            "rule_version": int(reward.rule_version),
            "rule_snapshot": dict(
                getattr(reward, "rule_snapshot_json", None) or {}
            ),
            "settlement_policy": confirmation.policy,
            "claimed_at": claimed_at.isoformat(),
            "eligible_at": due_at.isoformat(),
        },
    )
    assert_reward_transition(RewardStatus.OBSERVING, RewardStatus.SETTLED)
    reward.status = RewardStatus.SETTLED.value
    reward.settled_at = now
    reward.ledger_id = ledger.id
    reward.exception_reason = None
    db.flush()
    # The return-review commit hook also uses this entrypoint, so notifications
    # must be transactional here rather than only in the hourly job wrapper.
    from .notification_v12 import _notify_reward_state

    _notify_reward_state(db, reward)
    return RewardSettlementResult(reward=reward, ledger=ledger)


def _select_due_reward_ids(
    db: Session,
    *,
    as_of: datetime,
    limit: int,
    exclude_reward_ids: set[str] | None = None,
) -> list[str]:
    filters = [
        SupplierLeadReward.status.in_(SETTLEABLE_REWARD_STATUSES),
        Assignment.claimed_at.is_not(None),
        or_(
            and_(
                Assignment.appeal_paused_at.is_(None),
                or_(
                    and_(Assignment.appeal_resumed_at.is_(None),
                         Assignment.claimed_at <= as_of - timedelta(hours=48)),
                    and_(Assignment.appeal_resumed_at.is_not(None),
                         Assignment.appeal_deadline_at <= as_of),
                ),
            ),
            select(FollowUp.id)
            .where(
                FollowUp.assignment_id == Assignment.id,
                FollowUp.status == "DEAL",
                FollowUp.created_at <= as_of,
            )
            .exists(),
        ),
        Assignment.status.in_(SETTLEABLE_ASSIGNMENT_STATUSES | {"RETURN_PENDING"}),
    ]
    if exclude_reward_ids:
        filters.append(SupplierLeadReward.id.not_in(exclude_reward_ids))
    return list(
        db.scalars(
            select(SupplierLeadReward.id)
            .join(Assignment, Assignment.id == SupplierLeadReward.assignment_id)
            .join(Lead, Lead.id == Assignment.lead_id)
            .where(*filters, Lead.deleted_at.is_(None))
            .order_by(Assignment.claimed_at.asc(), SupplierLeadReward.id.asc())
            .limit(max(1, min(int(limit), 1000)))
        ).all()
    )


def run_due_supplier_reward_settlement(
    db: Session,
    *,
    as_of: datetime | None = None,
    limit: int = 100,
    settled_by: str | None = None,
    exclude_reward_ids: set[str] | None = None,
) -> dict[str, Any]:
    now = _now(as_of)
    reward_ids = _select_due_reward_ids(
        db,
        as_of=now,
        limit=limit,
        exclude_reward_ids=exclude_reward_ids,
    )
    settled = 0
    idempotent = 0
    frozen = 0
    skipped = 0
    failed = 0
    errors: list[dict[str, str]] = []
    for reward_id in reward_ids:
        try:
            with db.begin_nested():
                result = settle_supplier_reward(
                    db,
                    reward_id=reward_id,
                    as_of=now,
                    settled_by=settled_by,
                    skip_locked=True,
                )
                if result.frozen:
                    frozen += 1
                elif result.idempotent:
                    idempotent += 1
                else:
                    settled += 1
        except AppError as exc:
            if exc.code in REWARD_SETTLEMENT_BLOCKED_CODES:
                skipped += 1
            else:
                failed += 1
                errors.append(
                    {"reward_id": reward_id, "code": exc.code, "message": exc.message}
                )
            logger.warning(
                "supplier reward settlement blocked reward_id=%s code=%s",
                reward_id,
                exc.code,
            )
        except Exception as exc:  # pragma: no cover - defensive scheduler boundary
            logger.exception(
                "supplier reward settlement failed reward_id=%s", reward_id
            )
            failed += 1
            errors.append(
                {"reward_id": reward_id, "code": "UNEXPECTED", "message": str(exc)}
            )
    return {
        "scanned": len(reward_ids),
        "settled": settled,
        "idempotent": idempotent,
        "frozen": frozen,
        "skipped": skipped,
        "failed": failed,
        "errors": errors[:20],
        "processed_reward_ids": reward_ids,
        "as_of": now.isoformat(),
    }


def drain_due_supplier_reward_settlement(
    db: Session,
    *,
    as_of: datetime | None = None,
    batch_size: int = 500,
    max_batches: int = 20,
    settled_by: str | None = None,
) -> dict[str, Any]:
    """Drain due rewards without letting a bad oldest row block valid rows.

    Each reward is attempted at most once per drain cycle. Failed rows remain
    pending and are retried by the next scheduler cycle; later valid rows can
    still settle now. The safety bound caps one cycle at 20,000 rows because
    batch_size itself is capped at 1,000.
    """

    now = _now(as_of)
    safe_batch_size = max(1, min(int(batch_size), 1000))
    safe_max_batches = max(1, min(int(max_batches), 100))
    attempted: set[str] = set()
    totals: dict[str, Any] = {
        "batches": 0,
        "scanned": 0,
        "settled": 0,
        "idempotent": 0,
        "frozen": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
        "as_of": now.isoformat(),
    }
    for _ in range(safe_max_batches):
        result = run_due_supplier_reward_settlement(
            db,
            as_of=now,
            limit=safe_batch_size,
            settled_by=settled_by,
            exclude_reward_ids=attempted,
        )
        reward_ids = set(result.pop("processed_reward_ids", []))
        if not reward_ids:
            break
        attempted.update(reward_ids)
        totals["batches"] += 1
        for key in ("scanned", "settled", "idempotent", "frozen", "skipped", "failed"):
            totals[key] += int(result[key])
        totals["errors"].extend(result.get("errors", []))
        if int(result["scanned"]) < safe_batch_size:
            break

    remaining_due = len(
        _select_due_reward_ids(
            db,
            as_of=now,
            limit=1,
            exclude_reward_ids=attempted,
        )
    )
    totals["errors"] = totals["errors"][:50]
    totals["attempted_unique"] = len(attempted)
    totals["safety_limit_reached"] = bool(remaining_due)
    return totals


def reverse_supplier_reward(
    db: Session,
    *,
    reward_id: str,
    reason_code: str,
    note: str,
    reversed_by: str,
    as_of: datetime | None = None,
    return_request_id: str | None = None,
) -> RewardReversalResult:
    reward = get_reward(db, reward_id, lock=True)
    normalized_reason = reason_code.strip().upper()
    if normalized_reason == "RETURN_APPROVED":
        # This reason is internal; the manual reversal API keeps its original
        # exceptional reasons. The return and refund must be in this transaction.
        request = (
            db.get(ReturnRequest, return_request_id) if return_request_id else None
        )
        if (
            request is None
            or request.status != ReturnV12Status.APPROVED.value
            or request.submitted_at is None
            or request.assignment_id != reward.assignment_id
            or request.lead_id != reward.lead_id
            or request.company_id != reward.receiver_company_id
        ):
            raise AppError(
                "REWARD_RETURN_NOT_APPROVED", "奖励冲回需要对应的正式退回终审通过", 409
            )
    elif normalized_reason not in VALID_REVERSAL_REASONS:
        raise AppError("REWARD_REVERSAL_REASON_INVALID", "奖励冲正原因无效", 422)
    if reward.status == RewardStatus.REVERSED.value:
        ledger = (
            db.get(PointsLedger, reward.reversal_ledger_id)
            if reward.reversal_ledger_id
            else None
        )
        if ledger is None:
            raise AppError(
                "REWARD_REVERSAL_LEDGER_MISSING", "奖励已冲正但冲正流水缺失", 500
            )
        return RewardReversalResult(reward=reward, ledger=ledger, idempotent=True)
    if reward.status != RewardStatus.SETTLED.value:
        raise AppError(
            "REWARD_NOT_REVERSIBLE",
            "仅已结算奖励允许异常冲正",
            409,
            {"status": reward.status},
        )
    original = db.get(PointsLedger, reward.ledger_id) if reward.ledger_id else None
    if (
        original is None
        or original.business_type != "V12_SUPPLIER_REWARD"
        or original.business_id != reward.id
        or original.company_id != reward.supplier_company_id
        or int(original.delta) <= 0
    ):
        raise AppError("REWARD_LEDGER_MISSING", "未找到原奖励结算流水", 409)

    from .supply_termination import prepare_supplier_reward_reversal

    prepare_supplier_reward_reversal(
        db,
        company_id=reward.supplier_company_id,
        reward_id=reward.id,
    )

    ledger = change_points(
        db,
        company_id=reward.supplier_company_id,
        delta=-abs(int(original.delta)),
        ledger_type=PointsLedgerType.REVERSAL.value,
        business_type="V12_SUPPLIER_REWARD_REVERSAL",
        business_id=reward.id,
        idempotency_key=f"v12-reward:{reward.id}:reverse",
        related_ledger_id=original.id,
        created_by=reversed_by,
        point_kind="SUPPLY",
        allow_negative=True,
        metadata={
            "reason_code": normalized_reason,
            "note": note.strip(),
            "original_ledger_id": original.id,
            "assignment_id": reward.assignment_id,
            "lead_id": reward.lead_id,
            **({"return_request_id": return_request_id} if return_request_id else {}),
        },
    )
    assert_reward_transition(RewardStatus.SETTLED, RewardStatus.REVERSED)
    reward.status = RewardStatus.REVERSED.value
    reward.reversed_at = _now(as_of)
    reward.reversal_ledger_id = ledger.id
    reward.exception_reason = f"{normalized_reason}: {note.strip()}"
    db.flush()
    return RewardReversalResult(reward=reward, ledger=ledger)


def reward_to_dict(reward: SupplierLeadReward) -> dict[str, Any]:
    return {
        "id": reward.id,
        "lead_id": reward.lead_id,
        "assignment_id": reward.assignment_id,
        "supplier_company_id": reward.supplier_company_id,
        "receiver_company_id": reward.receiver_company_id,
        "status": reward.status,
        "claim_points": int(reward.claim_points),
        "reward_ratio_bps": int(reward.reward_ratio_bps),
        "reward_points": int(reward.reward_points),
        "rule_version": int(reward.rule_version),
        "rule_snapshot": dict(getattr(reward, "rule_snapshot_json", None) or {}),
        "observed_at": reward.observed_at.isoformat() if reward.observed_at else None,
        "appeal_deadline_at": reward.appeal_deadline_at.isoformat()
        if reward.appeal_deadline_at
        else None,
        "reward_due_at": reward.reward_due_at.isoformat()
        if reward.reward_due_at
        else None,
        "frozen_at": reward.frozen_at.isoformat() if reward.frozen_at else None,
        "settled_at": reward.settled_at.isoformat() if reward.settled_at else None,
        "cancelled_at": reward.cancelled_at.isoformat()
        if reward.cancelled_at
        else None,
        "reversed_at": reward.reversed_at.isoformat() if reward.reversed_at else None,
        "ledger_id": reward.ledger_id,
        "reversal_ledger_id": reward.reversal_ledger_id,
        "exception_reason": reward.exception_reason,
        "created_at": reward.created_at.isoformat(),
        "updated_at": reward.updated_at.isoformat(),
    }


def supplier_reward_summary(db: Session, supplier_company_id: str) -> dict[str, int]:
    rows = db.execute(
        select(
            SupplierLeadReward.status,
            func.count(SupplierLeadReward.id),
            func.coalesce(func.sum(SupplierLeadReward.reward_points), 0),
        )
        .where(SupplierLeadReward.supplier_company_id == supplier_company_id)
        .group_by(SupplierLeadReward.status)
    ).all()
    result: dict[str, int] = {
        "total_count": 0,
        "settled_points": 0,
        "observing_points": 0,
        "frozen_points": 0,
    }
    for status, count, points in rows:
        result["total_count"] += int(count)
        if status == RewardStatus.SETTLED.value:
            result["settled_points"] += int(points)
        elif status == RewardStatus.OBSERVING.value:
            result["observing_points"] += int(points)
        elif status == RewardStatus.FROZEN.value:
            result["frozen_points"] += int(points)
    return result
