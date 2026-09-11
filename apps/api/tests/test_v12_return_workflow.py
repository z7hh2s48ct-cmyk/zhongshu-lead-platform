from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, func, select
from starlette.requests import Request

from apps.api.src.core.auth import Principal
from apps.api.src.core.enums import AssignmentStatus, EvidenceType, PointsLedgerType, VerificationTaskStatus
from apps.api.src.core.errors import AppError
from apps.api.src.core.models import (
    Assignment,
    AssignmentEvent,
    Company,
    Lead,
    Notification,
    NotificationOutbox,
    PointsAccount,
    PointsLedger,
    ReturnEvidence,
    ReturnRequest,
    User,
    VerificationTask,
)
from apps.api.src.core.models_v12 import SupplierLeadReward
from apps.api.src.core.security import encrypt_text, fingerprint_phone, hash_phone
from apps.api.src.core.v12_enums import (
    LeadSourceKind,
    LeadV12Status,
    ReturnV12Status,
    RewardStatus,
    VerificationTaskType,
)
from apps.api.src.routers.v12_returns import list_return_verification_tasks
from apps.api.src.services.audit import write_audit
from apps.api.src.services.rbac import assign_role
from apps.api.src.services.return_v12 import (
    add_return_evidence,
    assign_return_verification_task,
    claim_return_verification_task,
    create_or_update_return_draft,
    direct_invalid_return,
    final_review_return,
    return_request_to_dict,
    return_request_list_to_dict,
    return_verification_task_list_to_dict,
    return_verification_task_to_dict,
    submit_return_request,
    submit_return_verification,
)
from apps.api.src.services.workday_calendar import WorkdayCalendarService


def _principal(user: User, *permissions: str) -> Principal:
    return Principal(
        user_id=user.id,
        display_name=user.display_name,
        company_id=user.company_id,
        role_codes=frozenset(role.code for role in user.roles),
        permission_codes=frozenset(permissions),
        session_version=1,
    )


def _evidence(db, request, principal, evidence_type: str) -> ReturnEvidence:
    return add_return_evidence(
        db,
        request=request,
        principal=principal,
        evidence_type=evidence_type,
        object_key=f"private/{request.id}/{evidence_type.lower()}",
        original_name="proof.jpg" if evidence_type == EvidenceType.CHAT_SCREENSHOT.value else "call.mp3",
        mime_type="image/jpeg" if evidence_type == EvidenceType.CHAT_SCREENSHOT.value else "audio/mpeg",
        file_size=1024,
        sha256="a" * 64,
        duration_seconds=None if evidence_type == EvidenceType.CHAT_SCREENSHOT.value else 30,
    )


def _workflow_setup(
    db,
    *,
    lead_status: str = LeadV12Status.CLAIMED.value,
    suffix: str = "",
):
    supplier = Company(code=f"RET-SUP{suffix}", name=f"退回测试供应商{suffix}", status="ACTIVE")
    receiver = Company(code=f"RET-REC{suffix}", name=f"退回测试接收方{suffix}", status="ACTIVE")
    db.add_all([supplier, receiver])
    db.flush()

    receiver_user = User(display_name="接收方负责人", status="ACTIVE", company_id=receiver.id)
    telesales = User(display_name="退回核验电销", status="ACTIVE")
    reviewer = User(display_name="退回终审员", status="ACTIVE")
    operator = User(display_name="运营分配员", status="ACTIVE")
    db.add_all([receiver_user, telesales, reviewer, operator])
    db.flush()
    assign_role(db, receiver_user, "FRANCHISE_OWNER")
    assign_role(db, telesales, "TELESALES")

    phone = "13800138301"
    now = datetime.now(timezone.utc)
    lead = Lead(
        source_type=LeadSourceKind.SUPPLIER_H5.value,
        source_kind=LeadSourceKind.SUPPLIER_H5.value,
        submitter_user_id=receiver_user.id,
        supplier_company_id=supplier.id,
        customer_name="退回测试客户",
        phone_encrypted=encrypt_text(phone),
        phone_hash=hash_phone(phone),
        phone_fingerprint=fingerprint_phone(phone),
        consent_confirmed=True,
        city="武汉市",
        district="武昌区",
        region_code="420106",
        need_summary="计划建设两层乡墅",
        status=lead_status,
        review_status="APPROVED",
        duplicate_status="CLEAR",
        current_follow_status="CONTACTED" if lead_status == LeadV12Status.FOLLOWING.value else "UNCONTACTED",
        imported_at=now,
        submitted_at=now,
        raw_payload={},
    )
    db.add(lead)
    db.flush()

    claimed_at = now - timedelta(hours=2)
    deadline = WorkdayCalendarService(db).add_workdays(claimed_at, 3)
    assignment_status = (
        AssignmentStatus.FOLLOWING.value
        if lead_status == LeadV12Status.FOLLOWING.value
        else AssignmentStatus.CLAIMED.value
    )
    assignment = Assignment(
        lead_id=lead.id,
        company_id=receiver.id,
        receiver_company_id=receiver.id,
        supplier_company_id=supplier.id,
        status=assignment_status,
        points_price=100,
        claim_points=100,
        lead_snapshot={"phone_masked": "138****8301"},
        assigned_by=operator.id,
        assigned_at=now - timedelta(hours=4),
        claimed_at=claimed_at,
        appeal_deadline_at=deadline,
        reward_due_at=deadline,
        internal_assignee_user_id=receiver_user.id,
        internal_assigned_by=receiver_user.id,
        idempotency_key=f"return-workflow-{receiver.id}",
    )
    db.add(assignment)
    db.flush()
    lead.current_assignment_id = assignment.id

    account = PointsAccount(company_id=receiver.id, balance=900, version=1)
    db.add(account)
    db.flush()
    claim_ledger = PointsLedger(
        account_id=account.id,
        company_id=receiver.id,
        ledger_type=PointsLedgerType.CLAIM.value,
        delta=-100,
        balance_after=900,
        business_type="V12_ASSIGNMENT_CLAIM",
        business_id=assignment.id,
        idempotency_key=f"v12-claim:{assignment.id}",
        created_by=receiver_user.id,
        metadata_json={},
    )
    db.add(claim_ledger)
    reward = SupplierLeadReward(
        lead_id=lead.id,
        assignment_id=assignment.id,
        supplier_company_id=supplier.id,
        receiver_company_id=receiver.id,
        status=RewardStatus.OBSERVING.value,
        claim_points=100,
        reward_ratio_bps=3000,
        reward_points=30,
        rule_version=1,
        observed_at=claimed_at,
        appeal_deadline_at=deadline,
        reward_due_at=deadline,
    )
    db.add(reward)
    db.commit()
    return {
        "supplier": supplier,
        "receiver": receiver,
        "receiver_user": receiver_user,
        "telesales": telesales,
        "reviewer": reviewer,
        "operator": operator,
        "lead": lead,
        "assignment": assignment,
        "account": account,
        "claim_ledger": claim_ledger,
        "reward": reward,
    }


@pytest.mark.parametrize(
    "evidence_type",
    [EvidenceType.CHAT_SCREENSHOT.value, EvidenceType.CALL_RECORDING.value],
)
def test_single_evidence_type_creates_post_call_task(db, evidence_type: str) -> None:
    setup = _workflow_setup(db)
    principal = _principal(setup["receiver_user"], "return.own.manage")
    request = create_or_update_return_draft(
        db,
        assignment_id=setup["assignment"].id,
        principal=principal,
        reason_code="EMPTY_NUMBER",
        description="多次联系后确认号码异常，需要申请退回",
    )
    assert db.scalar(select(VerificationTask).where(VerificationTask.lead_id == setup["lead"].id)) is None
    _evidence(db, request, principal, evidence_type)
    result = submit_return_request(db, return_id=request.id, principal=principal)
    db.commit()

    assert result.expired is False
    assert result.task is not None
    assert result.task.task_type == VerificationTaskType.RETURN_VERIFY.value
    assert result.task.status == VerificationTaskStatus.PENDING.value
    assert request.status == ReturnV12Status.VERIFYING.value


def test_return_verification_task_list_avoids_per_row_queries(db) -> None:
    setup = _workflow_setup(db)
    owner = _principal(setup["receiver_user"], "return.own.manage")
    request = create_or_update_return_draft(
        db,
        assignment_id=setup["assignment"].id,
        principal=owner,
        reason_code="EMPTY_NUMBER",
        description="核验任务列表应批量读取关联退回资料",
    )
    _evidence(db, request, owner, EvidenceType.CHAT_SCREENSHOT.value)
    first_task = submit_return_request(db, return_id=request.id, principal=owner).task
    assert first_task is not None
    second_task = VerificationTask(
        lead_id=setup["lead"].id,
        template_id=None,
        template_version=1,
        status=VerificationTaskStatus.PENDING.value,
        task_type=VerificationTaskType.RETURN_VERIFY.value,
        return_request_id=request.id,
        assignment_id=setup["assignment"].id,
    )
    db.add(second_task)
    db.commit()
    db.expire_all()
    tasks = db.scalars(
        select(VerificationTask)
        .where(VerificationTask.id.in_([first_task.id, second_task.id]))
        .order_by(VerificationTask.created_at.asc())
    ).all()
    statements: list[str] = []

    def record_statement(*args) -> None:
        if args[2].lstrip().upper().startswith("SELECT"):
            statements.append(args[2])

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        rows = return_verification_task_list_to_dict(db, tasks, owner)
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    assert [row["id"] for row in rows] == [task.id for task in tasks]
    assert all(row["return_request"]["id"] == request.id for row in rows)
    assert len(statements) <= 4


def test_return_request_list_avoids_per_row_queries(db) -> None:
    requests = []
    for suffix in ("-LIST-A", "-LIST-B"):
        setup = _workflow_setup(db, suffix=suffix)
        owner = _principal(setup["receiver_user"], "return.own.manage")
        requests.append(
            create_or_update_return_draft(
                db,
                assignment_id=setup["assignment"].id,
                principal=owner,
                reason_code="EMPTY_NUMBER",
                description="退回列表必须批量读取关联信息",
            )
        )
    db.commit()
    request_ids = [item.id for item in requests]
    db.expire_all()
    requests = db.scalars(
        select(ReturnRequest)
        .where(ReturnRequest.id.in_(request_ids))
        .order_by(ReturnRequest.created_at.asc())
    ).all()
    statements: list[str] = []

    def record_statement(*args) -> None:
        if args[2].lstrip().upper().startswith("SELECT"):
            statements.append(args[2])

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        rows = return_request_list_to_dict(db, list(requests))
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    assert [row["id"] for row in rows] == [item.id for item in requests]
    assert all(row["customer_name"] == "退回测试客户" for row in rows)
    assert len(statements) <= 5


def test_telesales_cannot_start_an_unassigned_return_verification_task(db) -> None:
    setup = _workflow_setup(db)
    owner = _principal(setup["receiver_user"], "return.own.manage")
    request = create_or_update_return_draft(
        db,
        assignment_id=setup["assignment"].id,
        principal=owner,
        reason_code="EMPTY_NUMBER",
        description="运营尚未派发电销任务时，电销不能自行领取。",
    )
    _evidence(db, request, owner, EvidenceType.CHAT_SCREENSHOT.value)
    submitted = submit_return_request(db, return_id=request.id, principal=owner)
    assert submitted.task is not None
    assert submitted.task.assignee_user_id is None

    telesales = _principal(setup["telesales"], "verification.task.start")
    with pytest.raises(AppError) as exc_info:
        claim_return_verification_task(
            db,
            task_id=submitted.task.id,
            principal=telesales,
        )

    assert exc_info.value.code == "RETURN_VERIFY_TASK_NOT_ASSIGNED"


def test_overdue_return_verification_blocks_telesales_and_allows_operation_reassignment(db) -> None:
    setup = _workflow_setup(db)
    owner = _principal(setup["receiver_user"], "return.own.manage")
    request = create_or_update_return_draft(
        db,
        assignment_id=setup["assignment"].id,
        principal=owner,
        reason_code="EMPTY_NUMBER",
        description="需要在处理期限内完成退回事实核验",
    )
    _evidence(db, request, owner, EvidenceType.CHAT_SCREENSHOT.value)
    submitted = submit_return_request(db, return_id=request.id, principal=owner)
    assert submitted.task is not None

    operator = _principal(setup["operator"], "verification.read")
    assignment = assign_return_verification_task(
        db,
        task_id=submitted.task.id,
        assignee_user_id=setup["telesales"].id,
        assigned_by=operator.user_id,
        reason="运营派发退回事实核验",
    )
    task = assignment.task
    assert task.due_at is not None
    task.due_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    telesales = _principal(
        setup["telesales"],
        "verification.task.start",
        "verification.submit",
        "lead.phone.read",
    )
    overdue_detail = return_verification_task_to_dict(db, task, telesales, include_phone=True)
    assert overdue_detail["is_overdue"] is True
    assert overdue_detail["lead"]["phone"] is None
    with pytest.raises(AppError) as start_after_due:
        claim_return_verification_task(db, task_id=task.id, principal=telesales)
    assert start_after_due.value.code == "RETURN_VERIFY_TASK_OVERDUE"

    assignment = assign_return_verification_task(
        db,
        task_id=task.id,
        assignee_user_id=setup["telesales"].id,
        assigned_by=operator.user_id,
        reason="原核验任务超时，重新指定期限",
    )
    task = assignment.task
    claim_return_verification_task(db, task_id=task.id, principal=telesales)
    task.due_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.flush()

    with pytest.raises(AppError) as submit_after_due:
        submit_return_verification(
            db,
            task_id=task.id,
            principal=telesales,
            contact_result="EMPTY_NUMBER",
            conclusion="SUPPORT_RETURN",
            note="超时后不得提交退回核验结论",
        )
    assert submit_after_due.value.code == "RETURN_VERIFY_TASK_OVERDUE"

    reassigned = assign_return_verification_task(
        db,
        task_id=task.id,
        assignee_user_id=setup["telesales"].id,
        assigned_by=operator.user_id,
        reason="原核验任务超时，改派其他电销人员",
    )
    assert reassigned.task.status == VerificationTaskStatus.ASSIGNED.value
    assert reassigned.task.started_at is None
    assert reassigned.task.due_at > datetime.now(timezone.utc)


def test_missing_return_evidence_is_rejected(db) -> None:
    setup = _workflow_setup(db)
    principal = _principal(setup["receiver_user"], "return.own.manage")
    request = create_or_update_return_draft(
        db,
        assignment_id=setup["assignment"].id,
        principal=principal,
        reason_code="EMPTY_NUMBER",
        description="多次联系后确认号码异常，需要申请退回",
    )

    with pytest.raises(AppError) as exc_info:
        submit_return_request(db, return_id=request.id, principal=principal)

    assert exc_info.value.code == "RETURN_EVIDENCE_REQUIRED"
    assert exc_info.value.details == {"evidence_count": 0}


def test_screenshot_and_recording_create_post_call_task(db) -> None:
    setup = _workflow_setup(db)
    principal = _principal(setup["receiver_user"], "return.own.manage")
    request = create_or_update_return_draft(
        db,
        assignment_id=setup["assignment"].id,
        principal=principal,
        reason_code="EMPTY_NUMBER",
        description="多次联系后确认号码异常，需要申请退回",
    )
    _evidence(db, request, principal, EvidenceType.CHAT_SCREENSHOT.value)
    _evidence(db, request, principal, EvidenceType.CALL_RECORDING.value)
    result = submit_return_request(db, return_id=request.id, principal=principal)
    db.commit()

    assert result.expired is False
    assert result.task is not None
    assert result.task.task_type == VerificationTaskType.RETURN_VERIFY.value
    assert result.task.status == VerificationTaskStatus.PENDING.value
    assert request.status == ReturnV12Status.VERIFYING.value
    assert setup["assignment"].status == AssignmentStatus.RETURN_PENDING.value
    assert setup["lead"].status == LeadV12Status.CLAIMED.value
    assert setup["reward"].status == RewardStatus.FROZEN.value
    assert request.appeal_deadline_at == setup["assignment"].appeal_deadline_at
    assert db.scalar(
        select(Notification).where(Notification.scene == "V12_RETURN_SUBMITTED")
    ) is None
    assert db.scalar(
        select(NotificationOutbox).where(
            NotificationOutbox.event_type == "V12_RETURN_SUBMITTED"
        )
    ) is None


def test_return_service_and_audit_projection_emit_one_station_and_wechat_message(db) -> None:
    setup = _workflow_setup(db)
    owner = _principal(setup["receiver_user"], "return.own.manage")
    request = create_or_update_return_draft(
        db,
        assignment_id=setup["assignment"].id,
        principal=owner,
        reason_code="EMPTY_NUMBER",
        description="退回通知必须只有一条幂等投影",
    )
    _evidence(db, request, owner, EvidenceType.CHAT_SCREENSHOT.value)
    result = submit_return_request(db, return_id=request.id, principal=owner)
    write_audit(
        db,
        principal=owner,
        action="V12_RETURN_SUBMIT",
        resource_type="return_request",
        resource_id=result.request.id,
        company_id=result.request.company_id,
        after={
            "status": result.request.status,
            "verification_task_id": result.task.id if result.task else None,
        },
        request_id="return-notification-single-projection",
    )
    db.flush()

    notifications = db.scalars(
        select(Notification).where(
            Notification.company_id == setup["receiver"].id,
            Notification.scene == "V12_RETURN_SUBMITTED",
        )
    ).all()
    outboxes = db.scalars(
        select(NotificationOutbox).where(
            NotificationOutbox.aggregate_id == request.id,
            NotificationOutbox.event_type == "V12_RETURN_SUBMITTED",
        )
    ).all()
    assert len(notifications) == 1
    assert len(outboxes) == 1
    assert notifications[0].deep_link == f"/h5/v12-workbench.html?view=returns&id={request.id}"
    assert outboxes[0].payload["deep_link"] == notifications[0].deep_link


def test_return_submit_rejects_no_evidence_and_invalid_reason(db) -> None:
    setup = _workflow_setup(db)
    principal = _principal(setup["receiver_user"], "return.own.manage")
    with pytest.raises(AppError) as invalid_reason:
        create_or_update_return_draft(
            db,
            assignment_id=setup["assignment"].id,
            principal=principal,
            reason_code="OTHER",
            description="不在冻结范围内的原因",
        )
    assert invalid_reason.value.code == "RETURN_REASON_INVALID"

    request = create_or_update_return_draft(
        db,
        assignment_id=setup["assignment"].id,
        principal=principal,
        reason_code="NON_HOUSING_CONSULTATION",
        description="客户确认并非建房或装修需求",
    )
    with pytest.raises(AppError) as no_evidence:
        submit_return_request(db, return_id=request.id, principal=principal)
    assert no_evidence.value.code == "RETURN_EVIDENCE_REQUIRED"


def _submit_and_verify(db, setup, *, conclusion: str = "SUPPORT_RETURN"):
    owner = _principal(setup["receiver_user"], "return.own.manage")
    request = create_or_update_return_draft(
        db,
        assignment_id=setup["assignment"].id,
        principal=owner,
        reason_code="EMPTY_NUMBER",
        description="客户号码经多次拨打确认为空号",
    )
    _evidence(db, request, owner, EvidenceType.CHAT_SCREENSHOT.value)
    _evidence(db, request, owner, EvidenceType.CALL_RECORDING.value)
    submitted = submit_return_request(db, return_id=request.id, principal=owner)
    assert submitted.task is not None
    operator = _principal(setup["operator"], "verification.read")
    assign_return_verification_task(
        db,
        task_id=submitted.task.id,
        assignee_user_id=setup["telesales"].id,
        assigned_by=operator.user_id,
        reason="运营确认需要电话核验退回事实",
    )
    telesales = _principal(
        setup["telesales"],
        "verification.task.read",
        "verification.task.claim",
        "verification.submit",
        "lead.phone.read",
    )
    claim_return_verification_task(db, task_id=submitted.task.id, principal=telesales)
    task = submit_return_verification(
        db,
        task_id=submitted.task.id,
        principal=telesales,
        contact_result="EMPTY_NUMBER",
        conclusion=conclusion,
        note="连续三次拨打均提示空号，事实核验完成",
    )
    db.flush()
    assert task.status == VerificationTaskStatus.SUBMITTED.value
    assert request.status == ReturnV12Status.REVIEWING.value
    return request, task


def test_final_approve_refunds_once_closes_invalid_lead_and_cancels_reward(db) -> None:
    setup = _workflow_setup(db)
    request, task = _submit_and_verify(db, setup)
    reviewer = _principal(setup["reviewer"], "return.review")
    result = final_review_return(
        db,
        return_id=request.id,
        principal=reviewer,
        decision="APPROVE",
        note="证据和电销事实均支持退回",
    )
    db.commit()

    assert result.refund_ledger is not None
    assert result.refund_ledger.delta == 100
    assert request.status == ReturnV12Status.APPROVED.value
    assert request.refund_points == 100
    db.expire(request)
    persisted_request = db.get(ReturnRequest, request.id)
    assert persisted_request is not None
    assert persisted_request.reviewed_by == reviewer.user_id
    assert persisted_request.reviewed_at is not None
    assert persisted_request.review_note == "证据和电销事实均支持退回"
    assert persisted_request.final_decision_reason == "证据和电销事实均支持退回"
    assert setup["assignment"].status == AssignmentStatus.RETURNED.value
    assert setup["lead"].status == LeadV12Status.CLOSED.value
    assert setup["lead"].current_assignment_id is None
    assert setup["reward"].status == RewardStatus.CANCELLED.value
    assert task.status == VerificationTaskStatus.RELEASED.value
    assert db.get(PointsAccount, setup["account"].id).balance == 1000
    assert db.scalar(
        select(Notification).where(Notification.scene == "V12_RETURN_APPROVED")
    ) is None
    telesales = _principal(setup["telesales"], "verification.task.read")
    released_detail = return_verification_task_to_dict(
        db,
        task,
        telesales,
        include_verification_info=True,
    )
    assert released_detail["verification_info"]["note"] == (
        "连续三次拨打均提示空号，事实核验完成"
    )

    repeated = final_review_return(
        db,
        return_id=request.id,
        principal=reviewer,
        decision="APPROVE",
        note="重复提交终审",
    )
    db.commit()
    assert repeated.idempotent is True
    refunds = db.scalars(
        select(PointsLedger).where(
            PointsLedger.company_id == setup["receiver"].id,
            PointsLedger.business_type == "V12_RETURN_REFUND",
            PointsLedger.business_id == request.id,
        )
    ).all()
    assert len(refunds) == 1
    assert db.get(PointsAccount, setup["account"].id).balance == 1000


def test_operation_can_directly_confirm_invalid_before_telesales_submission(db) -> None:
    setup = _workflow_setup(db, suffix="-DIRECT")
    owner = _principal(setup["receiver_user"], "return.own.manage")
    request = create_or_update_return_draft(
        db,
        assignment_id=setup["assignment"].id,
        principal=owner,
        reason_code="EMPTY_NUMBER",
        description="运营可从证据直接确认空号",
    )
    _evidence(db, request, owner, EvidenceType.CALL_RECORDING.value)
    submitted = submit_return_request(db, return_id=request.id, principal=owner)
    reviewer = _principal(setup["reviewer"], "return.review")

    result = direct_invalid_return(
        db,
        return_id=request.id,
        principal=reviewer,
        note="运营复核录音后确认号码为空号",
    )
    db.commit()

    assert result.request.status == ReturnV12Status.APPROVED.value
    assert result.refund_ledger is not None
    assert result.refund_ledger.delta == 100
    assert submitted.task is not None
    assert submitted.task.status == VerificationTaskStatus.CANCELLED.value
    assert setup["assignment"].status == AssignmentStatus.RETURNED.value
    assert setup["lead"].status == LeadV12Status.CLOSED.value
    assert setup["reward"].status == RewardStatus.CANCELLED.value
    assert db.get(PointsAccount, setup["account"].id).balance == 1000
    event = db.scalar(
        select(AssignmentEvent).where(
            AssignmentEvent.assignment_id == setup["assignment"].id,
            AssignmentEvent.event_type == "V12_RETURN_DIRECT_INVALID",
        )
    )
    assert event is not None
    assert event.payload["note"] == "运营复核录音后确认号码为空号"
    repeated = direct_invalid_return(
        db,
        return_id=request.id,
        principal=reviewer,
        note="重复确认不得重复返分",
    )
    db.commit()
    assert repeated.idempotent is True
    assert db.scalar(
        select(func.count(AssignmentEvent.id)).where(
            AssignmentEvent.assignment_id == setup["assignment"].id,
            AssignmentEvent.event_type == "V12_RETURN_DIRECT_INVALID",
        )
    ) == 1
    assert db.scalar(
        select(func.count(PointsLedger.id)).where(
            PointsLedger.business_type == "V12_RETURN_REFUND",
            PointsLedger.business_id == request.id,
        )
    ) == 1


def test_return_verification_records_selected_evidence_separately(db) -> None:
    setup = _workflow_setup(db, suffix="-VERIFY-EVIDENCE")
    owner = _principal(setup["receiver_user"], "return.own.manage")
    request = create_or_update_return_draft(
        db,
        assignment_id=setup["assignment"].id,
        principal=owner,
        reason_code="EMPTY_NUMBER",
        description="提交证据供电销核验",
    )
    evidence = _evidence(db, request, owner, EvidenceType.CALL_RECORDING.value)
    submitted = submit_return_request(db, return_id=request.id, principal=owner)
    assert submitted.task is not None
    assignment = assign_return_verification_task(
        db,
        task_id=submitted.task.id,
        assignee_user_id=setup["telesales"].id,
        assigned_by=setup["operator"].id,
        reason="核验空号证据",
    )
    telesales = _principal(
        setup["telesales"],
        "verification.task.read",
        "verification.task.start",
        "verification.submit",
        "lead.phone.read",
    )
    claim_return_verification_task(db, task_id=assignment.task.id, principal=telesales)
    submit_return_verification(
        db,
        task_id=assignment.task.id,
        principal=telesales,
        contact_result="EMPTY_NUMBER",
        conclusion="SUPPORT_RETURN",
        note="录音可确认号码为空号",
        evidence_ids=[evidence.id],
    )

    detail = return_verification_task_to_dict(
        db,
        assignment.task,
        telesales,
        include_verification_info=True,
    )
    verification_evidences = detail["verification_info"]["evidences"]
    assert detail["return_request"]["available_evidences"][0]["id"] == evidence.id
    assert verification_evidences == [
        {
            "id": evidence.id,
            "type": EvidenceType.CALL_RECORDING.value,
            "original_name": "call.mp3",
            "mime_type": "audio/mpeg",
            "file_size": 1024,
            "duration_seconds": 30,
            "created_at": evidence.created_at.isoformat(),
        }
    ]
    return_detail = return_request_to_dict(db, request, include_evidence=True)
    assert return_detail["verification"]["evidences"] == verification_evidences


def test_return_verification_rejects_evidence_from_outside_the_request(db) -> None:
    setup = _workflow_setup(db, suffix="-VERIFY-FOREIGN-EVIDENCE")
    owner = _principal(setup["receiver_user"], "return.own.manage")
    request = create_or_update_return_draft(
        db,
        assignment_id=setup["assignment"].id,
        principal=owner,
        reason_code="EMPTY_NUMBER",
        description="不得引用其他退回申请的证据",
    )
    _evidence(db, request, owner, EvidenceType.CALL_RECORDING.value)
    submitted = submit_return_request(db, return_id=request.id, principal=owner)
    assert submitted.task is not None
    assign_return_verification_task(
        db,
        task_id=submitted.task.id,
        assignee_user_id=setup["telesales"].id,
        assigned_by=setup["operator"].id,
        reason="核验空号证据",
    )
    telesales = _principal(
        setup["telesales"],
        "verification.task.read",
        "verification.task.start",
        "verification.submit",
        "lead.phone.read",
    )
    claim_return_verification_task(db, task_id=submitted.task.id, principal=telesales)

    with pytest.raises(AppError) as exc_info:
        submit_return_verification(
            db,
            task_id=submitted.task.id,
            principal=telesales,
            contact_result="EMPTY_NUMBER",
            conclusion="SUPPORT_RETURN",
            note="尝试引用外部证据",
            evidence_ids=["not-this-return-evidence"],
        )

    assert exc_info.value.code == "RETURN_VERIFY_EVIDENCE_INVALID"
    assert submitted.task.status == VerificationTaskStatus.IN_PROGRESS.value


def test_direct_invalid_rejects_an_existing_telesales_conclusion(db) -> None:
    setup = _workflow_setup(db, suffix="-DIRECT-CONFLICT")
    request, _ = _submit_and_verify(db, setup)
    reviewer = _principal(setup["reviewer"], "return.review")

    with pytest.raises(AppError) as exc_info:
        direct_invalid_return(
            db,
            return_id=request.id,
            principal=reviewer,
            note="不得绕过已有电销结论",
        )

    assert exc_info.value.code == "RETURN_VERIFY_CONCLUSION_EXISTS"


def test_missing_return_submission_event_never_misattributed_review_note(db, caplog) -> None:
    setup = _workflow_setup(db, suffix="-MISSING-EVENT")
    request, task = _submit_and_verify(db, setup)
    submission_event = db.scalar(
        select(AssignmentEvent).where(
            AssignmentEvent.assignment_id == setup["assignment"].id,
            AssignmentEvent.event_type == "V12_RETURN_VERIFY_SUBMITTED",
        )
    )
    assert submission_event is not None
    db.delete(submission_event)
    reviewer = _principal(setup["reviewer"], "return.review")
    final_review_return(
        db,
        return_id=request.id,
        principal=reviewer,
        decision="APPROVE",
        note="运营终审说明不能冒充电销核验备注",
    )
    db.flush()

    telesales = _principal(setup["telesales"], "verification.task.read")
    with caplog.at_level("WARNING", logger="zhongshu.return_v12"):
        detail = return_verification_task_to_dict(
            db,
            task,
            telesales,
            include_verification_info=True,
        )

    assert request.review_note == "运营终审说明不能冒充电销核验备注"
    assert detail["verification_info"]["note"] is None
    assert task.id in caplog.text
    assert request.id in caplog.text


def test_final_reject_restores_following_and_unfreezes_reward(db) -> None:
    setup = _workflow_setup(db, lead_status=LeadV12Status.FOLLOWING.value)
    request, task = _submit_and_verify(db, setup, conclusion="DOES_NOT_SUPPORT_RETURN")
    reviewer = _principal(setup["reviewer"], "return.review")
    result = final_review_return(
        db,
        return_id=request.id,
        principal=reviewer,
        decision="REJECT",
        note="客户确认需求真实，退回理由不成立",
    )
    db.commit()

    assert result.refund_ledger is None
    assert request.status == ReturnV12Status.REJECTED.value
    assert setup["assignment"].status == AssignmentStatus.FOLLOWING.value
    assert setup["lead"].status == LeadV12Status.FOLLOWING.value
    assert setup["reward"].status == RewardStatus.OBSERVING.value
    assert task.status == VerificationTaskStatus.RELEASED.value
    assert db.get(PointsAccount, setup["account"].id).balance == 900
    assert db.scalar(
        select(Notification).where(Notification.scene == "V12_RETURN_REJECTED")
    ) is None


def test_final_review_must_match_the_telesales_fact_conclusion(db) -> None:
    setup = _workflow_setup(db)
    request, _ = _submit_and_verify(db, setup, conclusion="DOES_NOT_SUPPORT_RETURN")
    reviewer = _principal(setup["reviewer"], "return.review")

    with pytest.raises(AppError) as exc_info:
        final_review_return(
            db,
            return_id=request.id,
            principal=reviewer,
            decision="APPROVE",
            note="不能在电销确认客资可用时仍批准退回",
        )

    assert exc_info.value.code == "RETURN_FINAL_DECISION_CONFLICT"


@pytest.mark.parametrize(
    ("conclusion", "decision"),
    [
        ("SUPPORT_RETURN", "REJECT"),
        ("INCONCLUSIVE", "APPROVE"),
        ("INCONCLUSIVE", "REJECT"),
    ],
)
def test_final_review_rejects_all_opposite_or_inconclusive_decisions(
    db,
    conclusion: str,
    decision: str,
) -> None:
    setup = _workflow_setup(db)
    request, _ = _submit_and_verify(db, setup, conclusion=conclusion)
    reviewer = _principal(setup["reviewer"], "return.review")

    with pytest.raises(AppError) as exc_info:
        final_review_return(
            db,
            return_id=request.id,
            principal=reviewer,
            decision=decision,
            note="终审不能越过电销事实结论",
        )

    assert exc_info.value.code == "RETURN_FINAL_DECISION_CONFLICT"


def test_need_more_allows_new_evidence_and_creates_second_verification_round(db) -> None:
    setup = _workflow_setup(db)
    request, first_task = _submit_and_verify(db, setup, conclusion="INCONCLUSIVE")
    reviewer = _principal(setup["reviewer"], "return.review")
    final_review_return(
        db,
        return_id=request.id,
        principal=reviewer,
        decision="NEED_MORE",
        note="请补充客户沟通录音后重新核验",
    )
    assert request.status == ReturnV12Status.NEED_MORE_EVIDENCE.value
    assert first_task.status == VerificationTaskStatus.RELEASED.value

    owner = _principal(setup["receiver_user"], "return.own.manage")
    supplementary = _evidence(db, request, owner, EvidenceType.CALL_RECORDING.value)
    supplementary.sha256 = "b" * 64
    db.flush()
    second = submit_return_request(db, return_id=request.id, principal=owner)
    db.commit()

    assert second.task is not None
    assert second.task.id != first_task.id
    assert second.task.task_type == VerificationTaskType.RETURN_VERIFY.value
    assert request.status == ReturnV12Status.VERIFYING.value
    assert setup["reward"].status == RewardStatus.FROZEN.value
    task_count = db.scalar(
        select(VerificationTask)
        .where(
            VerificationTask.return_request_id == request.id,
            VerificationTask.task_type == VerificationTaskType.RETURN_VERIFY.value,
        )
        .with_only_columns(__import__("sqlalchemy").func.count(VerificationTask.id))
    )
    assert task_count == 2


def test_return_verification_task_list_defaults_to_open_tasks(db) -> None:
    setup = _workflow_setup(db, suffix="-OPEN")
    return_request, submitted_task = _submit_and_verify(db, setup)
    released_task = VerificationTask(
        lead_id=setup["lead"].id,
        template_id=None,
        template_version=1,
        status=VerificationTaskStatus.RELEASED.value,
        task_type=VerificationTaskType.RETURN_VERIFY.value,
        return_request_id=return_request.id,
        assignment_id=setup["assignment"].id,
    )
    db.add(released_task)

    stale_setup = _workflow_setup(db, suffix="-CLOSED")
    closed_return, stale_submitted_task = _submit_and_verify(db, stale_setup)
    closed_return.status = ReturnV12Status.APPROVED.value
    now = datetime.now(timezone.utc)
    submitted_task.created_at = now - timedelta(hours=2)
    submitted_task.submitted_at = now
    stale_submitted_task.created_at = now
    stale_submitted_task.submitted_at = now - timedelta(hours=1)
    db.flush()

    principal = _principal(setup["operator"], "verification.read")
    http_request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    open_result = list_return_verification_tasks(
        http_request,
        principal,
        db,
        status=None,
        mine=False,
        page_no=1,
        page_size=20,
    )
    released_result = list_return_verification_tasks(
        http_request,
        principal,
        db,
        status=VerificationTaskStatus.RELEASED.value,
        mine=False,
        page_no=1,
        page_size=20,
    )
    submitted_result = list_return_verification_tasks(
        http_request,
        principal,
        db,
        status=VerificationTaskStatus.SUBMITTED.value,
        mine=False,
        page_no=1,
        page_size=20,
    )
    history_result = list_return_verification_tasks(
        http_request,
        principal,
        db,
        status=None,
        mine=False,
        submitted_history=True,
        page_no=1,
        page_size=20,
    )
    second_history_page = list_return_verification_tasks(
        http_request,
        principal,
        db,
        status=None,
        mine=False,
        submitted_history=True,
        page_no=2,
        page_size=1,
    )

    assert [item["id"] for item in open_result["data"]["items"]] == [submitted_task.id]
    assert [item["id"] for item in released_result["data"]["items"]] == [released_task.id]
    assert {item["id"] for item in submitted_result["data"]["items"]} == {
        submitted_task.id,
        stale_submitted_task.id,
    }
    assert [item["id"] for item in history_result["data"]["items"][:2]] == [
        submitted_task.id,
        stale_submitted_task.id,
    ]
    assert [item["id"] for item in second_history_page["data"]["items"]] == [
        stale_submitted_task.id
    ]


def test_final_review_requires_submitted_post_call_conclusion(db) -> None:
    setup = _workflow_setup(db)
    owner = _principal(setup["receiver_user"], "return.own.manage")
    request = create_or_update_return_draft(
        db,
        assignment_id=setup["assignment"].id,
        principal=owner,
        reason_code="OUT_OF_SERVICE_REGION",
        description="客户实际建房地点不属于当前公司服务范围",
    )
    _evidence(db, request, owner, EvidenceType.CHAT_SCREENSHOT.value)
    _evidence(db, request, owner, EvidenceType.CALL_RECORDING.value)
    submit_return_request(db, return_id=request.id, principal=owner)
    request.status = ReturnV12Status.REVIEWING.value
    reviewer = _principal(setup["reviewer"], "return.review")
    with pytest.raises(AppError) as exc_info:
        final_review_return(
            db,
            return_id=request.id,
            principal=reviewer,
            decision="APPROVE",
            note="尝试跳过电销事实核验",
        )
    assert exc_info.value.code == "RETURN_VERIFY_CONCLUSION_REQUIRED"


def test_initial_submission_after_deadline_is_marked_expired(db) -> None:
    setup = _workflow_setup(db)
    owner = _principal(setup["receiver_user"], "return.own.manage")
    request = create_or_update_return_draft(
        db,
        assignment_id=setup["assignment"].id,
        principal=owner,
        reason_code="DUPLICATE_TO_RECEIVER",
        description="提交后发现客户已在本公司历史客户库中",
    )
    _evidence(db, request, owner, EvidenceType.CHAT_SCREENSHOT.value)
    _evidence(db, request, owner, EvidenceType.CALL_RECORDING.value)
    setup["assignment"].claimed_at = datetime.now(timezone.utc) - timedelta(hours=48, minutes=1)
    db.flush()
    result = submit_return_request(db, return_id=request.id, principal=owner)
    db.commit()
    assert result.expired is True
    assert request.status == ReturnV12Status.EXPIRED.value
    assert db.scalar(select(VerificationTask).where(VerificationTask.return_request_id == request.id)) is None
