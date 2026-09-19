import pytest
from datetime import datetime, timedelta, timezone

from apps.api.src.core.auth import Principal
from apps.api.src.core.errors import AppError
from apps.api.src.core.models import Lead, LeadPriceRule
from apps.api.src.core.security import encrypt_text, hash_phone
from apps.api.src.schemas.company import CompanyCreateBody
from apps.api.src.services.claim_service import claim_assignment, run_assignment_timeouts
from apps.api.src.services.company_service import create_company
from apps.api.src.services.dispatch_service import dispatch_lead
from apps.api.src.services.points_service import change_points


def principal(uid, company_id, role):
    perms={"FRANCHISE_OWNER":{"assignment.own.claim","lead.own.phone.read"},"OPERATION":{"lead.dispatch"}}[role]
    return Principal(user_id=uid, display_name=role, company_id=company_id, role_codes=frozenset({role}), permission_codes=frozenset(perms), session_version=1)


def setup_assignment(db):
    company=create_company(db,CompanyCreateBody(code="CLM1",name="领取公司",region_codes=["310101"],capabilities=[{"category_code":"OLD_RENOVATION","brand_code":None}]))
    company.primary_user_id="owner"
    lead=Lead(customer_name="张先生",phone_encrypted=encrypt_text("13800138000"),phone_hash=hash_phone("13800138000"),region_code="310101",city="上海市",category_code="OLD_RENOVATION",status="QUALIFIED")
    db.add(lead); db.add(LeadPriceRule(region_code="310101",category_code="OLD_RENOVATION",points_cost=100,priority=1,version=1,status="PUBLISHED")); db.flush()
    change_points(db,company_id=company.id,delta=500,ledger_type="ADJUST",business_type="SEED",business_id="seed",idempotency_key="seed-claim-01",created_by=None)
    assignment=dispatch_lead(db,lead_id=lead.id,company_id=company.id,principal=principal("op",None,"OPERATION"),idempotency_key="dispatch-claim-01")
    db.commit(); return company,lead,assignment


def test_claim_is_atomic_and_idempotent(db):
    company,lead,assignment=setup_assignment(db)
    owner=principal("owner",company.id,"FRANCHISE_OWNER")
    claimed,ledger=claim_assignment(db,assignment.id,owner,"claim-key-001")
    db.commit()
    assert claimed.status=="CLAIMED"
    assert ledger.delta==-100
    again,same=claim_assignment(db,assignment.id,owner,"claim-key-001")
    assert same.id==ledger.id


def test_expired_assignment_is_released(db, monkeypatch):
    company,lead,assignment=setup_assignment(db)
    assignment.assigned_at=datetime.now(timezone.utc)-timedelta(hours=60)
    assignment.expires_at=datetime.now(timezone.utc)-timedelta(hours=1)
    db.commit()
    result=run_assignment_timeouts(db); db.commit()
    assert result["expired"]==1
    assert assignment.status=="EXPIRED"
    assert lead.current_assignment_id is None


def test_deeplink_rejects_other_company_and_inactive_assignment(db):
    from apps.api.src.services.deeplink import create_assignment_link_token, resolve_assignment_link

    company, lead, assignment = setup_assignment(db)
    token = create_assignment_link_token(assignment.id, company.id)
    owner = principal("owner", company.id, "FRANCHISE_OWNER")
    assert resolve_assignment_link(db, token, owner).id == assignment.id

    with pytest.raises(AppError) as exc:
        resolve_assignment_link(db, token, principal("other", "other-company", "FRANCHISE_OWNER"))
    assert exc.value.code == "DEEPLINK_COMPANY_MISMATCH"

    assignment.status = "RELEASED"
    lead.current_assignment_id = None
    db.commit()
    with pytest.raises(AppError) as exc:
        resolve_assignment_link(db, token, owner)
    assert exc.value.code == "DEEPLINK_ASSIGNMENT_INACTIVE"


def test_deeplink_rejects_tampering(db):
    from apps.api.src.services.deeplink import create_assignment_link_token, resolve_assignment_link

    company, _, assignment = setup_assignment(db)
    token = create_assignment_link_token(assignment.id, company.id)
    tampered = token[:-2] + ("aa" if token[-2:] != "aa" else "bb")
    with pytest.raises(AppError) as exc:
        resolve_assignment_link(db, tampered, principal("owner", company.id, "FRANCHISE_OWNER"))
    assert exc.value.code == "DEEPLINK_INVALID"
