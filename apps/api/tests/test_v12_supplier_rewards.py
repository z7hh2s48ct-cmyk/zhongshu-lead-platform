from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from apps.api.src.core import reward_models_v12 as _reward_models_v12  # noqa: F401
from apps.api.src.core.enums import AssignmentStatus, PointsLedgerType
from apps.api.src.core.errors import AppError
from apps.api.src.core.models import (
    Assignment,
    Company,
    Lead,
    PointsAccount,
    PointsLedger,
    ReturnRequest,
    User,
)
from apps.api.src.core.models_v12 import SupplierLeadReward
from apps.api.src.core.security import encrypt_text, fingerprint_phone, hash_phone
from apps.api.src.core.v12_enums import (
    DuplicateDecision,
    LeadSourceKind,
    LeadV12Status,
    ReturnV12Status,
    RewardStatus,
)
from apps.api.src.services.dedup_v12 import evaluate_phone
from apps.api.src.services.points_service import change_points
from apps.api.src.services.reward_rule_v12 import (
    calculate_reward_points,
    create_supplier_reward_rule,
    default_supplier_reward_rule,
    resolve_supplier_reward_rule,
    rule_from_values,
)
from apps.api.src.services.supplier_reward_v12 import (
    reverse_supplier_reward,
    run_due_supplier_reward_settlement,
    settle_supplier_reward,
)


def _company(db, code: str) -> Company:
    company = Company(code=code, name=f"测试公司-{code}", status="ACTIVE")
    db.add(company)
    db.flush()
    return company


def _lead(
    db,
    *,
    phone: str,
    user_id: str,
    supplier_company_id: str | None,
    submitted_at: datetime | None = None,
    status: str = LeadV12Status.CLAIMED.value,
) -> Lead:
    now = submitted_at or datetime.now(timezone.utc)
    lead = Lead(
        source_type=(LeadSourceKind.SUPPLIER_H5.value if supplier_company_id else LeadSourceKind.PLATFORM_MANUAL.value),
        source_kind=(LeadSourceKind.SUPPLIER_H5.value if supplier_company_id else LeadSourceKind.PLATFORM_MANUAL.value),
        submitter_user_id=user_id,
        supplier_company_id=supplier_company_id,
        customer_name="奖励测试客户",
        phone_encrypted=encrypt_text(phone),
        phone_hash=hash_phone(phone),
        phone_fingerprint=fingerprint_phone(phone),
        consent_confirmed=True,
        city="武汉市",
        region_code="420100",
        need_summary="供应商奖励测试需求",
        status=status,
        review_status="APPROVED",
        duplicate_status="CLEAR",
        imported_at=now,
        submitted_at=now,
        raw_payload={},
    )
    db.add(lead)
    db.flush()
    return lead


def _reward_setup(
    db,
    *,
    status: str = RewardStatus.OBSERVING.value,
    due_at: datetime | None = None,
    reward_points: int = 30,
):
    supplier = _company(db, f"R-SUP-{status[:3]}-{reward_points}")
    receiver = _company(db, f"R-REC-{status[:3]}-{reward_points}")
    user = User(display_name="奖励测试用户", status="ACTIVE")
    db.add(user)
    db.flush()
    lead = _lead(
        db,
        phone=f"1380013{reward_points:04d}"[-11:],
        user_id=user.id,
        supplier_company_id=supplier.id,
    )
    now = datetime.now(timezone.utc)
    assignment = Assignment(
        lead_id=lead.id,
        company_id=receiver.id,
        receiver_company_id=receiver.id,
        supplier_company_id=supplier.id,
        status=AssignmentStatus.CLAIMED.value,
        points_price=100,
        claim_points=100,
        lead_snapshot={},
        assigned_by=user.id,
        assigned_at=now - timedelta(days=5),
        claimed_at=(due_at - timedelta(hours=48))
        if due_at
        else now - timedelta(days=4),
        idempotency_key=f"reward-assignment-{supplier.id}",
    )
    db.add(assignment)
    db.flush()
    lead.current_assignment_id = assignment.id
    reward = SupplierLeadReward(
        lead_id=lead.id,
        assignment_id=assignment.id,
        supplier_company_id=supplier.id,
        receiver_company_id=receiver.id,
        status=status,
        claim_points=100,
        reward_ratio_bps=3000,
        reward_points=reward_points,
        rule_version=1,
        rule_snapshot_json={
            "version": 1,
            "ratio_bps": 3000,
            "min_points": 0,
            "max_points": None,
        },
        observed_at=now - timedelta(days=4),
        appeal_deadline_at=due_at or (now - timedelta(minutes=1)),
        reward_due_at=due_at or (now - timedelta(minutes=1)),
    )
    db.add(reward)
    db.flush()
    return supplier, receiver, user, lead, assignment, reward


def test_reward_formula_applies_ratio_minimum_and_maximum() -> None:
    default = default_supplier_reward_rule()
    assert calculate_reward_points(100, default) == 30
    minimum = rule_from_values(
        {
            "ratio_bps": 1000,
            "min_points": 25,
            "max_points": 80,
            "hard_duplicate_days": 10,
            "reward_duplicate_days": 20,
            "historical_suspect_days": 30,
        },
        version=2,
    )
    assert calculate_reward_points(100, minimum) == 25
    maximum = rule_from_values(
        {
            "ratio_bps": 9000,
            "min_points": 0,
            "max_points": 50,
            "hard_duplicate_days": 10,
            "reward_duplicate_days": 20,
            "historical_suspect_days": 30,
        },
        version=3,
    )
    assert calculate_reward_points(100, maximum) == 50


def test_direct_reward_insertion_does_not_implicitly_replace_supplied_snapshot(db) -> None:
    publisher = User(display_name="规则发布人", status="ACTIVE")
    db.add(publisher)
    db.flush()
    config = create_supplier_reward_rule(
        db,
        values={
            "ratio_bps": 2500,
            "min_points": 40,
            "max_points": 50,
            "hard_duplicate_days": 30,
            "reward_duplicate_days": 60,
            "historical_suspect_days": 120,
        },
        created_by=publisher.id,
        publish_immediately=True,
    )
    supplier, receiver, user, lead, assignment, _ = _reward_setup(
        db,
        status=RewardStatus.NOT_ELIGIBLE.value,
        reward_points=0,
    )
    reward = SupplierLeadReward(
        lead_id=lead.id,
        assignment_id=f"{assignment.id}-snapshot",
        supplier_company_id=supplier.id,
        receiver_company_id=receiver.id,
        status=RewardStatus.OBSERVING.value,
        claim_points=100,
        reward_ratio_bps=3000,
        reward_points=30,
        rule_version=1,
        rule_snapshot_json={"version": 1, "ratio_bps": 3000},
        observed_at=datetime.now(timezone.utc),
        reward_due_at=datetime.now(timezone.utc) + timedelta(days=3),
    )
    # Use a second real assignment because assignment_id has a foreign key.
    second_assignment = Assignment(
        lead_id=lead.id,
        company_id=receiver.id,
        receiver_company_id=receiver.id,
        supplier_company_id=supplier.id,
        status=AssignmentStatus.RETURNED.value,
        points_price=100,
        claim_points=100,
        lead_snapshot={},
        assigned_by=user.id,
        assigned_at=datetime.now(timezone.utc),
        claimed_at=datetime.now(timezone.utc),
        idempotency_key=f"snapshot-{supplier.id}",
    )
    db.add(second_assignment)
    db.flush()
    reward.assignment_id = second_assignment.id
    db.add(reward)
    db.flush()

    assert config.status == "PUBLISHED"
    assert reward.reward_ratio_bps == 3000
    assert reward.reward_points == 30
    assert reward.rule_version == 1
    assert reward.rule_snapshot_json == {"version": 1, "ratio_bps": 3000}


def test_published_rule_changes_dedup_windows(db) -> None:
    publisher = User(display_name="去重规则发布人", status="ACTIVE")
    db.add(publisher)
    db.flush()
    create_supplier_reward_rule(
        db,
        values={
            "ratio_bps": 3000,
            "min_points": 0,
            "max_points": None,
            "hard_duplicate_days": 10,
            "reward_duplicate_days": 20,
            "historical_suspect_days": 30,
        },
        created_by=publisher.id,
        publish_immediately=True,
    )
    old = _lead(
        db,
        phone="13800138401",
        user_id=publisher.id,
        supplier_company_id=None,
        submitted_at=datetime.now(timezone.utc) - timedelta(days=15),
        status=LeadV12Status.READY_DISPATCH.value,
    )
    new = _lead(
        db,
        phone="13800138401",
        user_id=publisher.id,
        supplier_company_id=None,
        status=LeadV12Status.DRAFT.value,
    )
    db.flush()
    result = evaluate_phone(
        db,
        lead=new,
        normalized_phone="13800138401",
        checkpoint="RULE_TEST",
        now=datetime.now(timezone.utc),
    )
    assert old.id == result.matched_lead_id
    assert result.decision is DuplicateDecision.REWARD_DUPLICATE
    assert result.window_days == 20


def test_due_reward_settles_once_and_credits_supplier(db) -> None:
    supplier, _, user, _, _, reward = _reward_setup(db)
    result = settle_supplier_reward(db, reward_id=reward.id, settled_by=user.id)
    db.commit()
    assert result.idempotent is False
    assert reward.status == RewardStatus.SETTLED.value
    assert result.ledger is not None and result.ledger.delta == 30
    account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == supplier.id))
    assert account is not None and account.balance == 30

    repeated = settle_supplier_reward(db, reward_id=reward.id, settled_by=user.id)
    db.commit()
    assert repeated.idempotent is True
    ledgers = db.scalars(
        select(PointsLedger).where(
            PointsLedger.company_id == supplier.id,
            PointsLedger.business_type == "V12_SUPPLIER_REWARD",
            PointsLedger.business_id == reward.id,
        )
    ).all()
    assert len(ledgers) == 1
    assert db.get(PointsAccount, account.id).balance == 30


def test_waiting_reward_cannot_be_settled_before_claim_48h(db) -> None:
    supplier, _, user, _, _, reward = _reward_setup(
        db,
        status=RewardStatus.WAITING_CLAIM.value,
        due_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )

    with pytest.raises(AppError) as exc_info:
        settle_supplier_reward(db, reward_id=reward.id, settled_by=user.id)

    assert exc_info.value.code == "REWARD_NOT_DUE"
    batch = run_due_supplier_reward_settlement(db, limit=100, settled_by=user.id)
    db.commit()
    assert batch["scanned"] == 0
    assert batch["settled"] == 0
    assert batch["processed_reward_ids"] == []
    assert batch["errors"] == []
    assert reward.status == RewardStatus.WAITING_CLAIM.value
    assert reward.ledger_id is None
    account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == supplier.id))
    assert account is None or account.balance == 0


def test_due_settlement_freezes_if_active_appeal_exists(db) -> None:
    _, _, user, lead, assignment, reward = _reward_setup(db)
    request = ReturnRequest(
        assignment_id=assignment.id,
        lead_id=lead.id,
        company_id=assignment.company_id,
        reason_code="EMPTY_NUMBER",
        reason_version=1,
        description="有效申诉阻止奖励结算",
        status=ReturnV12Status.VERIFYING.value,
        submitted_by=user.id,
        submitted_at=datetime.now(timezone.utc) - timedelta(hours=1),
        due_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    db.add(request)
    db.flush()
    result = run_due_supplier_reward_settlement(db, limit=100)
    db.commit()
    assert result["frozen"] == 1
    assert reward.status == RewardStatus.FROZEN.value
    assert reward.ledger_id is None


def test_rejected_overdue_appeal_settles_immediately_on_commit(db) -> None:
    supplier, _, user, lead, assignment, reward = _reward_setup(
        db,
        status=RewardStatus.FROZEN.value,
    )
    request = ReturnRequest(
        assignment_id=assignment.id,
        lead_id=lead.id,
        company_id=assignment.company_id,
        reason_code="EMPTY_NUMBER",
        reason_version=1,
        description="已驳回申诉",
        status=ReturnV12Status.REJECTED.value,
        submitted_by=user.id,
        submitted_at=datetime.now(timezone.utc) - timedelta(days=1),
        reviewed_at=datetime.now(timezone.utc),
        due_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db.add(request)
    db.flush()
    reward.status = RewardStatus.OBSERVING.value
    db.commit()

    assert reward.status == RewardStatus.SETTLED.value
    assert reward.ledger_id is not None
    account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == supplier.id))
    assert account is not None and account.balance == 30


def test_exceptional_reversal_is_idempotent_and_can_create_debt(db) -> None:
    supplier, _, user, _, _, reward = _reward_setup(db)
    settled = settle_supplier_reward(db, reward_id=reward.id, settled_by=user.id)
    assert settled.ledger is not None
    change_points(
        db,
        company_id=supplier.id,
        delta=-25,
        ledger_type=PointsLedgerType.ADJUST.value,
        business_type="TEST_SPEND",
        business_id=reward.id,
        idempotency_key=f"test-spend:{reward.id}",
        created_by=user.id,
    )
    db.commit()

    reversed_result = reverse_supplier_reward(
        db,
        reward_id=reward.id,
        reason_code="SYSTEM_ERROR",
        note="测试系统错误导致的奖励冲正",
        reversed_by=user.id,
    )
    db.commit()
    assert reversed_result.idempotent is False
    assert reward.status == RewardStatus.REVERSED.value
    assert reversed_result.ledger.delta == -30
    account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == supplier.id))
    assert account is not None and account.balance == -25

    repeated = reverse_supplier_reward(
        db,
        reward_id=reward.id,
        reason_code="SYSTEM_ERROR",
        note="重复冲正请求",
        reversed_by=user.id,
    )
    db.commit()
    assert repeated.idempotent is True
    reversal_count = db.scalar(
        select(func.count(PointsLedger.id)).where(
            PointsLedger.business_type == "V12_SUPPLIER_REWARD_REVERSAL",
            PointsLedger.business_id == reward.id,
        )
    )
    assert reversal_count == 1
    assert db.get(PointsAccount, account.id).balance == -25


def test_only_exceptional_reason_codes_can_reverse(db) -> None:
    _, _, user, _, _, reward = _reward_setup(db)
    settle_supplier_reward(db, reward_id=reward.id, settled_by=user.id)
    db.flush()
    with pytest.raises(AppError) as exc_info:
        reverse_supplier_reward(
            db,
            reward_id=reward.id,
            reason_code="NORMAL_RETURN",
            note="普通退回不得使用异常冲正",
            reversed_by=user.id,
        )
    assert exc_info.value.code == "REWARD_REVERSAL_REASON_INVALID"


def test_resolve_rule_falls_back_when_no_published_config(db) -> None:
    rule = resolve_supplier_reward_rule(db)
    assert rule.ratio_bps == 3000
    assert rule.hard_duplicate_days == 90
    assert rule.reward_duplicate_days == 180
    assert rule.historical_suspect_days == 365
