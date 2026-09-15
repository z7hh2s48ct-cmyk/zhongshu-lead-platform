from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from apps.api.src.core.models import (
    Assignment,
    Company,
    FollowUp,
    Lead,
    Notification,
    NotificationOutbox,
    PointsAccount,
    PointsLedger,
    ReturnRequest,
    User,
    VerificationTask,
)
from apps.api.src.core.models_v12 import SupplierLeadReward
from apps.api.src.core.security import encrypt_text, fingerprint_phone, hash_phone


def _login(client, username: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    token = response.cookies.get("access_token")
    assert token
    return {"Authorization": f"Bearer {token}"}


def _reward_case(factory) -> dict[str, str]:
    observed_at = datetime(2026, 9, 14, 3, 0, tzinfo=timezone.utc)
    settled_at = datetime(2026, 9, 14, 3, 5, tzinfo=timezone.utc)
    with factory() as db:
        supplier = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        owner = db.scalar(select(User).where(User.username == "franchise_demo"))
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert supplier is not None and owner is not None and operation is not None
        receiver = Company(
            code="FEEDBACK-914-RECEIVER",
            name="九一四接收加盟商",
            status="ACTIVE",
        )
        db.add(receiver)
        db.flush()
        db.add(PointsAccount(company_id=receiver.id, balance=914))
        lead = Lead(
            source_type="SUPPLIER_H5",
            source_kind="SUPPLIER_H5",
            supplier_company_id=supplier.id,
            submitter_user_id=owner.id,
            customer_name="九一四奖励客户",
            phone_encrypted=encrypt_text("13900139140"),
            phone_hash=hash_phone("13900139140"),
            phone_fingerprint=fingerprint_phone("13900139140"),
            consent_confirmed=True,
            province="湖北省",
            city="武汉市",
            district="江夏区",
            region_code="420115",
            status="COMPLETED",
            review_status="APPROVED",
            duplicate_status="CLEAR",
            imported_at=observed_at,
            submitted_at=observed_at,
            raw_payload={},
            created_at=observed_at,
        )
        db.add(lead)
        db.flush()
        assignment = Assignment(
            lead_id=lead.id,
            company_id=receiver.id,
            receiver_company_id=receiver.id,
            supplier_company_id=supplier.id,
            status="COMPLETED",
            points_price=100,
            claim_points=100,
            price_version=1,
            lead_snapshot={},
            assigned_by=operation.id,
            assigned_at=observed_at,
            claimed_at=observed_at,
        )
        db.add(assignment)
        db.flush()
        lead.current_assignment_id = assignment.id
        db.add(
            FollowUp(
                assignment_id=assignment.id,
                company_id=receiver.id,
                status="DEAL",
                note="人工提前确认有效",
                created_by=owner.id,
                created_at=observed_at,
            )
        )
        reward = SupplierLeadReward(
            lead_id=lead.id,
            assignment_id=assignment.id,
            supplier_company_id=supplier.id,
            receiver_company_id=receiver.id,
            status="SETTLED",
            claim_points=100,
            reward_ratio_bps=3000,
            reward_points=30,
            rule_version=1,
            rule_snapshot_json={"version": 1, "ratio_bps": 3000},
            observed_at=observed_at,
            reward_due_at=observed_at,
            settled_at=settled_at,
            created_at=observed_at,
        )
        db.add(reward)
        db.commit()
        return {
            "lead_id": lead.id,
            "assignment_id": assignment.id,
            "reward_id": reward.id,
            "supplier_name": supplier.name,
            "receiver_name": receiver.name,
            "receiver_id": receiver.id,
        }


def test_supplier_reward_api_names_the_exact_lead_and_companies(api_client) -> None:
    client, factory = api_client
    case = _reward_case(factory)
    headers = _login(client, "franchise_demo", "Franchise123!")

    response = client.get(
        "/api/v1/v1.2/supplier-rewards?page=1&page_size=100",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    item = next(
        row
        for row in response.json()["data"]["items"]
        if row["id"] == case["reward_id"]
    )
    assert item["lead_id"] == case["lead_id"]
    assert item["lead_code"].startswith("KZ-")
    assert item["customer_name"] == "九一四奖励客户"
    assert item["supplier_company_name"] == case["supplier_name"]
    assert item["receiver_company_name"] == case["receiver_name"]

    detail = client.get(
        f"/api/v1/v1.2/supplier-rewards/{case['reward_id']}",
        headers=headers,
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["customer_name"] == "九一四奖励客户"


def test_daily_and_monthly_point_flow_report_exposes_per_lead_economics(api_client) -> None:
    client, factory = api_client
    case = _reward_case(factory)
    headers = _login(client, "admin", "Admin123!")

    daily = client.get(
        "/api/v1/v1.2/reports/point-flows?period=day&anchor=2026-09-14&page=1&page_size=20",
        headers=headers,
    )

    assert daily.status_code == 200, daily.text
    data = daily.json()["data"]
    item = next(row for row in data["items"] if row["lead_id"] == case["lead_id"])
    assert data["period"]["kind"] == "day"
    assert data["summary"] == {
        "new_lead_count": 1,
        "transaction_confirmed_count": 1,
        "receiver_points_consumed": 100,
        "receiver_points_refunded": 0,
        "supplier_reward_points": 30,
        "supplier_reward_reversed_points": 0,
        "platform_net_points": 70,
    }
    assert item["sequence"] == 1
    assert item["receiver_points_consumed"] == 100
    assert item["supplier_reward_points"] == 30
    assert item["platform_net_points"] == 70
    assert item["transaction_confirmed_at"] == "2026-09-14T03:00:00+00:00"
    assert item["reward_settled_at"] == "2026-09-14T03:05:00+00:00"

    monthly = client.get(
        "/api/v1/v1.2/reports/point-flows?period=month&anchor=2026-09-14&page=1&page_size=20",
        headers=headers,
    )
    assert monthly.status_code == 200, monthly.text
    assert monthly.json()["data"]["summary"]["transaction_confirmed_count"] == 1


def test_point_flow_uses_claim_plus_48h_when_cached_deadlines_are_stale(api_client) -> None:
    client, factory = api_client
    case = _reward_case(factory)
    claimed_at = datetime(2026, 9, 12, 4, 0, tzinfo=timezone.utc)
    with factory() as db:
        assignment = db.get(Assignment, case["assignment_id"])
        reward = db.get(SupplierLeadReward, case["reward_id"])
        assert assignment is not None and reward is not None
        manual = db.scalar(
            select(FollowUp).where(FollowUp.assignment_id == assignment.id)
        )
        assert manual is not None
        db.delete(manual)
        assignment.claimed_at = claimed_at
        assignment.appeal_deadline_at = claimed_at + timedelta(days=6)
        assignment.reward_due_at = claimed_at + timedelta(days=6)
        reward.appeal_deadline_at = claimed_at + timedelta(days=6)
        reward.reward_due_at = claimed_at + timedelta(days=6)
        db.commit()

    headers = _login(client, "admin", "Admin123!")
    response = client.get(
        "/api/v1/v1.2/reports/point-flows?period=day&anchor=2026-09-14&page=1&page_size=20",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    item = next(
        row for row in response.json()["data"]["items"] if row["lead_id"] == case["lead_id"]
    )
    assert item["transaction_confirmed_at"] == (
        claimed_at + timedelta(hours=48)
    ).isoformat()


def test_point_flow_report_includes_platform_lead_without_supplier_reward(api_client) -> None:
    client, factory = api_client
    case = _reward_case(factory)
    confirmed_at = datetime(2026, 9, 14, 6, 0, tzinfo=timezone.utc)
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        receiver = db.get(Company, case["receiver_id"])
        assert operation is not None and receiver is not None
        lead = Lead(
            source_type="PLATFORM_MANUAL",
            source_kind="PLATFORM_MANUAL",
            submitter_user_id=operation.id,
            customer_name="九一四平台客户",
            phone_encrypted=encrypt_text("13900139141"),
            phone_hash=hash_phone("13900139141"),
            phone_fingerprint=fingerprint_phone("13900139141"),
            consent_confirmed=True,
            province="湖北省",
            city="武汉市",
            district="江夏区",
            region_code="420115",
            status="COMPLETED",
            review_status="APPROVED",
            duplicate_status="CLEAR",
            imported_at=confirmed_at,
            submitted_at=confirmed_at,
            raw_payload={},
            created_at=confirmed_at,
        )
        db.add(lead)
        db.flush()
        assignment = Assignment(
            lead_id=lead.id,
            company_id=receiver.id,
            receiver_company_id=receiver.id,
            status="COMPLETED",
            points_price=120,
            claim_points=120,
            price_version=1,
            lead_snapshot={},
            assigned_by=operation.id,
            assigned_at=confirmed_at,
            claimed_at=confirmed_at,
            appeal_deadline_at=confirmed_at,
        )
        db.add(assignment)
        db.flush()
        lead.current_assignment_id = assignment.id
        db.add(
            FollowUp(
                assignment_id=assignment.id,
                company_id=receiver.id,
                status="DEAL",
                note="人工提前确认有效",
                created_by=operation.id,
                created_at=confirmed_at,
            )
        )
        platform_lead_id = lead.id
        db.commit()

    headers = _login(client, "operation", "Operation123!")
    response = client.get(
        "/api/v1/v1.2/reports/point-flows?period=day&anchor=2026-09-14&page=1&page_size=20",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    item = next(
        row for row in response.json()["data"]["items"] if row["lead_id"] == platform_lead_id
    )
    assert item["receiver_points_consumed"] == 120
    assert item["supplier_reward_points"] == 0
    assert item["platform_net_points"] == 120
    assert item["settlement_status"] == "NOT_APPLICABLE"
    assert item["transaction_confirmed_at"] == "2026-09-14T06:00:00+00:00"


def test_point_flow_history_survives_later_deletion_and_reward_reversal(api_client) -> None:
    client, factory = api_client
    case = _reward_case(factory)
    reversed_at = datetime(2026, 9, 14, 7, 0, tzinfo=timezone.utc)
    with factory() as db:
        lead = db.get(Lead, case["lead_id"])
        assignment = db.get(Assignment, case["assignment_id"])
        reward = db.get(SupplierLeadReward, case["reward_id"])
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert lead is not None and assignment is not None and reward is not None
        assert operation is not None
        lead.deleted_at = reversed_at + timedelta(hours=1)
        lead.current_assignment_id = None
        assignment.status = "RETURNED"
        assignment.released_at = reversed_at
        assignment.release_reason = "V12_RETURN_APPROVED"
        reward.status = "REVERSED"
        reward.reversed_at = reversed_at
        return_request = ReturnRequest(
            assignment_id=assignment.id,
            lead_id=lead.id,
            company_id=assignment.company_id,
            reason_code="EMPTY_NUMBER",
            description="确认后退回成立",
            status="APPROVED",
            submitted_by=operation.id,
            submitted_at=datetime(2026, 9, 14, 4, 0, tzinfo=timezone.utc),
            reviewed_by=operation.id,
            reviewed_at=reversed_at,
            refund_points=100,
        )
        db.add(return_request)
        db.commit()

    headers = _login(client, "admin", "Admin123!")
    response = client.get(
        "/api/v1/v1.2/reports/point-flows?period=day&anchor=2026-09-14&page=1&page_size=20",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    item = next(row for row in data["items"] if row["lead_id"] == case["lead_id"])
    assert data["summary"]["new_lead_count"] == 1
    assert data["summary"]["transaction_confirmed_count"] == 1
    assert data["summary"]["receiver_points_consumed"] == 100
    assert data["summary"]["receiver_points_refunded"] == 100
    assert data["summary"]["supplier_reward_points"] == 30
    assert data["summary"]["supplier_reward_reversed_points"] == 30
    assert data["summary"]["platform_net_points"] == 0
    assert item["settlement_status"] == "REVERSED"
    assert item["receiver_points_refunded"] == 100
    assert item["receiver_refunded_at"] == "2026-09-14T07:00:00+00:00"
    assert item["supplier_reward_points"] == 30
    assert item["supplier_reward_reversed_points"] == 30
    assert item["supplier_reward_reversed_at"] == "2026-09-14T07:00:00+00:00"
    assert item["platform_net_points"] == 0


def test_point_flow_uses_return_rejection_as_confirmation_time(api_client) -> None:
    client, factory = api_client
    case = _reward_case(factory)
    claimed_at = datetime(2026, 9, 12, 0, 0, tzinfo=timezone.utc)
    rejected_at = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    with factory() as db:
        lead = db.get(Lead, case["lead_id"])
        assignment = db.get(Assignment, case["assignment_id"])
        reward = db.get(SupplierLeadReward, case["reward_id"])
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert lead is not None and assignment is not None and reward is not None
        assert operation is not None
        lead.created_at = claimed_at
        assignment.claimed_at = claimed_at
        assignment.appeal_deadline_at = claimed_at + timedelta(hours=48)
        reward.appeal_deadline_at = claimed_at + timedelta(hours=48)
        reward.reward_due_at = claimed_at + timedelta(hours=48)
        reward.settled_at = rejected_at
        db.add(
            ReturnRequest(
                assignment_id=assignment.id,
                lead_id=lead.id,
                company_id=assignment.company_id,
                reason_code="EMPTY_NUMBER",
                description="期限内正式申请后驳回",
                status="REJECTED",
                submitted_by=operation.id,
                submitted_at=claimed_at + timedelta(hours=40),
                reviewed_by=operation.id,
                reviewed_at=rejected_at,
            )
        )
        db.commit()

    headers = _login(client, "admin", "Admin123!")
    response = client.get(
        "/api/v1/v1.2/reports/point-flows?period=day&anchor=2026-09-14&page=1&page_size=20",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    item = next(
        row for row in response.json()["data"]["items"] if row["lead_id"] == case["lead_id"]
    )
    assert item["transaction_confirmed_at"] == rejected_at.isoformat()


def test_point_flow_does_not_create_confirmation_after_lead_was_deleted(api_client) -> None:
    client, factory = api_client
    case = _reward_case(factory)
    claimed_at = datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc)
    with factory() as db:
        lead = db.get(Lead, case["lead_id"])
        assignment = db.get(Assignment, case["assignment_id"])
        reward = db.get(SupplierLeadReward, case["reward_id"])
        assert lead is not None and assignment is not None and reward is not None
        manual = db.scalar(
            select(FollowUp).where(FollowUp.assignment_id == assignment.id)
        )
        assert manual is not None
        db.delete(manual)
        assignment.claimed_at = claimed_at
        reward.status = "CANCELLED"
        reward.settled_at = None
        lead.deleted_at = claimed_at + timedelta(hours=10)
        db.commit()

    headers = _login(client, "admin", "Admin123!")
    response = client.get(
        "/api/v1/v1.2/reports/point-flows?period=day&anchor=2026-09-16&page=1&page_size=20",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert all(
        row["lead_id"] != case["lead_id"]
        for row in response.json()["data"]["items"]
    )


def test_operation_can_read_point_flows_and_company_balances(api_client) -> None:
    client, factory = api_client
    case = _reward_case(factory)
    headers = _login(client, "operation", "Operation123!")

    point_flows = client.get(
        "/api/v1/v1.2/reports/point-flows?period=day&anchor=2026-09-14&page=1&page_size=20",
        headers=headers,
    )
    assert point_flows.status_code == 200, point_flows.text
    assert any(
        row["lead_id"] == case["lead_id"]
        for row in point_flows.json()["data"]["items"]
    )

    companies = client.get(
        "/api/v1/companies?keyword=九一四接收加盟商&page=1&page_size=20",
        headers=headers,
    )
    assert companies.status_code == 200, companies.text
    item = next(
        row
        for row in companies.json()["data"]["items"]
        if row["id"] == case["receiver_id"]
    )
    assert item["points_balance"] == 914


def test_deleted_active_lead_disappears_from_receiver_workbench_but_keeps_history(api_client) -> None:
    client, factory = api_client
    case = _reward_case(factory)
    operation = _login(client, "operation", "Operation123!")
    account = client.post(
        f"/api/v1/companies/{case['receiver_id']}/accounts",
        headers=operation,
        json={
            "username": "feedback914_receiver",
            "display_name": "九一四接收负责人",
            "role_code": "FRANCHISE_OWNER",
        },
    )
    assert account.status_code == 200, account.text
    receiver_password = account.json()["data"]["initial_password"]
    receiver = _login(client, "feedback914_receiver", receiver_password)
    with factory() as db:
        receiver_user = db.scalar(
            select(User).where(User.username == "feedback914_receiver")
        )
        telesales_user = db.scalar(select(User).where(User.username == "telesales"))
        assert receiver_user is not None and telesales_user is not None
        return_request = ReturnRequest(
            assignment_id=case["assignment_id"],
            lead_id=case["lead_id"],
            company_id=case["receiver_id"],
            reason_code="EMPTY_NUMBER",
            description="删除前可见的退回草稿",
            status="DRAFT",
            submitted_by=receiver_user.id,
        )
        db.add(return_request)
        verification_task = VerificationTask(
            lead_id=case["lead_id"],
            task_type="LEAD_VERIFY_LEGACY",
            status="ASSIGNED",
            assignee_user_id=telesales_user.id,
        )
        notification = Notification(
            user_id=receiver_user.id,
            company_id=case["receiver_id"],
            scene="FEEDBACK_914_DELETE",
            title="即将删除的客资提醒",
            body="删除后不可继续展示",
            deep_link=f"/h5/v12-workbench.html?view=assignment&id={case['assignment_id']}",
            status="CREATED",
        )
        db.add_all([verification_task, notification])
        db.flush()
        outbox = NotificationOutbox(
            event_key=f"feedback-914-delete:{case['assignment_id']}",
            event_type="V12_ASSIGNMENT_REMINDER",
            aggregate_type="assignment",
            aggregate_id=case["assignment_id"],
            payload={
                "notification_id": notification.id,
                "company_id": case["receiver_id"],
                "user_id": receiver_user.id,
                "deep_link": notification.deep_link,
                "business_ids": {
                    "lead_id": case["lead_id"],
                    "assignment_id": case["assignment_id"],
                },
            },
            status="PENDING",
        )
        db.add(outbox)
        db.commit()
        return_id = return_request.id
        verification_task_id = verification_task.id
        notification_id = notification.id
        outbox_id = outbox.id
    before = client.get("/api/v1/v1.2/assignments?page=1&page_size=100", headers=receiver)
    assert before.status_code == 200, before.text
    assert any(
        row["lead_id"] == case["lead_id"] for row in before.json()["data"]["items"]
    )
    returns_before = client.get("/api/v1/v1.2/returns?page=1&page_size=100", headers=receiver)
    assert returns_before.status_code == 200, returns_before.text
    assert any(row["id"] == return_id for row in returns_before.json()["data"]["items"])

    deleted = client.request(
        "DELETE",
        f"/api/v1/v1.2/operation/leads/{case['lead_id']}",
        headers=operation,
        json={"reason": "客户要求停止后续处理"},
    )
    assert deleted.status_code == 200, deleted.text
    after = client.get("/api/v1/v1.2/assignments?page=1&page_size=100", headers=receiver)
    assert after.status_code == 200, after.text
    assert all(
        row["lead_id"] != case["lead_id"] for row in after.json()["data"]["items"]
    )
    detail = client.get(
        f"/api/v1/v1.2/assignments/{case['assignment_id']}", headers=receiver
    )
    assert detail.status_code == 404
    returns_after = client.get("/api/v1/v1.2/returns?page=1&page_size=100", headers=receiver)
    assert returns_after.status_code == 200, returns_after.text
    assert all(row["id"] != return_id for row in returns_after.json()["data"]["items"])
    return_detail = client.get(f"/api/v1/v1.2/returns/{return_id}", headers=receiver)
    assert return_detail.status_code == 404
    legacy_claim = client.get(
        f"/api/v1/claims/assignments/{case['assignment_id']}", headers=receiver
    )
    assert legacy_claim.status_code == 404
    legacy_dispatches = client.get(
        "/api/v1/dispatch/assignments?page=1&page_size=100", headers=receiver
    )
    assert legacy_dispatches.status_code == 200, legacy_dispatches.text
    assert all(
        row["id"] != case["assignment_id"]
        for row in legacy_dispatches.json()["data"]["items"]
    )
    legacy_dispatch_detail = client.get(
        f"/api/v1/dispatch/assignments/{case['assignment_id']}", headers=receiver
    )
    assert legacy_dispatch_detail.status_code == 404
    legacy_followups = client.get(
        f"/api/v1/followups/assignments/{case['assignment_id']}", headers=receiver
    )
    assert legacy_followups.status_code == 404
    legacy_returns = client.get(
        "/api/v1/returns?page=1&page_size=100", headers=receiver
    )
    assert legacy_returns.status_code == 200, legacy_returns.text
    assert all(
        row["id"] != return_id for row in legacy_returns.json()["data"]["items"]
    )
    legacy_return_detail = client.get(
        f"/api/v1/returns/{return_id}", headers=receiver
    )
    assert legacy_return_detail.status_code == 404
    legacy_draft = client.post(
        f"/api/v1/returns/assignments/{case['assignment_id']}/draft",
        headers=receiver,
        json={
            "reason_code": "EMPTY_NUMBER",
            "description": "不能重新激活已删除客资的退回",
        },
    )
    assert legacy_draft.status_code == 404
    legacy_submit = client.post(
        f"/api/v1/returns/{return_id}/submit", headers=receiver
    )
    assert legacy_submit.status_code == 404
    legacy_evidence = client.post(
        f"/api/v1/returns/{return_id}/evidence",
        headers=receiver,
        data={"evidence_type": "CHAT_SCREENSHOT"},
        files={"file": ("deleted.jpg", b"deleted", "image/jpeg")},
    )
    assert legacy_evidence.status_code == 404
    operation_history = client.get(
        "/api/v1/v1.2/returns?status=CANCELLED&page=1&page_size=100",
        headers=operation,
    )
    assert operation_history.status_code == 200, operation_history.text
    assert any(
        row["id"] == return_id
        for row in operation_history.json()["data"]["items"]
    )
    notifications = client.get(
        "/api/v1/notifications?page=1&page_size=100", headers=receiver
    )
    assert notifications.status_code == 200, notifications.text
    assert all(
        row["id"] != notification_id
        for row in notifications.json()["data"]["items"]
    )
    receiver_report = client.get("/api/v1/v1.2/reports/own", headers=receiver)
    assert receiver_report.status_code == 200, receiver_report.text
    assert receiver_report.json()["data"]["received_assignments"]["total"] == 0

    telesales = _login(client, "telesales", "Telesales123!")
    legacy_tasks = client.get(
        "/api/v1/verification/tasks?mine=true&page=1&page_size=100",
        headers=telesales,
    )
    assert legacy_tasks.status_code == 200, legacy_tasks.text
    assert all(
        row["id"] != verification_task_id
        for row in legacy_tasks.json()["data"]["items"]
    )
    legacy_task_detail = client.get(
        f"/api/v1/verification/tasks/{verification_task_id}", headers=telesales
    )
    assert legacy_task_detail.status_code == 404

    with factory() as db:
        assert db.get(Assignment, case["assignment_id"]) is not None
        assert db.get(SupplierLeadReward, case["reward_id"]) is not None
        assert db.get(NotificationOutbox, outbox_id).status == "CANCELLED"
        assert db.get(Notification, notification_id).status == "CANCELLED"


def test_lead_report_has_stable_page_sequence_and_reward_status(api_client) -> None:
    client, factory = api_client
    case = _reward_case(factory)
    headers = _login(client, "admin", "Admin123!")

    response = client.post(
        "/api/v1/v1.2/reports/leads/search",
        headers=headers,
        json={"page": 1, "page_size": 200},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    items = data["items"]
    assert [item["sequence"] for item in items] == list(range(1, len(items) + 1))
    item = next(row for row in items if row["id"] == case["lead_id"])
    assert item["supplier_reward_points"] == 30
    assert item["supplier_reward_status"] == "SETTLED"
    assert item["supplier_reward_settled_at"] == "2026-09-14T03:05:00+00:00"


def test_trace_identifies_current_assignment_without_mixing_previous_round_finance(
    api_client,
) -> None:
    client, factory = api_client
    case = _reward_case(factory)
    now = datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc)
    with factory() as db:
        lead = db.get(Lead, case["lead_id"])
        old_assignment = db.get(Assignment, case["assignment_id"])
        old_reward = db.get(SupplierLeadReward, case["reward_id"])
        receiver = db.get(Company, case["receiver_id"])
        operation = db.scalar(select(User).where(User.username == "operation"))
        account = db.scalar(
            select(PointsAccount).where(PointsAccount.company_id == case["receiver_id"])
        )
        assert all(
            item is not None
            for item in (lead, old_assignment, old_reward, receiver, operation, account)
        )
        old_assignment.status = "RETURNED"
        old_assignment.released_at = now - timedelta(days=1)
        old_reward.status = "REVERSED"
        old_reward.reversed_at = now - timedelta(days=1)
        old_return = ReturnRequest(
            assignment_id=old_assignment.id,
            lead_id=lead.id,
            company_id=receiver.id,
            reason_code="EMPTY_NUMBER",
            description="旧轮退回并退款",
            status="APPROVED",
            submitted_by=operation.id,
            submitted_at=now - timedelta(days=2),
            reviewed_at=now - timedelta(days=1),
            refund_points=100,
        )
        new_assignment = Assignment(
            lead_id=lead.id,
            company_id=receiver.id,
            receiver_company_id=receiver.id,
            supplier_company_id=lead.supplier_company_id,
            status="CLAIMED",
            points_price=120,
            claim_points=120,
            assigned_by=operation.id,
            assigned_at=now,
            claimed_at=now,
        )
        db.add_all([old_return, new_assignment])
        db.flush()
        lead.current_assignment_id = new_assignment.id
        lead.status = "CLAIMED"
        db.add_all(
            [
                PointsLedger(
                    account_id=account.id,
                    company_id=receiver.id,
                    ledger_type="RETURN",
                    delta=100,
                    balance_after=1014,
                    business_type="V12_RETURN_REFUND",
                    business_id=old_return.id,
                    idempotency_key=f"trace-old-refund:{old_return.id}",
                ),
                PointsLedger(
                    account_id=account.id,
                    company_id=receiver.id,
                    ledger_type="CLAIM",
                    delta=-120,
                    balance_after=894,
                    business_type="V12_ASSIGNMENT_CLAIM",
                    business_id=new_assignment.id,
                    idempotency_key=f"trace-new-claim:{new_assignment.id}",
                ),
            ]
        )
        db.commit()
        new_assignment_id = new_assignment.id
        old_return_id = old_return.id

    headers = _login(client, "admin", "Admin123!")
    response = client.get(f"/api/v1/v1.2/trace/{case['lead_id']}", headers=headers)
    assert response.status_code == 200, response.text
    trace = response.json()["data"]
    assert trace["lead"]["current_assignment_id"] == new_assignment_id
    assert {item["id"] for item in trace["assignments"]} == {
        case["assignment_id"],
        new_assignment_id,
    }
    assert any(item["id"] == old_return_id for item in trace["returns"])

    admin = Path("apps/admin/public/v12-operations.js").read_text(encoding="utf-8")
    assert "assignments.find(item=>item.id===lead.current_assignment_id)" in admin
    assert "returns.filter(item=>item.assignment_id===assignment.id)" in admin
    assert "rewards.filter(item=>item.assignment_id===assignment.id)" in admin
    assert "const currentLedgers=assignment?ledgers.filter" in admin


def test_mark_all_notifications_read_is_scoped_and_idempotent(api_client) -> None:
    client, factory = api_client
    headers = _login(client, "admin", "Admin123!")
    with factory() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert admin is not None and operation is not None
        db.add_all(
            [
                Notification(
                    user_id=admin.id,
                    scene=f"FEEDBACK_914_{index}",
                    title=f"九一四消息 {index}",
                    body="待批量设为已读",
                    status="CREATED",
                )
                for index in range(3)
            ]
            + [
                Notification(
                    user_id=operation.id,
                    scene="FEEDBACK_914_OTHER",
                    title="其他账号消息",
                    body="不能被当前账号修改",
                    status="CREATED",
                )
            ]
        )
        db.commit()

    marked = client.post("/api/v1/notifications/read-all", headers=headers)
    assert marked.status_code == 200, marked.text
    assert marked.json()["data"] == {"marked_count": 3, "unread_total": 0}
    repeated = client.post("/api/v1/notifications/read-all", headers=headers)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["data"] == {"marked_count": 0, "unread_total": 0}

    with factory() as db:
        other = db.scalar(
            select(Notification).where(Notification.scene == "FEEDBACK_914_OTHER")
        )
        assert other is not None and other.read_at is None


def test_employee_unread_badge_matches_visible_notifications(api_client) -> None:
    client, factory = api_client
    headers = _login(client, "franchise_employee_demo", "Employee123!")
    client.post("/api/v1/notifications/read-all", headers=headers)
    with factory() as db:
        employee = db.scalar(
            select(User).where(User.username == "franchise_employee_demo")
        )
        assert employee is not None and employee.company_id is not None
        broadcast = Notification(
            user_id=None,
            company_id=employee.company_id,
            scene="FEEDBACK_914_OWNER_BROADCAST",
            title="仅负责人可见的公司广播",
            body="员工不应产生不可清除角标",
            status="CREATED",
        )
        db.add(broadcast)
        db.commit()
        broadcast_id = broadcast.id

    visible = client.get("/api/v1/notifications?page=1&page_size=100", headers=headers)
    assert visible.status_code == 200, visible.text
    assert visible.json()["data"]["unread_total"] == 0
    assert all(
        row["id"] != broadcast_id for row in visible.json()["data"]["items"]
    )
    report = client.get("/api/v1/v1.2/reports/own", headers=headers)
    assert report.status_code == 200, report.text
    assert report.json()["data"]["unread_notifications"] == 0
    marked = client.post("/api/v1/notifications/read-all", headers=headers)
    assert marked.status_code == 200, marked.text
    assert marked.json()["data"]["marked_count"] == 0
    with factory() as db:
        assert db.get(Notification, broadcast_id).read_at is None


def test_feedback_914_frontend_contracts_are_present() -> None:
    admin = Path("apps/admin/public/v12-operations.js").read_text(encoding="utf-8")
    h5 = Path("apps/h5/public/v12-workbench.js").read_text(encoding="utf-8")

    assert "unread>99?'99+':unread" not in admin
    assert "badgeCount>99?'99+':badgeCount" not in h5
    assert "/notifications/read-all" in admin
    assert "/notifications/read-all" in h5
    assert "/master-data/regions/search?keyword=" in h5
    assert "path_codes" in h5
    assert "supplier_reward_status" in admin
    assert "point-flows" in admin
    assert "closed:['已关闭客资'" in admin
    assert "/pre-dispatch-rework-history" in admin
    assert "/reopen" in admin
    assert "provision-owner-credentials" in admin
    assert "economics:['积分统计'" in admin
    assert "submitter_user_id===S.me?.id" not in admin
    assert admin.count("rowSequence(") >= 6
    assert h5.count("rowSequence(") >= 4
    assert "status=${encodeURIComponent(statuses.join(','))}" in h5
    assert "workbenchPager([d])" in h5
    assert "bindWorkbenchPager(rewards)" in h5
    assert "receiver_points_refunded" in admin
    assert "supplier_reward_reversed_points" in admin
