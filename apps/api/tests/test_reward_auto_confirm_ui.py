from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import event, select

from apps.api.src.core.models import (
    Assignment,
    AssignmentEvent,
    Company,
    FollowUp,
    Lead,
    User,
)
from apps.api.src.core.security import encrypt_text, hash_phone

AUTO_CONFIRMED_EVENT = "V12_ASSIGNMENT_AUTO_CONFIRMED"


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


def _as_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)


def _add_auto_confirmed_assignments(factory) -> tuple[list[str], str]:
    processed_at = datetime(2026, 9, 13, 4, 5, tzinfo=UTC)
    with factory() as db:
        receiver = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        owner = db.scalar(select(User).where(User.username == "franchise_demo"))
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert receiver is not None and owner is not None and operation is not None

        assignment_ids: list[str] = []
        for index in range(2):
            claimed_at = processed_at - timedelta(hours=48, minutes=index + 1)
            lead = Lead(
                source_type="PLATFORM_MANUAL",
                source_kind="PLATFORM_MANUAL",
                submitter_user_id=operation.id,
                customer_name=f"48小时自动有效客户{index}",
                phone_encrypted=encrypt_text(f"139001391{index:02d}"),
                phone_hash=hash_phone(f"139001391{index:02d}"),
                status="FOLLOWING",
                review_status="APPROVED",
                current_follow_status="CONTACTED",
            )
            db.add(lead)
            db.flush()
            assignment = Assignment(
                lead_id=lead.id,
                company_id=receiver.id,
                status="FOLLOWING",
                points_price=100,
                claim_points=100,
                assigned_by=operation.id,
                claimed_at=claimed_at,
                appeal_deadline_at=claimed_at + timedelta(hours=48),
            )
            db.add(assignment)
            db.flush()
            lead.current_assignment_id = assignment.id
            db.add(
                AssignmentEvent(
                    assignment_id=assignment.id,
                    event_type=AUTO_CONFIRMED_EVENT,
                    actor_user_id=None,
                    occurred_at=processed_at,
                    payload={
                        "effective_at": (claimed_at + timedelta(hours=48)).isoformat()
                    },
                )
            )
            assignment_ids.append(assignment.id)
        db.commit()
    return assignment_ids, processed_at.isoformat()


def test_assignment_list_and_detail_project_auto_confirmation_without_n_plus_one(
    api_client,
):
    client, factory = api_client
    assignment_ids, processed_at = _add_auto_confirmed_assignments(factory)
    headers = _login(client, "franchise_demo", "Franchise123!")

    statements: list[str] = []

    def record_statement(*args) -> None:
        if "assignment_events" in args[2].lower():
            statements.append(args[2])

    engine = factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        page = _data(
            client.get(
                "/api/v1/v1.2/assignments?status=FOLLOWING&page=1&page_size=100",
                headers=headers,
            )
        )
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    items = {item["id"]: item for item in page["items"]}
    assert all(
        _as_utc(items[assignment_id]["auto_confirmed_at"]) == _as_utc(processed_at)
        for assignment_id in assignment_ids
    )
    assert all(
        _as_utc(items[assignment_id]["transaction_confirmed_at"])
        == _as_utc(items[assignment_id]["claimed_at"]) + timedelta(hours=48)
        for assignment_id in assignment_ids
    )
    assert all(
        items[assignment_id]["transaction_confirmation_policy"] == "CLAIM_48H"
        for assignment_id in assignment_ids
    )
    assert len(statements) == 1

    detail = _data(
        client.get(f"/api/v1/v1.2/assignments/{assignment_ids[0]}", headers=headers)
    )
    assert _as_utc(detail["auto_confirmed_at"]) == _as_utc(processed_at)
    assert _as_utc(detail["transaction_confirmed_at"]) == _as_utc(
        detail["claimed_at"]
    ) + timedelta(hours=48)
    assert detail["current_follow_status"] == "CONTACTED"


def test_assignment_list_accepts_one_unified_multi_status_page(api_client):
    client, factory = api_client
    assignment_ids, _ = _add_auto_confirmed_assignments(factory)
    headers = _login(client, "franchise_demo", "Franchise123!")

    page = _data(
        client.get(
            "/api/v1/v1.2/assignments?status=CLAIMED,FOLLOWING,RETURN_PENDING,COMPLETED&page=1&page_size=100",
            headers=headers,
        )
    )

    listed_ids = {item["id"] for item in page["items"]}
    assert set(assignment_ids) <= listed_ids
    assert all(
        item["status"] in {"CLAIMED", "FOLLOWING", "RETURN_PENDING", "COMPLETED"}
        for item in page["items"]
    )


def test_admin_trace_exposes_auto_confirmation_as_an_independent_event(api_client):
    client, factory = api_client
    assignment_ids, processed_at = _add_auto_confirmed_assignments(factory)
    with factory() as db:
        auto_event = db.scalar(
            select(AssignmentEvent).where(
                AssignmentEvent.assignment_id == assignment_ids[0]
            )
        )
        assert auto_event is not None
        auto_event.payload = {**auto_event.payload, "reason": "RETURN_REJECTED"}
        db.commit()
    headers = _login(client, "admin", "Admin123!")

    trace = _data(
        client.get(f"/api/v1/v1.2/trace/{assignment_ids[0]}", headers=headers)
    )
    assignment = next(
        item for item in trace["assignments"] if item["id"] == assignment_ids[0]
    )
    assert _as_utc(assignment["auto_confirmed_at"]) == _as_utc(
        assignment["claimed_at"]
    ) + timedelta(hours=48)
    assert _as_utc(assignment["transaction_confirmed_at"]) == _as_utc(
        assignment["claimed_at"]
    ) + timedelta(hours=48)
    assert assignment["current_follow_status"] == "CONTACTED"
    auto_event = next(
        item for item in trace["timeline"] if item["action"] == AUTO_CONFIRMED_EVENT
    )
    assert _as_utc(auto_event["at"]) == _as_utc(processed_at)
    assert auto_event["summary"] == "退回申请已驳回，系统认定有效并结算"


def test_admin_trace_keeps_follow_status_scoped_to_each_assignment(api_client):
    client, factory = api_client
    with factory() as db:
        receiver = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert receiver is not None and operation is not None
        lead = Lead(
            source_type="PLATFORM_MANUAL",
            source_kind="PLATFORM_MANUAL",
            submitter_user_id=operation.id,
            customer_name="历史派发跟进隔离客户",
            phone_encrypted=encrypt_text("13900139199"),
            phone_hash=hash_phone("13900139199"),
            status="COMPLETED",
            review_status="APPROVED",
            current_follow_status="DEAL",
        )
        db.add(lead)
        db.flush()
        historical = Assignment(
            lead_id=lead.id,
            company_id=receiver.id,
            status="RELEASED",
            points_price=100,
            assigned_by=operation.id,
        )
        current = Assignment(
            lead_id=lead.id,
            company_id=receiver.id,
            status="COMPLETED",
            points_price=100,
            assigned_by=operation.id,
        )
        db.add_all([historical, current])
        db.flush()
        lead.current_assignment_id = current.id
        db.add(
            FollowUp(
                assignment_id=historical.id,
                company_id=receiver.id,
                status="CONTACTED",
                note="历史接收方只联系过客户",
                created_by=operation.id,
            )
        )
        db.commit()
        lead_id = lead.id
        historical_id = historical.id
        current_id = current.id

    headers = _login(client, "admin", "Admin123!")
    trace = _data(client.get(f"/api/v1/v1.2/trace/{lead_id}", headers=headers))
    assignments = {item["id"]: item for item in trace["assignments"]}
    assert assignments[historical_id]["current_follow_status"] == "CONTACTED"
    assert assignments[current_id]["current_follow_status"] == "DEAL"


def test_h5_and_admin_explain_the_48_hour_auto_confirmation_rule() -> None:
    h5 = Path("apps/h5/public/v12-workbench.js").read_text(encoding="utf-8")
    admin = Path("apps/admin/public/v12-operations.js").read_text(encoding="utf-8")

    assert "有效认定" in h5
    assert "领取满48小时自动有效" in h5
    assert "领取后连续 48 小时未正式申请退回则自动结算入账" in h5
    assert "人工电话确认有效时及时结算" in h5
    assert "已入账奖励在退回审核通过时自动冲回" in h5
    assert "驳回则保持已入账" in h5
    assert "退回审核中" in h5
    assert "退回审核通过 · 已判无效" in h5
    assert h5.index("assignment.status==='RETURNED'") < h5.index("assignment.transaction_confirmed_at")
    assert "派发状态" in admin
    assert "接收确认" in admin
    assert "有效认定" in admin
    assert "领取满48小时自动有效" in admin
    assert "人工电话确认有效时及时结算" in admin
    assert "已入账奖励在退回通过时自动冲回" in admin
    assert "驳回则保持入账" in admin
    assert "退回审核通过 · 已判无效" in admin
    assert admin.index("assignment?.status==='RETURNED'") < admin.index("assignment?.transaction_confirmed_at")
    for source in (h5, admin):
        assert "被领取并电话确认有效后" not in source
        assert "等待领取人电话确认客资有效" not in source
        assert "满 3 个工作日" not in source


def test_observing_reward_explanation_distinguishes_future_and_due_times() -> None:
    source = Path("apps/h5/public/v12-workbench.js").read_text(encoding="utf-8")
    function_source = source[
        source.index("function rewardExplanation") : source.index("const REWARD_FILTERS")
    ]
    script = f"""
const fmt=value=>value;
const rewardReason=()=>'';
Date.now=()=>Date.parse('2026-09-13T04:00:00Z');
{function_source}
console.log(JSON.stringify({{
  future:rewardExplanation({{status:'OBSERVING',reward_due_at:'2026-09-13T05:00:00Z'}}),
  due:rewardExplanation({{status:'OBSERVING',reward_due_at:'2026-09-13T03:00:00Z'}}),
}}));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    result = json.loads(completed.stdout)
    assert "等待人工确认或到期自动结算" in result["future"]
    assert "结算条件时间已到" in result["due"]
    assert "无正式退回申请" in result["due"]
