from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import event, select

from apps.api.src.core.enums import VerificationTaskStatus
from apps.api.src.core.models import (
    AuditLog,
    Company,
    Lead,
    Notification,
    PointsAccount,
    Region,
    User,
    VerificationSubmission,
    VerificationTask,
)
from apps.api.src.core.models_v12 import CompanyLeadCapability, CompanyServiceAreaV12
from apps.api.src.core.security import encrypt_text, hash_phone
from apps.api.src.core.v12_enums import LeadV12Status, VerificationTaskType
from apps.api.src.services.auth_service import create_internal_user
from apps.api.src.services.verification_service import publish_template


def _login(client, username: str, password: str) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    token = response.cookies.get("access_token")
    assert token
    return {"Authorization": f"Bearer {token}"}


def _data(response):
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["code"] == "OK"
    return payload["data"]


def test_pre_dispatch_http_flow_never_allows_telesales_to_self_assign(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        telesales = db.scalar(select(User).where(User.username == "telesales"))
        assert operation is not None and telesales is not None
        other = create_internal_user(
            db,
            username="pre-http-other",
            password="simple88",
            display_name="其他电销",
            role_code="TELESALES",
        )
        lead = Lead(
            source_type="SUPPLIER_H5",
            source_kind="SUPPLIER_H5",
            customer_name="HTTP 前置核验客户",
            phone_encrypted=encrypt_text("13900139010"),
            phone_hash=hash_phone("13900139010"),
            city="上海市",
            region_code="310000",
            category_code="OLD_RENOVATION",
            need_summary="确认装修需求真实性",
            consent_confirmed=True,
            status=LeadV12Status.PENDING_REVIEW.value,
            review_status="PENDING",
            duplicate_status="CLEAR",
            raw_payload={},
        )
        db.add(lead)
        publish_template(db, code="PRE_HTTP", name="HTTP 前置核验", schema={"fields": []})
        db.commit()
        lead_id = lead.id
        telesales_id = telesales.id
        other_id = other.id

    operation_headers = _login(client, "operation", "Operation123!")
    telesales_headers = _login(client, "telesales", "Telesales123!")
    other_headers = _login(client, "pre-http-other", "simple88")
    assigned = _data(
        client.post(
            f"/api/v1/v1.2/admin/leads/{lead_id}/pre-dispatch-verification",
            headers=operation_headers,
            json={
                "assignee_user_id": telesales_id,
                "reason": "资料描述需要电话确认",
                "template_code": "PRE_HTTP",
            },
        )
    )
    task_id = assigned["id"]
    assert assigned["lead"]["phone"] is None
    assert assigned["due_at"] is not None
    with factory() as db:
        notification = db.scalar(
            select(Notification).where(
                Notification.user_id == telesales_id,
                Notification.scene == "V12_PRE_DISPATCH_VERIFY_ASSIGNED",
            )
        )
        assert notification is not None
        assert notification.deep_link == "/h5/call/#/verify"

    own_tasks = _data(client.get("/api/v1/v1.2/pre-dispatch-verifications/tasks", headers=other_headers))
    assert own_tasks["total"] == 0
    other_start = client.post(
        f"/api/v1/v1.2/pre-dispatch-verifications/tasks/{task_id}/start",
        headers=other_headers,
    )
    assert other_start.status_code == 403

    started = _data(
        client.post(
            f"/api/v1/v1.2/pre-dispatch-verifications/tasks/{task_id}/start",
            headers=telesales_headers,
        )
    )
    assert started["lead"]["phone"] == "13900139010"
    dial = _data(
        client.post(
            f"/api/v1/v1.2/pre-dispatch-verifications/tasks/{task_id}/dial",
            headers=telesales_headers,
        )
    )
    assert dial == {"phone": "13900139010", "tel_url": "tel:13900139010"}
    submitted = _data(
        client.post(
            f"/api/v1/v1.2/pre-dispatch-verifications/tasks/{task_id}/submit",
            headers=telesales_headers,
            json={
                "contact_result": "CONNECTED",
                "conclusion": "QUALIFIED",
                "note": "客户确认本人咨询，需求明确。",
            },
        )
    )
    assert submitted["result"] == "QUALIFIED"
    telesales_detail = _data(
        client.get(
            f"/api/v1/v1.2/pre-dispatch-verifications/tasks/{task_id}",
            headers=telesales_headers,
        )
    )
    operation_detail = _data(
        client.get(
            f"/api/v1/v1.2/pre-dispatch-verifications/tasks/{task_id}",
            headers=operation_headers,
        )
    )
    for detail in (telesales_detail, operation_detail):
        verification_info = detail["verification_info"]
        assert verification_info["submitted_by"] == telesales_id
        assert verification_info["submitted_by_name"] == "电销人员"
        assert verification_info["submitted_at"]
        assert verification_info["contact_result"] == "CONNECTED"
        assert verification_info["conclusion"] == "QUALIFIED"
        assert verification_info["note"] == "客户确认本人咨询，需求明确。"
    disposition = _data(
        client.post(
            f"/api/v1/v1.2/admin/leads/{lead_id}/pre-dispatch-disposition",
            headers=operation_headers,
            json={"decision": "APPROVE_POOL", "note": "运营确认转入派发池。"},
        )
    )
    assert disposition["status"] == LeadV12Status.PUBLIC_POOL.value
    assert disposition["pending_reason"] == "PUBLIC_POOL_NO_LOCAL_RECEIVER"
    submitted_history = _data(
        client.get(
            "/api/v1/v1.2/pre-dispatch-verifications/tasks"
            "?submitted_history=true&page=1&page_size=20",
            headers=telesales_headers,
        )
    )
    history_item = next(item for item in submitted_history["items"] if item["id"] == task_id)
    assert history_item["status"] == "RELEASED"
    assert history_item["submitted_at"]
    assert history_item["conclusion"] == "QUALIFIED"
    assert history_item["lead"]["phone"] == "13900139010"
    assert "verification_info" not in history_item

    with factory() as db:
        released_task = db.get(VerificationTask, task_id)
        assert released_task is not None
        released_task.due_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    released_detail = _data(
        client.get(
            f"/api/v1/v1.2/pre-dispatch-verifications/tasks/{task_id}",
            headers=telesales_headers,
        )
    )
    assert released_detail["lead"]["phone"] == "13900139010"
    assert released_detail["is_overdue"] is False
    assert released_detail["lead"]["next_owner"] is None
    forbidden_detail = client.get(
        f"/api/v1/v1.2/pre-dispatch-verifications/tasks/{task_id}",
        headers=other_headers,
    )
    assert forbidden_detail.status_code == 403
    assert other_id != telesales_id


def test_pre_dispatch_submitted_history_orders_by_submission_and_batches_leads(api_client) -> None:
    client, factory = api_client
    now = datetime.now(timezone.utc)
    expected_ids: list[str] = []
    with factory() as db:
        telesales = db.scalar(select(User).where(User.username == "telesales"))
        assert telesales is not None
        engine = db.get_bind()
        for index in range(10):
            lead = Lead(
                source_type="PLATFORM_MANUAL",
                source_kind="PLATFORM_MANUAL",
                customer_name=f"历史排序客户 {index}",
                phone_encrypted=encrypt_text(f"139001391{index:02d}"),
                phone_hash=hash_phone(f"139001391{index:02d}"),
                city="上海市",
                region_code="310000",
                category_code="OLD_RENOVATION",
                need_summary="验证提交历史排序和批量读取",
                consent_confirmed=True,
                status=LeadV12Status.READY_DISPATCH.value,
                review_status="APPROVED",
                duplicate_status="CLEAR",
                raw_payload={},
            )
            db.add(lead)
            db.flush()
            submitted_at = now - timedelta(minutes=index)
            task = VerificationTask(
                lead_id=lead.id,
                task_type=VerificationTaskType.PRE_DISPATCH_VERIFY.value,
                status=VerificationTaskStatus.RELEASED.value,
                assignee_user_id=telesales.id,
                assigned_at=now - timedelta(minutes=20 - index),
                due_at=now - timedelta(minutes=1),
                submitted_at=submitted_at,
                contact_result="CONNECTED",
                verification_conclusion="QUALIFIED",
            )
            db.add(task)
            db.flush()
            db.add(
                VerificationSubmission(
                    task_id=task.id,
                    lead_id=lead.id,
                    result="QUALIFIED",
                    answers_json={},
                    corrections_json={},
                    note=f"第 {index} 条历史备注",
                    submitted_by=telesales.id,
                )
            )
            expected_ids.append(task.id)
        db.commit()

    telesales_headers = _login(client, "telesales", "Telesales123!")

    def fetch_with_query_count(page_size: int, page: int = 1):
        statements: list[str] = []

        def record_statement(*args) -> None:
            if args[2].lstrip().upper().startswith("SELECT"):
                statements.append(args[2])

        event.listen(engine, "before_cursor_execute", record_statement)
        try:
            response = client.get(
                "/api/v1/v1.2/pre-dispatch-verifications/tasks"
                f"?submitted_history=true&page={page}&page_size={page_size}",
                headers=telesales_headers,
            )
        finally:
            event.remove(engine, "before_cursor_execute", record_statement)
        return _data(response), len(statements)

    first_page, one_item_queries = fetch_with_query_count(1)
    full_page, ten_item_queries = fetch_with_query_count(10)
    second_page, _ = fetch_with_query_count(4, page=2)

    assert first_page["items"][0]["id"] == expected_ids[0]
    assert [item["id"] for item in full_page["items"]] == expected_ids
    assert [item["id"] for item in second_page["items"]] == expected_ids[4:8]
    assert ten_item_queries <= one_item_queries + 1


def test_verified_platform_lead_correction_recomputes_dispatch_candidates(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        telesales = db.scalar(select(User).where(User.username == "telesales"))
        assert company is not None and telesales is not None
        capability = db.scalar(
            select(CompanyLeadCapability).where(
                CompanyLeadCapability.company_id == company.id,
                CompanyLeadCapability.capability_code == "LEAD_RECEIVER",
            )
        )
        if capability is None:
            capability = CompanyLeadCapability(
                company_id=company.id,
                capability_code="LEAD_RECEIVER",
            )
            db.add(capability)
        capability.active = True
        capability.review_status = "APPROVED"
        area = db.scalar(
            select(CompanyServiceAreaV12).where(
                CompanyServiceAreaV12.company_id == company.id,
                CompanyServiceAreaV12.region_code == "310000",
            )
        )
        if area is None:
            area = CompanyServiceAreaV12(
                company_id=company.id,
                region_code="310000",
                region_level="CITY",
                is_primary_city=True,
            )
            db.add(area)
        area.active = True
        area.review_status = "APPROVED"
        account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == company.id))
        assert account is not None
        account.balance = 5000
        if db.get(Region, "110000") is None:
            db.add(
                Region(
                    code="110000",
                    name="北京市",
                    level="CITY",
                    parent_code=None,
                    aliases=["北京", "北京市"],
                    active=True,
                )
            )
        publish_template(db, code="FEEDBACK_94_E2E", name="9.4 完整流程", schema={"fields": []})
        db.commit()
        company_id = company.id
        telesales_id = telesales.id

    operation_headers = _login(client, "operation", "Operation123!")
    telesales_headers = _login(client, "telesales", "Telesales123!")
    lead = _data(
        client.post(
            "/api/v1/v1.2/platform/leads",
            headers=operation_headers,
            json={
                "customer_name": "完整流程核验客户",
                "phone": "13900139188",
                "city": "上海市",
                "region_code": "310000",
                "category_code": "OLD_RENOVATION",
                "need_summary": "客户计划在上海翻新住房",
                "consent_confirmed": True,
            },
        )
    )
    task = _data(
        client.post(
            f"/api/v1/v1.2/admin/leads/{lead['id']}/pre-dispatch-verification",
            headers=operation_headers,
            json={
                "assignee_user_id": telesales_id,
                "reason": "派发前核验客户实际施工地区",
                "template_code": "FEEDBACK_94_E2E",
            },
        )
    )
    _data(
        client.post(
            f"/api/v1/v1.2/pre-dispatch-verifications/tasks/{task['id']}/start",
            headers=telesales_headers,
        )
    )
    _data(
        client.post(
            f"/api/v1/v1.2/pre-dispatch-verifications/tasks/{task['id']}/submit",
            headers=telesales_headers,
            json={
                "contact_result": "CONNECTED",
                "conclusion": "QUALIFIED",
                "note": "客户说明实际项目地址还需运营更正。",
            },
        )
    )
    disposition = _data(
        client.post(
            f"/api/v1/v1.2/admin/leads/{lead['id']}/pre-dispatch-disposition",
            headers=operation_headers,
            json={"decision": "APPROVE_POOL", "note": "核验事实有效，进入派发池。"},
        )
    )
    assert disposition["status"] == LeadV12Status.READY_DISPATCH.value

    before = _data(
        client.get(
            f"/api/v1/v1.2/dispatch-pool/{lead['id']}/candidates",
            headers=operation_headers,
        )
    )
    before_candidate = next(
        item for item in before["candidates"] if item["company_id"] == company_id
    )
    assert before_candidate["eligible"] is True

    detail = _data(
        client.get(f"/api/v1/v1.2/platform/leads/{lead['id']}", headers=operation_headers)
    )
    corrected = _data(
        client.patch(
            f"/api/v1/v1.2/platform/leads/{lead['id']}/correction",
            headers=operation_headers,
            json={
                "region_code": "110000",
                "need_summary": "客户确认项目实际位于北京",
                "reason": "根据电销核验备注更正实际项目地区",
                "expected_snapshot_version": detail["snapshot_version"],
            },
        )
    )
    assert corrected["status"] == LeadV12Status.READY_DISPATCH.value
    assert corrected["city"] == "北京市"

    after = _data(
        client.get(
            f"/api/v1/v1.2/dispatch-pool/{lead['id']}/candidates",
            headers=operation_headers,
        )
    )
    after_candidate = next(
        item for item in after["candidates"] if item["company_id"] == company_id
    )
    assert after_candidate["eligible"] is False
    assert "SERVICE_REGION_MISMATCH" in after_candidate["exclusion_reasons"]
    pool = _data(
        client.get("/api/v1/v1.2/dispatch-pool?page=1&page_size=100", headers=operation_headers)
    )
    pool_item = next(item for item in pool["items"] if item["id"] == lead["id"])
    assert pool_item["has_verification_info"] is True
    assert pool_item["pre_dispatch_task_id"] == task["id"]


def test_overdue_pre_dispatch_task_can_be_dialed_and_reassigned(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        telesales = db.scalar(select(User).where(User.username == "telesales"))
        assert operation is not None and telesales is not None
        replacement = create_internal_user(
            db,
            username="pre-overdue-replacement",
            password="simple88",
            display_name="超时接手电销",
            role_code="TELESALES",
        )
        lead = Lead(
            source_type="SUPPLIER_H5",
            source_kind="SUPPLIER_H5",
            customer_name="拨号超时客户",
            phone_encrypted=encrypt_text("13900139012"),
            phone_hash=hash_phone("13900139012"),
            city="上海市",
            region_code="310000",
            category_code="OLD_RENOVATION",
            need_summary="提示期限后仍可继续拨打",
            consent_confirmed=True,
            status=LeadV12Status.PENDING_REVIEW.value,
            review_status="PENDING",
            duplicate_status="CLEAR",
            raw_payload={},
        )
        db.add(lead)
        publish_template(db, code="PRE_DIAL_OVERDUE", name="拨号超时核验", schema={"fields": []})
        db.commit()
        lead_id = lead.id
        telesales_id = telesales.id
        replacement_id = replacement.id

    operation_headers = _login(client, "operation", "Operation123!")
    telesales_headers = _login(client, "telesales", "Telesales123!")
    task = _data(
        client.post(
            f"/api/v1/v1.2/admin/leads/{lead_id}/pre-dispatch-verification",
            headers=operation_headers,
            json={
                "assignee_user_id": telesales_id,
                "reason": "请在期限内确认客户资料",
                "template_code": "PRE_DIAL_OVERDUE",
            },
        )
    )
    task_id = task["id"]
    _data(
        client.post(
            f"/api/v1/v1.2/pre-dispatch-verifications/tasks/{task_id}/start",
            headers=telesales_headers,
        )
    )
    with factory() as db:
        overdue_task = db.get(VerificationTask, task_id)
        assert overdue_task is not None
        overdue_task.due_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()

    detail = _data(
        client.get(
            f"/api/v1/v1.2/pre-dispatch-verifications/tasks/{task_id}",
            headers=telesales_headers,
        )
    )
    assert detail["is_overdue"] is True
    assert detail["lead"]["next_owner"] == "TELESALES"
    assert detail["lead"]["phone"] == "13900139012"
    dial = _data(
        client.post(
            f"/api/v1/v1.2/pre-dispatch-verifications/tasks/{task_id}/dial",
            headers=telesales_headers,
        )
    )
    assert dial["phone"] == "13900139012"

    reassigned = _data(
        client.post(
            f"/api/v1/v1.2/admin/leads/{lead_id}/pre-dispatch-verification",
            headers=operation_headers,
            json={
                "assignee_user_id": replacement_id,
                "reason": "原电销任务超时，运营重新安排核验",
                "template_code": "PRE_DIAL_OVERDUE",
            },
        )
    )
    assert reassigned["assignee_user_id"] == replacement_id
    with factory() as db:
        audit = db.scalar(
            select(AuditLog)
            .where(
                AuditLog.action == "V12_PRE_DISPATCH_VERIFY_ASSIGN",
                AuditLog.resource_id == task_id,
            )
            .order_by(AuditLog.created_at.desc())
        )
        assert audit is not None
        assert audit.before_json is not None
        assert audit.before_json["assignee_user_id"] == telesales_id
        assert audit.before_json["status"] == "IN_PROGRESS"
        assert audit.before_json["due_at"] is not None
        assert audit.after_json["assignee_user_id"] == replacement_id
        assert audit.after_json["status"] == "ASSIGNED"
        assert audit.after_json["due_at"] is not None
