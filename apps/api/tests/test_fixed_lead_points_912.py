from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, func, select

from apps.api.src.core.auth import Principal
from apps.api.src.core.errors import AppError
from apps.api.src.core.models import (
    Assignment,
    AuditLog,
    Company,
    Lead,
    LeadPriceRule,
    PointsAccount,
    PointsLedger,
    User,
)
from apps.api.src.core.models_v12 import CompanyLeadCapability, CompanyServiceAreaV12
from apps.api.src.core.security import encrypt_text, fingerprint_phone, hash_phone
from apps.api.src.core.v12_enums import LeadSourceKind, LeadV12Status, RewardStatus
from apps.api.src.schemas.company import CompanyCreateBody
from apps.api.src.services.company_service import create_company
from apps.api.src.services.dispatch_service import candidate_companies, dispatch_lead
from apps.api.src.services.dispatch_v12 import (
    _reward_for_claim,
    claim_assignment,
    dispatch_manually_with_outcome,
    evaluate_candidate,
    list_candidates,
)
from apps.api.src.services.lead_points_v12 import (
    assignment_points_price,
    get_lead_points_settings,
    update_lead_points_settings,
)
from apps.api.src.services.points_service import change_points, resolve_price
from apps.api.src.services.supplier_reward_v12 import (
    settle_supplier_reward,
)


def _login(client, username: str, password: str) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    token = response.cookies.get("access_token")
    assert token
    return {"Authorization": f"Bearer {token}"}


def _operation_lead(db, *, user_id: str, phone: str, source_kind: str) -> Lead:
    now = datetime.now(timezone.utc)
    lead = Lead(
        source_type=source_kind,
        source_kind=source_kind,
        submitter_user_id=user_id,
        customer_name="固定积分测试客户",
        phone_encrypted=encrypt_text(phone),
        phone_hash=hash_phone(phone),
        phone_fingerprint=fingerprint_phone(phone),
        consent_confirmed=True,
        city="上海市",
        region_code="310000",
        category_code="OLD_RENOVATION",
        brand_code="ZHONGSHU",
        need_summary="验证固定积分锁价",
        status=LeadV12Status.READY_DISPATCH.value,
        review_status="APPROVED",
        duplicate_status="CLEAR",
        imported_at=now,
        submitted_at=now,
        raw_payload={},
    )
    db.add(lead)
    db.flush()
    return lead


def _ensure_receiver(db) -> tuple[Company, User]:
    company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
    employee = db.scalar(select(User).where(User.username == "franchise_employee_demo"))
    assert company is not None and employee is not None
    if db.scalar(
        select(CompanyLeadCapability).where(
            CompanyLeadCapability.company_id == company.id,
            CompanyLeadCapability.capability_code == "LEAD_RECEIVER",
        )
    ) is None:
        db.add(CompanyLeadCapability(company_id=company.id, capability_code="LEAD_RECEIVER", active=True, review_status="APPROVED"))
    if db.scalar(
        select(CompanyServiceAreaV12).where(
            CompanyServiceAreaV12.company_id == company.id,
            CompanyServiceAreaV12.region_code == "310000",
        )
    ) is None:
        db.add(
            CompanyServiceAreaV12(
                company_id=company.id,
                region_code="310000",
                region_level="CITY",
                is_primary_city=True,
                active=True,
                review_status="APPROVED",
            )
        )
    account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == company.id))
    assert account is not None
    account.balance = 5_000
    db.flush()
    return company, employee


def test_lead_points_settings_api_is_superadmin_only_versioned_and_strict(api_client) -> None:
    client, factory = api_client
    admin_headers = _login(client, "admin", "Admin123!")
    endpoint = "/api/v1/v1.2/admin/lead-points-settings"

    initial = client.get(endpoint, headers=admin_headers)
    assert initial.status_code == 200, initial.text
    assert initial.json()["data"] == {
        "configured": False,
        "operation_claim_points": None,
        "supplier_provision_points": None,
        "version": 0,
    }
    saved = client.put(
        endpoint,
        headers=admin_headers,
        json={"operation_claim_points": 240, "supplier_provision_points": 36, "expected_version": 0},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"] == {
        "configured": True,
        "operation_claim_points": 240,
        "supplier_provision_points": 36,
        "version": 1,
    }
    stale = client.put(
        endpoint,
        headers=admin_headers,
        json={"operation_claim_points": 250, "supplier_provision_points": 40, "expected_version": 0},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "LEAD_POINTS_SETTINGS_VERSION_CONFLICT"
    for invalid_value in (True, 1.5, "12"):
        invalid = client.put(
            endpoint,
            headers=admin_headers,
            json={"operation_claim_points": invalid_value, "supplier_provision_points": 36, "expected_version": 1},
        )
        assert invalid.status_code == 422, invalid.text

    operation_headers = _login(client, "operation", "Operation123!")
    assert client.get(endpoint, headers=operation_headers).status_code == 403
    with factory() as db:
        audits = db.scalars(select(AuditLog).where(AuditLog.action == "V12_LEAD_POINTS_SETTINGS_UPDATE")).all()
        assert len(audits) == 1
        assert audits[0].before_json["configured"] is False
        assert audits[0].after_json["operation_claim_points"] == 240


def test_operation_fixed_price_covers_single_batch_and_dispatch_snapshot(api_client) -> None:
    client, factory = api_client
    admin_headers = _login(client, "admin", "Admin123!")
    response = client.put(
        "/api/v1/v1.2/admin/lead-points-settings",
        headers=admin_headers,
        json={"operation_claim_points": 240, "supplier_provision_points": 36, "expected_version": 0},
    )
    assert response.status_code == 200, response.text
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert operation is not None
        company, employee = _ensure_receiver(db)
        manual = _operation_lead(db, user_id=operation.id, phone="13900139801", source_kind=LeadSourceKind.PLATFORM_MANUAL.value)
        feishu = _operation_lead(db, user_id=operation.id, phone="13900139802", source_kind=LeadSourceKind.FEISHU_IMPORT.value)
        supplier_source = _operation_lead(
            db,
            user_id=operation.id,
            phone="13900139804",
            source_kind=LeadSourceKind.SUPPLIER_H5.value,
        )
        legacy_price_rule = LeadPriceRule(
            region_code="310000",
            category_code="OLD_RENOVATION",
            brand_code="ZHONGSHU",
            level_code=company.level_code,
            points_cost=888,
            priority=1,
            version=7,
            status="PUBLISHED",
        )
        db.add(legacy_price_rule)
        db.flush()
        single = evaluate_candidate(db, lead=manual, company=company)
        batch = next(item for item in list_candidates(db, lead=manual) if item.company_id == company.id)
        feishu_price, feishu_rule = resolve_price(db, feishu, company)
        supplier_price, _supplier_rule = resolve_price(db, supplier_source, company)
        assert single.points_price == batch.points_price == 240
        assert single.price_version == batch.price_version == 1
        assert single.price_rule_id is None and batch.price_rule_id is None
        assert feishu_price == 240 and feishu_rule is None
        assert supplier_price == 240

        outcome = dispatch_manually_with_outcome(
            db,
            lead_id=manual.id,
            company_id=company.id,
            employee_user_id=employee.id,
            assigned_by=operation.id,
            idempotency_key="fixed-points-912-dispatch",
        )
        assert outcome.assignment.points_price == 240
        assert outcome.assignment.claim_points == 240
        assert outcome.assignment.price_rule_id is None
        assert outcome.assignment.price_version == 1
        update_lead_points_settings(
            db,
            operation_claim_points=300,
            supplier_provision_points=50,
            expected_version=1,
            updated_by=operation.id,
        )
        db.commit()
        current_settings = get_lead_points_settings(db, lock=True)
        assert current_settings.version == 2
        assert assignment_points_price(outcome.assignment, current_settings) == 300
        assert outcome.assignment.points_price == 240
        claimed = claim_assignment(
            db,
            assignment_id=outcome.assignment.id,
            company_id=company.id,
            claimed_by=employee.id,
        )
        assert claimed.assignment.points_price == 300
        assert claimed.assignment.claim_points == 300
        assert claimed.assignment.price_rule_id is None
        assert claimed.assignment.price_version == 2
        assert claimed.ledger.delta == -300
        assert db.scalar(
            select(PointsAccount.balance).where(PointsAccount.company_id == company.id)
        ) == 4_700
        db.commit()


def test_claim_rejects_stale_expected_price_without_changing_assignment_or_balance(api_client) -> None:
    client, factory = api_client
    admin_headers = _login(client, "admin", "Admin123!")
    saved = client.put(
        "/api/v1/v1.2/admin/lead-points-settings",
        headers=admin_headers,
        json={"operation_claim_points": 240, "supplier_provision_points": 36, "expected_version": 0},
    )
    assert saved.status_code == 200, saved.text

    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert operation is not None
        company, employee = _ensure_receiver(db)
        lead = _operation_lead(
            db,
            user_id=operation.id,
            phone="13900139806",
            source_kind=LeadSourceKind.SUPPLIER_H5.value,
        )
        outcome = dispatch_manually_with_outcome(
            db,
            lead_id=lead.id,
            company_id=company.id,
            employee_user_id=employee.id,
            assigned_by=operation.id,
            idempotency_key="current-points-stale-price-dispatch",
        )
        assert outcome.assignment.points_price == 240
        update_lead_points_settings(
            db,
            operation_claim_points=300,
            supplier_provision_points=50,
            expected_version=1,
            updated_by=operation.id,
        )
        db.commit()
        balance_before = db.scalar(
            select(PointsAccount.balance).where(PointsAccount.company_id == company.id)
        )

        with pytest.raises(AppError) as stale_price:
            claim_assignment(
                db,
                assignment_id=outcome.assignment.id,
                company_id=company.id,
                claimed_by=employee.id,
                expected_points=240,
            )

        assert stale_price.value.code == "CLAIM_PRICE_CHANGED"
        assert stale_price.value.status_code == 409
        assert stale_price.value.details == {"expected_points": 240, "current_points": 300}
        assert outcome.assignment.status == "PENDING_CLAIM"
        assert outcome.assignment.points_price == 240
        assert outcome.assignment.claim_points == 240
        assert db.scalar(
            select(PointsAccount.balance).where(PointsAccount.company_id == company.id)
        ) == balance_before
        assert db.scalar(
            select(func.count(PointsLedger.id)).where(
                PointsLedger.business_type == "V12_ASSIGNMENT_CLAIM",
                PointsLedger.business_id == outcome.assignment.id,
            )
        ) == 0


def test_supplier_fixed_points_keep_48h_window_and_snapshot(db) -> None:
    actor = User(display_name="固定积分规则管理员", status="ACTIVE")
    supplier = Company(code="FIXED-SUP", name="固定积分供客方", status="ACTIVE")
    receiver = Company(code="FIXED-REC", name="固定积分领取方", status="ACTIVE")
    db.add_all([actor, supplier, receiver])
    db.flush()
    update_lead_points_settings(
        db,
        operation_claim_points=240,
        supplier_provision_points=36,
        expected_version=0,
        updated_by=actor.id,
    )
    now = datetime.now(timezone.utc)
    lead = Lead(
        source_type=LeadSourceKind.SUPPLIER_H5.value,
        source_kind=LeadSourceKind.SUPPLIER_H5.value,
        submitter_user_id=actor.id,
        supplier_company_id=supplier.id,
        customer_name="固定供客积分客户",
        phone_encrypted=encrypt_text("13900139803"),
        phone_hash=hash_phone("13900139803"),
        phone_fingerprint=fingerprint_phone("13900139803"),
        status=LeadV12Status.CLAIMED.value,
        review_status="APPROVED",
        duplicate_status="CLEAR",
        imported_at=now,
        submitted_at=now,
        raw_payload={},
    )
    db.add(lead)
    db.flush()
    assignment = Assignment(
        lead_id=lead.id,
        company_id=receiver.id,
        receiver_company_id=receiver.id,
        supplier_company_id=supplier.id,
        status="CLAIMED",
        points_price=999,
        claim_points=999,
        lead_snapshot={},
        assigned_by=actor.id,
        assigned_at=now,
        claimed_at=now,
        idempotency_key="fixed-points-912-reward",
    )
    db.add(assignment)
    db.flush()
    lead.current_assignment_id = assignment.id
    reward = _reward_for_claim(db, lead=lead, assignment=assignment, now=now)
    assert reward is not None
    assert reward.status == RewardStatus.OBSERVING.value
    assert reward.reward_points == 36
    assert reward.rule_snapshot_json["calculation_mode"] == "FIXED"
    assert reward.rule_snapshot_json["fixed_points"] == 36
    assert reward.observed_at == now
    assert reward.reward_due_at == now + timedelta(hours=48)

    update_lead_points_settings(
        db,
        operation_claim_points=300,
        supplier_provision_points=50,
        expected_version=1,
        updated_by=actor.id,
    )
    assert reward.reward_points == 36
    assert reward.rule_snapshot_json["fixed_points"] == 36
    with pytest.raises(AppError) as not_due:
        settle_supplier_reward(
            db, reward_id=reward.id, as_of=now + timedelta(days=1), settled_by=actor.id
        )
    assert not_due.value.code == "REWARD_NOT_DUE"
    first = settle_supplier_reward(db, reward_id=reward.id, as_of=reward.reward_due_at + timedelta(seconds=1), settled_by=actor.id)
    repeated = settle_supplier_reward(db, reward_id=reward.id, as_of=reward.reward_due_at + timedelta(seconds=1), settled_by=actor.id)
    db.commit()
    assert first.ledger is not None and first.ledger.delta == 36
    assert repeated.idempotent is True
    assert db.scalar(
        select(func.count(PointsLedger.id)).where(
            PointsLedger.business_type == "V12_SUPPLIER_REWARD",
            PointsLedger.business_id == reward.id,
        )
    ) == 1
    account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == supplier.id))
    assert account is not None and account.supply_balance == 36


def test_lead_points_settings_start_unconfigured(db) -> None:
    settings = get_lead_points_settings(db)
    assert settings.configured is False
    assert settings.operation_claim_points is None
    assert settings.supplier_provision_points is None
    assert settings.version == 0


def test_legacy_dispatch_uses_one_v2_settings_snapshot_for_all_candidates(db) -> None:
    actor = User(display_name="旧派发固定积分测试管理员", status="ACTIVE")
    db.add(actor)
    db.flush()
    update_lead_points_settings(
        db,
        operation_claim_points=200,
        supplier_provision_points=30,
        expected_version=0,
        updated_by=actor.id,
    )
    update_lead_points_settings(
        db,
        operation_claim_points=260,
        supplier_provision_points=40,
        expected_version=1,
        updated_by=actor.id,
    )
    companies = []
    for index in range(2):
        company = create_company(
            db,
            CompanyCreateBody(
                code=f"LEGACY-FIXED-{index}",
                name=f"旧派发固定积分加盟商{index}",
                region_codes=["310100"],
                capabilities=[{"category_code": "OLD_RENOVATION", "brand_code": None}],
            ),
        )
        company.primary_user_id = f"legacy-owner-{index}"
        change_points(
            db,
            company_id=company.id,
            delta=1_000,
            ledger_type="ADJUST",
            business_type="TEST",
            business_id=company.id,
            idempotency_key=f"legacy-fixed-balance-{index}",
            created_by=actor.id,
        )
        companies.append(company)
    lead = Lead(
        source_type=LeadSourceKind.PLATFORM_MANUAL.value,
        source_kind=LeadSourceKind.PLATFORM_MANUAL.value,
        customer_name="旧派发固定积分客户",
        phone_encrypted=encrypt_text("13900139805"),
        phone_hash=hash_phone("13900139805"),
        city="上海市",
        region_code="310100",
        category_code="OLD_RENOVATION",
        status="QUALIFIED",
    )
    db.add(lead)
    db.flush()
    settings_queries: list[str] = []

    def record_settings_query(*args) -> None:
        statement = args[2]
        if statement.lstrip().upper().startswith("SELECT") and "system_configs" in statement:
            settings_queries.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", record_settings_query)
    try:
        candidates = candidate_companies(db, lead)
    finally:
        event.remove(engine, "before_cursor_execute", record_settings_query)

    fixed_candidates = [item for item in candidates if item["company_id"] in {company.id for company in companies}]
    assert len(fixed_candidates) == 2
    assert len(settings_queries) == 1
    assert all(item["points_price"] == 260 for item in fixed_candidates)
    assert all(item["price_version"] == 2 for item in fixed_candidates)
    assert all(item["price_rule_id"] is None for item in fixed_candidates)

    principal = Principal(
        user_id=actor.id,
        display_name=actor.display_name,
        company_id=None,
        role_codes=frozenset({"OPERATION"}),
        permission_codes=frozenset({"lead.dispatch"}),
        session_version=1,
    )
    assignment = dispatch_lead(
        db,
        lead_id=lead.id,
        company_id=companies[0].id,
        principal=principal,
        idempotency_key="legacy-fixed-v2-dispatch",
    )
    assert assignment.points_price == 260
    assert assignment.price_version == 2
    assert assignment.price_rule_id is None
