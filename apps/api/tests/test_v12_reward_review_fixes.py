from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from apps.api.src.core import reward_models_v12 as _reward_models_v12  # noqa: F401
from apps.api.src.core.enums import AssignmentStatus
from apps.api.src.core.models import Assignment, Company, Lead, PointsAccount, User
from apps.api.src.core.models_v12 import (
    CompanyLeadCapability,
    CompanyServiceAreaV12,
    SupplierLeadReward,
)
from apps.api.src.core.security import encrypt_text, fingerprint_phone, hash_phone
from apps.api.src.core.v12_enums import LeadSourceKind, LeadV12Status, RewardStatus
from apps.api.src.services.dispatch_v12 import evaluate_candidate
from apps.api.src.services.reward_rule_v12 import create_supplier_reward_rule
from apps.api.src.services.supplier_reward_v12 import (
    drain_due_supplier_reward_settlement,
)


def _company(db, code: str) -> Company:
    company = Company(code=code, name=f"测试公司-{code}", status="ACTIVE")
    db.add(company)
    db.flush()
    return company


def _user(db, name: str, company_id: str | None = None) -> User:
    user = User(display_name=name, status="ACTIVE", company_id=company_id)
    db.add(user)
    db.flush()
    return user


def _lead(
    db,
    *,
    user_id: str,
    phone: str,
    status: str,
    supplier_company_id: str | None = None,
) -> Lead:
    now = datetime.now(timezone.utc)
    lead = Lead(
        source_type=LeadSourceKind.PLATFORM_MANUAL.value,
        source_kind=LeadSourceKind.PLATFORM_MANUAL.value,
        submitter_user_id=user_id,
        supplier_company_id=supplier_company_id,
        customer_name="评审修复测试客户",
        phone_encrypted=encrypt_text(phone),
        phone_hash=hash_phone(phone),
        phone_fingerprint=fingerprint_phone(phone),
        consent_confirmed=True,
        city="武汉市",
        district="武昌区",
        region_code="420106",
        category_code="SELF_BUILD",
        need_summary="独立建房需求",
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


def _due_reward(
    db,
    *,
    supplier: Company,
    receiver: Company,
    user: User,
    phone: str,
    reward_points: int,
    due_at: datetime,
) -> SupplierLeadReward:
    lead = _lead(
        db,
        user_id=user.id,
        phone=phone,
        status=LeadV12Status.CLAIMED.value,
        supplier_company_id=supplier.id,
    )
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
        assigned_at=due_at - timedelta(days=4),
        claimed_at=due_at - timedelta(days=3),
        idempotency_key=f"review-fix-{phone}",
    )
    db.add(assignment)
    db.flush()
    lead.current_assignment_id = assignment.id
    reward = SupplierLeadReward(
        lead_id=lead.id,
        assignment_id=assignment.id,
        supplier_company_id=supplier.id,
        receiver_company_id=receiver.id,
        status=RewardStatus.OBSERVING.value,
        claim_points=100,
        reward_ratio_bps=3000,
        reward_points=reward_points,
        rule_version=1,
        rule_snapshot_json={
            "version": 1,
            "ratio_bps": 3000,
            "min_points": 0,
            "max_points": None,
            "hard_duplicate_days": 90,
            "reward_duplicate_days": 180,
            "historical_suspect_days": 365,
        },
        observed_at=due_at - timedelta(days=3),
        appeal_deadline_at=due_at,
        reward_due_at=due_at,
    )
    db.add(reward)
    db.flush()
    return reward


def test_hourly_drain_processes_later_rows_after_oldest_failure(db) -> None:
    supplier = _company(db, "DRAIN-SUP")
    receiver = _company(db, "DRAIN-REC")
    user = _user(db, "奖励排空测试用户")
    now = datetime.now(timezone.utc)
    invalid = _due_reward(
        db,
        supplier=supplier,
        receiver=receiver,
        user=user,
        phone="13800138501",
        reward_points=0,
        due_at=now - timedelta(minutes=3),
    )
    first = _due_reward(
        db,
        supplier=supplier,
        receiver=receiver,
        user=user,
        phone="13800138502",
        reward_points=30,
        due_at=now - timedelta(minutes=2),
    )
    second = _due_reward(
        db,
        supplier=supplier,
        receiver=receiver,
        user=user,
        phone="13800138503",
        reward_points=30,
        due_at=now - timedelta(minutes=1),
    )
    db.commit()

    result = drain_due_supplier_reward_settlement(
        db,
        as_of=now,
        batch_size=1,
        max_batches=10,
        settled_by=user.id,
    )
    db.commit()

    assert result["scanned"] == 3
    assert result["failed"] == 1
    assert result["settled"] == 2
    assert result["attempted_unique"] == 3
    assert result["safety_limit_reached"] is False
    assert invalid.status == RewardStatus.OBSERVING.value
    assert first.status == RewardStatus.SETTLED.value
    assert second.status == RewardStatus.SETTLED.value
    account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == supplier.id))
    assert account is not None and account.balance == 0 and account.supply_balance == 60


def test_receiver_history_filter_uses_published_historical_window(db) -> None:
    receiver = _company(db, "WINDOW-REC")
    user = _user(db, "接收窗口测试用户", receiver.id)
    db.add_all(
        [
            CompanyLeadCapability(
                company_id=receiver.id,
                capability_code="LEAD_RECEIVER",
                active=True,
                review_status="APPROVED",
            ),
            CompanyServiceAreaV12(
                company_id=receiver.id,
                region_code="420106",
                region_level="DISTRICT",
                is_primary_city=False,
                active=True,
                review_status="APPROVED",
            ),
            PointsAccount(company_id=receiver.id, balance=1000, version=0),
        ]
    )
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
        created_by=user.id,
        publish_immediately=True,
    )
    phone = "13800138504"
    historical = _lead(
        db,
        user_id=user.id,
        phone=phone,
        status=LeadV12Status.CLAIMED.value,
    )
    old_assignment = Assignment(
        lead_id=historical.id,
        company_id=receiver.id,
        receiver_company_id=receiver.id,
        status=AssignmentStatus.CLAIMED.value,
        points_price=100,
        claim_points=100,
        lead_snapshot={},
        assigned_by=user.id,
        assigned_at=datetime.now(timezone.utc) - timedelta(days=46),
        claimed_at=datetime.now(timezone.utc) - timedelta(days=45),
        idempotency_key="receiver-window-history",
    )
    db.add(old_assignment)
    target = _lead(
        db,
        user_id=user.id,
        phone=phone,
        status=LeadV12Status.READY_DISPATCH.value,
    )
    db.commit()

    result = evaluate_candidate(db, lead=target, company=receiver)
    assert result.duplicate_to_receiver is False
    assert "DUPLICATE_TO_RECEIVER" not in result.exclusion_reasons
    assert result.eligible is True
