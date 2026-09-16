from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event

import pytest
from sqlalchemy import event, func, select

from apps.api.src.core import models_v12 as _models_v12  # noqa: F401
from apps.api.src.core.auth import Principal
from apps.api.src.core.enums import VerificationTaskStatus
from apps.api.src.core.models import Lead, User, VerificationSubmission, VerificationTask
from apps.api.src.core.models import Assignment, Company, SupplyTerminationRequest
from apps.api.src.core.security import encrypt_text, hash_phone
from apps.api.src.core.v12_enums import LeadV12Status, VerificationTaskType
from apps.api.src.services.auth_service import create_internal_user
from apps.api.src.services.verification_service import publish_template
from apps.api.tests.test_return_concurrency_postgres import (
    postgres_factory as postgres_factory,
)


def _principal(user: User, *permissions: str) -> Principal:
    return Principal(
        user_id=user.id,
        display_name=user.display_name,
        company_id=user.company_id,
        role_codes=frozenset(role.code for role in user.roles),
        permission_codes=frozenset(permissions),
        session_version=user.session_version,
    )


def _lead(*, customer_name: str, phone: str, status: LeadV12Status) -> Lead:
    return Lead(
        source_type="PLATFORM_MANUAL",
        source_kind="PLATFORM_MANUAL",
        customer_name=customer_name,
        phone_encrypted=encrypt_text(phone),
        phone_hash=hash_phone(phone),
        province="安徽省",
        city="阜阳市",
        district="颍州区",
        region_code="341202",
        category_code="SELF_BUILD",
        need_summary="反馈 9.15 回归测试",
        consent_confirmed=True,
        status=status.value,
        review_status="PENDING",
        duplicate_status="CLEAR",
        raw_payload={},
    )


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


@pytest.mark.parametrize("conclusion", ["UNVERIFIABLE", "INVALID", "INFO_INCOMPLETE"])
def test_unqualified_verification_cannot_be_approved_into_dispatch_pool(api_client, conclusion):
    client, session_factory = api_client
    with session_factory() as db:
        lead = _lead(customer_name="仍未核验通过", phone="13955559991",
            status=LeadV12Status.PENDING_OPERATION_DISPOSITION)
        lead.pending_reason = f"PRE_DISPATCH_REVERIFY_{conclusion}"
        db.add(lead)
        db.flush()
        task = VerificationTask(lead_id=lead.id, template_version=1,
            task_type=VerificationTaskType.PRE_DISPATCH_VERIFY.value,
            status="SUBMITTED", verification_conclusion=conclusion,
            contact_result="NO_ANSWER", submitted_at=datetime.now(timezone.utc))
        db.add(task)
        db.commit()
        lead_id = lead.id
    headers = _login(client, "operation", "Operation123!")
    response = client.post(f"/api/v1/v1.2/admin/leads/{lead_id}/pre-dispatch-disposition",
        headers=headers, json={"decision": "APPROVE_POOL", "note": "尝试跳过未通过的电销结论"})
    assert response.status_code == 409
    assert response.json()["code"] == "PRE_DISPATCH_NOT_QUALIFIED"
    with session_factory() as db:
        lead = db.get(Lead, lead_id)
        assert lead.status == LeadV12Status.PENDING_OPERATION_DISPOSITION.value
        assert lead.current_assignment_id is None


def test_overdue_pre_dispatch_task_remains_actionable_and_can_be_reassigned(db) -> None:
    from apps.api.src.services.pre_dispatch_v12 import (
        assign_pre_dispatch_task,
        start_pre_dispatch_task,
        submit_pre_dispatch_verification,
    )

    operation = create_internal_user(
        db,
        username="feedback915-operation",
        password="simple88",
        display_name="运营",
        role_code="OPERATION",
    )
    original = create_internal_user(
        db,
        username="feedback915-original",
        password="simple88",
        display_name="原电销",
        role_code="TELESALES",
    )
    replacement = create_internal_user(
        db,
        username="feedback915-replacement",
        password="simple88",
        display_name="接手电销",
        role_code="TELESALES",
    )
    lead = _lead(
        customer_name="超过期限仍可核验",
        phone="13900139501",
        status=LeadV12Status.PENDING_REVIEW,
    )
    db.add(lead)
    publish_template(db, code="PRE_915", name="9.15 前置核验", schema={"fields": []})
    db.flush()

    task = assign_pre_dispatch_task(
        db,
        lead_id=lead.id,
        assignee_user_id=original.id,
        assigned_by=operation.id,
        reason="核验客户需求",
        template_code="PRE_915",
    ).task
    task.due_at = datetime.now(timezone.utc) - timedelta(days=3)
    db.flush()

    started = start_pre_dispatch_task(
        db,
        task_id=task.id,
        principal=_principal(original, "verification.task.start"),
    )
    assert started.status == VerificationTaskStatus.IN_PROGRESS.value

    reassigned = assign_pre_dispatch_task(
        db,
        lead_id=lead.id,
        assignee_user_id=replacement.id,
        assigned_by=operation.id,
        reason="运营改派给新的电销人员",
        template_code="PRE_915",
    ).task
    assert reassigned.id == task.id
    assert reassigned.assignee_user_id == replacement.id
    assert reassigned.status == VerificationTaskStatus.ASSIGNED.value

    start_pre_dispatch_task(
        db,
        task_id=task.id,
        principal=_principal(replacement, "verification.task.start"),
    )
    task.due_at = datetime.now(timezone.utc) - timedelta(hours=1)
    submission = submit_pre_dispatch_verification(
        db,
        task_id=task.id,
        principal=_principal(replacement, "verification.submit"),
        contact_result="CONNECTED",
        conclusion="QUALIFIED",
        note="超过原期限后仍完成核验。",
    )
    assert submission.task_id == task.id
    assert lead.status == LeadV12Status.PENDING_OPERATION_DISPOSITION.value


def test_unreachable_submission_can_be_requeued_once_without_mutating_history(db) -> None:
    from apps.api.src.services.pre_dispatch_v12 import requeue_unreachable_pre_dispatch

    operation = create_internal_user(
        db,
        username="feedback915-requeue-operation",
        password="simple88",
        display_name="运营",
        role_code="OPERATION",
    )
    telesales = create_internal_user(
        db,
        username="feedback915-requeue-telesales",
        password="simple88",
        display_name="电销",
        role_code="TELESALES",
    )
    lead = _lead(
        customer_name="无人接听重新核验",
        phone="13900139502",
        status=LeadV12Status.PENDING_OPERATION_DISPOSITION,
    )
    old_task = VerificationTask(
        lead_id="placeholder",
        task_type=VerificationTaskType.PRE_DISPATCH_VERIFY.value,
        status=VerificationTaskStatus.SUBMITTED.value,
        assignee_user_id=telesales.id,
        assigned_by=operation.id,
        assigned_at=datetime.now(timezone.utc) - timedelta(days=1),
        due_at=datetime.now(timezone.utc) - timedelta(hours=12),
        started_at=datetime.now(timezone.utc) - timedelta(days=1),
        submitted_at=datetime.now(timezone.utc) - timedelta(hours=13),
        contact_result="NO_ANSWER",
        verification_conclusion="UNVERIFIABLE",
    )
    db.add(lead)
    db.flush()
    old_task.lead_id = lead.id
    db.add(old_task)
    db.flush()
    submission = VerificationSubmission(
        task_id=old_task.id,
        lead_id=lead.id,
        result="UNVERIFIABLE",
        answers_json={},
        corrections_json={},
        note="多次拨打均无人接听",
        submitted_by=telesales.id,
    )
    db.add(submission)
    db.flush()

    first = requeue_unreachable_pre_dispatch(
        db,
        lead_id=lead.id,
        principal=_principal(operation, "lead.supplier.review"),
        reason="稍后再次联系客户",
    )
    second = requeue_unreachable_pre_dispatch(
        db,
        lead_id=lead.id,
        principal=_principal(operation, "lead.supplier.review"),
        reason="重复点击不应重复建任务",
    )

    assert first.task.id == second.task.id
    assert first.idempotent is False
    assert second.idempotent is True
    assert first.previous_task_id == old_task.id
    assert old_task.status == VerificationTaskStatus.RELEASED.value
    assert old_task.submitted_at is not None
    assert db.scalar(
        select(func.count(VerificationSubmission.id)).where(
            VerificationSubmission.task_id == old_task.id
        )
    ) == 1
    assert first.task.status == VerificationTaskStatus.PENDING.value
    assert first.task.assignee_user_id is None
    assert first.task.id != old_task.id
    assert lead.status == LeadV12Status.PENDING_TELESALES_VERIFY.value
    assert lead.pending_reason == "PRE_DISPATCH_REVERIFY_REQUIRED"
    assert db.scalar(
        select(func.count(VerificationTask.id)).where(
            VerificationTask.lead_id == lead.id,
            VerificationTask.status.in_(
                (
                    VerificationTaskStatus.PENDING.value,
                    VerificationTaskStatus.ASSIGNED.value,
                    VerificationTaskStatus.IN_PROGRESS.value,
                )
            ),
        )
    ) == 1


def test_operation_can_requeue_unreachable_submission_over_http(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        telesales = db.scalar(select(User).where(User.username == "telesales"))
        assert operation is not None and telesales is not None
        lead = _lead(
            customer_name="HTTP 无人接听重新核验",
            phone="13900139503",
            status=LeadV12Status.PENDING_OPERATION_DISPOSITION,
        )
        db.add(lead)
        db.flush()
        old_task = VerificationTask(
            lead_id=lead.id,
            task_type=VerificationTaskType.PRE_DISPATCH_VERIFY.value,
            status=VerificationTaskStatus.SUBMITTED.value,
            assignee_user_id=telesales.id,
            assigned_by=operation.id,
            submitted_at=datetime.now(timezone.utc),
            contact_result="MISSED_CALL",
            verification_conclusion="UNVERIFIABLE",
        )
        db.add(old_task)
        db.flush()
        db.add(
            VerificationSubmission(
                task_id=old_task.id,
                lead_id=lead.id,
                result="UNVERIFIABLE",
                answers_json={},
                corrections_json={},
                note="客户未接听",
                submitted_by=telesales.id,
            )
        )
        db.commit()
        lead_id = lead.id
        old_task_id = old_task.id

    headers = _login(client, "operation", "Operation123!")
    url = (
        f"/api/v1/v1.2/admin/leads/{lead_id}"
        "/pre-dispatch-verification/requeue"
    )
    first = _data(
        client.post(url, headers=headers, json={"reason": "稍后重新联系客户"})
    )
    second = _data(
        client.post(url, headers=headers, json={"reason": "重复点击保持幂等"})
    )
    assert first["previous_task_id"] == old_task_id
    assert first["task"]["status"] == VerificationTaskStatus.PENDING.value
    assert first["task"]["assignee_user_id"] is None
    assert first["idempotent"] is False
    assert second["task"]["id"] == first["task"]["id"]
    assert second["idempotent"] is True


def test_closed_unreachable_lead_cannot_bypass_reverification_with_legacy_reopen(
    api_client,
) -> None:
    client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        telesales = db.scalar(select(User).where(User.username == "telesales"))
        assert operation is not None and telesales is not None
        lead = _lead(
            customer_name="拒接客资必须重新电销核验",
            phone="13900139521",
            status=LeadV12Status.CLOSED,
        )
        db.add(lead)
        db.flush()
        db.add(
            VerificationTask(
                lead_id=lead.id,
                task_type=VerificationTaskType.PRE_DISPATCH_VERIFY.value,
                status=VerificationTaskStatus.RELEASED.value,
                assignee_user_id=telesales.id,
                assigned_by=operation.id,
                submitted_at=datetime.now(timezone.utc),
                contact_result="REFUSED",
                verification_conclusion="UNVERIFIABLE",
            )
        )
        db.commit()
        lead_id = lead.id

    headers = _login(client, "operation", "Operation123!")
    legacy = client.post(
        f"/api/v1/v1.2/admin/leads/{lead_id}/reopen",
        headers=headers,
        json={"reason": "客户后续重新来电"},
    )
    assert legacy.status_code == 409, legacy.text
    assert legacy.json()["code"] == "PRE_DISPATCH_REVERIFY_REQUIRED"

    requeued = _data(
        client.post(
            f"/api/v1/v1.2/admin/leads/{lead_id}/pre-dispatch-verification/requeue",
            headers=headers,
            json={"reason": "客户后续重新来电，先重新核验"},
        )
    )
    assert requeued["task"]["status"] == VerificationTaskStatus.PENDING.value
    assert requeued["task"]["assignee_user_id"] is None
    with factory() as db:
        refreshed = db.get(Lead, lead_id)
        assert refreshed is not None
        assert refreshed.status == LeadV12Status.PENDING_TELESALES_VERIFY.value


@pytest.mark.parametrize("conclusion", ["QUALIFIED", "UNVERIFIABLE", "INVALID", "INFO_INCOMPLETE"])
def test_closed_refused_call_reenters_telesales_before_dispatch(db, conclusion) -> None:
    from apps.api.src.core.errors import AppError
    from apps.api.src.services.pre_dispatch_v12 import (
        assign_pre_dispatch_task,
        decide_pre_dispatch_disposition,
        requeue_unreachable_pre_dispatch,
        start_pre_dispatch_task,
        submit_pre_dispatch_verification,
    )

    operation = create_internal_user(
        db,
        username="feedback915-closed-operation",
        password="simple88",
        display_name="运营",
        role_code="OPERATION",
    )
    telesales = create_internal_user(
        db,
        username="feedback915-closed-telesales",
        password="simple88",
        display_name="电销",
        role_code="TELESALES",
    )
    lead = _lead(
        customer_name="拒接后再次回拨",
        phone="13900139504",
        status=LeadV12Status.CLOSED,
    )
    old_task = VerificationTask(
        lead_id="placeholder",
        task_type=VerificationTaskType.PRE_DISPATCH_VERIFY.value,
        status=VerificationTaskStatus.RELEASED.value,
        assignee_user_id=telesales.id,
        submitted_at=datetime.now(timezone.utc) - timedelta(days=2),
        contact_result="REFUSED",
        verification_conclusion="UNVERIFIABLE",
    )
    db.add(lead)
    db.flush()
    old_task.lead_id = lead.id
    db.add(old_task)
    publish_template(db, code="PRE_915_RECALL", name="回拨重新核验", schema={"fields": []})
    db.flush()

    from apps.api.src.services.lead_supply_v12 import lead_supply_list_to_dict

    closed_view = lead_supply_list_to_dict(
        db,
        [lead],
        _principal(operation, "lead.supplier.review"),
    )[0]
    assert closed_view["can_requeue_pre_dispatch"] is True
    assert closed_view["latest_pre_dispatch_contact_result"] == "REFUSED"
    assert closed_view["latest_pre_dispatch_task_id"] == old_task.id

    requeued = requeue_unreachable_pre_dispatch(
        db,
        lead_id=lead.id,
        principal=_principal(operation, "lead.supplier.review"),
        reason="客户后续回拨，重新核验",
    )
    assert requeued.task.id != old_task.id
    assert lead.status == LeadV12Status.PENDING_TELESALES_VERIFY.value
    assert lead.current_assignment_id is None

    assigned = assign_pre_dispatch_task(
        db,
        lead_id=lead.id,
        assignee_user_id=telesales.id,
        assigned_by=operation.id,
        reason="客户回拨后重新核验",
        template_code="PRE_915_RECALL",
    ).task
    start_pre_dispatch_task(
        db,
        task_id=assigned.id,
        principal=_principal(telesales, "verification.task.start"),
    )
    submit_pre_dispatch_verification(
        db,
        task_id=assigned.id,
        principal=_principal(telesales, "verification.submit"),
        contact_result="CONNECTED",
        conclusion=conclusion,
        note="回拨后确认客户需求有效",
    )
    assert lead.pending_reason == f"PRE_DISPATCH_REVERIFY_{conclusion}"
    if conclusion != "QUALIFIED":
        with pytest.raises(AppError) as error:
            decide_pre_dispatch_disposition(db, lead_id=lead.id,
                principal=_principal(operation, "lead.supplier.review"),
                decision="APPROVE_POOL", note="尚未核验通过不能派发")
        assert error.value.code == "PRE_DISPATCH_NOT_QUALIFIED"
        assert lead.status == LeadV12Status.PENDING_OPERATION_DISPOSITION.value
        assert lead.current_assignment_id is None
        return
    decide_pre_dispatch_disposition(
        db,
        lead_id=lead.id,
        principal=_principal(operation, "lead.supplier.review"),
        decision="APPROVE_POOL",
        note="重新核验通过后进入派发池",
    )
    assert lead.status in {
        LeadV12Status.PUBLIC_POOL.value,
        LeadV12Status.READY_DISPATCH.value,
    }


def test_requeue_rejects_connected_closed_lead_existing_assignment_and_terminated_supplier(
    db,
) -> None:
    from apps.api.src.core.errors import AppError
    from apps.api.src.services.pre_dispatch_v12 import requeue_unreachable_pre_dispatch

    operation = create_internal_user(
        db,
        username="feedback915-guard-operation",
        password="simple88",
        display_name="运营",
        role_code="OPERATION",
    )
    principal = _principal(operation, "lead.supplier.review")

    connected = _lead(
        customer_name="普通关闭客资",
        phone="13900139505",
        status=LeadV12Status.CLOSED,
    )
    db.add(connected)
    db.flush()
    db.add(
        VerificationTask(
            lead_id=connected.id,
            task_type=VerificationTaskType.PRE_DISPATCH_VERIFY.value,
            status=VerificationTaskStatus.RELEASED.value,
            submitted_at=datetime.now(timezone.utc),
            contact_result="CONNECTED",
            verification_conclusion="INVALID",
        )
    )
    db.flush()
    from apps.api.src.services.lead_supply_v12 import lead_supply_list_to_dict

    connected_view = lead_supply_list_to_dict(db, [connected], principal)[0]
    assert connected_view["can_requeue_pre_dispatch"] is False
    assert connected_view["latest_pre_dispatch_contact_result"] == "CONNECTED"
    with pytest.raises(AppError) as connected_error:
        requeue_unreachable_pre_dispatch(
            db,
            lead_id=connected.id,
            principal=principal,
            reason="普通关闭不允许重新入库",
        )
    assert connected_error.value.code == "PRE_DISPATCH_REQUEUE_CONCLUSION_INVALID"

    receiver = Company(code="915-RECEIVER", name="已有派发接收方", status="ACTIVE")
    db.add(receiver)
    db.flush()
    assigned = _lead(
        customer_name="已有派发客资",
        phone="13900139506",
        status=LeadV12Status.CLOSED,
    )
    db.add(assigned)
    db.flush()
    assignment = Assignment(
        lead_id=assigned.id,
        company_id=receiver.id,
        receiver_company_id=receiver.id,
        status="RELEASED",
        points_price=100,
        lead_snapshot={},
        assigned_by=operation.id,
        assigned_at=datetime.now(timezone.utc),
    )
    db.add(assignment)
    db.flush()
    assigned.current_assignment_id = assignment.id
    db.add(
        VerificationTask(
            lead_id=assigned.id,
            task_type=VerificationTaskType.PRE_DISPATCH_VERIFY.value,
            status=VerificationTaskStatus.RELEASED.value,
            submitted_at=datetime.now(timezone.utc),
            contact_result="REJECTED_CALL",
            verification_conclusion="UNVERIFIABLE",
        )
    )
    db.flush()
    with pytest.raises(AppError) as assignment_error:
        requeue_unreachable_pre_dispatch(
            db,
            lead_id=assigned.id,
            principal=principal,
            reason="已有派发记录不得绕过",
        )
    assert assignment_error.value.code == "PRE_DISPATCH_REQUEUE_ASSIGNMENT_EXISTS"

    supplier = Company(code="915-TERMINATED", name="已终止供资方", status="ACTIVE")
    db.add(supplier)
    db.flush()
    supplier.supplier_cooperation_status = "TERMINATED"
    terminated = _lead(
        customer_name="终止供资客资",
        phone="13900139507",
        status=LeadV12Status.INVALID,
    )
    terminated.source_type = terminated.source_kind = "SUPPLIER_H5"
    terminated.supplier_company_id = supplier.id
    db.add(terminated)
    db.flush()
    db.add(
        VerificationTask(
            lead_id=terminated.id,
            task_type=VerificationTaskType.PRE_DISPATCH_VERIFY.value,
            status=VerificationTaskStatus.RELEASED.value,
            submitted_at=datetime.now(timezone.utc),
            contact_result="拒接",
            verification_conclusion="UNVERIFIABLE",
        )
    )
    db.flush()
    with pytest.raises(AppError) as termination_error:
        requeue_unreachable_pre_dispatch(
            db,
            lead_id=terminated.id,
            principal=principal,
            reason="终止供资后不得重新入库",
        )
    assert termination_error.value.code == "SUPPLY_COOPERATION_TERMINATED"


def test_platform_lead_region_search_matches_all_levels_and_preserves_pagination(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        for index, values in enumerate(
            (
                ("安徽阜阳客户", "安徽省", "阜阳市", "颍州区", "341202"),
                ("河南阜阳无关客户", "河南省", "郑州市", "金水区", "410105"),
                ("安徽合肥客户", "安徽省", "合肥市", "蜀山区", "340104"),
            )
        ):
            lead = _lead(
                customer_name=values[0],
                phone=f"1390013951{index}",
                status=LeadV12Status.PENDING_REVIEW,
            )
            lead.province, lead.city, lead.district, lead.region_code = values[1:]
            db.add(lead)
        db.commit()

    headers = _login(client, "operation", "Operation123!")
    city = _data(
        client.get(
            "/api/v1/v1.2/platform/leads?region=%20阜阳%20&page=1&page_size=1",
            headers=headers,
        )
    )
    assert city["total"] == 1
    assert city["items"][0]["city"] == "阜阳市"

    province = _data(
        client.get(
            "/api/v1/v1.2/platform/leads?region=安徽省&page=1&page_size=20",
            headers=headers,
        )
    )
    assert province["total"] == 2
    assert {item["province"] for item in province["items"]} == {"安徽省"}

    district = _data(
        client.get(
            "/api/v1/v1.2/platform/leads?region=蜀山&page=1&page_size=20",
            headers=headers,
        )
    )
    assert district["total"] == 1
    assert district["items"][0]["district"] == "蜀山区"


def test_lead_report_search_uses_fuzzy_region_and_exposes_requeue_fields(
    api_client,
) -> None:
    client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert operation is not None
        lead = _lead(
            customer_name="报表搜索阜阳客户",
            phone="13900139523",
            status=LeadV12Status.CLOSED,
        )
        db.add(lead)
        db.flush()
        task = VerificationTask(
            lead_id=lead.id,
            task_type=VerificationTaskType.PRE_DISPATCH_VERIFY.value,
            status=VerificationTaskStatus.RELEASED.value,
            assignee_user_id=operation.id,
            submitted_at=datetime.now(timezone.utc),
            contact_result="拒接",
            verification_conclusion="UNVERIFIABLE",
        )
        db.add(task)
        db.commit()
        lead_id = lead.id
        task_id = task.id

    headers = _login(client, "operation", "Operation123!")
    response = client.post(
        "/api/v1/v1.2/reports/leads/search",
        headers=headers,
        json={"region": "  阜阳  ", "lead_status": "CLOSED", "page_size": 20},
    )
    data = _data(response)
    assert data["total"] == 1
    item = data["items"][0]
    assert item["id"] == lead_id
    assert item["city"] == "阜阳市"
    assert item["latest_pre_dispatch_task_id"] == task_id
    assert item["latest_pre_dispatch_contact_result"] == "拒接"
    assert item["latest_pre_dispatch_conclusion"] == "UNVERIFIABLE"
    assert item["can_requeue_pre_dispatch"] is True


def test_static_rework_history_route_wins_and_admin_detail_still_works_for_both_roles(
    api_client,
) -> None:
    client, factory = api_client
    with factory() as db:
        lead = _lead(
            customer_name="真实详情路由客户",
            phone="13900139520",
            status=LeadV12Status.PENDING_REVIEW,
        )
        db.add(lead)
        db.commit()
        lead_id = lead.id

    role_headers = (
        _login(client, "admin", "Admin123!"),
        _login(client, "operation", "Operation123!"),
    )
    for headers in role_headers:
        history = client.get(
            "/api/v1/v1.2/admin/leads/pre-dispatch-rework-history",
            headers=headers,
        )
        assert history.status_code == 200, history.text
        assert history.json()["code"] == "OK"

        detail = client.get(
            f"/api/v1/v1.2/admin/leads/{lead_id}",
            headers=headers,
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["data"]["id"] == lead_id


def test_requeue_serializes_with_supplier_termination_on_company_lock(
    postgres_factory,
) -> None:
    from apps.api.src.services.pre_dispatch_v12 import (
        requeue_unreachable_pre_dispatch,
    )
    from apps.api.src.services.supply_termination import create_termination_request

    with postgres_factory() as db:
        supplier = Company(code="915-REQUEUE-RACE", name="重新核验并发供资方", status="ACTIVE")
        db.add(supplier)
        db.flush()
        operation = create_internal_user(
            db,
            username="feedback915-requeue-race-operation",
            password="simple88",
            display_name="并发运营",
            role_code="OPERATION",
        )
        lead = _lead(
            customer_name="终止供资并发重新核验",
            phone="13900139522",
            status=LeadV12Status.CLOSED,
        )
        lead.source_type = lead.source_kind = "SUPPLIER_H5"
        lead.supplier_company_id = supplier.id
        db.add(lead)
        db.flush()
        db.add(
            VerificationTask(
                lead_id=lead.id,
                task_type=VerificationTaskType.PRE_DISPATCH_VERIFY.value,
                status=VerificationTaskStatus.RELEASED.value,
                submitted_at=datetime.now(timezone.utc),
                contact_result="REFUSED",
                verification_conclusion="UNVERIFIABLE",
            )
        )
        db.commit()
        supplier_id = supplier.id
        operation_id = operation.id
        lead_id = lead.id

    requeue_locked = Event()
    termination_attempted = Event()

    def requeue_first() -> str:
        with postgres_factory() as db:
            connection = db.connection()
            paused = False

            def pause_after_company_lock(
                conn, cursor, statement, parameters, context, executemany
            ):
                nonlocal paused
                normalized = " ".join(statement.upper().split())
                has_company_write_lock = (
                    "FOR UPDATE" in normalized
                    or "FOR NO KEY UPDATE" in normalized
                )
                if (
                    "FROM COMPANIES" in normalized
                    and has_company_write_lock
                    and not paused
                ):
                    paused = True
                    requeue_locked.set()
                    assert termination_attempted.wait(3), (
                        "termination did not contend on Company"
                    )

            event.listen(connection, "after_cursor_execute", pause_after_company_lock)
            operation = db.get(User, operation_id)
            result = requeue_unreachable_pre_dispatch(
                db,
                lead_id=lead_id,
                principal=_principal(operation, "lead.supplier.review"),
                reason="客户回拨后重新核验",
            )
            db.commit()
            return result.task.id

    def terminate_second() -> str:
        assert requeue_locked.wait(3), "requeue did not lock Company first"
        with postgres_factory() as db:
            connection = db.connection()

            def signal_company_attempt(
                conn, cursor, statement, parameters, context, executemany
            ):
                normalized = " ".join(statement.upper().split())
                has_company_write_lock = (
                    "FOR UPDATE" in normalized
                    or "FOR NO KEY UPDATE" in normalized
                )
                if "FROM COMPANIES" in normalized and has_company_write_lock:
                    termination_attempted.set()

            event.listen(connection, "before_cursor_execute", signal_company_attempt)
            request = create_termination_request(
                db,
                company_id=supplier_id,
                requested_by=operation_id,
                reason="停止提供客资",
                payee_name="测试收款人",
                payee_account="6222000012345678",
                payment_method="BANK_TRANSFER",
            )
            db.commit()
            return request.id

    with ThreadPoolExecutor(max_workers=2) as executor:
        requeue = executor.submit(requeue_first)
        termination = executor.submit(terminate_second)
        task_id = requeue.result(timeout=10)
        request_id = termination.result(timeout=10)

    with postgres_factory() as db:
        request = db.get(SupplyTerminationRequest, request_id)
        refreshed = db.get(Lead, lead_id)
        assert request is not None and refreshed is not None
        assert refreshed.status == LeadV12Status.PENDING_TELESALES_VERIFY.value
        blocker = next(
            item
            for item in request.blockers_json
            if item["code"] == "SUPPLIED_LEADS_UNFINISHED"
        )
        assert blocker["record_ids"] == [lead_id]
        assert task_id
