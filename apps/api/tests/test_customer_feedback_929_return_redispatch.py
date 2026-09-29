from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from apps.api.src.core.errors import AppError
from apps.api.src.core.models import Assignment, AssignmentEvent, PointsLedger
from apps.api.src.services.return_v12 import (
    correct_and_redispatch_return,
    final_review_return,
)
from apps.api.src.services.rbac import assign_role
from apps.api.tests.test_v12_dispatch_claim import _company, _receiver_setup
from apps.api.tests.test_v12_return_workflow import (
    _principal,
    _submit_and_verify,
    _workflow_setup,
)


def test_operations_final_review_can_override_telesales_conclusion(db) -> None:
    setup = _workflow_setup(db)
    request, _ = _submit_and_verify(db, setup, conclusion="DOES_NOT_SUPPORT_RETURN")

    result = final_review_return(
        db,
        return_id=request.id,
        principal=_principal(setup["reviewer"], "return.review"),
        decision="APPROVE",
        note="运营结合完整资料决定同意退回",
    )

    assert result.request.status == "APPROVED"
    assert result.refund_ledger is not None
    assert setup["assignment"].status == "RETURNED"


def test_operations_corrects_lead_and_atomically_redispatches_to_another_company(db) -> None:
    setup = _workflow_setup(db)
    assign_role(db, setup["reviewer"], "OPERATION")
    request, _ = _submit_and_verify(db, setup, conclusion="DOES_NOT_SUPPORT_RETURN")
    target, target_owner = _company(db, "RET-NEXT", "退回改派接收方")
    _receiver_setup(db, target, region_code="410102", balance=1000)
    db.commit()

    result = correct_and_redispatch_return(
        db,
        return_id=request.id,
        principal=_principal(setup["reviewer"], "return.review"),
        company_id=target.id,
        employee_user_id=None,
        idempotency_key="return-redispatch-929-1",
        customer_name="更正后的客户",
        need_summary="计划在郑州中原区建设三层住宅",
        consent_confirmed=True,
        province_code="410000",
        city_code="410100",
        district_code="410102",
        reason="运营核实资料后改派其他加盟商",
    )
    db.commit()

    assert result.assignment.company_id == target.id
    assert result.assignment.internal_assignee_user_id == target_owner.id
    assert setup["assignment"].status == "RETURNED"
    assert setup["lead"].current_assignment_id == result.assignment.id
    assert setup["lead"].status == "DISPATCHED"
    assert setup["lead"].customer_name == "更正后的客户"
    assert setup["lead"].need_summary == "计划在郑州中原区建设三层住宅"
    assert (setup["lead"].province, setup["lead"].city, setup["lead"].district) == (
        "河南省",
        "郑州市",
        "中原区",
    )
    assert setup["account"].balance == 1000
    assert db.scalar(
        select(func.count(PointsLedger.id)).where(
            PointsLedger.business_type == "V12_RETURN_REFUND"
        )
    ) == 1
    event = db.scalar(
        select(AssignmentEvent).where(
            AssignmentEvent.event_type == "V12_RETURN_CORRECTED_REDISPATCH"
        )
    )
    assert event is not None
    assert event.payload["new_assignment_id"] == result.assignment.id

    repeated = correct_and_redispatch_return(
        db,
        return_id=request.id,
        principal=_principal(setup["reviewer"], "return.review"),
        company_id=target.id,
        employee_user_id=None,
        idempotency_key="return-redispatch-929-1",
        customer_name="更正后的客户",
        need_summary="计划在郑州中原区建设三层住宅",
        consent_confirmed=True,
        province_code="410000",
        city_code="410100",
        district_code="410102",
        reason="运营核实资料后改派其他加盟商",
    )
    assert repeated.idempotent is True
    assert repeated.assignment.id == result.assignment.id
    assert db.scalar(
        select(func.count(Assignment.id)).where(Assignment.lead_id == setup["lead"].id)
    ) == 2


def test_return_redispatch_rejects_stale_snapshot_and_changed_retry(db) -> None:
    setup = _workflow_setup(db)
    assign_role(db, setup["reviewer"], "OPERATION")
    request, _ = _submit_and_verify(db, setup, conclusion="SUPPORT_RETURN")
    target, _ = _company(db, "RET-NEXT-RETRY", "改派目标")
    _receiver_setup(db, target, region_code="410102", balance=1000)
    db.commit()
    payload = dict(
        return_id=request.id,
        principal=_principal(setup["reviewer"], "return.review"),
        company_id=target.id,
        employee_user_id=None,
        idempotency_key="return-redispatch-929-retry",
        customer_name="核实后的客户",
        need_summary="新需求",
        consent_confirmed=True,
        province_code="410000",
        city_code="410100",
        district_code="410102",
        reason="运营核实后改派",
    )
    initial_version = setup["lead"].snapshot_version
    with pytest.raises(AppError) as stale:
        correct_and_redispatch_return(db, **payload, expected_snapshot_version=initial_version + 1)
    assert stale.value.code == "LEAD_SNAPSHOT_CONFLICT"
    assert setup["assignment"].status == "RETURN_PENDING"

    result = correct_and_redispatch_return(db, **payload, expected_snapshot_version=initial_version)
    assert result.assignment.company_id == target.id
    with pytest.raises(AppError) as changed_retry:
        correct_and_redispatch_return(db, **{**payload, "reason": "不同的终审原因"}, expected_snapshot_version=initial_version)
    assert changed_retry.value.code == "RETURN_ALREADY_REDISPATCHED"

def test_region_correction_routes_platform_lead_without_receiver_to_public_pool(db) -> None:
    from apps.api.src.services.return_v12 import correct_region_and_redispatch

    setup = _workflow_setup(db)
    assign_role(db, setup["reviewer"], "OPERATION")
    setup["lead"].source_kind = "PLATFORM_MANUAL"
    setup["lead"].source_type = "PLATFORM_MANUAL"
    request, _ = _submit_and_verify(db, setup, conclusion="SUPPORT_RETURN")
    request.reason_code = "OUT_OF_SERVICE_REGION"
    db.commit()

    result = correct_region_and_redispatch(
        db,
        return_id=request.id,
        principal=_principal(setup["reviewer"], "return.review"),
        province_code="410000",
        city_code="410100",
        district_code="410102",
        reason="已核实实际建房位置",
    )

    assert result.pool_target == "PUBLIC_POOL"
    assert setup["lead"].status == "PUBLIC_POOL"


def test_correct_and_redispatch_rolls_back_refund_when_new_dispatch_fails(
    db, monkeypatch
) -> None:
    from apps.api.src.services import return_v12 as service

    setup = _workflow_setup(db)
    assign_role(db, setup["reviewer"], "OPERATION")
    request, _ = _submit_and_verify(db, setup, conclusion="SUPPORT_RETURN")
    target, _ = _company(db, "RET-FAIL", "改派失败接收方")
    _receiver_setup(db, target, region_code="420106", balance=1000)
    db.commit()

    def fail_dispatch(*args, **kwargs):
        raise AppError("DISPATCH_FAILED", "模拟新派发失败", 409)

    monkeypatch.setattr(service, "dispatch_manually_with_outcome", fail_dispatch)
    with pytest.raises(AppError, match="模拟新派发失败"):
        service.correct_and_redispatch_return(
            db,
            return_id=request.id,
            principal=_principal(setup["reviewer"], "return.review"),
            company_id=target.id,
            employee_user_id=None,
            idempotency_key="return-redispatch-929-fail",
            customer_name=setup["lead"].customer_name,
            need_summary=setup["lead"].need_summary,
            consent_confirmed=True,
            province_code="420000",
            city_code="420100",
            district_code="420106",
            reason="验证新派发失败时整笔回滚",
        )
    db.rollback()

    assert request.status == "REVIEWING"
    assert setup["assignment"].status == "RETURN_PENDING"
    assert setup["lead"].current_assignment_id == setup["assignment"].id
    assert setup["account"].balance == 900
    assert db.scalar(
        select(func.count(PointsLedger.id)).where(
            PointsLedger.business_type == "V12_RETURN_REFUND"
        )
    ) == 0


def test_operations_ui_exposes_independent_review_and_same_page_redispatch() -> None:
    source = (
        Path(__file__).parents[2] / "admin" / "public" / "v12-operations.js"
    ).read_text(encoding="utf-8")

    assert "电销结论作为事实参考，最终退回、驳回或更正改派由运营决定" in source
    assert "更正信息并改派其他加盟商" in source
    assert "/correct-and-redispatch" in source
    assert "return-correct-company" in source


def test_correct_and_redispatch_http_endpoint(api_client) -> None:
    from apps.api.src.core.auth import get_current_principal
    from apps.api.src.main import app

    client, factory = api_client
    with factory() as db:
        setup = _workflow_setup(db)
        assign_role(db, setup["reviewer"], "OPERATION")
        request, _ = _submit_and_verify(db, setup, conclusion="INCONCLUSIVE")
        target, _ = _company(db, "RET-HTTP", "接口改派接收方")
        _receiver_setup(db, target, region_code="410102", balance=1000)
        db.commit()
        return_id = request.id
        target_id = target.id
        principal = _principal(setup["reviewer"], "return.review")

    try:
        app.dependency_overrides[get_current_principal] = lambda: principal
        response = client.post(
            f"/api/v1/v1.2/returns/{return_id}/correct-and-redispatch",
            json={
                "company_id": target_id,
                "idempotency_key": "return-redispatch-http-929",
                "customer_name": "接口更正客户",
                "need_summary": "接口核实后的建房需求",
                "consent_confirmed": True,
                "province_code": "410000",
                "city_code": "410100",
                "district_code": "410102",
                "reason": "接口终审更正后改派",
            },
        )
    finally:
        app.dependency_overrides.pop(get_current_principal, None)

    assert response.status_code == 200, response.text
    assert response.json()["data"]["new_receiver_company_id"] == target_id
