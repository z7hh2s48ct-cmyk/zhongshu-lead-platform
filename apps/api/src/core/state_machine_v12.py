from __future__ import annotations

from collections.abc import Mapping, Set

from .v12_enums import LeadV12Status, ReturnV12Status, RewardStatus


class InvalidStateTransition(ValueError):
    def __init__(self, domain: str, current: str, target: str) -> None:
        self.domain = domain
        self.current = current
        self.target = target
        super().__init__(f"invalid {domain} transition: {current} -> {target}")


class UnknownLegacyStatus(ValueError):
    def __init__(self, domain: str, value: str) -> None:
        self.domain = domain
        self.value = value
        super().__init__(f"unknown legacy {domain} status: {value}")


LEAD_TRANSITIONS: Mapping[LeadV12Status, Set[LeadV12Status]] = {
    LeadV12Status.DRAFT: {
        LeadV12Status.PUBLIC_POOL,
        LeadV12Status.PENDING_REVIEW,
        LeadV12Status.PENDING_TELESALES_VERIFY,
        LeadV12Status.READY_DISPATCH,
        LeadV12Status.CLOSED,
    },
    LeadV12Status.PENDING_REVIEW: {
        LeadV12Status.PUBLIC_POOL,
        LeadV12Status.PENDING_TELESALES_VERIFY,
        LeadV12Status.READY_DISPATCH,
        LeadV12Status.INVALID,
        LeadV12Status.DUPLICATE,
        LeadV12Status.CLOSED,
    },
    LeadV12Status.PENDING_TELESALES_VERIFY: {
        LeadV12Status.PENDING_OPERATION_DISPOSITION,
        LeadV12Status.PENDING_REVIEW,
        # 2026-09-23 口径：缺县不再硬性打回电销，电销/解卡路径可直接入池。
        LeadV12Status.READY_DISPATCH,
        LeadV12Status.PUBLIC_POOL,
        LeadV12Status.CLOSED,
    },
    LeadV12Status.PENDING_OPERATION_DISPOSITION: {
        LeadV12Status.PUBLIC_POOL,
        LeadV12Status.READY_DISPATCH,
        LeadV12Status.PENDING_TELESALES_VERIFY,
        LeadV12Status.DRAFT,
        LeadV12Status.INVALID,
        LeadV12Status.DUPLICATE,
        LeadV12Status.CLOSED,
    },
    LeadV12Status.READY_DISPATCH: {
        LeadV12Status.DISPATCHED,
        # 2026-09-23 口径：运营可把待派发客资显式转回电销核验（推荐动作）。
        LeadV12Status.PENDING_TELESALES_VERIFY,
        LeadV12Status.DUPLICATE,
        LeadV12Status.CLOSED,
    },
    LeadV12Status.PUBLIC_POOL: {
        LeadV12Status.READY_DISPATCH,
        # 2026-09-23 口径：公海池客资可由运营显式转电销核验补县。
        LeadV12Status.PENDING_TELESALES_VERIFY,
        LeadV12Status.DUPLICATE,
        LeadV12Status.CLOSED,
    },
    LeadV12Status.DISPATCHED: {LeadV12Status.CLAIMED, LeadV12Status.READY_DISPATCH, LeadV12Status.CLOSED},
    LeadV12Status.CLAIMED: {LeadV12Status.FOLLOWING, LeadV12Status.READY_DISPATCH, LeadV12Status.CLOSED},
    LeadV12Status.FOLLOWING: {LeadV12Status.COMPLETED, LeadV12Status.READY_DISPATCH, LeadV12Status.CLOSED},
    LeadV12Status.COMPLETED: {LeadV12Status.CLOSED},
    LeadV12Status.INVALID: {
        LeadV12Status.CLOSED,
        LeadV12Status.PENDING_TELESALES_VERIFY,
    },
    LeadV12Status.DUPLICATE: {LeadV12Status.READY_DISPATCH, LeadV12Status.CLOSED},
    LeadV12Status.CLOSED: {
        LeadV12Status.READY_DISPATCH,
        # 退回客资修改实际区域后，若改后区域无可承接加盟商，直接转入公海池
        # 而不是回到派发池（2026-09-19 S13 确认口径）。
        LeadV12Status.PUBLIC_POOL,
        LeadV12Status.PENDING_TELESALES_VERIFY,
    },
}

RETURN_TRANSITIONS: Mapping[ReturnV12Status, Set[ReturnV12Status]] = {
    ReturnV12Status.DRAFT: {
        ReturnV12Status.SUBMITTED,
        ReturnV12Status.EXPIRED,
        ReturnV12Status.CANCELLED,
    },
    ReturnV12Status.SUBMITTED: {
        ReturnV12Status.VERIFYING,
        ReturnV12Status.NEED_MORE_EVIDENCE,
        ReturnV12Status.EXPIRED,
        ReturnV12Status.CANCELLED,
    },
    ReturnV12Status.VERIFYING: {
        ReturnV12Status.REVIEWING,
        ReturnV12Status.NEED_MORE_EVIDENCE,
        ReturnV12Status.CANCELLED,
    },
    ReturnV12Status.NEED_MORE_EVIDENCE: {
        ReturnV12Status.SUBMITTED,
        ReturnV12Status.EXPIRED,
        ReturnV12Status.CANCELLED,
    },
    ReturnV12Status.REVIEWING: {
        ReturnV12Status.APPROVED,
        ReturnV12Status.REJECTED,
        ReturnV12Status.NEED_MORE_EVIDENCE,
        ReturnV12Status.CANCELLED,
    },
    ReturnV12Status.APPROVED: set(),
    ReturnV12Status.REJECTED: set(),
    ReturnV12Status.EXPIRED: set(),
    ReturnV12Status.CANCELLED: set(),
}

REWARD_TRANSITIONS: Mapping[RewardStatus, Set[RewardStatus]] = {
    RewardStatus.NOT_ELIGIBLE: set(),
    RewardStatus.WAITING_CLAIM: {
        RewardStatus.OBSERVING,
        RewardStatus.FROZEN,
        RewardStatus.CANCELLED,
    },
    RewardStatus.OBSERVING: {
        RewardStatus.FROZEN,
        RewardStatus.SETTLED,
        RewardStatus.CANCELLED,
    },
    RewardStatus.FROZEN: {
        RewardStatus.OBSERVING,
        RewardStatus.SETTLED,
        RewardStatus.CANCELLED,
    },
    RewardStatus.SETTLED: {RewardStatus.REVERSED},
    # Only correction-dedup cancellations may be restored, and the service
    # verifies its own exception marker before using these transitions.
    RewardStatus.CANCELLED: {
        RewardStatus.WAITING_CLAIM,
        RewardStatus.OBSERVING,
        RewardStatus.FROZEN,
    },
    RewardStatus.REVERSED: set(),
}

LEGACY_LEAD_STATUS_MAP: Mapping[str, LeadV12Status] = {
    "IMPORTED": LeadV12Status.DRAFT,
    "IMPORT_ERROR": LeadV12Status.INVALID,
    "DUPLICATE_REVIEW": LeadV12Status.DUPLICATE,
    "VERIFYING": LeadV12Status.PENDING_TELESALES_VERIFY,
    "QUALIFIED": LeadV12Status.READY_DISPATCH,
    "INVALID": LeadV12Status.INVALID,
    "ASSIGNED": LeadV12Status.DISPATCHED,
    "CLAIMED": LeadV12Status.CLAIMED,
    "FOLLOWING": LeadV12Status.FOLLOWING,
    "RETURN_PENDING": LeadV12Status.FOLLOWING,
    "RETURNED": LeadV12Status.READY_DISPATCH,
    "CLOSED": LeadV12Status.CLOSED,
}

LEGACY_RETURN_STATUS_MAP: Mapping[str, ReturnV12Status] = {
    "DRAFT": ReturnV12Status.DRAFT,
    "PENDING": ReturnV12Status.SUBMITTED,
    "NEED_MORE": ReturnV12Status.NEED_MORE_EVIDENCE,
    "APPROVED": ReturnV12Status.APPROVED,
    "REJECTED": ReturnV12Status.REJECTED,
    "CANCELLED": ReturnV12Status.EXPIRED,
}


def _assert_transition(domain: str, transitions: Mapping, current, target) -> None:
    if current == target:
        return
    if target not in transitions[current]:
        raise InvalidStateTransition(domain, str(current), str(target))


def assert_lead_transition(current: LeadV12Status | str, target: LeadV12Status | str) -> None:
    _assert_transition("lead", LEAD_TRANSITIONS, LeadV12Status(current), LeadV12Status(target))


def assert_return_transition(current: ReturnV12Status | str, target: ReturnV12Status | str) -> None:
    _assert_transition("return", RETURN_TRANSITIONS, ReturnV12Status(current), ReturnV12Status(target))


def assert_reward_transition(current: RewardStatus | str, target: RewardStatus | str) -> None:
    _assert_transition("reward", REWARD_TRANSITIONS, RewardStatus(current), RewardStatus(target))


def _canonical_legacy_status(value: str) -> str:
    return value.strip().upper()


def try_map_legacy_lead_status(value: str) -> LeadV12Status | None:
    """Return a read-only V1.2 view, or None when manual mapping is required."""

    return LEGACY_LEAD_STATUS_MAP.get(_canonical_legacy_status(value))


def try_map_legacy_return_status(value: str) -> ReturnV12Status | None:
    """Return a read-only V1.2 view, or None when manual mapping is required."""

    return LEGACY_RETURN_STATUS_MAP.get(_canonical_legacy_status(value))


def map_legacy_lead_status(value: str, *, strict: bool = False) -> LeadV12Status:
    mapped = try_map_legacy_lead_status(value)
    if mapped is not None:
        return mapped
    if strict:
        raise UnknownLegacyStatus("lead", value)
    return LeadV12Status.CLOSED


def map_legacy_return_status(value: str, *, strict: bool = False) -> ReturnV12Status:
    mapped = try_map_legacy_return_status(value)
    if mapped is not None:
        return mapped
    if strict:
        raise UnknownLegacyStatus("return", value)
    return ReturnV12Status.EXPIRED
