from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from apps.api.src.core.models import Assignment, AuditLog, Notification, User

from apps.api.tests.test_customer_feedback_910_dispatch import _login, _prepare_receiver_and_lead


ADMIN_JS = Path(__file__).resolve().parents[2] / "admin" / "public" / "v12-operations.js"


def test_manual_dispatch_falls_back_to_owner_when_company_has_no_active_employee(api_client) -> None:
    client, factory = api_client
    lead_id, company_id, employee_id, owner_id = _prepare_receiver_and_lead(
        factory,
        phone="13900139711",
    )
    with factory() as db:
        employee = db.get(User, employee_id)
        assert employee is not None
        employee.status = "DISABLED"
        db.commit()

    response = client.post(
        f"/api/v1/v1.2/dispatch-pool/{lead_id}/dispatch",
        headers=_login(client, "operation", "Operation123!"),
        json={
            "company_id": company_id,
            "idempotency_key": "feedback-911-owner-fallback",
        },
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["internal_assignee_user_id"] == owner_id
    replay = client.post(
        f"/api/v1/v1.2/dispatch-pool/{lead_id}/dispatch",
        headers=_login(client, "operation", "Operation123!"),
        json={
            "company_id": company_id,
            "idempotency_key": "feedback-911-owner-fallback",
        },
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["id"] == data["id"]
    with factory() as db:
        assignment = db.scalar(select(Assignment).where(Assignment.id == data["id"]))
        assert assignment is not None
        assert assignment.internal_assignee_user_id == owner_id
        notifications = db.scalars(
            select(Notification).where(
                Notification.scene == "V12_ASSIGNMENT_DISPATCHED",
                Notification.user_id == owner_id,
            )
        ).all()
        assert len(notifications) == 1
        audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "V12_MANUAL_DISPATCH",
                AuditLog.resource_id == data["id"],
            )
        )
        assert audit is not None
        assert audit.after_json["employee_user_id"] is None
        assert audit.after_json["recipient_user_id"] == owner_id
        assert audit.after_json["recipient_role_code"] == "FRANCHISE_OWNER"

    claimed = client.post(
        f"/api/v1/v1.2/assignments/{data['id']}/claim",
        headers=_login(client, "franchise_demo", "Franchise123!"),
    )
    assert claimed.status_code == 200, claimed.text


def test_operation_ui_submits_owner_fallback_when_no_employee_exists() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")

    assert "该加盟商暂无在职员工，客资将直接派发给负责人" in source
    assert "employee_user_id:employeeUserId||null" in source
    assert "接收负责人" in source


def test_owner_can_refuse_an_owner_fallback_assignment(api_client) -> None:
    client, factory = api_client
    lead_id, company_id, employee_id, _owner_id = _prepare_receiver_and_lead(
        factory,
        phone="13900139712",
    )
    with factory() as db:
        employee = db.get(User, employee_id)
        assert employee is not None
        employee.status = "DISABLED"
        db.commit()

    dispatched = client.post(
        f"/api/v1/v1.2/dispatch-pool/{lead_id}/dispatch",
        headers=_login(client, "operation", "Operation123!"),
        json={
            "company_id": company_id,
            "idempotency_key": "feedback-911-owner-refusal",
        },
    )
    assert dispatched.status_code == 200, dispatched.text

    refused = client.post(
        f"/api/v1/v1.2/assignments/{dispatched.json()['data']['id']}/refuse",
        headers=_login(client, "franchise_demo", "Franchise123!"),
        json={"reason": "负责人确认当前无法承接"},
    )

    assert refused.status_code == 200, refused.text
    assert refused.json()["data"]["status"] == "RELEASED"
