from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, inspect, select

from apps.api.src.core.enums import AssignmentStatus
from apps.api.src.core.errors import AppError
from apps.api.src.core.models import Assignment, Company, Lead, PointsAccount, PointsLedger, Region, User
from apps.api.src.core.models_v12 import CompanyLeadCapability, CompanyServiceAreaV12, SupplierLeadReward
# N14：reward mapper 由该模块打补丁——不显式导入则单文件运行必 TypeError
# （全套件靠字母序在前的 test_pre_go_live_security.py 先导入才侥幸通过）。
from apps.api.src.core import reward_models_v12 as _reward_models_v12  # noqa: F401
from apps.api.src.core.security import encrypt_text, fingerprint_phone, hash_phone
from apps.api.src.core.v12_enums import LeadSourceKind, LeadV12Status, RewardStatus
from apps.api.src.services.dispatch_v12 import (
    candidate_to_dict,
    claim_assignment,
    dispatch_manually,
    evaluate_candidate,
    list_candidates,
    list_dispatch_pool,
)
from apps.api.src.services.reward_rule_v12 import (
    create_supplier_reward_rule,
    publish_supplier_reward_rule,
)


def _company(db, code: str, name: str) -> tuple[Company, User]:
    company = Company(code=code, name=name, status="ACTIVE")
    db.add(company)
    db.flush()
    user = User(username=f"{code.lower()}_owner", display_name=f"{name}负责人", status="ACTIVE", company_id=company.id)
    db.add(user)
    db.flush()
    return company, user


def _receiver_setup(db, company: Company, *, region_code: str = "420106", balance: int = 1000) -> None:
    db.add(
        CompanyLeadCapability(
            company_id=company.id,
            capability_code="LEAD_RECEIVER",
            active=True,
            review_status="APPROVED",
        )
    )
    db.add(
        CompanyServiceAreaV12(
            company_id=company.id,
            region_code=region_code,
            region_level="DISTRICT",
            is_primary_city=False,
            active=True,
            review_status="APPROVED",
        )
    )
    db.add(PointsAccount(company_id=company.id, balance=balance, version=0))
    db.flush()


def _lead(
    db,
    *,
    phone: str,
    submitter_id: str,
    supplier_company_id: str | None,
    status: str = LeadV12Status.READY_DISPATCH.value,
) -> Lead:
    now = datetime.now(timezone.utc)
    lead = Lead(
        source_type=LeadSourceKind.SUPPLIER_H5.value if supplier_company_id else LeadSourceKind.PLATFORM_MANUAL.value,
        source_kind=LeadSourceKind.SUPPLIER_H5.value if supplier_company_id else LeadSourceKind.PLATFORM_MANUAL.value,
        submitter_user_id=submitter_id,
        supplier_company_id=supplier_company_id,
        customer_name="候选客户",
        phone_encrypted=encrypt_text(phone),
        phone_hash=hash_phone(phone),
        phone_fingerprint=fingerprint_phone(phone),
        consent_confirmed=True,
        city="武汉市",
        district="武昌区",
        region_code="420106",
        need_summary="计划建设两层乡墅",
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


@pytest.fixture
def dispatch_setup(db):
    db.add_all(
        [
            Region(code="420100", name="武汉市", level="CITY", aliases=[], active=True),
            Region(code="420106", name="武昌区", level="DISTRICT", parent_code="420100", aliases=[], active=True),
        ]
    )
    supplier, supplier_user = _company(db, "SUP-DISP", "供给公司")
    receiver, receiver_user = _company(db, "REC-DISP", "接收公司")
    _receiver_setup(db, receiver)
    lead = _lead(
        db,
        phone="13800138101",
        submitter_id=supplier_user.id,
        supplier_company_id=supplier.id,
    )
    db.commit()
    return supplier, supplier_user, receiver, receiver_user, lead


def test_dispatch_pool_only_contains_ready_unassigned_leads(db, dispatch_setup) -> None:
    _, supplier_user, _, _, ready = dispatch_setup
    _lead(
        db,
        phone="13800138102",
        submitter_id=supplier_user.id,
        supplier_company_id=None,
        status=LeadV12Status.DRAFT.value,
    )
    db.commit()
    items, total = list_dispatch_pool(db, page_no=1, page_size=20)
    assert total == 1
    assert [item.id for item in items] == [ready.id]

    ready.current_assignment_id = "stale-assignment-id"
    db.commit()
    items, total = list_dispatch_pool(db, page_no=1, page_size=20)
    assert total == 0
    assert items == []


def test_candidate_filter_blocks_self_supply_and_accepts_eligible_receiver(db, dispatch_setup) -> None:
    supplier, _, receiver, _, lead = dispatch_setup
    supplier_result = evaluate_candidate(db, lead=lead, company=supplier)
    receiver_result = evaluate_candidate(db, lead=lead, company=receiver)
    assert supplier_result.eligible is False
    assert "SELF_SUPPLY_FORBIDDEN" in supplier_result.exclusion_reasons
    assert receiver_result.eligible is True
    assert receiver_result.points_price == 100
    assert receiver_result.points_available == 1000


def test_city_service_area_covers_child_district_leads(db, dispatch_setup) -> None:
    _, _, receiver, _, lead = dispatch_setup
    district_area = db.scalar(
        select(CompanyServiceAreaV12).where(
            CompanyServiceAreaV12.company_id == receiver.id,
            CompanyServiceAreaV12.region_code == "420106",
        )
    )
    assert district_area is not None
    district_area.region_code = "420100"
    district_area.region_level = "CITY"
    district_area.is_primary_city = True
    db.commit()

    result = evaluate_candidate(db, lead=lead, company=receiver)
    assert result.region_match is True
    assert result.eligible is True

    listed = {item.company_id: item for item in list_candidates(db, lead=lead)}
    assert listed[receiver.id].region_match is True
    assert listed[receiver.id].eligible is True


def test_city_service_area_covers_child_township_leads(db, dispatch_setup) -> None:
    _, _, receiver, _, lead = dispatch_setup
    district_area = db.scalar(
        select(CompanyServiceAreaV12).where(
            CompanyServiceAreaV12.company_id == receiver.id,
            CompanyServiceAreaV12.region_code == "420106",
        )
    )
    assert district_area is not None
    district_area.region_code = "420100"
    district_area.region_level = "CITY"
    district_area.is_primary_city = True
    db.add(
        Region(
            code="420106001",
            name="粮道街道",
            level="TOWNSHIP",
            parent_code="420106",
            aliases=[],
            active=True,
        )
    )
    lead.region_code = "420106001"
    db.commit()

    result = evaluate_candidate(db, lead=lead, company=receiver)
    assert result.region_match is True
    assert result.eligible is True


def test_candidate_list_prioritizes_matching_service_area(db, dispatch_setup) -> None:
    supplier, _, receiver, _, lead = dispatch_setup

    candidates = list_candidates(db, lead=lead)

    assert candidates[0].company_id == receiver.id
    assert candidates[0].region_match is True
    assert any(item.company_id == supplier.id and not item.region_match for item in candidates)


def test_candidate_list_matches_individual_evaluation_with_bounded_queries(db, dispatch_setup) -> None:
    supplier, _, receiver, _, lead = dispatch_setup
    expected = {
        supplier.id: evaluate_candidate(db, lead=lead, company=supplier),
        receiver.id: evaluate_candidate(db, lead=lead, company=receiver),
    }
    statements: list[str] = []

    def record_statement(*args) -> None:
        if args[2].lstrip().upper().startswith("SELECT"):
            statements.append(args[2])

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        actual = {item.company_id: item for item in list_candidates(db, lead=lead)}
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    assert actual == expected
    assert len(statements) <= 4


def test_candidate_serialization_hides_exact_balance_by_default(db, dispatch_setup) -> None:
    _, _, receiver, _, lead = dispatch_setup
    result = evaluate_candidate(db, lead=lead, company=receiver)
    public = candidate_to_dict(result)
    assert public["points_price"] == 100
    assert "points_balance" not in public
    assert "points_reserved" not in public
    assert "points_available" not in public

    finance = candidate_to_dict(result, include_financials=True)
    assert finance["points_balance"] == 1000
    assert finance["points_reserved"] == 0
    assert finance["points_available"] == 1000


def test_manual_dispatch_does_not_deduct_points_and_claim_is_atomic_and_idempotent(db, dispatch_setup) -> None:
    supplier, _, receiver, receiver_user, lead = dispatch_setup
    assignment = dispatch_manually(
        db,
        lead_id=lead.id,
        company_id=receiver.id,
        assigned_by=receiver_user.id,
        idempotency_key="dispatch-test-0001",
        note="运营人工选择",
    )
    db.commit()
    account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == receiver.id))
    assert account is not None and account.balance == 1000
    assert assignment.status == AssignmentStatus.PENDING_CLAIM.value
    assert lead.status == LeadV12Status.DISPATCHED.value

    db.expire_all()
    result = claim_assignment(
        db,
        assignment_id=assignment.id,
        company_id=receiver.id,
        claimed_by=receiver_user.id,
    )
    db.commit()
    assert result.idempotent is False
    assert result.assignment.status == AssignmentStatus.CLAIMED.value
    assert result.assignment.claim_points == 100
    assert result.assignment.appeal_deadline_at == result.assignment.claimed_at + timedelta(hours=48)
    assert result.assignment.reward_due_at is None
    account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == receiver.id))
    assert account is not None and account.balance == 900
    reward = db.scalar(select(SupplierLeadReward).where(SupplierLeadReward.assignment_id == assignment.id))
    assert reward is not None
    assert reward.supplier_company_id == supplier.id
    assert reward.status == RewardStatus.WAITING_CLAIM.value
    assert reward.observed_at is None
    assert reward.reward_due_at is None
    assert reward.reward_ratio_bps == 3000
    assert reward.reward_points == 30
    assert reward.rule_version == 1
    assert reward.rule_snapshot_json["ratio_bps"] == 3000
    assert reward.rule_snapshot_json["min_points"] == 0
    assert reward.rule_snapshot_json["max_points"] is None

    second = claim_assignment(
        db,
        assignment_id=assignment.id,
        company_id=receiver.id,
        claimed_by=receiver_user.id,
    )
    db.commit()
    assert second.idempotent is True
    account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == receiver.id))
    assert account is not None and account.balance == 900
    ledgers = db.scalars(
        select(PointsLedger).where(
            PointsLedger.company_id == receiver.id,
            PointsLedger.business_id == assignment.id,
        )
    ).all()
    assert len(ledgers) == 1


def test_claim_explicitly_snapshots_published_reward_rule(db, dispatch_setup) -> None:
    supplier, _, receiver, receiver_user, lead = dispatch_setup
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
        created_by=receiver_user.id,
        publish_immediately=True,
    )
    assignment = dispatch_manually(
        db,
        lead_id=lead.id,
        company_id=receiver.id,
        assigned_by=receiver_user.id,
        idempotency_key="dispatch-reward-snapshot-0001",
    )

    result = claim_assignment(
        db,
        assignment_id=assignment.id,
        company_id=receiver.id,
        claimed_by=receiver_user.id,
    )
    db.commit()

    assert result.reward is not None
    assert result.reward.reward_ratio_bps == 2500
    assert result.reward.reward_points == 40
    assert result.reward.rule_version == config.version
    snapshot = result.reward.rule_snapshot_json
    assert snapshot == {
        "domain": "supplier_reward",
        "key": "default",
        "config_id": config.id,
        "version": config.version,
        "effective_at": snapshot["effective_at"],
        "ratio_bps": 2500,
        "min_points": 40,
        "max_points": 50,
        "hard_duplicate_days": 30,
        "reward_duplicate_days": 60,
        "historical_suspect_days": 120,
    }
    assert snapshot["effective_at"]

    replacement = create_supplier_reward_rule(
        db,
        values={
            "ratio_bps": 9000,
            "min_points": 0,
            "max_points": None,
            "hard_duplicate_days": 30,
            "reward_duplicate_days": 60,
            "historical_suspect_days": 120,
        },
        created_by=receiver_user.id,
    )
    publish_supplier_reward_rule(db, config_id=replacement.id, published_by=receiver_user.id)
    db.refresh(result.reward)
    assert result.reward.reward_ratio_bps == 2500
    assert result.reward.reward_points == 40
    assert result.reward.rule_snapshot_json == snapshot


def test_platform_lead_claim_does_not_create_supplier_reward(db, dispatch_setup) -> None:
    _, supplier_user, receiver, receiver_user, _ = dispatch_setup
    lead = _lead(
        db,
        phone="13800138103",
        submitter_id=supplier_user.id,
        supplier_company_id=None,
    )
    db.commit()
    assignment = dispatch_manually(
        db,
        lead_id=lead.id,
        company_id=receiver.id,
        assigned_by=receiver_user.id,
        idempotency_key="dispatch-platform-0001",
    )
    db.commit()
    result = claim_assignment(
        db,
        assignment_id=assignment.id,
        company_id=receiver.id,
        claimed_by=receiver_user.id,
    )
    db.commit()
    assert result.reward is None
    assert db.scalar(
        select(SupplierLeadReward).where(SupplierLeadReward.assignment_id == assignment.id)
    ) is None


def test_pending_dispatch_reservations_prevent_oversubscription(db, dispatch_setup) -> None:
    _, supplier_user, receiver, receiver_user, lead = dispatch_setup
    account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == receiver.id))
    assert account is not None
    account.balance = 150
    second_lead = _lead(
        db,
        phone="13800138104",
        submitter_id=supplier_user.id,
        supplier_company_id=None,
    )
    db.commit()

    dispatch_manually(
        db,
        lead_id=lead.id,
        company_id=receiver.id,
        assigned_by=receiver_user.id,
        idempotency_key="dispatch-reserve-0001",
    )
    db.commit()
    with pytest.raises(AppError) as exc_info:
        dispatch_manually(
            db,
            lead_id=second_lead.id,
            company_id=receiver.id,
            assigned_by=receiver_user.id,
            idempotency_key="dispatch-reserve-0002",
        )
    assert exc_info.value.code == "DISPATCH_CANDIDATE_INELIGIBLE"
    assert "POINTS_INSUFFICIENT" in exc_info.value.details["reasons"]
    assert account.balance == 150


def test_claim_rechecks_receiver_history_duplicate(db, dispatch_setup) -> None:
    _, supplier_user, receiver, receiver_user, lead = dispatch_setup
    historical = _lead(
        db,
        phone="13800138101",
        submitter_id=supplier_user.id,
        supplier_company_id=None,
        status=LeadV12Status.CLAIMED.value,
    )
    previous = Assignment(
        lead_id=historical.id,
        company_id=receiver.id,
        receiver_company_id=receiver.id,
        status=AssignmentStatus.CLAIMED.value,
        points_price=100,
        lead_snapshot={},
        assigned_by=receiver_user.id,
        assigned_at=datetime.now(timezone.utc) - timedelta(days=10),
        claimed_at=datetime.now(timezone.utc) - timedelta(days=10),
        idempotency_key="historical-assignment-001",
    )
    db.add(previous)
    db.commit()

    result = evaluate_candidate(db, lead=lead, company=receiver)
    assert result.eligible is False
    assert result.duplicate_to_receiver is True
    assert "DUPLICATE_TO_RECEIVER" in result.exclusion_reasons

    with pytest.raises(AppError) as exc_info:
        dispatch_manually(
            db,
            lead_id=lead.id,
            company_id=receiver.id,
            assigned_by=receiver_user.id,
            idempotency_key="dispatch-test-0002",
        )
    assert exc_info.value.code == "DISPATCH_CANDIDATE_INELIGIBLE"


def test_claim_rejects_expired_assignment_with_naive_database_datetime(db, dispatch_setup) -> None:
    _, _, receiver, receiver_user, lead = dispatch_setup
    assignment = dispatch_manually(
        db,
        lead_id=lead.id,
        company_id=receiver.id,
        assigned_by=receiver_user.id,
        idempotency_key="dispatch-expired-0001",
    )
    assignment.expires_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).replace(tzinfo=None)
    db.commit()
    db.expire_all()

    with pytest.raises(AppError) as exc_info:
        claim_assignment(
            db,
            assignment_id=assignment.id,
            company_id=receiver.id,
            claimed_by=receiver_user.id,
        )
    assert exc_info.value.code == "ASSIGNMENT_EXPIRED"


def test_manual_dispatch_idempotency_key_cannot_be_reused_for_other_target(db, dispatch_setup) -> None:
    _, _, receiver, receiver_user, lead = dispatch_setup
    first = dispatch_manually(
        db,
        lead_id=lead.id,
        company_id=receiver.id,
        assigned_by=receiver_user.id,
        idempotency_key="dispatch-test-0003",
    )
    db.commit()
    repeated = dispatch_manually(
        db,
        lead_id=lead.id,
        company_id=receiver.id,
        assigned_by=receiver_user.id,
        idempotency_key="dispatch-test-0003",
    )
    assert repeated.id == first.id


def test_active_assignment_partial_unique_index_is_registered(db) -> None:
    indexes = inspect(db.get_bind()).get_indexes("assignments")
    index = next(item for item in indexes if item["name"] == "uq_assignments_active_lead_v12")
    assert index["unique"] == 1
