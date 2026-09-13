from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Column, event
from sqlalchemy.orm import Session, object_session

from .models_v12 import SupplierLeadReward
from .v12_enums import RewardStatus

QUEUE_KEY = "v12_rewards_due_after_rejection"
SETTLING_KEY = "v12_rewards_settling_after_rejection"


# V1.2.0 introduced SupplierLeadReward in models_v12. Extend it in a separate
# module so the historical 0001/0002 Alembic environment remains isolated while
# new application metadata gets a typed, queryable rule snapshot field.
if "rule_snapshot_json" not in SupplierLeadReward.__table__.c:
    SupplierLeadReward.__table__.append_column(
        Column(
            "rule_snapshot_json",
            JSON,
            nullable=False,
            default=dict,
        )
    )
if "rule_snapshot_json" not in SupplierLeadReward.__mapper__.attrs:
    SupplierLeadReward.__mapper__.add_property(
        "rule_snapshot_json",
        SupplierLeadReward.__table__.c.rule_snapshot_json,
    )


def reward_rule_snapshot(reward: SupplierLeadReward) -> dict[str, Any]:
    return dict(reward.rule_snapshot_json or {})


@event.listens_for(SupplierLeadReward.status, "set", active_history=True)
def queue_overdue_reward_after_rejection(
    reward: SupplierLeadReward,
    value: str,
    old_value: str,
    _,
) -> None:
    """Queue a frozen reward for immediate settlement when an appeal is rejected."""

    if old_value != RewardStatus.FROZEN.value or value != RewardStatus.OBSERVING.value:
        return
    if not reward.id:
        return
    session = object_session(reward)
    if session is not None:
        session.info.setdefault(QUEUE_KEY, set()).add(reward.id)


@event.listens_for(Session, "before_commit")
def settle_queued_overdue_rewards(session: Session) -> None:
    reward_ids = set(session.info.pop(QUEUE_KEY, set()))
    if not reward_ids or session.info.get(SETTLING_KEY):
        return
    session.info[SETTLING_KEY] = True
    try:
        from ..services.supplier_reward_v12 import (
            REWARD_SETTLEMENT_BLOCKED_CODES,
            settle_supplier_reward,
        )
        from .errors import AppError

        session.flush()
        now = datetime.now(timezone.utc)
        for reward_id in sorted(reward_ids):
            try:
                settle_supplier_reward(
                    session, reward_id=reward_id, as_of=now, settled_by=None
                )
            except AppError as exc:
                if exc.code not in REWARD_SETTLEMENT_BLOCKED_CODES:
                    raise
    finally:
        session.info.pop(SETTLING_KEY, None)


@event.listens_for(Session, "after_rollback")
def clear_queued_overdue_rewards(session: Session) -> None:
    session.info.pop(QUEUE_KEY, None)
    session.info.pop(SETTLING_KEY, None)
