from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from apps.api.src.core.enums import AssignmentStatus
from apps.api.src.core.models import Assignment, Company, Lead, PointsAccount, PointsLedger, ReturnRequest, User
from apps.api.src.core.models_v12 import SupplierLeadReward
from apps.api.src.core.security import encrypt_text, hash_phone
from apps.api.src.core.v12_enums import LeadSourceKind
from apps.api.src.services.auth_service import create_internal_user


def _login(client, username: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    token = response.cookies.get("access_token")
    assert token
    return {"Authorization": f"Bearer {token}"}


def _data(response) -> dict:
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["code"] == "OK"
    return payload["data"]


def test_franchise_h5_keeps_role_specific_two_character_navigation() -> None:
    source = Path("apps/h5/public/v12-workbench.js").read_text(encoding="utf-8")
    insights_source = Path("apps/api/src/routers/v12_insights.py").read_text(encoding="utf-8")

    assert "FRANCHISE_NAV" in source
    assert "FRANCHISE_OWNER" in source
    assert "FRANCHISE_EMPLOYEE" in source
    for label in ("首页", "接收", "供资", "跟进", "我的"):
        assert label in source
    assert "['home','leads','points','profile']" not in source
    assert "assignment.employee.read" in source
    assert '"assignment.employee.read"' in insights_source
    assert "followups" in source
    assert "grid-template-columns:repeat(${tabs.length},minmax(0,1fr))" in source


def test_franchise_business_report_uses_backend_statistics_contract() -> None:
    source = Path("apps/h5/public/v12-workbench.js").read_text(encoding="utf-8")

    assert "d.statistics?.claimed" in source
    assert "拒绝领取" in source
    assert "发起退回" in source
    assert "确认无效" in source
    assert "消耗积分" in source


def test_employee_deep_links_keep_the_direct_assignment_page() -> None:
    source = Path("apps/h5/public/v12-workbench.js").read_text(encoding="utf-8")

    assert "FRANCHISE_EMPLOYEE:[['home','home','首页'],['assignments','hand-claim','接收']" in source
    assert "if(!isFranchiseOwner()&&S.view==='assignments')" not in source
    assert "if(view==='assignments')return canReadAssignments();" in source


def test_franchise_owner_home_prioritizes_company_todos_and_internal_collaboration() -> None:
    source = Path("apps/h5/public/v12-workbench.js").read_text(encoding="utf-8")

    assert "公司待办" in source
    assert "待领取客资" in source
    assert "待补资料" in source
    assert "公司内部直接分配，无需运营审批" in source
    assert "所在地" in source
    assert "预算最低（万元）" in source
    assert "supplyBudgetToWan" in source
    assert "supplyBudgetFromWan" in source


def test_employee_own_report_hides_company_peers_and_rewards(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        owner = db.scalar(select(User).where(User.username == "franchise_demo"))
        assert company is not None and owner is not None
        employee = create_internal_user(
            db,
            username="h5_employee_scope",
            password="Employee123!",
            display_name="移动端员工范围测试",
            role_code="FRANCHISE_EMPLOYEE",
            company_id=company.id,
        )
        employee_lead = Lead(
            source_type=LeadSourceKind.SUPPLIER_H5.value,
            source_kind=LeadSourceKind.SUPPLIER_H5.value,
            supplier_company_id=company.id,
            submitter_user_id=employee.id,
            customer_name="员工供资",
            phone_encrypted=encrypt_text("13900139071"),
            phone_hash=hash_phone("13900139071"),
            status="DRAFT",
            review_status="DRAFT",
        )
        owner_lead = Lead(
            source_type=LeadSourceKind.SUPPLIER_H5.value,
            source_kind=LeadSourceKind.SUPPLIER_H5.value,
            supplier_company_id=company.id,
            submitter_user_id=owner.id,
            customer_name="负责人供资",
            phone_encrypted=encrypt_text("13900139072"),
            phone_hash=hash_phone("13900139072"),
            status="DRAFT",
            review_status="DRAFT",
        )
        db.add_all([employee_lead, owner_lead])
        db.flush()
        employee_assignment = Assignment(
            lead_id=employee_lead.id,
            company_id=company.id,
            status=AssignmentStatus.FOLLOWING.value,
            points_price=100,
            assigned_by=owner.id,
            internal_assignee_user_id=employee.id,
            internal_assigned_by=owner.id,
        )
        owner_assignment = Assignment(
            lead_id=owner_lead.id,
            company_id=company.id,
            status=AssignmentStatus.FOLLOWING.value,
            points_price=100,
            assigned_by=owner.id,
            internal_assignee_user_id=owner.id,
            internal_assigned_by=owner.id,
        )
        db.add_all([employee_assignment, owner_assignment])
        db.flush()
        account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == company.id))
        if account is None:
            account = PointsAccount(company_id=company.id, balance=1000)
            db.add(account)
            db.flush()
        db.add(
            PointsLedger(
                account_id=account.id,
                company_id=company.id,
                ledger_type="CLAIM",
                delta=-100,
                balance_after=900,
                business_type="V12_ASSIGNMENT_CLAIM",
                business_id=employee_assignment.id,
                idempotency_key="h5-employee-scope-claim",
                created_by=employee.id,
            )
        )
        db.add(
            ReturnRequest(
                assignment_id=employee_assignment.id,
                lead_id=employee_lead.id,
                company_id=company.id,
                submitted_by=employee.id,
                reason_code="EMPTY_NUMBER",
                reason_version=1,
                description="员工本人发起退回",
                status="APPROVED",
            )
        )
        reward = SupplierLeadReward(
            lead_id=owner_lead.id,
            assignment_id=owner_assignment.id,
            supplier_company_id=company.id,
            receiver_company_id=company.id,
            status="WAITING_CLAIM",
            claim_points=100,
            reward_points=30,
        )
        db.add(reward)
        db.commit()
        reward_id = reward.id

    employee = _login(client, "h5_employee_scope", "Employee123!")
    report = _data(client.get("/api/v1/v1.2/reports/own", headers=employee))

    assert report["supplier_leads"]["total"] == 1
    assert report["received_assignments"]["total"] == 1
    assert report["exception_breakdown"] == {
        "refused_claim": 0,
        "return_requested": 1,
        "confirmed_invalid": 1,
    }
    assert report["finance"]["consumed_points"] == 100
    assert report["supplier_rewards"] == {"total": 0, "by_status": {}, "points": 0}
    assert client.get("/api/v1/v1.2/supplier-rewards", headers=employee).status_code == 403
    assert client.get(f"/api/v1/v1.2/supplier-rewards/{reward_id}", headers=employee).status_code == 403
    assert client.get(f"/api/v1/points/accounts/{company.id}", headers=employee).status_code == 403

    owner = _login(client, "franchise_demo", "Franchise123!")
    owner_report = _data(client.get("/api/v1/v1.2/reports/own", headers=owner))
    assert owner_report["supplier_leads"]["total"] >= 2
    assert owner_report["received_assignments"]["total"] >= 2
    assert owner_report["exception_breakdown"]["return_requested"] >= 1
    assert owner_report["finance"]["consumed_points"] >= 100
