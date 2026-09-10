from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.api.src.core.auth import Principal
from apps.api.src.core.enums import AssignmentStatus
from apps.api.src.core.errors import AppError
from apps.api.src.core.models import (
    Assignment,
    AssignmentEvent,
    AuditLog,
    Company,
    FollowUp,
    Lead,
    LeadExportTask,
    Notification,
    NotificationOutbox,
    PointsAccount,
    PointsLedger,
    Region,
    StorageCleanupOutbox,
    User,
)
from apps.api.src.core.models_v12 import (
    CompanyLeadCapability,
    CompanyServiceAreaV12,
    LeadDedupEvent,
)
from apps.api.src.core.security import encrypt_text, fingerprint_phone, hash_phone
from apps.api.src.core.v12_enums import LeadSourceKind, LeadV12Status
from apps.api.src.services.lead_supply_v12 import correct_platform_lead
from apps.api.src.services.assignment_timeout_v12 import run_assignment_timeouts_v12
from apps.api.tests.xlsx_reader import read_xlsx


ADMIN_WORKBENCH = Path("apps/admin/public/v12-operations.js")
H5_WORKBENCH = Path("apps/h5/public/v12-workbench.js")
LEAD_EXPORT_SERVICE = Path("apps/api/src/services/lead_export_v12.py")
INSIGHTS_ROUTER = Path("apps/api/src/routers/v12_insights.py")


def _login(client, username: str, password: str) -> None:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text


def _lead(*, operation_id: str, phone: str, name: str) -> Lead:
    now = datetime.now(timezone.utc)
    return Lead(
        source_type=LeadSourceKind.PLATFORM_MANUAL.value,
        source_kind=LeadSourceKind.PLATFORM_MANUAL.value,
        submitter_user_id=operation_id,
        customer_name=name,
        phone_encrypted=encrypt_text(phone),
        phone_hash=hash_phone(phone),
        phone_fingerprint=fingerprint_phone(phone),
        consent_confirmed=True,
        city="上海市",
        district="浦东新区",
        region_code="310115",
        category_code="OLD_RENOVATION",
        brand_code="ZHONGSHU",
        source_channel="MANUAL",
        need_summary="客户反馈验收测试",
        status=LeadV12Status.DISPATCHED.value,
        review_status="APPROVED",
        duplicate_status="CLEAR",
        imported_at=now,
        submitted_at=now,
        raw_payload={},
    )


def _seed_item_5_assignment(
    factory,
    *,
    phone: str,
    name: str,
    idempotency_key: str,
    snapshot_version: int,
    assigned_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert company is not None and operation is not None
        _enable_item_7_receiver(db, company)
        lead = _lead(operation_id=operation.id, phone=phone, name=name)
        lead.snapshot_version = snapshot_version
        db.add(lead)
        db.flush()
        assignment = Assignment(
            lead_id=lead.id,
            company_id=company.id,
            receiver_company_id=company.id,
            status=AssignmentStatus.PENDING_CLAIM.value,
            points_price=100,
            claim_points=100,
            price_version=1,
            lead_snapshot={"region_code": "310115"},
            assigned_by=operation.id,
            assigned_at=assigned_at or now,
            expires_at=expires_at,
            idempotency_key=idempotency_key,
        )
        db.add(assignment)
        db.flush()
        lead.current_assignment_id = assignment.id
        db.commit()
        return lead.id, assignment.id


def test_item_1_company_received_count_drills_down_with_optional_status_filter(api_client) -> None:
    client, factory = api_client
    now = datetime.now(timezone.utc)
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert company is not None and operation is not None
        following_lead = _lead(
            operation_id=operation.id,
            phone="13900139801",
            name="跟进中客户",
        )
        returned_lead = _lead(
            operation_id=operation.id,
            phone="13900139802",
            name="已退回客户",
        )
        db.add_all([following_lead, returned_lead])
        db.flush()
        following = Assignment(
            lead_id=following_lead.id,
            company_id=company.id,
            receiver_company_id=company.id,
            status=AssignmentStatus.FOLLOWING.value,
            points_price=100,
            claim_points=100,
            price_version=1,
            lead_snapshot={},
            assigned_by=operation.id,
            assigned_at=now - timedelta(minutes=1),
            claimed_at=now,
            idempotency_key="feedback-item-1-following",
        )
        returned = Assignment(
            lead_id=returned_lead.id,
            company_id=company.id,
            receiver_company_id=company.id,
            status=AssignmentStatus.RETURNED.value,
            points_price=100,
            claim_points=100,
            price_version=1,
            lead_snapshot={},
            assigned_by=operation.id,
            assigned_at=now,
            claimed_at=now,
            released_at=now,
            idempotency_key="feedback-item-1-returned",
        )
        db.add_all([following, returned])
        db.flush()
        following_lead.current_assignment_id = following.id
        returned_lead.current_assignment_id = returned.id
        db.commit()
        company_id = company.id

    _login(client, "operation", "Operation123!")

    all_response = client.get(
        f"/api/v1/v1.2/companies/{company_id}/assignments?page=1&page_size=100"
    )
    assert all_response.status_code == 200, all_response.text
    all_data = all_response.json()["data"]
    assert all_data["total"] >= 2
    assert {"FOLLOWING", "RETURNED"} <= {
        item["status"] for item in all_data["items"]
    }

    returned_response = client.get(
        f"/api/v1/v1.2/companies/{company_id}/assignments"
        "?assignment_status=RETURNED&page=1&page_size=100"
    )
    assert returned_response.status_code == 200, returned_response.text
    returned_data = returned_response.json()["data"]
    assert returned_data["total"] >= 1
    assert returned_data["items"]
    assert all(item["status"] == "RETURNED" for item in returned_data["items"])


def test_item_1_admin_workbench_makes_total_and_status_counts_clickable() -> None:
    source = ADMIN_WORKBENCH.read_text(encoding="utf-8")

    assert "data-company-received-total" in source
    assert "data-company-received-status" in source
    assert "assignment_status" in source


def test_item_2_receive_confirmation_is_independent_from_current_followup(api_client) -> None:
    client, factory = api_client
    now = datetime.now(timezone.utc)
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert company is not None and operation is not None
        lead = _lead(
            operation_id=operation.id,
            phone="13900139803",
            name="确认与跟进分离客户",
        )
        lead.status = LeadV12Status.FOLLOWING.value
        lead.current_follow_status = "INTERESTED"
        db.add(lead)
        db.flush()
        assignment = Assignment(
            lead_id=lead.id,
            company_id=company.id,
            receiver_company_id=company.id,
            status=AssignmentStatus.FOLLOWING.value,
            points_price=100,
            claim_points=100,
            price_version=1,
            lead_snapshot={},
            assigned_by=operation.id,
            assigned_at=now,
            claimed_at=now,
            idempotency_key="feedback-item-2-confirmed",
        )
        db.add(assignment)
        db.flush()
        lead.current_assignment_id = assignment.id
        db.commit()
        assignment_id = assignment.id

    _login(client, "franchise_demo", "Franchise123!")
    response = client.get(f"/api/v1/v1.2/assignments/{assignment_id}")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["receive_confirmation_status"] == "CONFIRMED"
    assert data["receive_confirmed_at"] is not None
    assert data["current_follow_status"] == "INTERESTED"


def test_item_2_h5_displays_receive_confirmation_and_followup_separately() -> None:
    source = H5_WORKBENCH.read_text(encoding="utf-8")
    detail = source[source.index("async function assignmentDetail") : source.index("async function claim")]

    assert "接收确认" in detail
    assert "当前跟进" in detail
    assert "receive_confirmation_status" in detail
    assert "确认接收" in detail


def test_item_3_other_source_requires_detail_before_submission(api_client) -> None:
    client, factory = api_client
    _login(client, "operation", "Operation123!")
    draft_response = client.post(
        "/api/v1/v1.2/platform/leads",
        json={
            "customer_name": "其他来源客户",
            "phone": "13900139804",
            "region_code": "310000",
            "source_channel": "OTHER",
            "consent_confirmed": True,
        },
    )
    assert draft_response.status_code == 200, draft_response.text
    lead_id = draft_response.json()["data"]["id"]

    rejected = client.post(f"/api/v1/v1.2/platform/leads/{lead_id}/submit")
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["details"]["fields"]["source_detail"]

    updated = client.patch(
        f"/api/v1/v1.2/platform/leads/{lead_id}",
        json={"source_detail": "  老客户转介绍  "},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["source_channel"] == "OTHER"
    assert updated.json()["data"]["source_detail"] == "老客户转介绍"

    accepted = client.post(f"/api/v1/v1.2/platform/leads/{lead_id}/submit")
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["data"]["lead"]["source_detail"] == "老客户转介绍"

    with factory() as db:
        lead = db.get(Lead, lead_id)
        assert lead is not None
        assert lead.source_detail == "老客户转介绍"


def test_item_3_region_search_returns_standard_code_and_full_parent_path(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        db.add_all(
            [
                Region(
                    code="990000",
                    name="测试省",
                    level="PROVINCE",
                    parent_code=None,
                    aliases=[],
                    active=True,
                ),
                Region(
                    code="990100",
                    name="测试市",
                    level="CITY",
                    parent_code="990000",
                    aliases=[],
                    active=True,
                ),
                Region(
                    code="990101",
                    name="同名区",
                    level="DISTRICT",
                    parent_code="990100",
                    aliases=["测试同名区"],
                    active=True,
                ),
            ]
        )
        db.commit()

    response = client.get(
        "/api/v1/master-data/regions/search?keyword="
        + "%E5%90%8C%E5%90%8D%E5%8C%BA"
    )
    assert response.status_code == 200, response.text
    item = next(row for row in response.json()["data"] if row["code"] == "990101")
    assert item["level"] == "DISTRICT"
    assert item["path_codes"] == ["990000", "990100", "990101"]
    assert item["path_label"] == "测试省 · 测试市 · 同名区"


def test_item_3_admin_form_uses_other_detail_and_region_search() -> None:
    source = ADMIN_WORKBENCH.read_text(encoding="utf-8")

    assert "['OTHER','其他']" in source
    assert "item?.source_channel||'OTHER'" in source
    assert 'id="platform-lead-source-detail"' in source
    assert "source_detail" in source
    assert "/master-data/regions/search" in source


def test_item_4_service_region_select_all_is_scoped_to_current_parent() -> None:
    source = ADMIN_WORKBENCH.read_text(encoding="utf-8")
    markup = source[
        source.index("function serviceRegionBuilderMarkup") :
        source.index("async function openNewFranchiseCompany")
    ]

    assert "全选当前省城市" in markup
    assert "全选当前市区县" in markup
    assert "select-province-cities" in markup
    assert "select-city-districts" in markup
    assert "cities.filter(item=>item.province_code===province.value)" in markup
    assert "currentCity()?.districts" in markup
    assert "ensurePrimary" not in markup
    assert "addMany([{code:cityItem.code" not in markup
    assert "items.map(item=>({code:item.code,label:`${cityItem.option_name} · ${item.name}`,level:'DISTRICT'}))" in markup


def test_item_4_all_current_city_districts_accept_city_only_lead_and_claim(
    api_client,
) -> None:
    client, factory = api_client
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        assert company is not None
        if db.scalar(
            select(CompanyLeadCapability).where(
                CompanyLeadCapability.company_id == company.id,
                CompanyLeadCapability.capability_code == "LEAD_RECEIVER",
            )
        ) is None:
            db.add(
                CompanyLeadCapability(
                    company_id=company.id,
                    capability_code="LEAD_RECEIVER",
                    active=True,
                    review_status="APPROVED",
                )
            )
        db.execute(
            delete(CompanyServiceAreaV12).where(
                CompanyServiceAreaV12.company_id == company.id
            )
        )
        db.add_all(
            [
                CompanyServiceAreaV12(
                    company_id=company.id,
                    region_code=code,
                    region_level="DISTRICT",
                    active=True,
                    review_status="APPROVED",
                )
                for code in ("310104", "310115")
            ]
        )
        db.commit()
        company_id = company.id

    payload = _item_7_quick_dispatch_payload(
        factory,
        company_id,
        phone="13900139825",
        key="feedback-item-4-city-covered",
    )
    payload.update({"district": None, "region_code": "310000"})
    _login(client, "operation", "Operation123!")
    preview = client.post(
        "/api/v1/v1.2/platform/leads/quick-dispatch/candidates?keyword=SH-DEMO",
        json={
            key: value
            for key, value in payload.items()
            if key not in {"company_id", "employee_user_id", "idempotency_key", "note"}
        },
    )
    assert preview.status_code == 200, preview.text
    candidate = next(
        item
        for item in preview.json()["data"]["candidates"]
        if item["company_id"] == company_id
    )
    assert candidate["eligible"] is True
    assert candidate["region_match"] is True

    dispatched = client.post(
        "/api/v1/v1.2/platform/leads/quick-dispatch",
        json=payload,
    )
    assert dispatched.status_code == 200, dispatched.text
    assignment_id = dispatched.json()["data"]["assignment"]["id"]
    client.post("/api/v1/auth/logout")
    _login(client, "franchise_employee_demo", "Employee123!")
    claimed = client.post(f"/api/v1/v1.2/assignments/{assignment_id}/claim")
    assert claimed.status_code == 200, claimed.text


def test_item_4_missing_one_current_city_district_does_not_expand_scope(
    api_client,
) -> None:
    client, factory = api_client
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        assert company is not None
        _enable_item_7_receiver(db, company)
        db.execute(
            delete(CompanyServiceAreaV12).where(
                CompanyServiceAreaV12.company_id == company.id
            )
        )
        db.add(
            CompanyServiceAreaV12(
                company_id=company.id,
                region_code="310115",
                region_level="DISTRICT",
                active=True,
                review_status="APPROVED",
            )
        )
        db.commit()
        company_id = company.id

    payload = _item_7_quick_dispatch_payload(
        factory,
        company_id,
        phone="13900139826",
        key="feedback-item-4-city-incomplete",
    )
    payload.update({"district": None, "region_code": "310000"})
    _login(client, "operation", "Operation123!")
    preview = client.post(
        "/api/v1/v1.2/platform/leads/quick-dispatch/candidates?keyword=SH-DEMO",
        json={
            key: value
            for key, value in payload.items()
            if key not in {"company_id", "employee_user_id", "idempotency_key", "note"}
        },
    )
    assert preview.status_code == 200, preview.text
    assert all(
        item["company_id"] != company_id
        for item in preview.json()["data"]["candidates"]
    )
    dispatched = client.post(
        "/api/v1/v1.2/platform/leads/quick-dispatch",
        json=payload,
    )
    assert dispatched.status_code == 409, dispatched.text
    assert dispatched.json()["code"] == "DISPATCH_CANDIDATE_INELIGIBLE"
    assert "SERVICE_REGION_MISMATCH" in dispatched.json()["details"]["reasons"]


def test_item_5_unassigned_platform_lead_can_be_corrected_directly(api_client) -> None:
    client, _ = api_client
    _login(client, "operation", "Operation123!")
    draft = client.post(
        "/api/v1/v1.2/platform/leads",
        json={
            "customer_name": "更正前姓名",
            "phone": "13900139805",
            "region_code": "310000",
            "source_channel": "MANUAL",
            "consent_confirmed": True,
        },
    ).json()["data"]
    submitted = client.post(
        f"/api/v1/v1.2/platform/leads/{draft['id']}/submit"
    ).json()["data"]["lead"]
    assert submitted["status"] == "READY_DISPATCH"

    corrected = client.patch(
        f"/api/v1/v1.2/platform/leads/{draft['id']}/correction",
        json={
            "customer_name": "更正后姓名",
            "expected_snapshot_version": submitted["snapshot_version"],
        },
    )
    assert corrected.status_code == 200, corrected.text
    data = corrected.json()["data"]
    assert data["customer_name"] == "更正后姓名"
    assert data["status"] == "READY_DISPATCH"
    assert data["snapshot_version"] == submitted["snapshot_version"] + 1


def test_item_5_correction_lock_refreshes_a_stale_session_identity(api_client) -> None:
    _client, factory = api_client
    with factory() as setup_db:
        operation = setup_db.scalar(select(User).where(User.username == "operation"))
        assert operation is not None
        lead = _lead(
            operation_id=operation.id,
            phone="13900139819",
            name="并发更正前",
        )
        lead.status = LeadV12Status.DRAFT.value
        lead.review_status = "DRAFT"
        lead.current_assignment_id = None
        lead.snapshot_version = 4
        setup_db.add(lead)
        setup_db.commit()
        lead_id = lead.id
        operation_id = operation.id

    first = factory()
    second = factory()
    try:
        stale = first.get(Lead, lead_id)
        assert stale is not None and stale.snapshot_version == 4
        first.commit()
        second.execute(
            update(Lead)
            .where(Lead.id == lead_id)
            .values(customer_name="另一运营已更正", snapshot_version=5)
        )
        second.commit()
        principal = Principal(
            user_id=operation_id,
            display_name="运营管理员",
            company_id=None,
            role_codes=frozenset({"OPERATION"}),
            permission_codes=frozenset({"lead.manual.manage"}),
            session_version=1,
        )
        with pytest.raises(AppError) as error:
            correct_platform_lead(
                first,
                lead_id=lead_id,
                principal=principal,
                values={"customer_name": "当前请求的更正"},
                reason=None,
                expected_snapshot_version=4,
            )
        assert error.value.code == "LEAD_VERSION_CONFLICT"
        assert error.value.details["current_snapshot_version"] == 5
    finally:
        first.close()
        second.close()


def test_item_5_dispatched_correction_requires_reason_version_and_rechecks_receiver(api_client) -> None:
    client, factory = api_client
    now = datetime.now(timezone.utc)
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert company is not None and operation is not None
        _enable_item_7_receiver(db, company)
        lead = _lead(
            operation_id=operation.id,
            phone="13900139806",
            name="已派发待更正客户",
        )
        lead.snapshot_version = 7
        db.add(lead)
        db.flush()
        assignment = Assignment(
            lead_id=lead.id,
            company_id=company.id,
            receiver_company_id=company.id,
            status=AssignmentStatus.PENDING_CLAIM.value,
            points_price=100,
            claim_points=100,
            price_version=1,
            lead_snapshot={"region_code": "310115"},
            assigned_by=operation.id,
            assigned_at=now,
            idempotency_key="feedback-item-5-dispatched",
        )
        db.add(assignment)
        db.flush()
        lead.current_assignment_id = assignment.id
        db.commit()
        lead_id = lead.id
        assignment_id = assignment.id

    _login(client, "operation", "Operation123!")
    missing_reason = client.patch(
        f"/api/v1/v1.2/platform/leads/{lead_id}/correction",
        json={"region_code": "110000", "expected_snapshot_version": 7},
    )
    assert missing_reason.status_code == 422, missing_reason.text

    stale = client.patch(
        f"/api/v1/v1.2/platform/leads/{lead_id}/correction",
        json={
            "region_code": "110000",
            "reason": "客户确认实际建房地在北京",
            "expected_snapshot_version": 6,
        },
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "LEAD_VERSION_CONFLICT"

    corrected = client.patch(
        f"/api/v1/v1.2/platform/leads/{lead_id}/correction",
        json={
            "region_code": "110000",
            "reason": "客户确认实际建房地在北京",
            "expected_snapshot_version": 7,
        },
    )
    assert corrected.status_code == 200, corrected.text
    data = corrected.json()["data"]
    assert data["region_code"] == "110000"
    assert data["current_assignment_id"] == assignment_id
    assert data["pending_reason"] == "CORRECTION_REVIEW_REQUIRED"
    assert "SERVICE_REGION_MISMATCH" in data["correction_issues"]
    assert data["snapshot_version"] == 8

    renamed = client.patch(
        f"/api/v1/v1.2/platform/leads/{lead_id}/correction",
        json={
            "customer_name": "只更正姓名的客户",
            "reason": "客户补充了真实姓名",
            "expected_snapshot_version": 8,
        },
    )
    assert renamed.status_code == 200, renamed.text
    renamed_data = renamed.json()["data"]
    assert renamed_data["pending_reason"] == "CORRECTION_REVIEW_REQUIRED"
    assert "SERVICE_REGION_MISMATCH" in renamed_data["correction_issues"]
    assert renamed_data["snapshot_version"] == 9

    no_change = client.patch(
        f"/api/v1/v1.2/platform/leads/{lead_id}/correction",
        json={
            "customer_name": "只更正姓名的客户",
            "phone": None,
            "reason": "再次核对但事实没有变化",
            "expected_snapshot_version": 9,
        },
    )
    assert no_change.status_code == 422, no_change.text
    assert no_change.json()["code"] == "LEAD_CORRECTION_NO_CHANGES"

    with factory() as db:
        audits = list(
            db.scalars(
            select(AuditLog).where(
                AuditLog.action == "V12_PLATFORM_LEAD_FACT_CORRECTION",
                AuditLog.resource_id == lead_id,
            )
            ).all()
        )
        assert {tuple(item.metadata_json["changed_fields"]) for item in audits} == {
            ("region_code",),
            ("customer_name",),
        }
        region_audit = next(
            item for item in audits if item.metadata_json["changed_fields"] == ["region_code"]
        )
        assert region_audit.metadata_json["reason"] == "客户确认实际建房地在北京"
        assert region_audit.before_json["region_code"] == "310115"
        assert region_audit.after_json["region_code"] == "110000"
        lead = db.get(Lead, lead_id)
        assert lead is not None
        assert lead.snapshot_version == 9

    client.post("/api/v1/auth/logout")
    _login(client, "franchise_demo", "Franchise123!")
    legacy_detail = client.get(f"/api/v1/claims/assignments/{assignment_id}")
    assert legacy_detail.status_code == 200, legacy_detail.text
    legacy_data = legacy_detail.json()["data"]
    assert legacy_data["lead"]["customer_name"] == "只更正姓名的客户"
    assert legacy_data["lead"]["region_code"] == "110000"
    assert legacy_data["historical_lead_snapshot"]["region_code"] == "310115"
    blocked = client.post(f"/api/v1/v1.2/assignments/{assignment_id}/claim")
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["code"] == "LEAD_CORRECTION_REVIEW_REQUIRED"
    legacy_blocked = client.post(
        f"/api/v1/claims/assignments/{assignment_id}",
        json={"idempotency_key": "feedback-item-5-legacy-claim"},
    )
    assert legacy_blocked.status_code == 409, legacy_blocked.text
    assert legacy_blocked.json()["code"] == "LEAD_CORRECTION_REVIEW_REQUIRED"

    client.post("/api/v1/auth/logout")
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        assert company is not None
        db.add(
            CompanyServiceAreaV12(
                company_id=company.id,
                region_code="110000",
                region_level="PROVINCE",
                active=True,
                review_status="APPROVED",
            )
        )
        db.commit()
    _login(client, "operation", "Operation123!")
    rechecked = client.post(
        f"/api/v1/v1.2/platform/leads/{lead_id}/correction/recheck",
        json={
            "reason": "接收方服务区域恢复后再次复核",
            "expected_snapshot_version": 9,
        },
    )
    assert rechecked.status_code == 200, rechecked.text
    assert rechecked.json()["data"]["pending_reason"] is None
    assert rechecked.json()["data"]["correction_issues"] == []
    assert rechecked.json()["data"]["need_summary"] == "客户反馈验收测试"

    client.post("/api/v1/auth/logout")
    _login(client, "franchise_demo", "Franchise123!")
    claimed = client.post(f"/api/v1/v1.2/assignments/{assignment_id}/claim")
    assert claimed.status_code == 200, claimed.text


def test_item_5_post_dispatch_duplicate_phone_correction_is_rejected(api_client) -> None:
    client, factory = api_client
    now = datetime.now(timezone.utc)
    duplicate_phone = "13900139816"
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert company is not None and operation is not None
        _enable_item_7_receiver(db, company)
        existing = _lead(
            operation_id=operation.id,
            phone=duplicate_phone,
            name="已存在客户",
        )
        lead = _lead(
            operation_id=operation.id,
            phone="13977779816",
            name="电话待更正客户",
        )
        lead.snapshot_version = 3
        db.add_all([existing, lead])
        db.flush()
        assignment = Assignment(
            lead_id=lead.id,
            company_id=company.id,
            receiver_company_id=company.id,
            status=AssignmentStatus.PENDING_CLAIM.value,
            points_price=100,
            claim_points=100,
            price_version=1,
            lead_snapshot={"customer_name": lead.customer_name, "region_code": lead.region_code},
            assigned_by=operation.id,
            assigned_at=now,
            idempotency_key="feedback-item-5-post-dispatch-dedup",
        )
        db.add(assignment)
        db.flush()
        lead.current_assignment_id = assignment.id
        db.commit()
        lead_id = lead.id
        assignment_id = assignment.id

    _login(client, "franchise_demo", "Franchise123!")
    initially_claimed = client.post(
        f"/api/v1/v1.2/assignments/{assignment_id}/claim"
    )
    assert initially_claimed.status_code == 200, initially_claimed.text
    client.post("/api/v1/auth/logout")
    _login(client, "operation", "Operation123!")
    corrected = client.patch(
        f"/api/v1/v1.2/platform/leads/{lead_id}/correction",
        json={
            "phone": duplicate_phone,
            "reason": "客户确认此前登记的号码错误",
            "expected_snapshot_version": 3,
        },
    )
    assert corrected.status_code == 409, corrected.text
    assert corrected.json()["code"] == "LEAD_PHONE_DUPLICATE"
    with factory() as db:
        lead = db.get(Lead, lead_id)
        assignment = db.get(Assignment, assignment_id)
        assert lead is not None and assignment is not None
        assert lead.phone_hash == hash_phone("13977779816")
        assert lead.snapshot_version == 3
        assert lead.pending_reason is None
        assert assignment.status == AssignmentStatus.CLAIMED.value
        correction_audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "V12_PLATFORM_LEAD_FACT_CORRECTION",
                AuditLog.resource_id == lead_id,
            )
        )
        assert correction_audit is None


def test_item_5_receiver_mismatch_releases_unclaimed_assignment_for_redispatch(
    api_client,
) -> None:
    client, factory = api_client
    now = datetime.now(timezone.utc)
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert company is not None and operation is not None
        _enable_item_7_receiver(db, company)
        lead = _lead(
            operation_id=operation.id,
            phone="13900139819",
            name="未领取更正改派客户",
        )
        lead.snapshot_version = 2
        db.add(lead)
        db.flush()
        assignment = Assignment(
            lead_id=lead.id,
            company_id=company.id,
            receiver_company_id=company.id,
            status=AssignmentStatus.PENDING_CLAIM.value,
            points_price=100,
            claim_points=100,
            price_version=1,
            lead_snapshot={"region_code": "310115"},
            assigned_by=operation.id,
            assigned_at=now,
            idempotency_key="feedback-item-5-release-pending",
        )
        db.add(assignment)
        db.flush()
        lead.current_assignment_id = assignment.id
        db.commit()
        lead_id = lead.id
        assignment_id = assignment.id

    _login(client, "operation", "Operation123!")
    corrected = client.patch(
        f"/api/v1/v1.2/platform/leads/{lead_id}/correction",
        json={
            "region_code": "110000",
            "reason": "客户确认实际地区为北京",
            "expected_snapshot_version": 2,
        },
    )
    assert corrected.status_code == 200, corrected.text
    assert "SERVICE_REGION_MISMATCH" in corrected.json()["data"]["correction_issues"]

    released = client.post(
        f"/api/v1/v1.2/platform/leads/{lead_id}/correction/release-for-redispatch",
        json={
            "reason": "地区更正后原接收方不再匹配，解除后重新派发",
            "expected_snapshot_version": 3,
        },
    )
    assert released.status_code == 200, released.text
    data = released.json()["data"]
    assert data["lead"]["status"] == LeadV12Status.READY_DISPATCH.value
    assert data["lead"]["current_assignment_id"] is None
    assert data["lead"]["correction_issues"] == []
    assert data["assignment"]["status"] == AssignmentStatus.RELEASED.value
    assert data["refund_ledger"] is None
    processed = client.get(
        "/api/v1/v1.2/operations/my-processed?page=1&page_size=100"
    )
    assert processed.status_code == 200, processed.text
    assert any(
        item["action"] == "V12_PLATFORM_LEAD_CORRECTION_REDISPATCH"
        and item["resource_id"] == lead_id
        for item in processed.json()["data"]["items"]
    )

    with factory() as db:
        assert db.scalar(
            select(func.count(AssignmentEvent.id)).where(
                AssignmentEvent.assignment_id == assignment_id,
                AssignmentEvent.event_type == "V12_CORRECTION_REDISPATCH_RELEASE",
            )
        ) == 1
        assert db.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.action == "V12_PLATFORM_LEAD_CORRECTION_REDISPATCH",
                AuditLog.resource_id == lead_id,
            )
        ) == 1
        assert db.scalar(
            select(func.count(PointsLedger.id)).where(
                PointsLedger.business_type == "V12_CORRECTION_REDISPATCH_REFUND",
                PointsLedger.business_id == assignment_id,
            )
        ) == 0


def test_item_5_receiver_mismatch_refunds_claimed_assignment_before_redispatch(
    api_client,
) -> None:
    client, factory = api_client
    now = datetime.now(timezone.utc)
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        operation = db.scalar(select(User).where(User.username == "operation"))
        account = (
            db.scalar(select(PointsAccount).where(PointsAccount.company_id == company.id))
            if company
            else None
        )
        assert company is not None and operation is not None and account is not None
        _enable_item_7_receiver(db, company)
        balance_before_claim = int(account.balance)
        lead = _lead(
            operation_id=operation.id,
            phone="13900139820",
            name="已领取更正改派客户",
        )
        lead.snapshot_version = 4
        db.add(lead)
        db.flush()
        assignment = Assignment(
            lead_id=lead.id,
            company_id=company.id,
            receiver_company_id=company.id,
            status=AssignmentStatus.PENDING_CLAIM.value,
            points_price=100,
            claim_points=100,
            price_version=1,
            lead_snapshot={"region_code": "310115"},
            assigned_by=operation.id,
            assigned_at=now,
            expires_at=now + timedelta(hours=1),
            idempotency_key="feedback-item-5-release-claimed",
        )
        db.add(assignment)
        db.flush()
        lead.current_assignment_id = assignment.id
        db.commit()
        lead_id = lead.id
        assignment_id = assignment.id
        company_id = company.id

    _login(client, "franchise_demo", "Franchise123!")
    claimed = client.post(f"/api/v1/v1.2/assignments/{assignment_id}/claim")
    assert claimed.status_code == 200, claimed.text
    claim_ledger_id = claimed.json()["data"]["ledger"]["id"]
    client.post("/api/v1/auth/logout")

    _login(client, "operation", "Operation123!")
    corrected = client.patch(
        f"/api/v1/v1.2/platform/leads/{lead_id}/correction",
        json={
            "region_code": "110000",
            "reason": "领取后客户确认实际地区为北京",
            "expected_snapshot_version": 4,
        },
    )
    assert corrected.status_code == 200, corrected.text
    released = client.post(
        f"/api/v1/v1.2/platform/leads/{lead_id}/correction/release-for-redispatch",
        json={
            "reason": "平台事实更正导致接收资格失效，全额退回后重新派发",
            "expected_snapshot_version": 5,
        },
    )
    assert released.status_code == 200, released.text
    refund = released.json()["data"]["refund_ledger"]
    assert refund["delta"] == 100
    assert refund["related_ledger_id"] == claim_ledger_id

    with factory() as db:
        account = db.scalar(
            select(PointsAccount).where(PointsAccount.company_id == company_id)
        )
        lead = db.get(Lead, lead_id)
        assignment = db.get(Assignment, assignment_id)
        assert account is not None and int(account.balance) == balance_before_claim
        assert lead is not None and lead.status == LeadV12Status.READY_DISPATCH.value
        assert lead.current_assignment_id is None
        assert assignment is not None and assignment.status == AssignmentStatus.RELEASED.value
        refund_ledger = db.scalar(
            select(PointsLedger).where(
                PointsLedger.business_type == "V12_CORRECTION_REDISPATCH_REFUND",
                PointsLedger.business_id == assignment_id,
            )
        )
        assert refund_ledger is not None
        assert refund_ledger.related_ledger_id == claim_ledger_id
        notification = db.scalar(
            select(Notification).where(
                Notification.company_id == company_id,
                Notification.scene == "V12_CORRECTION_REDISPATCH",
            )
        )
        assert notification is not None
        assert "100 积分已全额退回" in notification.body
        assert "139" not in notification.body
        notification_outbox = db.scalar(
            select(NotificationOutbox).where(
                NotificationOutbox.event_key
                == f"v12-correction:{assignment_id}:redispatch-notification"
            )
        )
        assert notification_outbox is not None
        assert notification_outbox.payload["notification_id"] == notification.id
        assert notification_outbox.payload["deep_link"] == notification.deep_link


def test_item_5_pending_correction_blocks_receiver_mutations_and_timeout(
    api_client,
) -> None:
    client, factory = api_client
    now = datetime.now(timezone.utc)
    pending_lead_id, pending_assignment_id = _seed_item_5_assignment(
        factory,
        phone="13900139821",
        name="待领取更正门禁客户",
        idempotency_key="feedback-item-5-pending-gates",
        snapshot_version=2,
        assigned_at=now - timedelta(hours=48),
        expires_at=now - timedelta(minutes=1),
    )
    claimed_lead_id, claimed_assignment_id = _seed_item_5_assignment(
        factory,
        phone="13900139822",
        name="已领取更正门禁客户",
        idempotency_key="feedback-item-5-claimed-gates",
        snapshot_version=3,
        expires_at=now + timedelta(hours=1),
    )

    _login(client, "franchise_demo", "Franchise123!")
    claimed = client.post(f"/api/v1/v1.2/assignments/{claimed_assignment_id}/claim")
    assert claimed.status_code == 200, claimed.text
    client.post("/api/v1/auth/logout")

    _login(client, "operation", "Operation123!")
    for lead_id, version in ((pending_lead_id, 2), (claimed_lead_id, 3)):
        corrected = client.patch(
            f"/api/v1/v1.2/platform/leads/{lead_id}/correction",
            json={
                "region_code": "110000",
                "reason": "客户确认实际地区为北京，原接收方不再匹配",
                "expected_snapshot_version": version,
            },
        )
        assert corrected.status_code == 200, corrected.text
        assert corrected.json()["data"]["pending_reason"] == "CORRECTION_REVIEW_REQUIRED"
    client.post("/api/v1/auth/logout")

    _login(client, "franchise_demo", "Franchise123!")
    refused = client.post(
        f"/api/v1/v1.2/assignments/{pending_assignment_id}/refuse",
        json={"reason": "不接收该客资"},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "LEAD_CORRECTION_REVIEW_REQUIRED"

    followed = client.post(
        f"/api/v1/followups/assignments/{claimed_assignment_id}",
        json={"status": "CONTACTED", "note": "尝试绕过更正门禁继续跟进"},
    )
    assert followed.status_code == 409, followed.text
    assert followed.json()["code"] == "LEAD_CORRECTION_REVIEW_REQUIRED"

    drafted = client.post(
        f"/api/v1/v1.2/returns/assignments/{claimed_assignment_id}/draft",
        json={
            "reason_code": "EMPTY_NUMBER",
            "description": "尝试在更正待处理后新建退回申请",
        },
    )
    assert drafted.status_code == 409, drafted.text
    assert drafted.json()["code"] == "LEAD_CORRECTION_REVIEW_REQUIRED"

    with factory() as db:
        result = run_assignment_timeouts_v12(db, now=now)
        db.commit()
        pending_lead = db.get(Lead, pending_lead_id)
        pending_assignment = db.get(Assignment, pending_assignment_id)
        assert result == {"reminded": 0, "expired": 0}
        assert pending_lead is not None
        assert pending_lead.pending_reason == "CORRECTION_REVIEW_REQUIRED"
        assert pending_lead.current_assignment_id == pending_assignment_id
        assert pending_assignment is not None
        assert pending_assignment.status == AssignmentStatus.PENDING_CLAIM.value


def test_item_5_existing_return_draft_cannot_be_first_submitted_after_correction(
    api_client,
) -> None:
    client, factory = api_client
    lead_id, assignment_id = _seed_item_5_assignment(
        factory,
        phone="13900139823",
        name="更正前退回草稿客户",
        idempotency_key="feedback-item-5-return-submit-gate",
        snapshot_version=6,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )

    _login(client, "franchise_demo", "Franchise123!")
    claimed = client.post(f"/api/v1/v1.2/assignments/{assignment_id}/claim")
    assert claimed.status_code == 200, claimed.text
    draft = client.post(
        f"/api/v1/v1.2/returns/assignments/{assignment_id}/draft",
        json={
            "reason_code": "EMPTY_NUMBER",
            "description": "更正前已经保存但尚未提交的退回草稿",
        },
    )
    assert draft.status_code == 200, draft.text
    return_id = draft.json()["data"]["id"]
    client.post("/api/v1/auth/logout")

    _login(client, "operation", "Operation123!")
    corrected = client.patch(
        f"/api/v1/v1.2/platform/leads/{lead_id}/correction",
        json={
            "region_code": "110000",
            "reason": "客户确认地区登记错误，需先处理原接收资格",
            "expected_snapshot_version": 6,
        },
    )
    assert corrected.status_code == 200, corrected.text
    client.post("/api/v1/auth/logout")

    _login(client, "franchise_demo", "Franchise123!")
    submitted = client.post(f"/api/v1/v1.2/returns/{return_id}/submit")
    assert submitted.status_code == 409, submitted.text
    assert submitted.json()["code"] == "LEAD_CORRECTION_REVIEW_REQUIRED"


def test_item_5_completed_assignment_correction_records_warning_without_reopening(
    api_client,
) -> None:
    client, factory = api_client
    lead_id, assignment_id = _seed_item_5_assignment(
        factory,
        phone="13900139824",
        name="已成交事实更正客户",
        idempotency_key="feedback-item-5-completed-correction",
        snapshot_version=8,
    )
    with factory() as db:
        lead = db.get(Lead, lead_id)
        assignment = db.get(Assignment, assignment_id)
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert lead is not None and assignment is not None and operation is not None
        db.add(
            _lead(
                operation_id=operation.id,
                phone="13900139827",
                name="已存在的重复号码客户",
            )
        )
        assignment.status = AssignmentStatus.COMPLETED.value
        lead.status = LeadV12Status.COMPLETED.value
        lead.current_follow_status = "DEAL"
        db.commit()

    _login(client, "operation", "Operation123!")
    duplicate = client.patch(
        f"/api/v1/v1.2/platform/leads/{lead_id}/correction",
        json={
            "region_code": "110000",
            "phone": "13900139827",
            "reason": "成交归档后客户补充了真实项目所在地",
            "expected_snapshot_version": 8,
        },
    )
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["code"] == "LEAD_PHONE_DUPLICATE"

    corrected = client.patch(
        f"/api/v1/v1.2/platform/leads/{lead_id}/correction",
        json={
            "region_code": "110000",
            "reason": "成交归档后客户补充了真实项目所在地",
            "expected_snapshot_version": 8,
        },
    )
    assert corrected.status_code == 200, corrected.text
    data = corrected.json()["data"]
    assert data["status"] == LeadV12Status.COMPLETED.value
    assert data["current_assignment_id"] == assignment_id
    assert data["pending_reason"] is None
    assert "SERVICE_REGION_MISMATCH" in data["correction_issues"]

    with factory() as db:
        assignment = db.get(Assignment, assignment_id)
        assert assignment is not None
        assert assignment.status == AssignmentStatus.COMPLETED.value


def test_item_5_admin_correction_uses_one_audited_request() -> None:
    source = ADMIN_WORKBENCH.read_text(encoding="utf-8")
    save = source[source.index("async function savePlatformLead") : source.index("async function submitPlatformLead")]

    assert "expected_snapshot_version" in save
    assert "platform-lead-correction-reason" in source
    assert "method:'PATCH'" in save
    assert "V12_PLATFORM_LEAD_FACT_CORRECTION" in Path(
        "apps/api/src/routers/v12_lead_supply.py"
    ).read_text(encoding="utf-8")
    assert "/correction/recheck" in source
    assert "重新检查接收资格" in source
    assert "解除原派发并重新入池" in source
    assert "/correction/release-for-redispatch" in source
    assert "releasePlatformLeadCorrection" in source
    assert "V12_PLATFORM_LEAD_CORRECTION_REDISPATCH:'解除原派发并重新入池'" in source


def test_item_6_list_shows_only_current_receiver_and_detail_keeps_history(api_client) -> None:
    client, factory = api_client
    now = datetime.now(timezone.utc)
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        current_company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        assert operation is not None and current_company is not None
        historical_company = Company(
            code="FEEDBACK-HISTORY",
            name="历史接收加盟商",
            status="DISABLED",
        )
        db.add(historical_company)
        db.flush()
        lead = _lead(
            operation_id=operation.id,
            phone="13900139807",
            name="转派接收方客户",
        )
        lead.status = LeadV12Status.FOLLOWING.value
        db.add(lead)
        db.flush()
        historical = Assignment(
            lead_id=lead.id,
            company_id=historical_company.id,
            receiver_company_id=historical_company.id,
            status=AssignmentStatus.RETURNED.value,
            points_price=100,
            claim_points=100,
            price_version=1,
            lead_snapshot={},
            assigned_by=operation.id,
            assigned_at=now - timedelta(minutes=1),
            claimed_at=now,
            released_at=now,
            idempotency_key="feedback-item-6-history",
        )
        current = Assignment(
            lead_id=lead.id,
            company_id=current_company.id,
            receiver_company_id=current_company.id,
            status=AssignmentStatus.FOLLOWING.value,
            points_price=100,
            claim_points=100,
            price_version=1,
            lead_snapshot={},
            assigned_by=operation.id,
            assigned_at=now,
            claimed_at=now,
            idempotency_key="feedback-item-6-current",
        )
        db.add_all([historical, current])
        db.flush()
        lead.current_assignment_id = current.id
        db.commit()
        lead_id = lead.id
        current_company_id = current_company.id

    _login(client, "operation", "Operation123!")
    listed = client.get("/api/v1/v1.2/platform/leads?page=1&page_size=200")
    assert listed.status_code == 200, listed.text
    row = next(item for item in listed.json()["data"]["items"] if item["id"] == lead_id)
    assert row["current_receiver_company_id"] == current_company_id
    assert row["current_receiver_company_name"] == current_company.name
    assert "assignment_history" not in row

    detail = client.get(f"/api/v1/v1.2/platform/leads/{lead_id}")
    assert detail.status_code == 200, detail.text
    data = detail.json()["data"]
    assert data["current_receiver_company_name"] == current_company.name
    assert [item["receiver_company_name"] for item in data["assignment_history"]] == [
        historical_company.name,
        current_company.name,
    ]


def test_item_6_admin_list_and_detail_separate_current_receiver_from_history() -> None:
    source = ADMIN_WORKBENCH.read_text(encoding="utf-8")

    assert "current_receiver_company_name" in source
    assert "assignment_history" in source
    assert "当前接收方" in source
    assert "派发历史" in source


def _item_7_quick_dispatch_payload(factory, company_id: str, *, phone: str, key: str) -> dict:
    with factory() as db:
        employee_id = db.scalar(
            select(User.id).where(User.username == "franchise_employee_demo")
        )
    assert employee_id is not None
    return {
        "customer_name": "快捷派发客户",
        "phone": phone,
        "city": "上海市",
        "district": "浦东新区",
        "region_code": "310115",
        "category_code": "OLD_RENOVATION",
        "brand_code": "ZHONGSHU",
        "source_channel": "OTHER",
        "source_detail": "老客户转介绍",
        "need_summary": "客户希望尽快联系",
        "consent_confirmed": True,
        "company_id": company_id,
        "employee_user_id": employee_id,
        "idempotency_key": key,
        "note": "创建后直接派发",
    }


def _enable_item_7_receiver(db, company: Company) -> None:
    if db.scalar(
        select(CompanyLeadCapability).where(
            CompanyLeadCapability.company_id == company.id,
            CompanyLeadCapability.capability_code == "LEAD_RECEIVER",
        )
    ) is None:
        db.add(
            CompanyLeadCapability(
                company_id=company.id,
                capability_code="LEAD_RECEIVER",
                active=True,
                review_status="APPROVED",
            )
        )
    if db.scalar(
        select(CompanyServiceAreaV12).where(
            CompanyServiceAreaV12.company_id == company.id,
            CompanyServiceAreaV12.region_code == "310115",
        )
    ) is None:
        db.add(
            CompanyServiceAreaV12(
                company_id=company.id,
                region_code="310115",
                region_level="DISTRICT",
                active=True,
                review_status="APPROVED",
            )
        )
    db.commit()


def test_item_7_quick_dispatch_is_atomic_and_idempotent(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        assert company is not None
        _enable_item_7_receiver(db, company)
        company_id = company.id

    _login(client, "operation", "Operation123!")
    payload = _item_7_quick_dispatch_payload(
        factory,
        company_id,
        phone="13900139808",
        key="feedback-item-7-quick-dispatch",
    )
    created = client.post(
        "/api/v1/v1.2/platform/leads/quick-dispatch",
        json=payload,
    )
    assert created.status_code == 200, created.text
    created_data = created.json()["data"]
    assert created_data["idempotent"] is False
    assert created_data["lead"]["status"] == LeadV12Status.DISPATCHED.value
    assert created_data["assignment"]["company_id"] == company_id
    assert created_data["lead"]["current_assignment_id"] == created_data["assignment"]["id"]

    replayed = client.post(
        "/api/v1/v1.2/platform/leads/quick-dispatch",
        json=payload,
    )
    assert replayed.status_code == 200, replayed.text
    replayed_data = replayed.json()["data"]
    assert replayed_data["idempotent"] is True
    assert replayed_data["lead"]["id"] == created_data["lead"]["id"]
    assert replayed_data["assignment"]["id"] == created_data["assignment"]["id"]

    with factory() as db:
        lead_id = created_data["lead"]["id"]
        assignment_id = created_data["assignment"]["id"]
        assert db.scalar(select(func.count(Lead.id)).where(Lead.id == lead_id)) == 1
        assert db.scalar(
            select(func.count(Assignment.id)).where(
                Assignment.idempotency_key == payload["idempotency_key"]
            )
        ) == 1
        assert db.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.resource_id == assignment_id,
                AuditLog.action == "V12_MANUAL_DISPATCH",
            )
        ) == 1


def test_item_7_quick_dispatch_recovers_after_commit_conflict(
    api_client,
    monkeypatch,
) -> None:
    client, factory = api_client
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        assert company is not None
        _enable_item_7_receiver(db, company)
        company_id = company.id
    _login(client, "operation", "Operation123!")
    payload = _item_7_quick_dispatch_payload(
        factory,
        company_id,
        phone="13900139818",
        key="feedback-item-7-commit-conflict",
    )
    original_commit = Session.commit
    armed = {"value": True}

    def commit_then_report_conflict(session):
        original_commit(session)
        if armed["value"]:
            armed["value"] = False
            raise IntegrityError("simulated unique race", {}, RuntimeError("conflict"))

    with monkeypatch.context() as patcher:
        patcher.setattr(Session, "commit", commit_then_report_conflict)
        response = client.post(
            "/api/v1/v1.2/platform/leads/quick-dispatch",
            json=payload,
        )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["idempotent"] is True
    assignment_id = response.json()["data"]["assignment"]["id"]
    with factory() as db:
        assert db.scalar(
            select(func.count(Assignment.id)).where(
                Assignment.idempotency_key == payload["idempotency_key"]
            )
        ) == 1
        assert db.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.resource_id == assignment_id,
                AuditLog.action == "V12_MANUAL_DISPATCH",
            )
        ) == 1


def test_item_7_quick_dispatch_rolls_back_when_receiver_is_ineligible(api_client) -> None:
    client, factory = api_client
    phone = "13900139809"
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        assert company is not None
        company.status = "SUSPENDED"
        db.commit()
        company_id = company.id

    _login(client, "operation", "Operation123!")
    response = client.post(
        "/api/v1/v1.2/platform/leads/quick-dispatch",
        json=_item_7_quick_dispatch_payload(
            factory,
            company_id,
            phone=phone,
            key="feedback-item-7-rollback",
        ),
    )
    assert response.status_code == 409, response.text
    with factory() as db:
        assert db.scalar(
            select(func.count(Lead.id)).where(Lead.phone_hash == hash_phone(phone))
        ) == 0


def test_item_7_quick_dispatch_candidate_preview_has_no_persistent_side_effect(api_client) -> None:
    client, factory = api_client
    phone = "13900139810"
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        assert company is not None
        _enable_item_7_receiver(db, company)
        company_id = company.id

    _login(client, "operation", "Operation123!")
    payload = _item_7_quick_dispatch_payload(
        factory,
        company_id,
        phone=phone,
        key="feedback-item-7-preview",
    )
    preview = client.post(
        "/api/v1/v1.2/platform/leads/quick-dispatch/candidates",
        json={
            key: value
            for key, value in payload.items()
            if key not in {"company_id", "employee_user_id", "idempotency_key", "note"}
        },
    )
    assert preview.status_code == 200, preview.text
    candidates = preview.json()["data"]["candidates"]
    assert any(item["company_id"] == company_id and item["eligible"] for item in candidates)
    with factory() as db:
        assert db.scalar(
            select(func.count(Lead.id)).where(Lead.phone_hash == hash_phone(phone))
        ) == 0


def test_item_7_admin_workbench_exposes_quick_dispatch_action() -> None:
    source = ADMIN_WORKBENCH.read_text(encoding="utf-8")

    assert "创建并直接派发" in source
    assert "/platform/leads/quick-dispatch/candidates" in source
    assert "/platform/leads/quick-dispatch" in source
    assert "quickDispatchKeys" in source


def test_item_7_quick_dispatch_acquires_database_idempotency_lock_before_creation() -> None:
    source = Path("apps/api/src/routers/v12_lead_supply.py").read_text(encoding="utf-8")
    block = source[
        source.index("def quick_dispatch_platform_lead") :
        source.index('@router.patch("/platform/leads/{lead_id}")')
    ]

    lock_call = "acquire_manual_dispatch_idempotency_lock(db, body.idempotency_key)"
    assert lock_call in block
    assert block.index(lock_call) < block.index("replay = _existing_quick_dispatch")


def _prepare_item_8_lead(factory) -> tuple[str, str, str, datetime]:
    now = datetime.now(timezone.utc)
    phone = "13900139811"
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert company is not None and operation is not None
        lead = _lead(operation_id=operation.id, phone=phone, name="第八条筛选客户")
        lead.status = LeadV12Status.FOLLOWING.value
        lead.created_at = now
        db.add(lead)
        db.flush()
        assignment = Assignment(
            lead_id=lead.id,
            company_id=company.id,
            receiver_company_id=company.id,
            status=AssignmentStatus.FOLLOWING.value,
            points_price=100,
            claim_points=100,
            price_version=1,
            lead_snapshot={},
            assigned_by=operation.id,
            assigned_at=now,
            claimed_at=now,
            idempotency_key="feedback-item-8-following",
        )
        db.add(assignment)
        db.flush()
        lead.current_assignment_id = assignment.id
        db.add(
            FollowUp(
                assignment_id=assignment.id,
                company_id=company.id,
                status="INTERESTED",
                note="加盟商已与客户约好量房",
                next_followup_at=now + timedelta(days=1),
                created_by=db.scalar(
                    select(User.id).where(User.username == "franchise_demo")
                ),
                created_at=now,
            )
        )
        db.commit()
        return lead.id, assignment.id, operation.id, now


def test_item_8_operation_list_filters_created_time_and_separate_statuses(api_client) -> None:
    client, factory = api_client
    lead_id, _assignment_id, operation_id, now = _prepare_item_8_lead(factory)
    _login(client, "operation", "Operation123!")

    response = client.get(
        "/api/v1/v1.2/reports/leads",
        params={
            "created_from": (now - timedelta(minutes=1)).isoformat(),
            "created_to": (now + timedelta(minutes=1)).isoformat(),
            "lead_status": LeadV12Status.FOLLOWING.value,
            "assignment_status": AssignmentStatus.FOLLOWING.value,
            "assigned_by_user_id": operation_id,
            "page": 1,
            "page_size": 100,
        },
    )
    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]
    row = next(item for item in items if item["id"] == lead_id)
    assert row["lead_status"] == LeadV12Status.FOLLOWING.value
    assert row["assignment_status"] == AssignmentStatus.FOLLOWING.value
    assert row["assigned_by_user_id"] == operation_id
    assert row["assigned_by_name"] == "运营管理员"
    assert row["latest_followup"]["note"] == "加盟商已与客户约好量房"
    assert row["phone"] is None
    assert row["phone_masked"] == "139****9811"


def test_item_8_beijing_day_uses_exclusive_next_midnight(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert operation is not None
        included = _lead(
            operation_id=operation.id,
            phone="13900139820",
            name="北京时间当天最后一微秒客户",
        )
        excluded = _lead(
            operation_id=operation.id,
            phone="13900139821",
            name="北京时间次日零点客户",
        )
        included.created_at = datetime(
            2026,
            8,
            29,
            15,
            59,
            59,
            999500,
            tzinfo=timezone.utc,
        )
        excluded.created_at = datetime(2026, 8, 29, 16, 0, tzinfo=timezone.utc)
        db.add_all([included, excluded])
        db.commit()
        included_id = included.id
        excluded_id = excluded.id

    _login(client, "operation", "Operation123!")
    response = client.get(
        "/api/v1/v1.2/reports/leads",
        params={
            "created_from": "2026-08-28T16:00:00Z",
            "created_to": "2026-08-29T16:00:00Z",
            "page": 1,
            "page_size": 200,
        },
    )
    assert response.status_code == 200, response.text
    ids = {item["id"] for item in response.json()["data"]["items"]}
    assert included_id in ids
    assert excluded_id not in ids
    source = ADMIN_WORKBENCH.read_text(encoding="utf-8")
    assert "T00:00:00.000+08:00" in source
    assert "end?86400000:0" in source


def test_item_8_operation_detail_can_see_franchise_followup_history(api_client) -> None:
    client, factory = api_client
    lead_id, _assignment_id, _operation_id, _now = _prepare_item_8_lead(factory)
    _login(client, "operation", "Operation123!")

    response = client.get(f"/api/v1/v1.2/platform/leads/{lead_id}")
    assert response.status_code == 200, response.text
    history = response.json()["data"]["followup_history"]
    assert len(history) == 1
    assert history[0]["note"] == "加盟商已与客户约好量房"
    assert history[0]["created_by_name"] == "张老板"


def test_item_8_full_phone_export_runs_as_audited_background_task(api_client) -> None:
    from apps.api.src.services.lead_export_v12 import process_lead_export_tasks

    client, factory = api_client
    lead_id, _assignment_id, operation_id, now = _prepare_item_8_lead(factory)
    _login(client, "operation", "Operation123!")
    filters = {
        "created_from": (now - timedelta(minutes=1)).isoformat(),
        "created_to": (now + timedelta(minutes=1)).isoformat(),
        "lead_status": LeadV12Status.FOLLOWING.value,
        "assignment_status": AssignmentStatus.FOLLOWING.value,
        "assigned_by_user_id": operation_id,
    }
    requested = client.post(
        "/api/v1/v1.2/reports/leads/exports",
        json={**filters, "idempotency_key": "feedback-item-8-export"},
    )
    assert requested.status_code == 200, requested.text
    task_id = requested.json()["data"]["id"]
    assert requested.json()["data"]["status"] == "PENDING"

    with factory() as db:
        result = process_lead_export_tasks(db, limit=10)
        assert result["completed"] == 1

    status = client.get(f"/api/v1/v1.2/reports/leads/exports/{task_id}")
    assert status.status_code == 200, status.text
    assert status.json()["data"]["status"] == "COMPLETED"
    download = client.get(
        f"/api/v1/v1.2/reports/leads/exports/{task_id}/download"
    )
    assert download.status_code == 200, download.text
    assert download.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert ".xlsx" in download.headers["content-disposition"]
    workbook = read_xlsx(download.content)
    assert set(workbook) == {"客资明细", "跟进记录"}
    assert any(row["客资编号"] == lead_id for row in workbook["客资明细"])
    assert any(row["完整手机号"] == "13900139811" for row in workbook["客资明细"])
    assert any(
        row["跟进内容"] == "加盟商已与客户约好量房"
        for row in workbook["跟进记录"]
    )

    with factory() as db:
        task = db.get(LeadExportTask, task_id)
        assert task is not None
        assert task.file_name is not None and task.file_name.endswith(".xlsx")
        assert task.mime_type == (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        assert task.requested_by == operation_id
        assert task.filters_json["lead_status"] == LeadV12Status.FOLLOWING.value
        audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "V12_LEAD_EXPORT_REQUESTED",
                AuditLog.resource_id == task_id,
            )
        )
        assert audit is not None
        assert audit.actor_user_id == operation_id
        assert audit.metadata_json["filters"]["assignment_status"] == AssignmentStatus.FOLLOWING.value
        download_audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "V12_LEAD_EXPORT_DOWNLOADED",
                AuditLog.resource_id == task_id,
            )
        )
        assert download_audit is not None
        assert download_audit.actor_user_id == operation_id
        assert download_audit.metadata_json["owner_user_id"] == operation_id
        assert download_audit.metadata_json["file_sha256"] == task.sha256
        cleanup = db.scalar(
            select(StorageCleanupOutbox).where(
                StorageCleanupOutbox.event_key == f"lead-export-expire:{task_id}"
            )
        )
        assert cleanup is not None
        assert cleanup.object_key == task.object_key
        assert cleanup.next_attempt_at == task.expires_at


def test_item_8_export_request_recovers_after_commit_conflict(
    api_client,
    monkeypatch,
) -> None:
    client, factory = api_client
    _login(client, "operation", "Operation123!")
    original_commit = Session.commit
    armed = {"value": True}

    def commit_then_report_conflict(session):
        original_commit(session)
        if armed["value"]:
            armed["value"] = False
            raise IntegrityError("simulated unique race", {}, RuntimeError("conflict"))

    with monkeypatch.context() as patcher:
        patcher.setattr(Session, "commit", commit_then_report_conflict)
        response = client.post(
            "/api/v1/v1.2/reports/leads/exports",
            json={"idempotency_key": "feedback-item-8-commit-conflict"},
        )
    assert response.status_code == 200, response.text
    with factory() as db:
        assert db.scalar(
            select(func.count(LeadExportTask.id)).where(
                LeadExportTask.idempotency_key == "feedback-item-8-commit-conflict"
            )
        ) == 1


def test_item_8_export_queue_limits_active_tasks_per_requester(api_client) -> None:
    client, _factory = api_client
    _login(client, "operation", "Operation123!")
    first = client.post(
        "/api/v1/v1.2/reports/leads/exports",
        json={"idempotency_key": "feedback-item-8-active-limit-1"},
    )
    second = client.post(
        "/api/v1/v1.2/reports/leads/exports",
        json={"idempotency_key": "feedback-item-8-active-limit-2"},
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    replay = client.post(
        "/api/v1/v1.2/reports/leads/exports",
        json={"idempotency_key": "feedback-item-8-active-limit-1"},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["id"] == first.json()["data"]["id"]

    limited = client.post(
        "/api/v1/v1.2/reports/leads/exports",
        json={"idempotency_key": "feedback-item-8-active-limit-3"},
    )
    assert limited.status_code == 409, limited.text
    assert limited.json()["code"] == "LEAD_EXPORT_ACTIVE_LIMIT"
    assert limited.json()["details"]["scope"] == "REQUESTER"


def test_item_8_export_queue_limits_global_active_tasks(api_client) -> None:
    client, factory = api_client
    now = datetime.now(timezone.utc)
    with factory() as db:
        db.add_all(
            [
                LeadExportTask(
                    requested_by=None,
                    requested_by_name="并发队列测试",
                    status="PENDING",
                    filters_json={},
                    include_full_phone=True,
                    idempotency_key=f"feedback-item-8-global-limit-{index}",
                    row_count=0,
                    created_at=now,
                )
                for index in range(20)
            ]
        )
        db.commit()

    _login(client, "operation", "Operation123!")
    limited = client.post(
        "/api/v1/v1.2/reports/leads/exports",
        json={"idempotency_key": "feedback-item-8-global-limit-request"},
    )
    assert limited.status_code == 409, limited.text
    assert limited.json()["code"] == "LEAD_EXPORT_ACTIVE_LIMIT"
    assert limited.json()["details"]["scope"] == "GLOBAL"


def test_item_8_export_queue_limits_rolling_request_rate(api_client) -> None:
    client, factory = api_client
    now = datetime.now(timezone.utc)
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert operation is not None
        db.add_all(
            [
                LeadExportTask(
                    requested_by=operation.id,
                    requested_by_name=operation.display_name,
                    status="COMPLETED",
                    filters_json={},
                    include_full_phone=True,
                    idempotency_key=f"feedback-item-8-rate-limit-{index}",
                    row_count=0,
                    created_at=now - timedelta(minutes=index),
                )
                for index in range(10)
            ]
        )
        db.commit()

    _login(client, "operation", "Operation123!")
    limited = client.post(
        "/api/v1/v1.2/reports/leads/exports",
        json={"idempotency_key": "feedback-item-8-rate-limit-request"},
    )
    assert limited.status_code == 429, limited.text
    assert limited.json()["code"] == "LEAD_EXPORT_RATE_LIMIT"
    assert int(limited.headers["retry-after"]) > 0


def test_item_8_admin_workbench_has_filters_followups_and_background_export() -> None:
    source = ADMIN_WORKBENCH.read_text(encoding="utf-8")

    for token in (
        "created_from",
        "created_to",
        "lead_status",
        "assignment_status",
        "assigned_by_user_id",
        "派发运营人员",
        "加盟商跟进记录",
        "后台导出任务",
        "/reports/leads/exports",
    ):
        assert token in source
    assert "exportIdempotencyKey" in source
    export_handler = source[
        source.index("let exportIdempotencyKey=null") :
        source.index("document.querySelector('#lead-export-refresh')")
    ]
    assert export_handler.index("exportIdempotencyKey=null;toast") > export_handler.index(
        "await api('/v1.2/reports/leads/exports'"
    )


def test_item_8_date_filters_require_an_explicit_timezone(api_client) -> None:
    client, _factory = api_client
    _login(client, "operation", "Operation123!")
    mixed_window = {
        "created_from": "2026-08-29T00:00:00",
        "created_to": "2026-08-30T00:00:00Z",
    }

    report = client.get("/api/v1/v1.2/reports/leads", params=mixed_window)
    assert report.status_code == 422, report.text
    assert report.json()["code"] == "DATE_TIMEZONE_REQUIRED"

    processed = client.get(
        "/api/v1/v1.2/operations/my-processed",
        params=mixed_window,
    )
    assert processed.status_code == 422, processed.text
    assert processed.json()["code"] == "DATE_TIMEZONE_REQUIRED"

    export = client.post(
        "/api/v1/v1.2/reports/leads/exports",
        json={
            **mixed_window,
            "idempotency_key": "feedback-item-8-timezone",
        },
    )
    assert export.status_code == 422, export.text


def test_item_8_export_pipeline_streams_database_storage_and_download() -> None:
    service_source = LEAD_EXPORT_SERVICE.read_text(encoding="utf-8")
    router_source = INSIGHTS_ROUTER.read_text(encoding="utf-8")
    download_block = router_source[
        router_source.index("def download_lead_export") :
        router_source.index('@router.get("/reports/leads/export.csv")')
    ]

    assert "yield_per=EXPORT_STREAM_BATCH_SIZE" in service_source
    assert "storage.save_file(" in service_source
    assert "StreamingResponse(" in download_block
    assert "storage.iter_read(" in download_block
    assert "storage.read(" not in download_block


def test_item_8_postgresql_lease_heartbeat_uses_an_independent_session(
    monkeypatch,
) -> None:
    from apps.api.src.services import lead_export_v12

    class Dialect:
        name = "postgresql"

    class Engine:
        dialect = Dialect()

    engine = Engine()

    class Bind:
        dialect = Dialect()

        def __init__(self) -> None:
            self.engine = engine

    class MainSession:
        def get_bind(self):
            return Bind()

        def execute(self, _statement):
            raise AssertionError("主导出 Session 不得用于续租")

        def commit(self):
            raise AssertionError("主导出 Session 不得在流式游标期间提交")

    lease_state = {"executed": False, "committed": False}

    class LeaseSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _statement):
            lease_state["executed"] = True
            return type("Result", (), {"rowcount": 1})()

        def commit(self):
            lease_state["committed"] = True

        def rollback(self):
            raise AssertionError("有效租约不应回滚")

    def create_lease_session(*, bind, expire_on_commit):
        assert bind is engine
        assert expire_on_commit is False
        return LeaseSession()

    monkeypatch.setattr(lead_export_v12, "Session", create_lease_session)

    lead_export_v12._renew_lead_export_lease(
        MainSession(),
        task_id="task-1",
        attempt_token="attempt-1",
    )

    assert lease_state == {"executed": True, "committed": True}


def test_item_8_slow_upload_progress_renews_postgresql_lease_on_a_throttle(
    monkeypatch,
) -> None:
    from apps.api.src.services import lead_export_v12

    class Dialect:
        name = "postgresql"

    class Bind:
        dialect = Dialect()

    class MainSession:
        def get_bind(self):
            return Bind()

    now = {"value": 0.0}
    progress_events: list[float] = []
    lease_renewals: list[tuple[str, str]] = []
    monkeypatch.setattr(
        lead_export_v12,
        "_renew_lead_export_lease_from_bind",
        lambda _bind, *, task_id, attempt_token: lease_renewals.append(
            (task_id, attempt_token)
        ),
    )
    callback = lead_export_v12._lead_export_upload_progress(
        MainSession(),
        task_id="task-1",
        attempt_token="attempt-1",
        report_progress=lambda: progress_events.append(now["value"]),
        clock=lambda: now["value"],
    )

    for timestamp in (10.0, 59.0, 60.0, 61.0, 120.0):
        now["value"] = timestamp
        callback()

    assert progress_events == [10.0, 59.0, 60.0, 61.0, 120.0]
    assert lease_renewals == [
        ("task-1", "attempt-1"),
        ("task-1", "attempt-1"),
    ]


def test_item_8_export_worker_does_not_overwrite_a_newer_lease(
    api_client,
    monkeypatch,
    tmp_path,
) -> None:
    from apps.api.src.services import lead_export_v12
    from apps.api.src.services.storage import StoredObject

    client, factory = api_client
    _login(client, "operation", "Operation123!")
    requested = client.post(
        "/api/v1/v1.2/reports/leads/exports",
        json={"idempotency_key": "feedback-item-8-lease"},
    )
    assert requested.status_code == 200, requested.text
    task_id = requested.json()["data"]["id"]
    deleted: list[str] = []
    archive_path = tmp_path / "superseded.zip"

    class FakeStorage:
        def save_file(
            self,
            source,
            *,
            prefix,
            filename,
            mime_type,
            object_key=None,
            progress_callback=None,
        ):
            if progress_callback is not None:
                progress_callback()
            return StoredObject(
                object_key=object_key or "lead-exports/superseded.zip",
                size=source.stat().st_size,
                sha256="0" * 64,
                mime_type=mime_type,
            )

        def delete(self, object_key):
            deleted.append(object_key)

    def simulate_takeover(db, filters, *, heartbeat=None):
        db.execute(
            update(LeadExportTask)
            .where(LeadExportTask.id == task_id)
            .values(attempt_token="newer-worker-token")
        )
        db.commit()
        archive_path.write_bytes(b"archive")
        return archive_path, 1

    monkeypatch.setattr(lead_export_v12, "get_storage", lambda: FakeStorage())
    monkeypatch.setattr(
        lead_export_v12,
        "build_lead_export_workbook",
        simulate_takeover,
    )
    with factory() as db:
        result = lead_export_v12.process_lead_export_tasks(db, limit=1)
    assert result == {"claimed": 1, "completed": 0, "failed": 0, "superseded": 1}
    assert len(deleted) == 1
    assert deleted[0].startswith(f"lead-exports/")
    assert deleted[0].endswith(".xlsx")
    assert not archive_path.exists()
    with factory() as db:
        task = db.get(LeadExportTask, task_id)
        assert task is not None
        assert task.status == "RUNNING"
        assert task.attempt_token == "newer-worker-token"
        assert task.object_key is None


def test_item_8_lost_lease_persists_cleanup_when_immediate_delete_fails(
    api_client,
    monkeypatch,
    tmp_path,
) -> None:
    from apps.api.src.services import lead_export_v12
    from apps.api.src.services.storage import StoredObject

    client, factory = api_client
    _login(client, "operation", "Operation123!")
    requested = client.post(
        "/api/v1/v1.2/reports/leads/exports",
        json={"idempotency_key": "feedback-item-8-orphan-cleanup"},
    )
    assert requested.status_code == 200, requested.text
    task_id = requested.json()["data"]["id"]
    delete_attempts: list[str] = []
    archive_path = tmp_path / "orphaned.zip"

    class FailingDeleteStorage:
        def save_file(
            self,
            source,
            *,
            prefix,
            filename,
            mime_type,
            object_key=None,
            progress_callback=None,
        ):
            if progress_callback is not None:
                progress_callback()
            return StoredObject(
                object_key=object_key or "lead-exports/orphaned-sensitive.zip",
                size=source.stat().st_size,
                sha256="1" * 64,
                mime_type=mime_type,
            )

        def delete(self, object_key):
            delete_attempts.append(object_key)
            raise RuntimeError("temporary object storage outage")

    def simulate_takeover(db, filters, *, heartbeat=None):
        db.execute(
            update(LeadExportTask)
            .where(LeadExportTask.id == task_id)
            .values(attempt_token="newer-worker-token")
        )
        db.commit()
        archive_path.write_bytes(b"archive")
        return archive_path, 1

    monkeypatch.setattr(lead_export_v12, "get_storage", lambda: FailingDeleteStorage())
    monkeypatch.setattr(lead_export_v12, "build_lead_export_workbook", simulate_takeover)
    with factory() as db:
        result = lead_export_v12.process_lead_export_tasks(db, limit=1)
    assert result["superseded"] == 1
    assert delete_attempts
    assert not archive_path.exists()
    with factory() as db:
        cleanup = db.scalar(
            select(StorageCleanupOutbox).where(
                StorageCleanupOutbox.source_id == task_id,
                StorageCleanupOutbox.source_type == "lead_export_attempt",
            )
        )
        assert cleanup is not None
        assert cleanup.status == "PENDING"
        assert cleanup.source_type == "lead_export_attempt"
        assert cleanup.object_key == delete_attempts[0]


def test_item_8_upload_crash_leaves_cleanup_intent_for_stale_worker_takeover(
    api_client,
    monkeypatch,
    tmp_path,
) -> None:
    from apps.api.src.services import lead_export_v12, storage_cleanup_worker
    from apps.api.src.services.storage import StoredObject

    client, factory = api_client
    _login(client, "operation", "Operation123!")
    requested = client.post(
        "/api/v1/v1.2/reports/leads/exports",
        json={"idempotency_key": "feedback-item-8-upload-crash"},
    )
    assert requested.status_code == 200, requested.text
    task_id = requested.json()["data"]["id"]
    stored_keys: set[str] = set()
    deleted_keys: list[str] = []
    upload_calls = {"count": 0}

    class CrashOnceStorage:
        def save_file(
            self,
            source,
            *,
            prefix,
            filename,
            mime_type,
            object_key=None,
            progress_callback=None,
        ):
            assert object_key is not None
            if progress_callback is not None:
                progress_callback()
            stored_keys.add(object_key)
            upload_calls["count"] += 1
            if upload_calls["count"] == 1:
                raise KeyboardInterrupt("simulated process death after upload")
            return StoredObject(
                object_key=object_key,
                size=source.stat().st_size,
                sha256="2" * 64,
                mime_type=mime_type,
            )

        def delete(self, object_key):
            deleted_keys.append(object_key)
            stored_keys.discard(object_key)

    storage = CrashOnceStorage()
    archive_counter = {"value": 0}

    def build_archive(_db, _filters, *, heartbeat=None):
        archive_counter["value"] += 1
        archive = tmp_path / f"crash-attempt-{archive_counter['value']}.xlsx"
        archive.write_bytes(b"sensitive-archive")
        return archive, 1

    monkeypatch.setattr(lead_export_v12, "get_storage", lambda: storage)
    monkeypatch.setattr(lead_export_v12, "build_lead_export_workbook", build_archive)
    with factory() as db, pytest.raises(KeyboardInterrupt):
        lead_export_v12.process_lead_export_tasks(db, limit=1)

    with factory() as db:
        task = db.get(LeadExportTask, task_id)
        assert task is not None
        assert task.status == "RUNNING"
        first_attempt = db.scalar(
            select(StorageCleanupOutbox).where(
                StorageCleanupOutbox.source_id == task_id,
                StorageCleanupOutbox.source_type == "lead_export_attempt",
            )
        )
        assert first_attempt is not None
        assert first_attempt.status == "PENDING"
        assert first_attempt.object_key in stored_keys
        first_object_key = first_attempt.object_key
        task.started_at = datetime.now(timezone.utc) - timedelta(hours=3)
        db.commit()

    with factory() as db:
        recovered = lead_export_v12.process_lead_export_tasks(db, limit=1)
    assert recovered == {
        "claimed": 1,
        "completed": 1,
        "failed": 0,
        "superseded": 0,
    }

    with factory() as db:
        task = db.get(LeadExportTask, task_id)
        assert task is not None
        assert task.status == "COMPLETED"
        assert task.object_key in stored_keys
        assert task.object_key != first_object_key
        first_attempt = db.scalar(
            select(StorageCleanupOutbox).where(
                StorageCleanupOutbox.object_key == first_object_key
            )
        )
        assert first_attempt is not None
        first_attempt.next_attempt_at = datetime.now(timezone.utc)
        db.commit()

    monkeypatch.setattr(storage_cleanup_worker, "get_storage", lambda: storage)
    with factory() as db:
        cleanup_result = storage_cleanup_worker.process_storage_cleanup(db, limit=10)
        db.commit()
    assert cleanup_result == {"processed": 1, "deleted": 1, "failed": 0}
    assert deleted_keys == [first_object_key]
    assert first_object_key not in stored_keys


def test_item_9_my_processed_only_contains_current_operation_decisions(api_client) -> None:
    client, factory = api_client
    now = datetime.now(timezone.utc)
    request_id = "feedback-item-9-same-operation"
    quick_dispatch_request_id = "feedback-item-9-quick-dispatch"
    company_approval_request_id = "feedback-item-9-company-approval"
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        admin = db.scalar(select(User).where(User.username == "admin"))
        assert operation is not None and admin is not None
        db.add_all(
            [
                AuditLog(
                    request_id=request_id,
                    actor_user_id=operation.id,
                    actor_role_codes=["OPERATION"],
                    action="V12_SUPPLIER_LEAD_REVIEW",
                    resource_type="lead",
                    resource_id="item-9-lead",
                    before_json=None,
                    after_json={"status": "PENDING_TELESALES_VERIFY"},
                    metadata_json={"reason": "资料不全"},
                    created_at=now,
                ),
                AuditLog(
                    request_id=request_id,
                    actor_user_id=operation.id,
                    actor_role_codes=["OPERATION"],
                    action="V12_PRE_DISPATCH_VERIFY_ASSIGN",
                    resource_type="verification_task",
                    resource_id="item-9-task",
                    before_json=None,
                    after_json={"status": "ASSIGNED"},
                    metadata_json={},
                    created_at=now + timedelta(seconds=1),
                ),
                AuditLog(
                    request_id="feedback-item-9-other-actor",
                    actor_user_id=admin.id,
                    actor_role_codes=["SUPER_ADMIN"],
                    action="V12_MANUAL_DISPATCH",
                    resource_type="assignment",
                    resource_id="item-9-other-assignment",
                    before_json=None,
                    after_json={"status": "PENDING_CLAIM"},
                    metadata_json={},
                    created_at=now,
                ),
                AuditLog(
                    request_id="feedback-item-9-read-event",
                    actor_user_id=operation.id,
                    actor_role_codes=["OPERATION"],
                    action="V12_INTERNAL_ASSIGNMENT_DETAIL_READ",
                    resource_type="assignment",
                    resource_id="item-9-read",
                    before_json=None,
                    after_json=None,
                    metadata_json={},
                    created_at=now,
                ),
                AuditLog(
                    request_id="feedback-item-9-legacy-dispatch",
                    actor_user_id=operation.id,
                    actor_role_codes=["OPERATION"],
                    action="LEAD_DISPATCH",
                    resource_type="assignment",
                    resource_id="item-9-legacy-assignment",
                    before_json=None,
                    after_json={"status": "PENDING_CLAIM"},
                    metadata_json={},
                    created_at=now,
                ),
                AuditLog(
                    request_id="feedback-item-9-export-request",
                    actor_user_id=operation.id,
                    actor_role_codes=["OPERATION"],
                    action="V12_LEAD_EXPORT_REQUESTED",
                    resource_type="lead_export_task",
                    resource_id="item-9-export",
                    before_json=None,
                    after_json={"status": "PENDING"},
                    metadata_json={},
                    created_at=now,
                ),
                *[
                    AuditLog(
                        request_id=quick_dispatch_request_id,
                        actor_user_id=operation.id,
                        actor_role_codes=["OPERATION"],
                        action=action,
                        resource_type="lead" if action != "V12_MANUAL_DISPATCH" else "assignment",
                        resource_id=f"item-9-quick-{action.lower()}",
                        before_json=None,
                        after_json={"status": "DONE"},
                        metadata_json={},
                        created_at=now,
                    )
                    for action in (
                        "V12_PLATFORM_LEAD_DRAFT_CREATE",
                        "V12_PLATFORM_LEAD_SUBMIT",
                        "V12_MANUAL_DISPATCH",
                    )
                ],
                *[
                    AuditLog(
                        request_id=company_approval_request_id,
                        actor_user_id=operation.id,
                        actor_role_codes=["OPERATION"],
                        action=action,
                        resource_type="company",
                        resource_id=f"item-9-company-{action.lower()}",
                        before_json=None,
                        after_json={"status": "DONE"},
                        metadata_json={},
                        created_at=now,
                    )
                    for action in (
                        "COMPANY_UPDATE",
                        "V12_COMPANY_PROFILE_BULK_APPROVE",
                    )
                ],
                *[
                    AuditLog(
                        request_id=f"feedback-item-9-{action.lower()}",
                        actor_user_id=operation.id,
                        actor_role_codes=["OPERATION"],
                        action=action,
                        resource_type="operation_record",
                        resource_id=f"item-9-{action.lower()}",
                        before_json=None,
                        after_json={"status": "DONE"},
                        metadata_json={},
                        created_at=now,
                    )
                    for action in (
                        "COMPANY_CREATE",
                        "COMPANY_UPDATE",
                        "COMPANY_WECHAT_UNBIND",
                        "INVITE_CREATE",
                        "INVITE_REVOKE",
                        "VERIFICATION_TASK_CREATE",
                        "VERIFICATION_TASK_ASSIGN",
                        "VERIFICATION_TASK_RECLAIM",
                        "LEAD_STAGING_UPDATE",
                        "V12_PLATFORM_LEAD_CORRECTION_RECHECK",
                    )
                ],
            ]
        )
        db.commit()
        operation_id = operation.id

    _login(client, "operation", "Operation123!")
    response = client.get(
        "/api/v1/v1.2/operations/my-processed?page=1&page_size=100"
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    matching = [
        item for item in data["items"] if item["request_id"] == request_id
    ]
    assert len(matching) == 1
    assert matching[0]["action"] == "V12_SUPPLIER_LEAD_REVIEW"
    assert all(item["actor_user_id"] == operation_id for item in data["items"])
    assert all(item["action"] != "V12_INTERNAL_ASSIGNMENT_DETAIL_READ" for item in data["items"])
    assert all(item["resource_id"] != "item-9-other-assignment" for item in data["items"])
    assert any(item["action"] == "LEAD_DISPATCH" for item in data["items"])
    assert all(item["action"] != "V12_LEAD_EXPORT_REQUESTED" for item in data["items"])
    quick_dispatch = next(
        item
        for item in data["items"]
        if item["request_id"] == quick_dispatch_request_id
    )
    assert quick_dispatch["action"] == "V12_MANUAL_DISPATCH"
    company_approval = next(
        item
        for item in data["items"]
        if item["request_id"] == company_approval_request_id
    )
    assert company_approval["action"] == "V12_COMPANY_PROFILE_BULK_APPROVE"
    assert {
        "COMPANY_CREATE",
        "COMPANY_UPDATE",
        "COMPANY_WECHAT_UNBIND",
        "INVITE_CREATE",
        "INVITE_REVOKE",
        "VERIFICATION_TASK_CREATE",
        "VERIFICATION_TASK_ASSIGN",
        "VERIFICATION_TASK_RECLAIM",
        "LEAD_STAGING_UPDATE",
        "V12_PLATFORM_LEAD_CORRECTION_RECHECK",
    }.issubset({item["action"] for item in data["items"]})


def test_item_9_admin_overview_separates_pending_and_processed() -> None:
    source = ADMIN_WORKBENCH.read_text(encoding="utf-8")

    assert "我的待处理" in source
    assert "我已处理" in source
    assert "/operations/my-processed" in source
    assert "仅统计当前运营账号" in source
    assert "processedPage" in source
