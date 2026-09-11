from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from apps.api.src.core.enums import AssignmentStatus
from apps.api.src.core.models import (
    Assignment,
    AuditLog,
    Company,
    Lead,
    LeadExportTask,
    Permission,
    Role,
    RolePermission,
    User,
)
from apps.api.src.core.security import encrypt_text, fingerprint_phone, hash_phone
from apps.api.src.core.v12_enums import LeadSourceKind, LeadV12Status
from apps.api.tests.xlsx_reader import read_xlsx


def _login(client, username: str = "operation", password: str = "Operation123!") -> None:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text


def _lead(
    *,
    submitter_user_id: str,
    phone: str,
    name: str,
    province: str,
    city: str,
    district: str,
    region_code: str,
) -> Lead:
    return Lead(
        source_type=LeadSourceKind.PLATFORM_MANUAL.value,
        source_kind=LeadSourceKind.PLATFORM_MANUAL.value,
        submitter_user_id=submitter_user_id,
        customer_name=name,
        phone_encrypted=encrypt_text(phone),
        phone_hash=hash_phone(phone),
        phone_fingerprint=fingerprint_phone(phone),
        consent_confirmed=True,
        province=province,
        city=city,
        district=district,
        region_code=region_code,
        source_channel="MANUAL",
        status=LeadV12Status.FOLLOWING.value,
        review_status="APPROVED",
        duplicate_status="CLEAR",
        imported_at=datetime.now(timezone.utc),
    )


def test_item_8_report_filters_submitter_phone_region_and_current_receiver(
    api_client,
) -> None:
    client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        admin = db.scalar(select(User).where(User.username == "admin"))
        receiver = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        assert operation is not None and admin is not None and receiver is not None

        matched = _lead(
            submitter_user_id=operation.id,
            phone="13900139831",
            name="四项筛选命中客户",
            province="上海市",
            city="上海市",
            district="浦东新区",
            region_code="310115",
        )
        wrong_submitter = _lead(
            submitter_user_id=admin.id,
            phone="13900139831",
            name="录入人员不符客户",
            province="上海市",
            city="上海市",
            district="浦东新区",
            region_code="310115",
        )
        wrong_phone = _lead(
            submitter_user_id=operation.id,
            phone="13900139832",
            name="电话不符客户",
            province="上海市",
            city="上海市",
            district="浦东新区",
            region_code="310115",
        )
        wrong_region = _lead(
            submitter_user_id=operation.id,
            phone="13900139831",
            name="地区不符客户",
            province="浙江省",
            city="杭州市",
            district="西湖区",
            region_code="330106",
        )
        wrong_receiver = _lead(
            submitter_user_id=operation.id,
            phone="13900139831",
            name="当前接收方不符客户",
            province="上海市",
            city="上海市",
            district="浦东新区",
            region_code="310115",
        )
        other_receiver = Company(code="HZ-ITEM8", name="杭州筛选测试加盟商")
        db.add_all(
            [
                matched,
                wrong_submitter,
                wrong_phone,
                wrong_region,
                wrong_receiver,
                other_receiver,
            ]
        )
        db.flush()
        assignments = []
        for index, lead in enumerate(
            [matched, wrong_submitter, wrong_phone, wrong_region, wrong_receiver]
        ):
            company = other_receiver if lead is wrong_receiver else receiver
            assignments.append(
                Assignment(
                    lead_id=lead.id,
                    company_id=company.id,
                    receiver_company_id=company.id,
                    status=AssignmentStatus.FOLLOWING.value,
                    points_price=100,
                    lead_snapshot={},
                    assigned_by=operation.id,
                    assigned_at=datetime.now(timezone.utc),
                    idempotency_key=f"feedback-item-8-completion-filter-{index}",
                )
            )
        db.add_all(assignments)
        db.flush()
        for lead, assignment in zip(
            [matched, wrong_submitter, wrong_phone, wrong_region, wrong_receiver],
            assignments,
            strict=True,
        ):
            lead.current_assignment_id = assignment.id
        db.commit()
        matched_id = matched.id
        operation_id = operation.id
        receiver_id = receiver.id

    _login(client)
    for region in ("310115", "上海市", "浦东新区"):
        response = client.post(
            "/api/v1/v1.2/reports/leads/search",
            json={
                "submitter_user_id": operation_id,
                "phone": "+86 139-0013-9831",
                "region": region,
                "receiver_company_id": receiver_id,
                "page_size": 200,
            },
        )
        assert response.status_code == 200, response.text
        items = response.json()["data"]["items"]
        assert [item["id"] for item in items] == [matched_id]
        assert items[0]["phone"] == "13900139831"
        assert items[0]["phone_masked"] == "139****9831"
        assert items[0]["submitter_user_id"] == operation_id


def test_item_8_export_persists_phone_hash_instead_of_plain_phone(api_client) -> None:
    client, factory = api_client
    _login(client)
    response = client.post(
        "/api/v1/v1.2/reports/leads/exports",
        json={
            "phone": "+86 139-0013-9831",
            "region": "310115",
            "idempotency_key": "feedback-item-8-completion-sensitive-filter",
        },
    )
    assert response.status_code == 200, response.text
    task_id = response.json()["data"]["id"]

    with factory() as db:
        task = db.get(LeadExportTask, task_id)
        audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "V12_LEAD_EXPORT_REQUESTED",
                AuditLog.resource_id == task_id,
            )
        )
        assert task is not None and audit is not None
        assert "phone" not in task.filters_json
        assert task.filters_json["phone_hash"] == hash_phone("13900139831")
        assert "13900139831" not in str(task.filters_json)
        assert "13900139831" not in str(audit.metadata_json)


def test_item_8_exact_phone_filter_requires_full_phone_export_permission(
    api_client,
) -> None:
    client, factory = api_client
    with factory() as db:
        role = db.scalar(select(Role).where(Role.code == "OPERATION"))
        permission = db.scalar(
            select(Permission).where(Permission.code == "lead.phone.export")
        )
        assert role is not None and permission is not None
        binding = db.scalar(
            select(RolePermission).where(
                RolePermission.role_id == role.id,
                RolePermission.permission_id == permission.id,
            )
        )
        assert binding is not None
        db.delete(binding)
        db.commit()

    _login(client)
    allowed = client.get("/api/v1/v1.2/reports/leads")
    denied = client.post(
        "/api/v1/v1.2/reports/leads/search",
        json={"phone": "13900139831"},
    )
    assert allowed.status_code == 200, allowed.text
    assert denied.status_code == 403, denied.text
    assert denied.json()["code"] == "FORBIDDEN"


def test_item_8_filter_options_include_actual_submitters_and_inactive_companies(
    api_client,
) -> None:
    client, factory = api_client
    with factory() as db:
        franchise = db.scalar(select(User).where(User.username == "franchise_demo"))
        assert franchise is not None
        lead = _lead(
            submitter_user_id=franchise.id,
            phone="13900139839",
            name="加盟商历史录入客户",
            province="上海市",
            city="上海市",
            district="浦东新区",
            region_code="310115",
        )
        inactive = Company(
            code="INACTIVE-ITEM8",
            name="已停用历史加盟商",
            status="INACTIVE",
        )
        historical_assigner = User(
            display_name="已停用历史运营",
            status="DISABLED",
        )
        db.add_all([lead, inactive, historical_assigner])
        db.flush()
        db.add(
            Assignment(
                lead_id=lead.id,
                company_id=inactive.id,
                receiver_company_id=inactive.id,
                status=AssignmentStatus.RELEASED.value,
                points_price=100,
                lead_snapshot={},
                assigned_by=historical_assigner.id,
                assigned_at=datetime.now(timezone.utc),
                idempotency_key="feedback-item-8-historical-assigner",
            )
        )
        db.commit()
        franchise_id = franchise.id
        inactive_id = inactive.id
        historical_assigner_id = historical_assigner.id

    _login(client)
    response = client.get("/api/v1/v1.2/reports/leads/filter-options")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert {item["id"] for item in data["submitters"]} >= {franchise_id}
    inactive_option = next(
        item for item in data["receiver_companies"] if item["id"] == inactive_id
    )
    assert inactive_option == {
        "id": inactive_id,
        "name": "已停用历史加盟商",
        "status": "INACTIVE",
    }
    historical_assigner_option = next(
        item for item in data["assigners"] if item["id"] == historical_assigner_id
    )
    assert historical_assigner_option == {
        "id": historical_assigner_id,
        "name": "已停用历史运营",
        "status": "DISABLED",
    }


def test_item_8_export_splits_all_matches_into_one_zip_without_row_limit_failure(
    api_client,
    monkeypatch,
) -> None:
    from apps.api.src.services import lead_export_v12

    _client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert operation is not None
        leads = [
            _lead(
                submitter_user_id=operation.id,
                phone=f"1390013984{index}",
                name=f"分片客户{index}",
                province="上海市",
                city="上海市",
                district="浦东新区",
                region_code="310115",
            )
            for index in range(5)
        ]
        db.add_all(leads)
        db.commit()

        original_list = lead_export_v12.list_lead_report_rows

        def undercount(*args, **kwargs):
            rows, _total = original_list(*args, **kwargs)
            return rows, 1

        monkeypatch.setattr(lead_export_v12, "LEAD_EXPORT_ROWS_PER_FILE", 2)
        monkeypatch.setattr(lead_export_v12, "list_lead_report_rows", undercount)
        archive_path, row_count = lead_export_v12.build_lead_export_workbook(db, {})

    try:
        assert row_count >= 5
        workbook = read_xlsx(archive_path)
        lead_members = sorted(name for name in workbook if name.startswith("客资明细"))
        assert len(lead_members) >= 3
        assert len(lead_members) == len(set(lead_members))
        exported = "".join(
            row["客户姓名"]
            for name in lead_members
            for row in workbook[name]
        )
        for index in range(5):
            assert f"分片客户{index}" in exported
    finally:
        archive_path.unlink(missing_ok=True)


def test_item_8_full_phone_filter_is_not_exposed_as_a_get_query_parameter() -> None:
    source = Path("apps/api/src/routers/v12_insights.py").read_text(encoding="utf-8")
    get_report = source[
        source.index('@router.get("/reports/leads")') :
        source.index('@router.post("/reports/leads/exports")')
    ]

    assert 'phone: str | None = Query' not in get_report
    assert '@router.post("/reports/leads/search")' in get_report


def test_item_8_export_fails_instead_of_silently_omitting_an_undecryptable_phone(
    api_client,
    caplog,
) -> None:
    from apps.api.src.services.lead_export_v12 import process_lead_export_tasks

    client, factory = api_client
    damaged_ciphertext = "invalid-fernet-ciphertext"
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert operation is not None
        lead = _lead(
            submitter_user_id=operation.id,
            phone="13900139849",
            name="密文损坏客户",
            province="密文测试省",
            city="密文测试市",
            district="密文测试区",
            region_code="990049",
        )
        lead.phone_encrypted = damaged_ciphertext
        db.add(lead)
        db.commit()
        lead_id = lead.id

    _login(client)
    requested = client.post(
        "/api/v1/v1.2/reports/leads/exports",
        json={
            "region": "密文测试区",
            "idempotency_key": "feedback-item-8-damaged-ciphertext",
        },
    )
    assert requested.status_code == 200, requested.text
    task_id = requested.json()["data"]["id"]

    caplog.set_level("ERROR", logger="zhongshu.lead_export")
    with factory() as db:
        result = process_lead_export_tasks(db, limit=1)
    assert result == {"claimed": 1, "completed": 0, "failed": 1, "superseded": 0}

    with factory() as db:
        task = db.get(LeadExportTask, task_id)
        assert task is not None
        assert task.status == "FAILED"
        assert task.row_count == 0
        assert task.object_key is None
        assert task.file_name is None
        assert task.error_message == "完整手机号解密失败"
        assert damaged_ciphertext not in task.error_message
    messages = [record.getMessage() for record in caplog.records]
    assert any(lead_id in message for message in messages)
    assert all(damaged_ciphertext not in message for message in messages)
