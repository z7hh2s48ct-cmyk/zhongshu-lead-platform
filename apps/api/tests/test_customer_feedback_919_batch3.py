"""2026-09-19 客户反馈第三批服务端验收：S6 七列导出、S2 员工供资业绩。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from apps.api.src.core.enums import AssignmentStatus
from apps.api.src.core.models import Assignment, Lead, User
from apps.api.src.core.models_v12 import SupplierLeadReward
from apps.api.src.core.security import encrypt_text, fingerprint_phone, hash_phone
from apps.api.src.core.v12_enums import LeadSourceKind, LeadV12Status, RewardStatus
from apps.api.src.services.rbac import assign_role
from apps.api.src.services.followup_service import add_followup
from apps.api.tests.test_v12_return_workflow import _principal, _workflow_setup

HEADERS = ["姓名", "电话", "地址", "备注", "分配时间", "领取时间", "领取员工"]


def _parse_csv(text: str) -> list[list[str]]:
    import csv
    import io

    body = text.lstrip("﻿")
    return list(csv.reader(io.StringIO(body)))


def _owner_export_headers(client, principal_override_factory):
    from apps.api.src.core.auth import get_current_principal
    from apps.api.src.main import app

    app.dependency_overrides[get_current_principal] = principal_override_factory
    try:
        response = client.get("/api/v1/v1.2/company/claimed-leads/export")
        return response
    finally:
        app.dependency_overrides.pop(get_current_principal, None)


def test_owner_exports_company_claimed_leads_in_seven_columns(api_client):
    client, factory = api_client
    with factory() as db:
        setup = _workflow_setup(db)
        principal = _principal(
            setup["receiver_user"], "assignment.own.read", "lead.own.phone.read"
        )
    response = _owner_export_headers(client, lambda: principal)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    rows = _parse_csv(response.text)
    assert rows[0] == HEADERS
    assert len(rows) == 2
    data = rows[1]
    assert data[0] == "退回测试客户"
    # 负责人有权查看完整电话。
    assert data[1] == "13800138301"
    assert data[2] == "武汉市武昌区"
    assert data[3] == "计划建设两层乡墅"
    assert data[4] and data[5] and data[6] == "接收方负责人"


def test_employee_export_scopes_to_own_rows_and_masks_phone(api_client):
    client, factory = api_client
    with factory() as db:
        setup = _workflow_setup(db)
        employee_a = User(
            display_name="员工甲", status="ACTIVE", company_id=setup["receiver"].id
        )
        employee_b = User(
            display_name="员工乙", status="ACTIVE", company_id=setup["receiver"].id
        )
        db.add_all([employee_a, employee_b])
        db.flush()
        assign_role(db, employee_a, "FRANCHISE_EMPLOYEE")
        assign_role(db, employee_b, "FRANCHISE_EMPLOYEE")
        setup["assignment"].internal_assignee_user_id = employee_a.id
        db.commit()

        principal_a = _principal(employee_a, "assignment.employee.read")
        principal_b = _principal(employee_b, "assignment.employee.read")
        from apps.api.src.core.auth import get_current_principal
        from apps.api.src.main import app

        app.dependency_overrides[get_current_principal] = lambda: principal_a
        try:
            own = client.get("/api/v1/v1.2/company/claimed-leads/export")
        finally:
            app.dependency_overrides.pop(get_current_principal, None)
        assert own.status_code == 200, own.text
        own_rows = _parse_csv(own.text)
        assert own_rows[0] == HEADERS
        assert len(own_rows) == 2
        # 员工无完整电话权限 → 脱敏展示。
        assert own_rows[1][1] == "138****8301"
        assert own_rows[1][6] == "员工甲"

        app.dependency_overrides[get_current_principal] = lambda: principal_b
        try:
            others = client.get("/api/v1/v1.2/company/claimed-leads/export")
        finally:
            app.dependency_overrides.pop(get_current_principal, None)
        others_rows = _parse_csv(others.text)
        assert len(others_rows) == 1  # 只有表头，无越权数据。


def test_supplier_performance_report_aggregates_by_employee(api_client):
    client, factory = api_client
    with factory() as db:
        setup = _workflow_setup(db)
        receiver = setup["receiver"]
        owner_user = setup["receiver_user"]
        now = datetime.now(timezone.utc)
        supplied_lead = Lead(
            source_type=LeadSourceKind.SUPPLIER_H5.value,
            source_kind=LeadSourceKind.SUPPLIER_H5.value,
            submitter_user_id=owner_user.id,
            supplier_company_id=receiver.id,
            customer_name="业绩测试客户",
            phone_encrypted=encrypt_text("13800138302"),
            phone_hash=hash_phone("13800138302"),
            phone_fingerprint=fingerprint_phone("13800138302"),
            consent_confirmed=True,
            city="武汉市",
            district="武昌区",
            region_code="420106",
            status=LeadV12Status.FOLLOWING.value,
            review_status="APPROVED",
            raw_payload={},
        )
        db.add(supplied_lead)
        db.flush()
        confirmed_assignment = Assignment(
            lead_id=supplied_lead.id,
            company_id=receiver.id,
            receiver_company_id=receiver.id,
            supplier_company_id=receiver.id,
            status=AssignmentStatus.FOLLOWING.value,
            points_price=100,
            claim_points=100,
            lead_snapshot={},
            assigned_by=owner_user.id,
            assigned_at=now,
            claimed_at=now,
            internal_assignee_user_id=owner_user.id,
            internal_assigned_by=owner_user.id,
            idempotency_key=f"perf-{receiver.id}",
        )
        db.add(confirmed_assignment)
        db.flush()
        credited_reward = SupplierLeadReward(
            lead_id=supplied_lead.id,
            assignment_id=confirmed_assignment.id,
            supplier_company_id=receiver.id,
            receiver_company_id=setup["supplier"].id,
            status=RewardStatus.SETTLED.value,
            claim_points=100,
            reward_ratio_bps=3000,
            reward_points=30,
            rule_version=1,
            ledger_id=setup["claim_ledger"].id,
        )
        reversed_lead = Lead(
            source_type=LeadSourceKind.SUPPLIER_H5.value,
            source_kind=LeadSourceKind.SUPPLIER_H5.value,
            submitter_user_id=owner_user.id,
            supplier_company_id=receiver.id,
            customer_name="冲回测试客户",
            phone_encrypted=encrypt_text("13800138303"),
            phone_hash=hash_phone("13800138303"),
            phone_fingerprint=fingerprint_phone("13800138303"),
            consent_confirmed=True,
            city="武汉市",
            district="武昌区",
            region_code="420106",
            status=LeadV12Status.FOLLOWING.value,
            review_status="APPROVED",
            raw_payload={},
        )
        db.add(reversed_lead)
        db.flush()
        reversed_assignment = Assignment(
            lead_id=reversed_lead.id,
            company_id=receiver.id,
            receiver_company_id=receiver.id,
            supplier_company_id=receiver.id,
            status=AssignmentStatus.FOLLOWING.value,
            points_price=100,
            claim_points=100,
            lead_snapshot={},
            assigned_by=owner_user.id,
            assigned_at=now,
            claimed_at=now,
            internal_assignee_user_id=owner_user.id,
            internal_assigned_by=owner_user.id,
            idempotency_key=f"perf-2-{receiver.id}",
        )
        db.add(reversed_assignment)
        db.flush()
        reversed_reward = SupplierLeadReward(
            lead_id=reversed_lead.id,
            assignment_id=reversed_assignment.id,
            supplier_company_id=receiver.id,
            receiver_company_id=setup["supplier"].id,
            status=RewardStatus.REVERSED.value,
            claim_points=100,
            reward_ratio_bps=3000,
            reward_points=20,
            rule_version=1,
            ledger_id=setup["claim_ledger"].id,
            reversal_ledger_id=setup["claim_ledger"].id,
        )
        db.add_all([credited_reward, reversed_reward])
        supplied_lead.current_assignment_id = confirmed_assignment.id
        db.flush()
        # 人工确认有效：DEAL 跟进记录驱动交易确认表达式。
        add_followup(
            db,
            assignment=confirmed_assignment,
            principal=_principal(owner_user, "followup.own.manage"),
            status="DEAL",
            note="客户确认有效，提前确认完成",
            next_followup_at=None,
        )
        # 人工确认时间回拨一小时，确保 SQL 侧 <= now() 稳定成立。
        from apps.api.src.core.models import FollowUp

        followup = db.scalar(select(FollowUp).where(FollowUp.assignment_id == confirmed_assignment.id))
        followup.created_at = now - timedelta(hours=1)
        db.commit()
        principal = _principal(owner_user, "assignment.own.read", "supplier.reward.own.read")

        from apps.api.src.core.auth import get_current_principal
        from apps.api.src.main import app

        app.dependency_overrides[get_current_principal] = lambda: principal
        try:
            response = client.get("/api/v1/v1.2/reports/supplier-performance")
        finally:
            app.dependency_overrides.pop(get_current_principal, None)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["scope"] == "company"
        assert data["summary"]["uploaded"] == 2
        assert data["summary"]["effective"] == 1
        # 累计入账 30+20，冲回 20，净贡献 10；公司提现不影响员工贡献。
        assert data["summary"]["points_credited"] == 50
        assert data["summary"]["points_reversed"] == 20
        assert data["summary"]["points_net"] == 30
        owner_rows = [
            row for row in data["employees"] if row["user_id"] == owner_user.id
        ]
        assert owner_rows and owner_rows[0]["uploaded"] == 2


def test_supplier_performance_employee_sees_only_self(api_client):
    client, factory = api_client
    with factory() as db:
        setup = _workflow_setup(db)
        employee = User(
            display_name="普通员工", status="ACTIVE", company_id=setup["receiver"].id
        )
        db.add(employee)
        db.flush()
        assign_role(db, employee, "FRANCHISE_EMPLOYEE")
        db.commit()
        principal = _principal(employee, "assignment.employee.read")
        from apps.api.src.core.auth import get_current_principal
        from apps.api.src.main import app

        app.dependency_overrides[get_current_principal] = lambda: principal
        try:
            response = client.get("/api/v1/v1.2/reports/supplier-performance")
        finally:
            app.dependency_overrides.pop(get_current_principal, None)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["scope"] == "employee"
        assert [row["user_id"] for row in data["employees"]] == [employee.id]
