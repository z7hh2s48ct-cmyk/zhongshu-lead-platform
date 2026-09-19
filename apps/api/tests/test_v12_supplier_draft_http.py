from __future__ import annotations

from sqlalchemy import func, select

from apps.api.src.core.models import (
    AuditLog,
    Company,
    Lead,
    Notification,
    NotificationOutbox,
    Permission,
    Region,
    Role,
    RolePermission,
    User,
    VerificationTask,
)
from apps.api.src.core.models_v12 import CompanyLeadCapability
from apps.api.src.core.security import encrypt_text, fingerprint_phone, hash_phone
from apps.api.src.core.v12_enums import LeadSourceKind, LeadV12Status, VerificationTaskType
from apps.api.src.services.auth_service import create_internal_user
from apps.api.src.services.verification_service import publish_template


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


def _approve_supplier_capability(factory) -> tuple[str, str]:
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        supplier = db.scalar(select(User).where(User.username == "franchise_demo"))
        assert company is not None and supplier is not None
        capability = db.scalar(
            select(CompanyLeadCapability).where(
                CompanyLeadCapability.company_id == company.id,
                CompanyLeadCapability.capability_code == "LEAD_SUPPLIER",
            )
        )
        if capability is None:
            capability = CompanyLeadCapability(
                company_id=company.id,
                capability_code="LEAD_SUPPLIER",
            )
            db.add(capability)
        capability.active = True
        capability.review_status = "APPROVED"
        db.commit()
        return company.id, supplier.id


def _valid_lead_body(phone: str) -> dict:
    return {
        "customer_name": "接口验收客户",
        "phone": phone,
        "city": "上海市",
        "region_code": "310101",
        "need_summary": "计划建设两层自住房，近期确认设计方案",
        "consent_confirmed": True,
    }


def test_platform_lead_exposes_creator_and_supports_controlled_pre_dispatch_correction(api_client) -> None:
    client, _factory = api_client
    operation = _login(client, "operation", "Operation123!")
    me = _data(client.get("/api/v1/auth/me", headers=operation))
    created = _data(
        client.post(
            "/api/v1/v1.2/platform/leads",
            headers=operation,
            json=_valid_lead_body("13900139039"),
        )
    )
    submitted = _data(
        client.post(
            f"/api/v1/v1.2/platform/leads/{created['id']}/submit",
            headers=operation,
        )
    )
    assert submitted["lead"]["status"] == LeadV12Status.READY_DISPATCH.value

    queue = _data(
        client.get(
            "/api/v1/v1.2/platform/leads?page=1&page_size=100",
            headers=operation,
        )
    )
    item = next(row for row in queue["items"] if row["id"] == created["id"])
    assert item["submitter_user_id"] == me["id"]
    assert item["submitter_name"] == me["display_name"]

    correction = _data(
        client.post(
            f"/api/v1/v1.2/platform/leads/{created['id']}/correction",
            headers=operation,
        )
    )
    assert correction["status"] == LeadV12Status.DRAFT.value
    assert correction["submitter_name"] == me["display_name"]


def test_platform_lead_can_submit_a_township_region_and_keeps_its_hierarchy(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        db.add(
            Region(
                code="310115001",
                name="花木街道",
                level="TOWNSHIP",
                parent_code="310115",
                aliases=["花木街道"],
                active=True,
            )
        )
        db.commit()

    operation = _login(client, "operation", "Operation123!")
    body = _valid_lead_body("13900139040")
    body.update(
        {
            "city": "不应信任的城市文案",
            "district": "不应信任的区县文案",
            "region_code": "310115001",
        }
    )
    created = _data(
        client.post("/api/v1/v1.2/platform/leads", headers=operation, json=body)
    )
    submitted = _data(
        client.post(
            f"/api/v1/v1.2/platform/leads/{created['id']}/submit",
            headers=operation,
        )
    )["lead"]

    assert submitted["status"] == LeadV12Status.READY_DISPATCH.value
    assert submitted["city"] == "上海市"
    assert submitted["district"] == "浦东新区"
    assert submitted["region_code"] == "310115001"
    assert submitted["region_level"] == "TOWNSHIP"
    assert submitted["region_name"] == "花木街道"


def _lead(
    *,
    source_kind: LeadSourceKind,
    submitter_user_id: str,
    supplier_company_id: str | None,
    status: LeadV12Status,
    review_status: str,
    phone: str,
) -> Lead:
    return Lead(
        source_type=source_kind.value,
        source_kind=source_kind.value,
        submitter_user_id=submitter_user_id,
        supplier_company_id=supplier_company_id,
        customer_name="边界验收客户",
        phone_encrypted=encrypt_text(phone),
        phone_hash=hash_phone(phone),
        consent_confirmed=status is not LeadV12Status.DRAFT,
        city="上海市",
        region_code="310101",
        need_summary="用于验证跨公司对象状态不可见",
        status=status.value,
        review_status=review_status,
        raw_payload={},
    )


def test_supplier_draft_delete_route_is_scoped_and_audited(api_client) -> None:
    client, factory = api_client
    _approve_supplier_capability(factory)
    supplier = _login(client, "franchise_demo", "Franchise123!")

    draft = _data(
        client.post(
            "/api/v1/v1.2/supplier/leads",
            headers=supplier,
            json={"customer_name": "待确认客户"},
        )
    )
    deleted = _data(
        client.delete(
            f"/api/v1/v1.2/supplier/leads/{draft['id']}",
            headers=supplier,
        )
    )
    assert deleted == {"id": draft["id"]}
    assert client.get(
        f"/api/v1/v1.2/supplier/leads/{draft['id']}",
        headers=supplier,
    ).status_code == 404

    submitted = _data(
        client.post(
            "/api/v1/v1.2/supplier/leads",
            headers=supplier,
            json=_valid_lead_body("13900139011"),
        )
    )
    _data(
        client.post(
            f"/api/v1/v1.2/supplier/leads/{submitted['id']}/submit",
            headers=supplier,
        )
    )
    blocked = client.delete(
        f"/api/v1/v1.2/supplier/leads/{submitted['id']}",
        headers=supplier,
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "LEAD_NOT_EDITABLE"

    with factory() as db:
        assert db.get(Lead, draft["id"]) is None
        audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "V12_SUPPLIER_LEAD_DRAFT_DELETE",
                AuditLog.resource_id == draft["id"],
            )
        )
        assert audit is not None


def test_supplier_employee_can_only_read_own_supplier_leads(api_client) -> None:
    client, factory = api_client
    company_id, _ = _approve_supplier_capability(factory)
    with factory() as db:
        employee = create_internal_user(
            db,
            username="supplier_employee_scope",
            password="Employee123!",
            display_name="供资员工范围测试",
            role_code="FRANCHISE_EMPLOYEE",
            company_id=company_id,
        )
        db.commit()
        employee_id = employee.id

    owner = _login(client, "franchise_demo", "Franchise123!")
    owner_lead = _data(
        client.post(
            "/api/v1/v1.2/supplier/leads",
            headers=owner,
            json={"customer_name": "负责人录入的草稿"},
        )
    )
    employee = _login(client, "supplier_employee_scope", "Employee123!")
    employee_lead = _data(
        client.post(
            "/api/v1/v1.2/supplier/leads",
            headers=employee,
            json={"customer_name": "员工本人录入的草稿"},
        )
    )

    listed = _data(client.get("/api/v1/v1.2/supplier/leads", headers=employee))
    assert listed["total"] == 1
    assert [item["id"] for item in listed["items"]] == [employee_lead["id"]]
    denied = client.get(
        f"/api/v1/v1.2/supplier/leads/{owner_lead['id']}",
        headers=employee,
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "SUPPLIER_LEAD_NOT_OWNED"

    own = _data(
        client.get(
            f"/api/v1/v1.2/supplier/leads/{employee_lead['id']}",
            headers=employee,
        )
    )
    assert own["submitter_user_id"] == employee_id


def test_cross_company_delete_and_revise_do_not_expose_lead_state(api_client) -> None:
    client, factory = api_client
    own_company_id, supplier_user_id = _approve_supplier_capability(factory)
    supplier = _login(client, "franchise_demo", "Franchise123!")

    with factory() as db:
        foreign_company = Company(
            code="FOREIGN-SUPPLIER",
            name="外部供应商",
            status="ACTIVE",
        )
        db.add(foreign_company)
        db.flush()
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert operation is not None
        leads = [
            _lead(
                source_kind=LeadSourceKind.SUPPLIER_H5,
                submitter_user_id=operation.id,
                supplier_company_id=foreign_company.id,
                status=LeadV12Status.DRAFT,
                review_status="DRAFT",
                phone="13900139021",
            ),
            _lead(
                source_kind=LeadSourceKind.SUPPLIER_H5,
                submitter_user_id=operation.id,
                supplier_company_id=foreign_company.id,
                status=LeadV12Status.PENDING_REVIEW,
                review_status="PENDING",
                phone="13900139022",
            ),
            _lead(
                source_kind=LeadSourceKind.SUPPLIER_H5,
                submitter_user_id=operation.id,
                supplier_company_id=foreign_company.id,
                status=LeadV12Status.INVALID,
                review_status="REJECTED",
                phone="13900139023",
            ),
            _lead(
                source_kind=LeadSourceKind.PLATFORM_MANUAL,
                submitter_user_id=supplier_user_id,
                supplier_company_id=None,
                status=LeadV12Status.DRAFT,
                review_status="DRAFT",
                phone="13900139024",
            ),
        ]
        db.add_all(leads)
        db.commit()
        lead_ids = [lead.id for lead in leads]

    for lead_id in lead_ids:
        response = client.delete(
            f"/api/v1/v1.2/supplier/leads/{lead_id}",
            headers=supplier,
        )
        assert response.status_code == 403
        assert response.json()["code"] == "FORBIDDEN"

    for lead_id in lead_ids:
        response = client.post(
            f"/api/v1/v1.2/supplier/leads/{lead_id}/revise",
            headers=supplier,
        )
        assert response.status_code == 403
        assert response.json()["code"] == "FORBIDDEN"

    assert own_company_id


def test_rejected_supplier_lead_can_revise_and_resubmit_over_http(api_client) -> None:
    client, factory = api_client
    company_id, _ = _approve_supplier_capability(factory)
    with factory() as db:
        telesales_user = db.scalar(select(User).where(User.username == "telesales"))
        assert telesales_user is not None
        telesales_id = telesales_user.id
    supplier = _login(client, "franchise_demo", "Franchise123!")
    admin = _login(client, "admin", "Admin123!")
    telesales = _login(client, "telesales", "Telesales123!")

    def close_after_telesales_verification(note: str) -> dict:
        assigned = _data(
            client.post(
                f"/api/v1/v1.2/admin/leads/{lead['id']}/pre-dispatch-verification",
                headers=admin,
                json={"assignee_user_id": telesales_id, "reason": "电销核实客资有效性"},
            )
        )
        _data(
            client.post(
                f"/api/v1/v1.2/pre-dispatch-verifications/tasks/{assigned['id']}/start",
                headers=telesales,
            )
        )
        _data(
            client.post(
                f"/api/v1/v1.2/pre-dispatch-verifications/tasks/{assigned['id']}/submit",
                headers=telesales,
                json={
                    "contact_result": "UNREACHABLE",
                    "conclusion": "INVALID",
                    "note": note,
                },
            )
        )
        return _data(
            client.post(
                f"/api/v1/v1.2/admin/leads/{lead['id']}/pre-dispatch-disposition",
                headers=admin,
                json={"decision": "CLOSE", "note": note},
            )
        )

    lead = _data(
        client.post(
            "/api/v1/v1.2/supplier/leads",
            headers=supplier,
            json={
                "customer_name": "接口验收客户",
                "phone": "+86 139-0013-9031",
                "need_summary": "计划建设两层自住房，待补充具体地区",
                "consent_confirmed": True,
            },
        )
    )
    submitted = _data(
        client.post(
            f"/api/v1/v1.2/supplier/leads/{lead['id']}/submit",
            headers=supplier,
        )
    )
    assert submitted["lead"]["status"] == "PENDING_TELESALES_VERIFY"

    rejected = close_after_telesales_verification("请补充更具体的施工计划")
    assert rejected["status"] == "INVALID"

    revised = _data(
        client.post(
            f"/api/v1/v1.2/supplier/leads/{lead['id']}/revise",
            headers=supplier,
        )
    )
    assert revised["status"] == "DRAFT"
    assert revised["review_note"] == "请补充更具体的施工计划"

    _data(
        client.patch(
            f"/api/v1/v1.2/supplier/leads/{lead['id']}",
            headers=supplier,
            json={
                "city": "上海市",
                "region_code": "310101",
                "need_summary": "已补充施工时间、地点和预算安排",
            },
        )
    )
    resubmitted = _data(
        client.post(
            f"/api/v1/v1.2/supplier/leads/{lead['id']}/submit",
            headers=supplier,
        )
    )
    assert resubmitted["lead"]["status"] == "PUBLIC_POOL"
    assert resubmitted["lead"]["review_status"] == "APPROVED"
    assert resubmitted["lead"]["review_note"] is None

    blocked = client.post(
        f"/api/v1/v1.2/supplier/leads/{lead['id']}/revise",
        headers=supplier,
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "LEAD_REVISION_NOT_ALLOWED"

    with factory() as db:
        audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "V12_SUPPLIER_LEAD_REVISE",
                AuditLog.resource_id == lead["id"],
                AuditLog.company_id == company_id,
            )
        )
        assert audit is not None
        submissions = db.scalars(
            select(AuditLog)
            .where(
                AuditLog.action == "V12_SUPPLIER_LEAD_SUBMIT",
                AuditLog.resource_id == lead["id"],
            )
            .order_by(AuditLog.created_at.asc())
        ).all()
        assert len(submissions) == 2
        assert (
            submissions[0].after_json["submission_snapshot"]["need_summary"]
            == "计划建设两层自住房，待补充具体地区"
        )
        assert submissions[1].after_json["submission_snapshot"]["need_summary"] == "已补充施工时间、地点和预算安排"
        submitted_notifications = db.scalars(
            select(Notification).where(
                Notification.company_id == company_id,
                Notification.scene == "V12_SUPPLIER_LEAD_SUBMITTED",
            )
        ).all()
        rejected_notifications = db.scalars(
            select(Notification).where(
                Notification.company_id == company_id,
                Notification.scene == "V12_SUPPLIER_LEAD_DISPOSITION",
            )
        ).all()
        submission_outboxes = db.scalars(
            select(NotificationOutbox).where(
                NotificationOutbox.aggregate_id == lead["id"],
                NotificationOutbox.event_type == "V12_SUPPLIER_LEAD_SUBMITTED",
            )
        ).all()
        assert len(submitted_notifications) == 2
        assert len(rejected_notifications) == 1
        assert len(submission_outboxes) == 2
        assert len({item.event_key for item in submission_outboxes}) == 2


def test_supplier_initial_review_can_require_assigned_telesales_verification(api_client) -> None:
    client, factory = api_client
    _approve_supplier_capability(factory)
    with factory() as db:
        telesales = db.scalar(select(User).where(User.username == "telesales"))
        assert telesales is not None
        publish_template(
            db,
            code="PRE_DISPATCH",
            name="前置事实核验",
            schema={"fields": []},
        )
        db.commit()
        telesales_id = telesales.id

    supplier = _login(client, "franchise_demo", "Franchise123!")
    operation = _login(client, "operation", "Operation123!")
    initial_body = _valid_lead_body("13900139032")
    initial_body.pop("city", None)
    initial_body.pop("district", None)
    initial_body.pop("region_code", None)
    lead = _data(
        client.post(
            "/api/v1/v1.2/supplier/leads",
            headers=supplier,
            json=initial_body,
        )
    )
    submitted = _data(
        client.post(
            f"/api/v1/v1.2/supplier/leads/{lead['id']}/submit",
            headers=supplier,
        )
    )
    assert submitted["lead"]["status"] == LeadV12Status.PENDING_TELESALES_VERIFY.value

    assigned = _data(
        client.post(
            f"/api/v1/v1.2/admin/leads/{lead['id']}/pre-dispatch-verification",
            headers=operation,
            json={
                "assignee_user_id": telesales_id,
                "reason": "补齐客户意向、预算和可联系时间",
            },
        )
    )
    assert assigned["lead"]["status"] == LeadV12Status.PENDING_TELESALES_VERIFY.value
    assert assigned["task_type"] == VerificationTaskType.PRE_DISPATCH_VERIFY.value
    assert assigned["assignee_user_id"] == telesales_id
    assert assigned["due_at"] is not None

    with factory() as db:
        task_audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "V12_PRE_DISPATCH_VERIFY_ASSIGN",
                AuditLog.resource_id == assigned["id"],
            )
        )
        assert task_audit is not None


def test_supplier_submission_missing_dispatch_region_requires_telesales_before_dispatch(api_client) -> None:
    client, factory = api_client
    _approve_supplier_capability(factory)
    with factory() as db:
        telesales = db.scalar(select(User).where(User.username == "telesales"))
        assert telesales is not None
        publish_template(db, code="PRE_DISPATCH", name="前置事实核验", schema={"fields": []})
        db.commit()
        telesales_id = telesales.id

    supplier = _login(client, "franchise_demo", "Franchise123!")
    operation = _login(client, "operation", "Operation123!")
    lead = _data(
        client.post(
            "/api/v1/v1.2/supplier/leads",
            headers=supplier,
            json={
                **_valid_lead_body("13900139038"),
                "city": None,
                "district": None,
                "region_code": None,
            },
        )
    )
    submitted = _data(client.post(f"/api/v1/v1.2/supplier/leads/{lead['id']}/submit", headers=supplier))
    assert submitted["lead"]["status"] == LeadV12Status.PENDING_TELESALES_VERIFY.value

    missing_assignee = client.post(
        f"/api/v1/v1.2/admin/leads/{lead['id']}/pre-dispatch-verification",
        headers=operation,
        json={"reason": "资料完整，仍需电话核实"},
    )
    assert missing_assignee.status_code == 422, missing_assignee.text

    assigned = _data(
        client.post(
            f"/api/v1/v1.2/admin/leads/{lead['id']}/pre-dispatch-verification",
            headers=operation,
            json={
                "assignee_user_id": telesales_id,
                "reason": "确认客户意向、预算和可联系时间",
            },
        )
    )
    assert assigned["lead"]["status"] == LeadV12Status.PENDING_TELESALES_VERIFY.value
    assert assigned["lead"]["status"] != LeadV12Status.READY_DISPATCH.value
    assert assigned["assignee_user_id"] == telesales_id


def test_legacy_pending_supplier_lead_can_be_qualified_without_telesales_assignment(api_client) -> None:
    client, factory = api_client
    _approve_supplier_capability(factory)
    supplier = _login(client, "franchise_demo", "Franchise123!")
    operation = _login(client, "operation", "Operation123!")
    lead = _data(
        client.post(
            "/api/v1/v1.2/supplier/leads",
            headers=supplier,
            json=_valid_lead_body("13900139040"),
        )
    )
    _data(
        client.post(
            f"/api/v1/v1.2/supplier/leads/{lead['id']}/submit",
            headers=supplier,
        )
    )
    with factory() as db:
        stored = db.get(Lead, lead["id"])
        assert stored is not None
        stored.status = LeadV12Status.PENDING_REVIEW.value
        stored.review_status = "PENDING"
        db.commit()

    reviewed = _data(
        client.post(
            f"/api/v1/v1.2/admin/supplier-leads/{lead['id']}/review",
            headers=operation,
            json={"decision": "QUALIFIED"},
        )
    )

    assert reviewed["lead"]["status"] == LeadV12Status.PUBLIC_POOL.value
    assert reviewed["lead"]["review_status"] == "APPROVED"
    assert reviewed["task"] is None
    with factory() as db:
        assert db.scalar(
            select(VerificationTask).where(VerificationTask.lead_id == lead["id"])
        ) is None


def test_supplier_approval_notification_keys_fit_outbox_limit(api_client) -> None:
    client, factory = api_client
    _approve_supplier_capability(factory)
    with factory() as db:
        telesales = db.scalar(select(User).where(User.username == "telesales"))
        assert telesales is not None
        publish_template(db, code="PRE_DISPATCH", name="前置事实核验", schema={"fields": []})
        db.commit()
        telesales_id = telesales.id
    supplier = _login(client, "franchise_demo", "Franchise123!")
    operation = _login(client, "operation", "Operation123!")
    lead = _data(
        client.post(
            "/api/v1/v1.2/supplier/leads",
            headers=supplier,
            json={
                **_valid_lead_body("13900139033"),
                "city": None,
                "district": None,
                "region_code": None,
            },
        )
    )
    _data(
        client.post(
            f"/api/v1/v1.2/supplier/leads/{lead['id']}/submit",
            headers=supplier,
        )
    )
    assigned = _data(
        client.post(
            f"/api/v1/v1.2/admin/leads/{lead['id']}/pre-dispatch-verification",
            headers=operation,
            json={
                "assignee_user_id": telesales_id,
                "reason": "确认客户意向、预算和可联系时间",
            },
        )
    )
    assert assigned["lead"]["status"] == LeadV12Status.PENDING_TELESALES_VERIFY.value

    with factory() as db:
        outboxes = db.scalars(
            select(NotificationOutbox).where(
                NotificationOutbox.aggregate_id == assigned["id"]
            )
        ).all()
        assert any(item.event_type == "V12_PRE_DISPATCH_VERIFY_ASSIGNED" for item in outboxes)
        assert all(len(item.event_key) <= 128 for item in outboxes)


def test_platform_draft_can_enter_telesales_then_return_to_operation_rework(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        telesales = db.scalar(select(User).where(User.username == "telesales"))
        operation_user = db.scalar(select(User).where(User.username == "operation"))
        assert telesales is not None and operation_user is not None
        telesales_id = telesales.id
        operation_id = operation_user.id

    operation = _login(client, "operation", "Operation123!")
    telesales = _login(client, "telesales", "Telesales123!")
    platform_draft = _data(
        client.post(
            "/api/v1/v1.2/platform/leads",
            headers=operation,
            json={
                "customer_name": "待电话确认的平台客户",
                "phone": "13900139034",
                "city": "上海市",
            },
        )
    )
    assigned = _data(
        client.post(
            f"/api/v1/v1.2/admin/leads/{platform_draft['id']}/pre-dispatch-verification",
            headers=operation,
            json={
                "assignee_user_id": telesales_id,
                "reason": "缺少客户授权、联系方式和需求说明，需要电话补充",
            },
        )
    )
    assert assigned["lead"]["status"] == LeadV12Status.PENDING_TELESALES_VERIFY.value
    assert assigned["lead"]["source_kind"] == LeadSourceKind.PLATFORM_MANUAL.value
    assert assigned["task_type"] == VerificationTaskType.PRE_DISPATCH_VERIFY.value

    _data(
        client.post(
            f"/api/v1/v1.2/pre-dispatch-verifications/tasks/{assigned['id']}/start",
            headers=telesales,
        )
    )
    _data(
        client.post(
            f"/api/v1/v1.2/pre-dispatch-verifications/tasks/{assigned['id']}/submit",
            headers=telesales,
            json={
                "contact_result": "CONNECTED",
                "conclusion": "INFO_INCOMPLETE",
                "note": "客户愿意继续沟通，但尚未确认具体需求和授权方式",
            },
        )
    )
    disposition = _data(
        client.post(
            f"/api/v1/v1.2/admin/leads/{platform_draft['id']}/pre-dispatch-disposition",
            headers=operation,
            json={"decision": "RETURN_REWORK", "note": "平台补充客户授权和明确需求后再提交"},
        )
    )
    assert disposition["status"] == LeadV12Status.DRAFT.value

    with factory() as db:
        completed_task = db.get(VerificationTask, assigned["id"])
        assert completed_task is not None
        assert completed_task.status == "RELEASED"

    open_tasks = _data(
        client.get(
            "/api/v1/v1.2/pre-dispatch-verifications/tasks?page=1&page_size=100",
            headers=operation,
        )
    )
    assert assigned["id"] not in {item["id"] for item in open_tasks["items"]}

    reworked = _data(
        client.patch(
            f"/api/v1/v1.2/platform/leads/{platform_draft['id']}",
            headers=operation,
            json=_valid_lead_body("13900139034"),
        )
    )
    assert reworked["source_kind"] == LeadSourceKind.PLATFORM_MANUAL.value
    assert reworked["supplier_company_id"] is None
    assert reworked["status"] == LeadV12Status.DRAFT.value

    with factory() as db:
        operation_notifications = db.scalars(
            select(Notification).where(
                Notification.user_id == operation_id,
                Notification.scene.in_(
                    {
                        "V12_PRE_DISPATCH_OPERATION_REQUIRED",
                        "V12_PLATFORM_LEAD_DISPOSITION",
                    }
                ),
            )
        ).all()
        links_by_scene = {item.scene: item.deep_link for item in operation_notifications}
        assert links_by_scene["V12_PRE_DISPATCH_OPERATION_REQUIRED"] == (
            f"/admin/v12-operations.html?view=telesales&id={assigned['id']}"
        )
        assert links_by_scene["V12_PLATFORM_LEAD_DISPOSITION"] == (
            f"/admin/v12-operations.html?view=leads&id={platform_draft['id']}"
        )
        outboxes = db.scalars(
            select(NotificationOutbox).where(
                NotificationOutbox.aggregate_id.in_([platform_draft["id"], assigned["id"]])
            )
        ).all()
        assert {
            "V12_PRE_DISPATCH_OPERATION_REQUIRED",
            "V12_PLATFORM_LEAD_DISPOSITION",
        }.issubset({item.event_type for item in outboxes})
        assert all(len(item.event_key) <= 128 for item in outboxes)


def test_platform_draft_without_phone_cannot_enter_telesales_verification(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        telesales = db.scalar(select(User).where(User.username == "telesales"))
        assert telesales is not None
        telesales_id = telesales.id

    operation = _login(client, "operation", "Operation123!")
    draft = _data(
        client.post(
            "/api/v1/v1.2/platform/leads",
            headers=operation,
            json={"customer_name": "没有联系电话的平台客户", "city": "上海市"},
        )
    )

    blocked = client.post(
        f"/api/v1/v1.2/admin/leads/{draft['id']}/pre-dispatch-verification",
        headers=operation,
        json={"assignee_user_id": telesales_id, "reason": "需电话补充客户需求"},
    )

    assert blocked.status_code == 422
    assert blocked.json()["code"] == "PRE_DISPATCH_PHONE_REQUIRED"


def test_platform_create_and_telesales_assignment_is_atomic_and_idempotent(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        telesales = db.scalar(select(User).where(User.username == "telesales"))
        assert telesales is not None
        publish_template(
            db,
            code="PRE_DISPATCH",
            name="前置事实核验",
            schema={"fields": []},
        )
        db.commit()
        telesales_id = telesales.id

    operation = _login(client, "operation", "Operation123!")
    payload = {
        "customer_name": "幂等直派电销客户",
        "phone": "13900139035",
        "assignee_user_id": telesales_id,
        "reason": "确认客户需求和服务区域",
        "idempotency_key": "feedback-91-direct-telesales",
    }

    first = _data(
        client.post(
            "/api/v1/v1.2/platform/leads/pre-dispatch-verification",
            headers=operation,
            json=payload,
        )
    )
    replay = _data(
        client.post(
            "/api/v1/v1.2/platform/leads/pre-dispatch-verification",
            headers=operation,
            json=payload,
        )
    )

    assert first["idempotent"] is False
    assert replay["idempotent"] is True
    assert replay["lead"]["id"] == first["lead"]["id"]
    assert replay["task"]["id"] == first["task"]["id"]
    with factory() as db:
        leads = db.scalars(
            select(Lead).where(Lead.phone_hash == hash_phone(payload["phone"]))
        ).all()
        tasks = db.scalars(
            select(VerificationTask).where(
                VerificationTask.lead_id == first["lead"]["id"],
                VerificationTask.task_type
                == VerificationTaskType.PRE_DISPATCH_VERIFY.value,
            )
        ).all()
        assert len(leads) == 1
        assert len(tasks) == 1

    conflict = client.post(
        "/api/v1/v1.2/platform/leads/pre-dispatch-verification",
        headers=operation,
        json={**payload, "customer_name": "冲突请求"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_platform_create_and_telesales_assignment_rolls_back_when_assignment_fails(api_client) -> None:
    client, factory = api_client
    operation = _login(client, "operation", "Operation123!")
    phone = "13900139034"

    blocked = client.post(
        "/api/v1/v1.2/platform/leads/pre-dispatch-verification",
        headers=operation,
        json={
            "customer_name": "派发失败不应留草稿",
            "phone": phone,
            "assignee_user_id": "missing-telesales-user",
            "reason": "确认客户需求和服务区域",
            "idempotency_key": "feedback-91-atomic-rollback",
        },
    )

    assert blocked.status_code == 422
    with factory() as db:
        assert (
            db.scalar(select(Lead).where(Lead.phone_hash == hash_phone(phone)))
            is None
        )


def test_platform_create_and_telesales_assignment_requires_dispatch_review_permission(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        operation_role = db.scalar(select(Role).where(Role.code == "OPERATION"))
        permission = db.scalar(
            select(Permission).where(Permission.code == "lead.supplier.review")
        )
        telesales = db.scalar(select(User).where(User.username == "telesales"))
        assert operation_role is not None and permission is not None and telesales is not None
        mapping = db.get(RolePermission, (operation_role.id, permission.id))
        assert mapping is not None
        db.delete(mapping)
        db.commit()
        telesales_id = telesales.id

    operation = _login(client, "operation", "Operation123!")
    with factory() as db:
        before = (
            int(db.scalar(select(func.count(Lead.id))) or 0),
            int(db.scalar(select(func.count(VerificationTask.id))) or 0),
            int(db.scalar(select(func.count(AuditLog.id))) or 0),
        )

    blocked = client.post(
        "/api/v1/v1.2/platform/leads/pre-dispatch-verification",
        headers=operation,
        json={
            "customer_name": "无派发权限客户",
            "phone": "13900139033",
            "assignee_user_id": telesales_id,
            "reason": "尝试绕过派发权限",
            "idempotency_key": "feedback-91-permission-boundary",
        },
    )

    assert blocked.status_code == 403
    with factory() as db:
        after = (
            int(db.scalar(select(func.count(Lead.id))) or 0),
            int(db.scalar(select(func.count(VerificationTask.id))) or 0),
            int(db.scalar(select(func.count(AuditLog.id))) or 0),
        )
    assert after == before


def test_feishu_public_pool_draft_with_valid_phone_can_enter_telesales_verification(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        telesales = db.scalar(select(User).where(User.username == "telesales"))
        operation_user = db.scalar(select(User).where(User.username == "operation"))
        assert telesales is not None and operation_user is not None
        publish_template(
            db,
            code="PRE_DISPATCH",
            name="前置事实核验",
            schema={"fields": []},
        )
        phone = "13900139036"
        lead = Lead(
            source_type=LeadSourceKind.FEISHU_IMPORT.value,
            source_kind=LeadSourceKind.FEISHU_IMPORT.value,
            submitter_user_id=operation_user.id,
            customer_name="飞书导入待核验客户",
            phone_encrypted=encrypt_text(phone),
            phone_hash=hash_phone(phone),
            phone_fingerprint=fingerprint_phone(phone),
            consent_confirmed=False,
            status=LeadV12Status.DRAFT.value,
            review_status="DRAFT",
            duplicate_status="CLEAR",
            raw_payload={},
        )
        db.add(lead)
        db.commit()
        lead_id = lead.id
        telesales_id = telesales.id

    operation = _login(client, "operation", "Operation123!")
    telesales_headers = _login(client, "telesales", "Telesales123!")
    assigned = _data(
        client.post(
            f"/api/v1/v1.2/admin/leads/{lead_id}/pre-dispatch-verification",
            headers=operation,
            json={
                "assignee_user_id": telesales_id,
                "reason": "先电话补充客户需求和授权信息",
            },
        )
    )

    assert assigned["lead"]["status"] == LeadV12Status.PENDING_TELESALES_VERIFY.value
    assert assigned["lead"]["source_kind"] == LeadSourceKind.FEISHU_IMPORT.value
    _data(
        client.post(
            f"/api/v1/v1.2/pre-dispatch-verifications/tasks/{assigned['id']}/start",
            headers=telesales_headers,
        )
    )
    _data(
        client.post(
            f"/api/v1/v1.2/pre-dispatch-verifications/tasks/{assigned['id']}/submit",
            headers=telesales_headers,
            json={
                "contact_result": "CONNECTED",
                "conclusion": "INFO_INCOMPLETE",
                "note": "客户已接通，但需求和授权仍需运营补充",
            },
        )
    )
    disposition = _data(
        client.post(
            f"/api/v1/v1.2/admin/leads/{lead_id}/pre-dispatch-disposition",
            headers=operation,
            json={"decision": "RETURN_REWORK", "note": "由运营继续补充资料"},
        )
    )
    assert disposition["status"] == LeadV12Status.DRAFT.value
    with factory() as db:
        rework = db.get(Lead, lead_id)
        assert rework is not None
        assert rework.source_kind == LeadSourceKind.FEISHU_IMPORT.value
        assert rework.supplier_company_id is None


def test_public_pool_draft_with_invalid_phone_cannot_enter_telesales_verification(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        telesales = db.scalar(select(User).where(User.username == "telesales"))
        operation_user = db.scalar(select(User).where(User.username == "operation"))
        assert telesales is not None and operation_user is not None
        phone = "12345"
        lead = Lead(
            source_type=LeadSourceKind.FEISHU_IMPORT.value,
            source_kind=LeadSourceKind.FEISHU_IMPORT.value,
            submitter_user_id=operation_user.id,
            customer_name="手机号无效的导入客户",
            phone_encrypted=encrypt_text(phone),
            phone_hash=hash_phone(phone),
            phone_fingerprint=fingerprint_phone(phone),
            consent_confirmed=False,
            status=LeadV12Status.DRAFT.value,
            review_status="DRAFT",
            duplicate_status="CLEAR",
            raw_payload={},
        )
        db.add(lead)
        db.commit()
        lead_id = lead.id
        telesales_id = telesales.id

    operation = _login(client, "operation", "Operation123!")
    blocked = client.post(
        f"/api/v1/v1.2/admin/leads/{lead_id}/pre-dispatch-verification",
        headers=operation,
        json={"assignee_user_id": telesales_id, "reason": "尝试核验无效号码"},
    )

    assert blocked.status_code == 422
    assert blocked.json()["code"] == "PRE_DISPATCH_PHONE_REQUIRED"


def test_public_pool_draft_with_unresolved_duplicate_cannot_enter_telesales_verification(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        telesales = db.scalar(select(User).where(User.username == "telesales"))
        operation_user = db.scalar(select(User).where(User.username == "operation"))
        assert telesales is not None and operation_user is not None
        phone = "13900139037"
        lead = Lead(
            source_type=LeadSourceKind.FEISHU_IMPORT.value,
            source_kind=LeadSourceKind.FEISHU_IMPORT.value,
            submitter_user_id=operation_user.id,
            customer_name="重复状态待处理的导入客户",
            phone_encrypted=encrypt_text(phone),
            phone_hash=hash_phone(phone),
            phone_fingerprint=fingerprint_phone(phone),
            consent_confirmed=False,
            status=LeadV12Status.DRAFT.value,
            review_status="DRAFT",
            duplicate_status="HISTORICAL_SUSPECT",
            raw_payload={},
        )
        db.add(lead)
        db.commit()
        lead_id = lead.id
        telesales_id = telesales.id

    operation = _login(client, "operation", "Operation123!")
    blocked = client.post(
        f"/api/v1/v1.2/admin/leads/{lead_id}/pre-dispatch-verification",
        headers=operation,
        json={"assignee_user_id": telesales_id, "reason": "尝试绕过未决查重"},
    )

    assert blocked.status_code == 409
    assert blocked.json()["code"] == "PRE_DISPATCH_DUPLICATE_UNRESOLVED"


def test_supplier_draft_cannot_bypass_initial_review_into_telesales_verification(api_client) -> None:
    client, factory = api_client
    _approve_supplier_capability(factory)
    with factory() as db:
        telesales = db.scalar(select(User).where(User.username == "telesales"))
        assert telesales is not None
        telesales_id = telesales.id

    supplier = _login(client, "franchise_demo", "Franchise123!")
    operation = _login(client, "operation", "Operation123!")
    draft = _data(
        client.post(
            "/api/v1/v1.2/supplier/leads",
            headers=supplier,
            json=_valid_lead_body("13900139035"),
        )
    )

    blocked = client.post(
        f"/api/v1/v1.2/admin/leads/{draft['id']}/pre-dispatch-verification",
        headers=operation,
        json={"assignee_user_id": telesales_id, "reason": "错误尝试绕过初审"},
    )

    assert blocked.status_code == 409
    assert blocked.json()["code"] == "PRE_DISPATCH_LEAD_STATE_INVALID"


def test_platform_endpoints_reject_supplier_lead_ids(api_client) -> None:
    client, factory = api_client
    _approve_supplier_capability(factory)
    supplier = _login(client, "franchise_demo", "Franchise123!")
    operation = _login(client, "operation", "Operation123!")
    draft = _data(
        client.post(
            "/api/v1/v1.2/supplier/leads",
            headers=supplier,
            json={"customer_name": "不能走平台接口的加盟商草稿"},
        )
    )

    update = client.patch(
        f"/api/v1/v1.2/platform/leads/{draft['id']}",
        headers=operation,
        json={"need_summary": "错误入口"},
    )
    submit = client.post(
        f"/api/v1/v1.2/platform/leads/{draft['id']}/submit",
        headers=operation,
    )

    assert update.status_code == 409
    assert update.json()["code"] == "LEAD_SOURCE_INVALID"
    assert submit.status_code == 409
    assert submit.json()["code"] == "LEAD_SOURCE_INVALID"
