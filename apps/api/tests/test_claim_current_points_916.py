from __future__ import annotations

from sqlalchemy import select

from apps.api.src.core.auth import Principal
from apps.api.src.core.models import Lead, LeadPriceRule, PointsAccount, User
from apps.api.src.core.security import encrypt_text, hash_phone
from apps.api.src.schemas.company import CompanyCreateBody
from apps.api.src.services.claim_service import claim_assignment, own_assignment_detail
from apps.api.src.services.company_service import create_company
from apps.api.src.services.dispatch_service import dispatch_lead
from apps.api.src.services.lead_points_v12 import update_lead_points_settings
from apps.api.src.services.points_service import change_points, points_available_for_dispatch


def _principal(user_id: str, company_id: str | None, role: str) -> Principal:
    permissions = {
        "FRANCHISE_OWNER": {"assignment.own.claim", "lead.own.phone.read"},
        "OPERATION": {"lead.dispatch"},
    }[role]
    return Principal(
        user_id=user_id,
        display_name=role,
        company_id=company_id,
        role_codes=frozenset({role}),
        permission_codes=frozenset(permissions),
        session_version=1,
    )


def test_legacy_claim_uses_latest_global_price_and_replay_keeps_actual_charge(db) -> None:
    actor = User(display_name="实时积分测试管理员", status="ACTIVE")
    db.add(actor)
    db.flush()
    update_lead_points_settings(
        db,
        operation_claim_points=100,
        supplier_provision_points=30,
        expected_version=0,
        updated_by=actor.id,
    )
    db.commit()
    company = create_company(
        db,
        CompanyCreateBody(
            code="CURRENT-POINTS",
            name="实时积分领取公司",
            region_codes=["310101"],
            capabilities=[{"category_code": "OLD_RENOVATION", "brand_code": None}],
        ),
    )
    company.primary_user_id = "current-points-owner"
    lead = Lead(
        customer_name="实时积分客户",
        phone_encrypted=encrypt_text("13900139807"),
        phone_hash=hash_phone("13900139807"),
        source_type="SUPPLIER_H5",
        source_kind="SUPPLIER_H5",
        region_code="310101",
        city="上海市",
        category_code="OLD_RENOVATION",
        status="QUALIFIED",
    )
    db.add_all(
        [
            lead,
            LeadPriceRule(
                region_code="310101",
                category_code="OLD_RENOVATION",
                points_cost=888,
                priority=1,
                version=1,
                status="PUBLISHED",
            ),
        ]
    )
    db.flush()
    change_points(
        db,
        company_id=company.id,
        delta=500,
        ledger_type="ADJUST",
        business_type="TEST",
        business_id="current-points-seed",
        idempotency_key="current-points-seed",
        created_by=actor.id,
    )
    assignment = dispatch_lead(
        db,
        lead_id=lead.id,
        company_id=company.id,
        principal=_principal(actor.id, None, "OPERATION"),
        idempotency_key="current-points-dispatch",
    )
    assert assignment.points_price == 100
    update_lead_points_settings(
        db,
        operation_claim_points=150,
        supplier_provision_points=30,
        expected_version=1,
        updated_by=actor.id,
    )
    db.commit()
    assert points_available_for_dispatch(db, company.id) == (500, 150, 350)

    owner = _principal("current-points-owner", company.id, "FRANCHISE_OWNER")
    detail = own_assignment_detail(db, assignment, owner)
    assert detail["points_price"] == 150
    assert detail["points"] == {"balance": 500, "pending_claim_points": 150, "available": 350}
    claimed, ledger = claim_assignment(
        db,
        assignment.id,
        owner,
        "current-points-claim",
        expected_points=150,
    )
    assert claimed.points_price == 150
    assert claimed.claim_points == 150
    assert claimed.price_rule_id is None
    assert claimed.price_version == 2
    assert ledger.delta == -150
    assert db.scalar(
        select(PointsAccount.balance).where(PointsAccount.company_id == company.id)
    ) == 350

    update_lead_points_settings(
        db,
        operation_claim_points=200,
        supplier_provision_points=30,
        expected_version=2,
        updated_by=actor.id,
    )
    db.commit()
    replayed, same_ledger = claim_assignment(
        db,
        assignment.id,
        owner,
        "current-points-claim",
        expected_points=200,
    )
    assert replayed.points_price == 150
    assert same_ledger.id == ledger.id
    assert db.scalar(
        select(PointsAccount.balance).where(PointsAccount.company_id == company.id)
    ) == 350
