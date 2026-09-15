from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import event, select

from apps.api.src.core import models_v12 as _models_v12  # noqa: F401
from apps.api.src.core.enums import AssignmentStatus
from apps.api.src.core.models import Assignment, Company, FollowUp, Lead, LeadExportTask, User
from apps.api.src.core.security import encrypt_text, fingerprint_phone, hash_phone
from apps.api.src.core.v12_enums import LeadSourceKind, LeadV12Status
from apps.api.src.services.lead_export_v12 import (
    LeadReportRow,
    build_lead_export_workbook,
    lead_report_to_dicts,
    list_lead_report_rows,
)
from apps.api.src.services.public_pool_v12 import list_public_pool_leads
from apps.api.src.services.rbac import assign_role
from apps.api.tests.xlsx_reader import read_xlsx


def _lead(*, submitter_id: str, phone: str, source_kind: str, status: str) -> Lead:
    return Lead(
        source_type=source_kind,
        source_kind=source_kind,
        submitter_user_id=submitter_id,
        customer_name="完整导出客户",
        phone_encrypted=encrypt_text(phone),
        phone_hash=hash_phone(phone),
        phone_fingerprint=fingerprint_phone(phone),
        consent_confirmed=True,
        province="湖北省",
        city="仙桃市",
        district="干河街道",
        region_code="429004",
        category_code="SELF_BUILD",
        brand_code="ZHONGSHU",
        source_channel="OTHER",
        source_detail="线下活动",
        need_summary="需要两层自建房设计",
        budget_min=500_000,
        budget_max=800_000,
        status=status,
        review_status="APPROVED",
        duplicate_status="CLEAR",
        current_follow_status="INTERESTED",
        imported_at=datetime.now(timezone.utc),
        raw_payload={},
    )


def _read_lead_rows(path) -> list[dict[str, str]]:
    workbook = read_xlsx(path)
    sheet_name = "公海池明细" if "公海池明细" in workbook else "客资明细"
    return workbook[sheet_name]


def test_full_lead_export_contains_business_dispatch_and_current_followup_fields(db) -> None:
    submitter = User(display_name="录入运营", status="ACTIVE")
    employee = User(display_name="加盟商小王", status="ACTIVE")
    assigner = User(display_name="派发运营", status="ACTIVE")
    company = Company(code="EXPORT-831", name="仙桃加盟商", status="ACTIVE")
    db.add_all([submitter, employee, assigner, company])
    db.flush()
    employee.company_id = company.id
    assign_role(db, employee, "FRANCHISE_EMPLOYEE")
    lead = _lead(
        submitter_id=submitter.id,
        phone="13900139831",
        source_kind=LeadSourceKind.PLATFORM_MANUAL.value,
        status=LeadV12Status.FOLLOWING.value,
    )
    db.add(lead)
    db.flush()
    assigned_at = datetime(2026, 8, 30, 1, 2, tzinfo=timezone.utc)
    internal_assigned_at = datetime(2026, 8, 30, 2, 3, tzinfo=timezone.utc)
    assignment = Assignment(
        lead_id=lead.id,
        company_id=company.id,
        receiver_company_id=company.id,
        status=AssignmentStatus.FOLLOWING.value,
        points_price=100,
        price_version=1,
        lead_snapshot={},
        assigned_by=assigner.id,
        assigned_at=assigned_at,
        internal_assignee_user_id=employee.id,
        internal_assigned_by=employee.id,
        internal_assigned_at=internal_assigned_at,
    )
    db.add(assignment)
    db.flush()
    lead.current_assignment_id = assignment.id
    db.add(
        FollowUp(
            assignment_id=assignment.id,
            company_id=company.id,
            status="INTERESTED",
            note="已约定上门量房",
            created_by=employee.id,
        )
    )
    db.commit()

    report_rows, report_total = list_lead_report_rows(
        db,
        filters={"phone_hash": lead.phone_hash},
        page_no=1,
        page_size=10,
    )
    report_item = lead_report_to_dicts(db, report_rows)[0]
    assert report_total == 1
    assert report_item["franchise_handler_name"] == "加盟商小王"
    assert report_item["franchise_handler_kind"] == "FRANCHISE_EMPLOYEE"
    assert report_item["internal_assigned_at"] == internal_assigned_at.isoformat()
    owner_handled = lead_report_to_dicts(
        db,
        [
            LeadReportRow(
                lead=lead,
                assignment=assignment,
                receiver_company_name=company.name,
                assigned_by_name=assigner.display_name,
                submitter_name=submitter.display_name,
                supplier_company_name=None,
                internal_assignee_name="加盟商负责人",
                internal_assignee_role_code="FRANCHISE_OWNER",
                latest_followup_status=None,
                latest_followup_note=None,
                latest_followup_next_at=None,
                latest_followup_by_name=None,
                latest_followup_at=None,
            )
        ],
    )[0]
    assert owner_handled["franchise_handler_name"] == company.name
    assert owner_handled["franchise_handler_kind"] == "FRANCHISE_COMPANY"
    assert owner_handled["internal_assigned_at"] is None

    archive_path, count = build_lead_export_workbook(db, {"scope": "ALL_LEADS"})
    try:
        assert archive_path.suffix == ".xlsx"
        assert set(read_xlsx(archive_path)) == {"客资明细", "跟进记录"}
        rows = _read_lead_rows(archive_path)
        followup_rows = read_xlsx(archive_path)["跟进记录"]
    finally:
        archive_path.unlink(missing_ok=True)

    assert count == 1
    row = rows[0]
    assert row["序号"] == "1"
    assert row["客户需求"] == "需要两层自建房设计"
    assert row["咨询类别"] == "SELF_BUILD"
    assert row["预算下限"] == "500000"
    assert row["预算上限"] == "800000"
    assert row["当前跟进状态"] == "INTERESTED"
    assert row["加盟商跟进人"] == "加盟商小王"
    assert row["内部分配时间"] == internal_assigned_at.isoformat()
    assert row["最新跟进内容"] == "已约定上门量房"
    assert row["最新跟进人"] == "加盟商小王"
    assert "获客成本" not in row
    assert "积分" not in "".join(row)
    assert followup_rows[0]["序号"] == "1"


def test_public_pool_export_reuses_membership_and_current_filters(db) -> None:
    operation = User(display_name="公海池运营", status="ACTIVE")
    other_operation = User(display_name="其他录入人员", status="ACTIVE")
    db.add_all([operation, other_operation])
    db.flush()
    created_at = datetime(2026, 8, 30, 2, 0, tzinfo=timezone.utc)
    matching = _lead(
        submitter_id=operation.id,
        phone="13900139832",
        source_kind=LeadSourceKind.PLATFORM_MANUAL.value,
        status=LeadV12Status.DRAFT.value,
    )
    matching.customer_name = "仙桃待补客户"
    matching.consent_confirmed = False
    matching.pending_reason = "PUBLIC_POOL_INCOMPLETE"
    matching.created_at = created_at
    wrong_keyword = _lead(
        submitter_id=operation.id,
        phone="13900139833",
        source_kind=LeadSourceKind.PLATFORM_MANUAL.value,
        status=LeadV12Status.DRAFT.value,
    )
    wrong_keyword.customer_name = "武汉待补客户"
    wrong_keyword.created_at = created_at
    wrong_submitter = _lead(
        submitter_id=other_operation.id,
        phone="13900139835",
        source_kind=LeadSourceKind.PLATFORM_MANUAL.value,
        status=LeadV12Status.DRAFT.value,
    )
    wrong_submitter.customer_name = "仙桃其他人员客户"
    wrong_submitter.created_at = created_at
    outside_pool = _lead(
        submitter_id=operation.id,
        phone="13900139834",
        source_kind=LeadSourceKind.PLATFORM_MANUAL.value,
        status=LeadV12Status.READY_DISPATCH.value,
    )
    outside_pool.customer_name = "仙桃已转派发"
    outside_pool.created_at = created_at
    db.add_all([matching, wrong_keyword, wrong_submitter, outside_pool])
    db.commit()

    filters = {
        "scope": "PUBLIC_POOL",
        "keyword": "仙桃",
        "customer_source": "OPERATION_ENTRY",
        "source_kind": "PLATFORM_MANUAL",
        "completeness": "INCOMPLETE",
        "duplicate_status": None,
        "created_from": datetime(2026, 8, 30, 0, 0, tzinfo=timezone.utc),
        "created_to": datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc),
        "submitter_user_id": operation.id,
    }

    listed, total = list_public_pool_leads(
        db,
        keyword=filters["keyword"],
        customer_source=filters["customer_source"],
        source_kind=filters["source_kind"],
        completeness=filters["completeness"],
        duplicate_status=filters["duplicate_status"],
        created_from=filters["created_from"],
        created_to=filters["created_to"],
        submitter_user_id=filters["submitter_user_id"],
        page_no=1,
        page_size=20,
    )
    assert total == 1
    assert [item.id for item in listed] == [matching.id]

    archive_path, count = build_lead_export_workbook(
        db,
        filters,
    )
    try:
        assert archive_path.suffix == ".xlsx"
        assert set(read_xlsx(archive_path)) == {"公海池明细"}
        rows = _read_lead_rows(archive_path)
    finally:
        archive_path.unlink(missing_ok=True)

    assert count == 1
    assert [row["客资编号"] for row in rows] == [matching.id]

    all_archive_path, all_count = build_lead_export_workbook(
        db,
        {"scope": "PUBLIC_POOL"},
    )
    try:
        all_rows = _read_lead_rows(all_archive_path)
    finally:
        all_archive_path.unlink(missing_ok=True)

    assert all_count == 3
    assert {row["客资编号"] for row in all_rows} == {
        matching.id,
        wrong_keyword.id,
        wrong_submitter.id,
    }


def test_public_pool_export_request_keeps_scope_and_filters_in_task(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert operation is not None
        submitter_user_id = operation.id
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "operation", "password": "Operation123!"},
    )
    assert login.status_code == 200, login.text

    response = client.post(
        "/api/v1/v1.2/reports/leads/exports",
        json={
            "scope": "PUBLIC_POOL",
            "keyword": "仙桃",
            "customer_source": "OPERATION_ENTRY",
            "source_kind": "PLATFORM_MANUAL",
            "completeness": "INCOMPLETE",
            "duplicate_status": "CLEAR",
            "created_from": "2026-08-30T00:00:00+08:00",
            "created_to": "2026-09-01T00:00:00+08:00",
            "submitter_user_id": submitter_user_id,
            "idempotency_key": "feedback-831-public-pool-export",
        },
    )

    assert response.status_code == 200, response.text
    task_id = response.json()["data"]["id"]
    with factory() as db:
        task = db.scalar(select(LeadExportTask).where(LeadExportTask.id == task_id))
        assert task is not None
        assert task.filters_json == {
            "scope": "PUBLIC_POOL",
            "keyword": "仙桃",
            "customer_source": "OPERATION_ENTRY",
            "source_kind": "PLATFORM_MANUAL",
            "completeness": "INCOMPLETE",
            "duplicate_status": "CLEAR",
            "created_from": "2026-08-29T16:00:00+00:00",
            "created_to": "2026-08-31T16:00:00+00:00",
            "submitter_user_id": submitter_user_id,
        }


def test_public_pool_export_streams_one_stable_result_set(db) -> None:
    operation = User(display_name="批量导出运营", status="ACTIVE")
    db.add(operation)
    db.flush()
    created_at = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)
    leads = []
    for index in range(501):
        lead = _lead(
            submitter_id=operation.id,
            phone=f"1390013{index:04d}",
            source_kind=LeadSourceKind.PLATFORM_MANUAL.value,
            status=LeadV12Status.DRAFT.value,
        )
        lead.customer_name = f"批量客户{index:04d}"
        lead.consent_confirmed = False
        lead.pending_reason = "PUBLIC_POOL_INCOMPLETE"
        lead.created_at = created_at
        leads.append(lead)
    db.add_all(leads)
    db.commit()

    select_count = 0

    def count_selects(_conn, _cursor, statement, _parameters, _context, _many):
        nonlocal select_count
        if statement.lstrip().upper().startswith("SELECT"):
            select_count += 1

    event.listen(db.get_bind(), "before_cursor_execute", count_selects)
    archive_path = None
    try:
        archive_path, count = build_lead_export_workbook(
            db,
            {"scope": "PUBLIC_POOL"},
        )
        rows = _read_lead_rows(archive_path)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", count_selects)
        if archive_path is not None:
            archive_path.unlink(missing_ok=True)

    assert count == 501
    assert len(rows) == 501
    assert len({row["客资编号"] for row in rows}) == 501
    assert select_count <= 3
