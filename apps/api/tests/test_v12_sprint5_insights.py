from __future__ import annotations

import csv
from io import StringIO
from datetime import datetime, timedelta, timezone

from sqlalchemy import event, func, select

from apps.api.src.core.models import (
    Assignment,
    AuditLog,
    Company,
    FollowUp,
    Lead,
    PointsAccount,
    PointsLedger,
    ReturnRequest,
    SupplyTerminationRequest,
    User,
)
from apps.api.src.core.models_v12 import SupplierLeadReward
from apps.api.src.core.security import encrypt_text
from apps.api.src.services.points_service import reverse_ledger


def login(client, username: str, password: str) -> None:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    token = response.cookies.get("access_token")
    assert token
    client.headers.update({"Authorization": f"Bearer {token}"})


def _contains_key(value, names: set[str]) -> bool:
    if isinstance(value, dict):
        return any(key in names or _contains_key(item, names) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_key(item, names) for item in value)
    return False


def test_superadmin_can_read_v12_overview_and_audit(api_client):
    client, _factory = api_client
    login(client, "admin", "Admin123!")

    report = client.get("/api/v1/v1.2/reports/overview")
    assert report.status_code == 200, report.text
    data = report.json()["data"]
    assert {"leads", "assignments", "returns", "supplier_rewards", "points_ledger"} <= set(data)

    audit = client.get("/api/v1/v1.2/audit-events?page=1&page_size=20")
    assert audit.status_code == 200, audit.text
    assert {"items", "total", "page", "page_size"} <= set(audit.json()["data"])


def test_superadmin_can_read_management_and_finance_decision_dashboards(api_client):
    client, _factory = api_client
    login(client, "admin", "Admin123!")

    management = client.get("/api/v1/v1.2/reports/management-dashboard?days=30")
    assert management.status_code == 200, management.text
    data = management.json()["data"]
    assert {
        "new_leads",
        "pending_verification",
        "ready_dispatch",
        "claimed",
        "effective_completion_rate",
        "returned_exceptions",
        "pending_reward_settlement",
    } <= set(data["kpis"])
    assert {"trend", "funnel", "source_distribution", "region_distribution", "provider_distribution", "exceptions"} <= set(data)

    with _factory() as db:
        company = Company(code="FINANCE-DASHBOARD", name="充值看板测试加盟商", status="ACTIVE")
        db.add(company)
        db.flush()
        account = PointsAccount(company_id=company.id, balance=345)
        db.add(account)
        db.flush()
        db.add(
            PointsLedger(
                account_id=account.id,
                company_id=company.id,
                ledger_type="RECHARGE",
                delta=500,
                balance_after=345,
                business_type="TEST",
                business_id="finance-dashboard",
                idempotency_key="finance-dashboard-recharge",
                external_reference="TEST-FINANCE-RECHARGE",
            )
        )
        db.commit()

    finance = client.get("/api/v1/v1.2/reports/finance-dashboard?days=30")
    assert finance.status_code == 200, finance.text
    finance_data = finance.json()["data"]
    assert {"pending_settlement", "settled", "disputed", "voided"} <= set(finance_data["summary"])
    assert {"trend", "source_ranking", "details"} <= set(finance_data)
    assert {"period_recharged_points", "period_recharge_count", "total_recharged_points", "total_recharge_count", "remaining_points"} <= set(finance_data["recharge_summary"])
    assert {"trend", "recent_records"} <= set(finance_data["recharge"])
    assert finance_data["recharge_summary"]["period_recharged_points"] >= 500
    assert finance_data["recharge_summary"]["total_recharge_count"] >= 1
    assert finance_data["recharge_summary"]["remaining_points"] >= 345
    assert any(record["external_reference"] == "TEST-FINANCE-RECHARGE" for record in finance_data["recharge"]["recent_records"])


def _dashboard_lead(*, name: str, created_at: datetime, status: str = "READY_DISPATCH") -> Lead:
    return Lead(
        source_type="H5",
        source_kind="SUPPLIER_H5",
        customer_name=name,
        phone_encrypted=f"encrypted-{name}",
        phone_hash=f"hash-{name}",
        status=status,
        created_at=created_at,
        updated_at=created_at,
    )


def test_management_dashboard_uses_each_new_lead_cohort_for_effective_rate(api_client) -> None:
    client, factory = api_client
    login(client, "admin", "Admin123!")
    cohort_at = datetime.now(timezone.utc) - timedelta(days=20)

    with factory() as db:
        company = db.scalar(select(Company).where(Company.status == "ACTIVE"))
        actor = db.scalar(select(User).where(User.username == "admin"))
        assert company is not None and actor is not None

        completed_lead = _dashboard_lead(name="同批已完成", created_at=cohort_at)
        pending_lead = _dashboard_lead(
            name="同批待审核",
            created_at=cohort_at,
            status="PENDING_REVIEW",
        )
        old_lead_one = _dashboard_lead(
            name="旧批次一",
            created_at=cohort_at - timedelta(days=60),
        )
        old_lead_two = _dashboard_lead(
            name="旧批次二",
            created_at=cohort_at - timedelta(days=61),
        )
        db.add_all([completed_lead, pending_lead, old_lead_one, old_lead_two])
        db.flush()

        completed_assignment = Assignment(
            lead_id=completed_lead.id,
            company_id=company.id,
            status="COMPLETED",
            points_price=100,
            assigned_by=actor.id,
        )
        old_assignments = [
            Assignment(
                lead_id=lead.id,
                company_id=company.id,
                status="FOLLOWING",
                points_price=100,
                assigned_by=actor.id,
            )
            for lead in (old_lead_one, old_lead_two)
        ]
        db.add_all([completed_assignment, *old_assignments])
        db.flush()
        db.add_all(
            [
                FollowUp(
                    assignment_id=assignment.id,
                    company_id=company.id,
                    status="DEAL",
                    created_by=actor.id,
                    created_at=cohort_at,
                )
                for assignment in old_assignments
            ]
        )
        expected_pending = int(
            db.scalar(
                select(func.count(Lead.id)).where(
                    Lead.source_kind.is_not(None),
                    Lead.status.in_(
                        (
                            "PENDING_REVIEW",
                            "PENDING_TELESALES_VERIFY",
                            "PENDING_OPERATION_DISPOSITION",
                        )
                    ),
                )
            )
            or 0
        )
        db.commit()

    response = client.get("/api/v1/v1.2/reports/management-dashboard?days=30")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    cohort = next(item for item in data["trend"] if item["date"] == cohort_at.date().isoformat())
    assert cohort == {
        "date": cohort_at.date().isoformat(),
        "new_leads": 2,
        "effective_completed": 1,
        "effective_rate": 50.0,
    }
    assert data["kpis"]["pending_verification"] == expected_pending


def test_decision_dashboards_do_not_materialize_unbounded_business_entities(api_client) -> None:
    client, _factory = api_client
    login(client, "admin", "Admin123!")
    loaded: list[str] = []
    tracked = (Lead, Assignment, ReturnRequest, SupplierLeadReward, PointsLedger)

    def remember_loaded(target, _context) -> None:
        loaded.append(type(target).__name__)

    for model in tracked:
        event.listen(model, "load", remember_loaded)
    try:
        management = client.get("/api/v1/v1.2/reports/management-dashboard?days=30")
        finance = client.get("/api/v1/v1.2/reports/finance-dashboard?days=30")
    finally:
        for model in tracked:
            event.remove(model, "load", remember_loaded)

    assert management.status_code == 200, management.text
    assert finance.status_code == 200, finance.text
    assert loaded == []


def test_finance_dashboard_nets_reversed_recharges_and_marks_recent_record(api_client) -> None:
    client, factory = api_client
    login(client, "admin", "Admin123!")
    before = client.get("/api/v1/v1.2/reports/finance-dashboard?days=30")
    assert before.status_code == 200, before.text
    baseline = before.json()["data"]["recharge_summary"]

    with factory() as db:
        actor = db.scalar(select(User).where(User.username == "admin"))
        assert actor is not None
        company = Company(code="REVERSED-DASHBOARD", name="冲正看板测试加盟商", status="ACTIVE")
        db.add(company)
        db.flush()
        account = PointsAccount(company_id=company.id, balance=500)
        db.add(account)
        db.flush()
        recharge = PointsLedger(
            account_id=account.id,
            company_id=company.id,
            ledger_type="RECHARGE",
            delta=500,
            balance_after=500,
            business_type="TEST",
            business_id="reversed-finance-dashboard",
            idempotency_key="reversed-finance-dashboard-recharge",
            external_reference="TEST-FINANCE-REVERSED",
        )
        db.add(recharge)
        db.flush()
        reverse_ledger(
            db,
            recharge,
            reason="看板冲正口径回归测试",
            idempotency_key="reversed-finance-dashboard-reversal",
            created_by=actor.id,
        )
        db.commit()

    response = client.get("/api/v1/v1.2/reports/finance-dashboard?days=30")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    summary = data["recharge_summary"]
    assert summary["period_recharged_points"] == baseline["period_recharged_points"]
    assert summary["period_recharge_count"] == baseline["period_recharge_count"]
    assert summary["total_recharged_points"] == baseline["total_recharged_points"]
    assert summary["total_recharge_count"] == baseline["total_recharge_count"]
    record = next(
        item
        for item in data["recharge"]["recent_records"]
        if item["external_reference"] == "TEST-FINANCE-REVERSED"
    )
    assert record["original_points"] == 500
    assert record["points"] == 0
    assert record["reversed"] is True
    assert record["reversed_at"]


def test_operation_can_read_report_and_audit_after_sprint5_rbac(api_client):
    client, _factory = api_client
    login(client, "operation", "Operation123!")

    assert client.get("/api/v1/v1.2/reports/overview").status_code == 200
    assert client.get("/api/v1/v1.2/audit-events").status_code == 200


def test_operation_audit_cannot_read_full_supply_termination_payment_reference(api_client):
    client, factory = api_client
    with factory() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        assert admin is not None
        company = Company(
            code="AUDIT-PAYMENT-MASK",
            name="付款审计脱敏公司",
            status="ACTIVE",
            supplier_cooperation_status="TERMINATION_PENDING",
        )
        db.add(company)
        db.flush()
        db.add(PointsAccount(company_id=company.id, balance=100))
        termination = SupplyTerminationRequest(
            company_id=company.id,
            status="APPROVED_PENDING_PAYMENT",
            reason="停止提供客资",
            requested_by=admin.id,
            payee_name_encrypted=encrypt_text("张三"),
            payee_account_encrypted=encrypt_text("6222000012345678"),
            payment_method="BANK_TRANSFER",
            blockers_json=[],
            customer_points_snapshot=100,
            supply_points_snapshot=0,
            general_points_snapshot=100,
            cash_cents_per_point_snapshot=25,
            cash_amount_cents_snapshot=2500,
        )
        db.add(termination)
        db.commit()
        termination_id = termination.id

    login(client, "admin", "Admin123!")
    payment_reference = "BANK-SENSITIVE-123456"
    paid = client.post(
        f"/api/v1/v1.2/supply-terminations/{termination_id}/payment",
        json={
            "external_reference": payment_reference,
            "paid_at": datetime.now(timezone.utc).isoformat(),
            "payment_amount_cents": 2500,
            "note": "已核对线下付款",
        },
    )
    assert paid.status_code == 200, paid.text

    login(client, "operation", "Operation123!")
    response = client.get(
        "/api/v1/v1.2/audit-events?action=V12_SUPPLY_TERMINATION_PAYMENT_RECORDED"
    )
    assert response.status_code == 200, response.text
    assert payment_reference not in response.text
    event = response.json()["data"]["items"][0]
    assert event["after"]["payment_external_reference_masked"] == (
        "*" * (len(payment_reference) - 4) + "3456"
    )
    assert "payment_external_reference" not in event["after"]


def test_audit_events_expose_the_business_operator_name(api_client):
    client, _factory = api_client
    login(client, "operation", "Operation123!")

    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200, me.text
    display_name = me.json()["data"]["display_name"]
    response = client.get("/api/v1/v1.2/audit-events?page=1&page_size=50")

    assert response.status_code == 200, response.text
    events = response.json()["data"]["items"]
    own_login = next(item for item in events if item["action"] == "AUTH_LOGIN")
    assert own_login["actor_name"] == display_name


def test_operation_overview_hides_financial_projection(api_client):
    client, _factory = api_client
    login(client, "operation", "Operation123!")

    response = client.get("/api/v1/v1.2/reports/overview")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert "points_ledger" not in data
    assert not _contains_key(data, {"net_delta", "balance", "recharge", "income", "revenue"})


def test_superadmin_overview_keeps_points_ledger_projection(api_client):
    client, _factory = api_client
    login(client, "admin", "Admin123!")

    response = client.get("/api/v1/v1.2/reports/overview")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert {"count", "net_delta"} <= set(data["points_ledger"])


def test_management_overview_exposes_business_pool_verification_and_risk_counts(api_client):
    client, _factory = api_client
    login(client, "admin", "Admin123!")

    response = client.get("/api/v1/v1.2/reports/overview")

    assert response.status_code == 200, response.text
    management = response.json()["data"]["management"]
    assert {"lead_pool", "verification", "return_verification", "exceptions"} <= set(management)
    assert {"total", "unassigned", "dispatching", "problem"} <= set(management["lead_pool"])
    assert {"pending", "in_progress", "awaiting_operation", "overdue"} <= set(management["verification"])
    assert {"pending", "in_progress", "awaiting_operation", "overdue"} <= set(management["return_verification"])


def test_operation_can_update_a_franchise_company_profile(api_client):
    client, _factory = api_client
    login(client, "operation", "Operation123!")

    companies = client.get("/api/v1/companies?page=1&page_size=1")
    assert companies.status_code == 200, companies.text
    company = companies.json()["data"]["items"][0]

    response = client.patch(
        f"/api/v1/companies/{company['id']}",
        json={"owner_name": "运营更新负责人"},
    )

    assert response.status_code == 200, response.text


def test_platform_user_can_change_own_password_and_keep_current_session(api_client):
    client, _factory = api_client
    login(client, "operation", "Operation123!")

    response = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "Operation123!", "new_password": "Operation456!"},
    )

    assert response.status_code == 200, response.text
    client.headers.pop("Authorization", None)
    assert client.get("/api/v1/auth/me").status_code == 200

    old_password_login = client.post(
        "/api/v1/auth/login",
        json={"username": "operation", "password": "Operation123!"},
    )
    assert old_password_login.status_code == 401
    assert client.post(
        "/api/v1/auth/login",
        json={"username": "operation", "password": "Operation456!"},
    ).status_code == 200


def test_platform_user_can_change_own_username_and_keep_current_session(api_client):
    client, factory = api_client
    login(client, "operation", "Operation123!")

    response = client.post(
        "/api/v1/auth/change-username",
        json={"current_password": "Operation123!", "username": "operation-renamed"},
    )

    assert response.status_code == 200, response.text
    client.headers.pop("Authorization", None)
    assert client.get("/api/v1/auth/me").json()["data"]["username"] == "operation-renamed"
    assert client.post(
        "/api/v1/auth/login",
        json={"username": "operation", "password": "Operation123!"},
    ).status_code == 401
    assert client.post(
        "/api/v1/auth/login",
        json={"username": "operation-renamed", "password": "Operation123!"},
    ).status_code == 200

    with factory() as db:
        audit = db.scalar(
            select(AuditLog).where(AuditLog.action == "AUTH_USERNAME_CHANGE").order_by(AuditLog.created_at.desc())
        )
        assert audit is not None
        assert audit.before_json == {"username": "operation"}
        assert audit.after_json == {"username": "operation-renamed"}


def test_franchise_has_company_report_but_not_platform_overview(api_client):
    client, _factory = api_client
    login(client, "franchise_demo", "Franchise123!")

    own = client.get("/api/v1/v1.2/reports/own")
    assert own.status_code == 200, own.text
    assert own.json()["data"]["company_id"]

    overview = client.get("/api/v1/v1.2/reports/overview")
    assert overview.status_code == 403


def test_own_report_owner_aggregates_company_while_employee_only_sees_self(api_client):
    client, factory = api_client
    now = datetime.now(timezone.utc)
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        operation = db.scalar(select(User).where(User.username == "operation"))
        owner = db.scalar(select(User).where(User.username == "franchise_demo"))
        employee = db.scalar(select(User).where(User.username == "franchise_employee_demo"))
        assert company is not None and operation is not None and owner is not None and employee is not None
        account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == company.id))
        assert account is not None
        owner_lead = _dashboard_lead(name="负责人录入", created_at=now)
        owner_lead.supplier_company_id = company.id
        owner_lead.submitter_user_id = owner.id
        employee_lead = _dashboard_lead(name="员工录入", created_at=now)
        employee_lead.supplier_company_id = company.id
        employee_lead.submitter_user_id = employee.id
        outsider_lead = _dashboard_lead(name="外部提供", created_at=now)
        refused_lead = _dashboard_lead(name="拒绝领取", created_at=now)
        expired_lead = _dashboard_lead(name="逾期未领取", created_at=now)
        returned_lead = _dashboard_lead(name="退回无效", created_at=now)
        db.add_all([owner_lead, employee_lead, outsider_lead, refused_lead, expired_lead, returned_lead])
        db.flush()
        employee_assignment = Assignment(
            lead_id=outsider_lead.id,
            company_id=company.id,
            receiver_company_id=company.id,
            status="CLAIMED",
            points_price=100,
            claim_points=100,
            assigned_by=operation.id,
            claimed_at=now,
            internal_assignee_user_id=employee.id,
        )
        owner_assignment = Assignment(
            lead_id=owner_lead.id,
            company_id=company.id,
            receiver_company_id=company.id,
            status="CLAIMED",
            points_price=200,
            claim_points=200,
            assigned_by=operation.id,
            claimed_at=now,
        )
        refused_assignment = Assignment(
            lead_id=refused_lead.id,
            company_id=company.id,
            receiver_company_id=company.id,
            status="RELEASED",
            points_price=100,
            claim_points=100,
            assigned_by=operation.id,
            released_at=now,
            release_reason="REFUSED_CLAIM",
        )
        expired_assignment = Assignment(
            lead_id=expired_lead.id,
            company_id=company.id,
            receiver_company_id=company.id,
            status="EXPIRED",
            points_price=100,
            claim_points=100,
            assigned_by=operation.id,
            released_at=now,
            release_reason="CLAIM_TIMEOUT",
        )
        returned_assignment = Assignment(
            lead_id=returned_lead.id,
            company_id=company.id,
            receiver_company_id=company.id,
            status="RETURNED",
            points_price=100,
            claim_points=100,
            assigned_by=operation.id,
            claimed_at=now,
        )
        db.add_all(
            [employee_assignment, owner_assignment, refused_assignment, expired_assignment, returned_assignment]
        )
        db.flush()
        db.add(
            ReturnRequest(
                assignment_id=returned_assignment.id,
                lead_id=returned_lead.id,
                company_id=company.id,
                reason_code="INVALID_PHONE",
                description="确认无效",
                status="APPROVED",
                submitted_by=owner.id,
                submitted_at=now,
                reviewed_at=now,
            )
        )
        db.add_all(
            [
                PointsLedger(
                    account_id=account.id,
                    company_id=company.id,
                    ledger_type="CLAIM",
                    delta=-100,
                    balance_after=900,
                    business_type="ASSIGNMENT",
                    business_id=employee_assignment.id,
                    idempotency_key="own-report-employee-claim",
                    created_by=owner.id,
                    created_at=now,
                ),
                PointsLedger(
                    account_id=account.id,
                    company_id=company.id,
                    ledger_type="CLAIM",
                    delta=-200,
                    balance_after=700,
                    business_type="ASSIGNMENT",
                    business_id=owner_assignment.id,
                    idempotency_key="own-report-owner-claim",
                    created_by=owner.id,
                    created_at=now,
                ),
            ]
        )
        db.commit()

    login(client, "franchise_demo", "Franchise123!")
    owner_report = client.get("/api/v1/v1.2/reports/own?period=month")
    assert owner_report.status_code == 200, owner_report.text
    owner_data = owner_report.json()["data"]
    assert owner_data["scope"] == "company"
    assert owner_data["supplier_leads"]["total"] >= 2
    assert owner_data["received_assignments"]["total"] >= 5
    assert owner_data["exception_breakdown"]["refused_claim"] == 1
    assert owner_data["exception_breakdown"]["return_requested"] >= 1
    assert owner_data["exception_breakdown"]["confirmed_invalid"] >= 1
    assert owner_data["points"]["consumed_points"] >= 300
    client.post("/api/v1/auth/logout")

    login(client, "franchise_employee_demo", "Employee123!")
    employee_report = client.get("/api/v1/v1.2/reports/own?period=month")
    assert employee_report.status_code == 200, employee_report.text
    employee_data = employee_report.json()["data"]
    assert employee_data["scope"] == "employee"
    assert employee_data["supplier_leads"]["by_status"].get("READY_DISPATCH") == 1
    assert employee_data["received_assignments"]["by_status"].get("CLAIMED") == 1
    assert employee_data["exception_breakdown"]["refused_claim"] == 0
    assert employee_data["exception_breakdown"]["confirmed_invalid"] == 0
    assert employee_data["points"]["consumed_points"] == 100
    client.post("/api/v1/auth/logout")

    login(client, "operation", "Operation123!")
    companies = client.get("/api/v1/companies?page=1&page_size=20")
    assert companies.status_code == 200, companies.text
    company_row = next(item for item in companies.json()["data"]["items"] if item["id"] == company.id)
    assert {"provided", "received", "exception_breakdown"} <= set(company_row)
    assert company_row["provided"]["total"] >= 2
    assert company_row["received"]["total"] >= 5
    assert company_row["exception_breakdown"]["refused_claim"] == 1
    assert company_row["exception_breakdown"]["return_requested"] >= 1
    assert company_row["exception_breakdown"]["confirmed_invalid"] >= 1


def test_operation_can_export_bounded_lead_csv_with_refusal_and_return_columns(api_client):
    client, factory = api_client
    now = datetime.now(timezone.utc)
    dangerous_name = '=HYPERLINK("https://example.test","CSV导出客户")'
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        operation = db.scalar(select(User).where(User.username == "operation"))
        owner = db.scalar(select(User).where(User.username == "franchise_demo"))
        assert company is not None and operation is not None and owner is not None
        lead = _dashboard_lead(name=dangerous_name, created_at=now)
        lead.source_kind = "PLATFORM_MANUAL"
        lead.source_channel = "\t+运营手工"
        lead.submitter_user_id = operation.id
        lead.review_status = "APPROVED"
        lead.current_follow_status = "RETURN_PENDING"
        db.add(lead)
        db.flush()
        lead_id = lead.id
        assignment = Assignment(
            lead_id=lead.id,
            company_id=company.id,
            receiver_company_id=company.id,
            status="RELEASED",
            points_price=100,
            claim_points=100,
            assigned_by=operation.id,
            released_at=now,
            release_reason="REFUSED_CLAIM",
        )
        db.add(assignment)
        db.flush()
        return_request = ReturnRequest(
            assignment_id=assignment.id,
            lead_id=lead.id,
            company_id=company.id,
            reason_code="INVALID_PHONE",
            description="号码无效",
            status="SUBMITTED",
            submitted_by=owner.id,
            submitted_at=now,
        )
        db.add(return_request)
        db.flush()
        assignment_id = assignment.id
        return_id = return_request.id
        company_id = company.id
        db.commit()

    login(client, "operation", "Operation123!")
    response = client.get(
        "/api/v1/v1.2/reports/leads/export.csv",
        params={
            "period": "month",
            "limit": 200,
            "source_kind": "PLATFORM_MANUAL",
            "receiver_company_id": company_id,
            "assignment_status": "RELEASED",
            "return_status": "SUBMITTED",
            "lead_status": "READY_DISPATCH",
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(StringIO(response.text)))
    row = next(item for item in rows if item["lead_id"] == lead_id)
    assert {
        "lead_source",
        "source_kind",
        "creator_name",
        "supplier_company",
        "receiver_company",
        "receiver_company_id",
        "assignment_id",
        "assignment_status",
        "assigned_at",
        "refusal_reason",
        "return_id",
        "return_status",
        "return_submitted_at",
        "review_status",
        "current_follow_status",
        "processing_outcome",
        "last_handled_at",
    } <= set(row)
    assert row["customer_name"] == f"'{dangerous_name}"
    assert row["lead_source"] == "'\t+运营手工"
    assert row["source_kind"] == "PLATFORM_MANUAL"
    assert row["creator_name"] == "运营管理员"
    assert row["receiver_company"] == "上海合家美宅加盟服务中心"
    assert row["receiver_company_id"] == company_id
    assert row["assignment_id"] == assignment_id
    assert row["assignment_status"] == "RELEASED"
    assert row["refusal_reason"] == "REFUSED_CLAIM"
    assert row["return_id"] == return_id
    assert row["return_status"] == "SUBMITTED"
    assert row["review_status"] == "APPROVED"
    assert row["current_follow_status"] == "RETURN_PENDING"
    assert row["processing_outcome"] == "RETURN_PENDING"
    assert row["assigned_at"]
    assert row["return_submitted_at"]
    assert row["last_handled_at"]


def test_trace_returns_not_found_for_unknown_business_id(api_client):
    client, _factory = api_client
    login(client, "admin", "Admin123!")
    response = client.get("/api/v1/v1.2/trace/unknown-business-id")
    assert response.status_code == 404
    assert response.json()["code"] == "BUSINESS_TRACE_NOT_FOUND"
