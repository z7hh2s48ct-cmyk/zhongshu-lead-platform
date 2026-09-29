from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from apps.api.src.core.models import Lead, User, VerificationSubmission, VerificationTask
from apps.api.src.core.security import encrypt_text, hash_phone
from apps.api.src.core.v12_enums import LeadV12Status, VerificationTaskType


ROOT = Path(__file__).resolve().parents[3]
OPERATIONS_JS = ROOT / "apps/admin/public/v12-operations.js"


def _login(client, username: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    token = response.cookies.get("access_token")
    assert token
    return {"Authorization": f"Bearer {token}"}


def _data(response):
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["code"] == "OK"
    return payload["data"]


def _seed_verification_history(factory) -> tuple[str, str]:
    with factory() as db:
        telesales = db.scalar(select(User).where(User.username == "telesales"))
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert telesales is not None and operation is not None
        lead = Lead(
            source_type="PLATFORM_MANUAL",
            source_kind="PLATFORM_MANUAL",
            customer_name="跨状态可见客户",
            phone_encrypted=encrypt_text("13900139929"),
            phone_hash=hash_phone("13900139929"),
            province="安徽省",
            city="阜阳市",
            district="颍州区",
            region_code="341202",
            category_code="SELF_BUILD",
            need_summary="自建房设计与施工",
            consent_confirmed=True,
            status=LeadV12Status.PENDING_TELESALES_VERIFY.value,
            review_status="PENDING",
            duplicate_status="CLEAR",
            raw_payload={},
        )
        db.add(lead)
        db.flush()
        old_task = VerificationTask(
            lead_id=lead.id,
            task_type=VerificationTaskType.PRE_DISPATCH_VERIFY.value,
            status="RELEASED",
            assignee_user_id=telesales.id,
            assigned_by=operation.id,
            contact_result="NO_ANSWER",
            verification_conclusion="UNVERIFIABLE",
        )
        current_task = VerificationTask(
            lead_id=lead.id,
            task_type=VerificationTaskType.PRE_DISPATCH_VERIFY.value,
            status="IN_PROGRESS",
            assignee_user_id=telesales.id,
            assigned_by=operation.id,
        )
        db.add_all([old_task, current_task])
        db.flush()
        db.add(
            VerificationSubmission(
                task_id=old_task.id,
                lead_id=lead.id,
                result="UNVERIFIABLE",
                answers_json={},
                corrections_json={},
                note="首次无人接听，已安排再次核验",
                submitted_by=telesales.id,
            )
        )
        db.commit()
        return lead.id, current_task.id


def test_operation_task_and_lead_details_include_full_verification_history(api_client) -> None:
    client, factory = api_client
    lead_id, task_id = _seed_verification_history(factory)
    headers = _login(client, "operation", "Operation123!")

    task = _data(
        client.get(
            f"/api/v1/v1.2/pre-dispatch-verifications/tasks/{task_id}",
            headers=headers,
        )
    )
    assert task["lead"] == {
        **task["lead"],
        "customer_name": "跨状态可见客户",
        "phone": "13900139929",
        "city": "阜阳市",
        "district": "颍州区",
        "need_summary": "自建房设计与施工",
    }
    assert task["assignee_name"] == "电销人员"
    assert len(task["verification_history"]) == 2
    assert task["verification_history"][0]["note"] == "首次无人接听，已安排再次核验"
    assert task["verification_history"][1]["status"] == "IN_PROGRESS"

    lead = _data(
        client.get(f"/api/v1/v1.2/admin/leads/{lead_id}", headers=headers)
    )
    assert len(lead["pre_dispatch_history"]) == 2
    assert lead["pre_dispatch_history"][0]["assignee_name"] == "电销人员"
    assert lead["pre_dispatch_history"][0]["note"] == "首次无人接听，已安排再次核验"
    assert lead["pre_dispatch_history"][1]["status"] == "IN_PROGRESS"


def test_operations_ui_exposes_read_only_cross_status_locator_and_task_detail() -> None:
    source = OPERATIONS_JS.read_text(encoding="utf-8")
    public_pool = source[source.index("async function publicPool()") : source.index("function filterLeadSubmitters")]
    telesales = source[source.index("async function telesales()") : source.index("async function openPreDispatchTask")]
    detail = source[source.index("function leadDetailBody") : source.index("function showLeadDetail")]

    assert "/v1.2/reports/leads/search" in public_pool
    assert "当前不在公海库存" in public_pool
    assert "data-public-pool-locator-detail" in public_pool
    assert "outsidePoolRows" in public_pool
    assert "data-public-pool-transfer" not in source[
        source.index("const outsidePoolRows") : source.index("const inlineForm")
    ]
    assert "data-pre-task-detail" in telesales
    assert "showPreDispatchTaskDetail" in telesales
    assert "pre_dispatch_history" in detail
    assert "历轮电销核验" in detail
