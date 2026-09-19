"""2026-09-19 客户反馈第二批服务端验收：S7 县级派发门槛、S10 电销通话统计。

S4（手机号查重排除逻辑删除）的用例更新在
test_customer_feedback_910_phone_uniqueness.py 内。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from apps.api.src.core.errors import AppError
from apps.api.src.core.enums import AssignmentStatus
from apps.api.src.core.models import AuditLog, Region
from apps.api.src.core.models_v12 import CompanyLeadCapability, CompanyServiceAreaV12
from apps.api.src.core.v12_enums import LeadV12Status
from apps.api.src.services.dispatch_v12 import (
    approved_lead_pool_target,
    lead_district_region_code,
    lead_missing_district_region,
    route_approved_lead_to_pool,
)
from apps.api.src.services.dispatch_v12 import dispatch_manually_with_outcome
from apps.api.tests.test_v12_return_workflow import (
    _principal,
    _submit_and_verify,
    _workflow_setup,
)


# ---------------------------------------------------------------------------
# S7：县级派发门槛
# ---------------------------------------------------------------------------


def test_district_resolution_supports_county_and_township(db):
    setup = _workflow_setup(db)
    lead = setup["lead"]
    # 武昌区：快照内的区县级编码。
    lead.region_code = "420106"
    assert lead_district_region_code(db, lead) == "420106"
    # 市级编码不算县级。
    lead.region_code = "420100"
    assert lead_district_region_code(db, lead) is None
    assert lead_missing_district_region(db, lead)
    # 乡镇编码沿数据库地区表向上归属到快照内的县级。
    db.add(
        Region(
            code="420106990",
            name="测试街道",
            level="TOWNSHIP",
            parent_code="420106",
            aliases=[],
            active=True,
        )
    )
    db.commit()
    lead.region_code = "420106990"
    assert lead_district_region_code(db, lead) == "420106"


def test_approved_pool_target_routes_missing_county_to_telesales(db):
    setup = _workflow_setup(db)
    lead = setup["lead"]
    lead.region_code = "420900"
    assert approved_lead_pool_target(db, lead) is LeadV12Status.PENDING_TELESALES_VERIFY


def test_route_approved_lead_marks_district_pending_verify(db):
    setup = _workflow_setup(db)
    lead = setup["lead"]
    lead.status = LeadV12Status.PENDING_REVIEW.value
    lead.region_code = "420900"
    target = route_approved_lead_to_pool(db, lead)
    db.commit()
    assert target is LeadV12Status.PENDING_TELESALES_VERIFY
    assert lead.status == LeadV12Status.PENDING_TELESALES_VERIFY.value
    assert lead.pending_reason == "DISTRICT_PENDING_VERIFY"


def test_manual_dispatch_requires_district_region(db):
    setup = _workflow_setup(db)
    lead = setup["lead"]
    lead.status = LeadV12Status.READY_DISPATCH.value
    lead.current_assignment_id = None
    lead.region_code = "420900"
    db.commit()
    with pytest.raises(AppError) as error:
        dispatch_manually_with_outcome(
            db,
            lead_id=lead.id,
            company_id=setup["receiver"].id,
            employee_user_id=None,
            assigned_by=setup["receiver_user"].id,
            idempotency_key="dispatch-district-missing-1",
        )
    assert error.value.code == "LEAD_DISTRICT_REQUIRED"


def test_manual_dispatch_passes_district_gate(db):
    setup = _workflow_setup(db)
    lead = setup["lead"]
    lead.status = LeadV12Status.READY_DISPATCH.value
    lead.current_assignment_id = None
    setup["assignment"].status = AssignmentStatus.RELEASED.value
    # 满足派发资格：接收方能力 + 服务区域覆盖武昌区。
    db.add_all(
        [
            CompanyLeadCapability(
                company_id=setup["receiver"].id,
                capability_code="LEAD_RECEIVER",
                review_status="APPROVED",
            ),
            CompanyServiceAreaV12(
                company_id=setup["receiver"].id,
                region_code="420106",
                region_level="DISTRICT",
                review_status="APPROVED",
            ),
        ]
    )
    # fixture 默认 region_code=420106（武昌区），满足县级要求。
    outcome = dispatch_manually_with_outcome(
        db,
        lead_id=lead.id,
        company_id=setup["receiver"].id,
        employee_user_id=None,
        assigned_by=setup["receiver_user"].id,
        idempotency_key="dispatch-district-ok-1",
    )
    db.commit()
    assert outcome.created is True
    assert outcome.assignment.lead_id == lead.id


# ---------------------------------------------------------------------------
# S10：电销本人通话量统计（拨号审计 + 已提交核验）
# ---------------------------------------------------------------------------


def test_dial_stats_counts_own_dial_events(api_client):
    from apps.api.src.core.auth import get_current_principal
    from apps.api.src.main import app

    client, factory = api_client
    with factory() as db:
        setup = _workflow_setup(db)
        _submit_and_verify(db, setup, conclusion="SUPPORT_RETURN")
        now = datetime.now(timezone.utc)
        common = dict(
            actor_role_codes=["TELESALES"],
            action="V12_PRE_DISPATCH_DIAL_CLICK",
            resource_type="lead",
            resource_id=setup["lead"].id,
        )
        db.add_all(
            [
                AuditLog(actor_user_id=setup["telesales"].id, created_at=now, **common),
                AuditLog(actor_user_id=setup["telesales"].id, created_at=now, **common),
                # 其他人的拨号不计入本人统计。
                AuditLog(actor_user_id=setup["reviewer"].id, created_at=now, **common),
            ]
        )
        db.commit()
        principal = _principal(setup["telesales"], "dashboard.telesales.read")
    app.dependency_overrides[get_current_principal] = lambda: principal
    try:
        response = client.get("/api/v1/v1.2/pre-dispatch-verifications/dial-stats")
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["today"]["dials"] == 2
        assert data["today"]["submitted"] >= 1
        assert data["month"]["dials"] >= data["today"]["dials"]
        assert data["week"]["dials"] >= data["today"]["dials"]
    finally:
        app.dependency_overrides.pop(get_current_principal, None)


def test_dial_stats_requires_telesales_permission(api_client):
    from apps.api.src.core.auth import get_current_principal
    from apps.api.src.main import app

    client, factory = api_client
    with factory() as db:
        setup = _workflow_setup(db)
        db.commit()
        principal = _principal(setup["receiver_user"], "lead.manual.manage")
    app.dependency_overrides[get_current_principal] = lambda: principal
    try:
        response = client.get("/api/v1/v1.2/pre-dispatch-verifications/dial-stats")
        assert response.status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_principal, None)
