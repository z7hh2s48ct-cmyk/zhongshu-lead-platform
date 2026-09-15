from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from apps.api.src.core.auth import Principal
from apps.api.src.core.models import (
    Assignment,
    AuditLog,
    Company,
    Lead,
    NotificationOutbox,
    PointsAccount,
    PointsLedger,
    ReturnRequest,
    Role,
    User,
    UserRole,
    VerificationSubmission,
    VerificationTask,
    WechatIdentity,
)
from apps.api.src.core.models_v12 import SupplierLeadReward
from apps.api.src.core.security import encrypt_text, hash_phone, verify_password
from apps.api.src.core.v12_enums import ReturnV12Status, RewardStatus
from apps.api.src.core.errors import AppError
from apps.api.src.services.company_account_management import provision_owner_credentials
from apps.api.src.services.lead_deletion_v12 import delete_operation_lead, preview_lead_deletion
from apps.api.src.services.points_service import points_available_for_dispatch
from apps.api.src.services.pre_dispatch_v12 import (
    list_historical_rework_leads,
    reopen_closed_lead,
    restore_historical_rework_lead,
)
from apps.api.src.services.verification_service import (
    claim_task,
    create_tasks,
    publish_template,
    submit_verification,
)


def _operation(user_id: str = "operation-914") -> Principal:
    return Principal(
        user_id=user_id,
        display_name="需求确认运营",
        company_id=None,
        role_codes=frozenset({"OPERATION"}),
        permission_codes=frozenset({"*"}),
        session_version=1,
    )


def _lead(db, *, status: str = "DRAFT", submitter_user_id: str | None = None) -> Lead:
    lead = Lead(
        source_type="SUPPLIER_H5",
        source_kind="SUPPLIER_H5",
        submitter_user_id=submitter_user_id,
        customer_name="九一四客户",
        phone_encrypted=encrypt_text("13800139914"),
        phone_hash=hash_phone("13800139914"),
        status=status,
        review_status="APPROVED",
    )
    db.add(lead)
    db.flush()
    return lead


def test_operation_soft_deletes_any_lead_without_erasing_financial_or_workflow_history(db):
    operator = User(display_name="当前运营", status="ACTIVE")
    submitter = User(display_name="原录入人", status="ACTIVE")
    company = Company(code="CF914", name="九一四接收方", status="ACTIVE")
    db.add_all([operator, submitter, company])
    db.flush()
    account = PointsAccount(company_id=company.id, balance=1000)
    db.add(account)
    db.flush()
    lead = _lead(db, status="FOLLOWING", submitter_user_id=submitter.id)
    assignment = Assignment(
        lead_id=lead.id,
        company_id=company.id,
        assigned_by=operator.id,
        points_price=100,
        status="FOLLOWING",
    )
    db.add(assignment)
    db.flush()
    ledger = PointsLedger(
        account_id=account.id,
        company_id=company.id,
        delta=-100,
        balance_after=900,
        ledger_type="CLAIM",
        business_type="ASSIGNMENT",
        business_id=assignment.id,
        idempotency_key=f"claim:{assignment.id}",
    )
    reward = SupplierLeadReward(
        assignment_id=assignment.id,
        lead_id=lead.id,
        supplier_company_id=company.id,
        receiver_company_id=company.id,
        claim_points=100,
        reward_points=50,
        status="SETTLED",
    )
    db.add_all([ledger, reward])
    db.flush()

    preview = preview_lead_deletion(db, lead_id=lead.id, principal=_operation(operator.id))
    assert preview["deletable"] is True
    assert preview["impact"]["assignment_history"] == 1
    result = delete_operation_lead(
        db,
        lead_id=lead.id,
        principal=_operation(operator.id),
        reason="客户要求停止处理",
    )

    assert result.lead.deleted_at is not None
    assert result.lead.deleted_by == operator.id
    assert db.get(Assignment, assignment.id) is assignment
    assert db.get(PointsLedger, ledger.id) is ledger
    assert db.get(SupplierLeadReward, reward.id) is reward


def test_operation_deletion_closes_unfinished_return_tasks_and_rewards(db):
    operator = User(display_name="删除流程运营", status="ACTIVE")
    receiver = Company(code="CF914-DELETE", name="删除流程接收方", status="ACTIVE")
    supplier = Company(code="CF914-SUPPLIER", name="删除流程供资方", status="ACTIVE")
    db.add_all([operator, receiver, supplier])
    db.flush()
    lead = _lead(db, status="FOLLOWING", submitter_user_id=operator.id)
    lead.supplier_company_id = supplier.id
    assignment = Assignment(
        lead_id=lead.id,
        company_id=receiver.id,
        receiver_company_id=receiver.id,
        supplier_company_id=supplier.id,
        assigned_by=operator.id,
        points_price=100,
        claim_points=100,
        status="RETURN_PENDING",
    )
    db.add(assignment)
    db.flush()
    lead.current_assignment_id = assignment.id
    return_request = ReturnRequest(
        assignment_id=assignment.id,
        lead_id=lead.id,
        company_id=receiver.id,
        reason_code="EMPTY_NUMBER",
        description="正式退回仍在核验",
        status=ReturnV12Status.VERIFYING.value,
        submitted_by=operator.id,
    )
    db.add(return_request)
    db.flush()
    task = VerificationTask(
        lead_id=lead.id,
        assignment_id=assignment.id,
        return_request_id=return_request.id,
        task_type="RETURN_VERIFY",
        status="SUBMITTED",
    )
    reward = SupplierLeadReward(
        assignment_id=assignment.id,
        lead_id=lead.id,
        supplier_company_id=supplier.id,
        receiver_company_id=receiver.id,
        claim_points=100,
        reward_points=30,
        status=RewardStatus.OBSERVING.value,
    )
    db.add_all([task, reward])
    db.flush()

    result = delete_operation_lead(
        db,
        lead_id=lead.id,
        principal=_operation(operator.id),
        reason="客户要求终止全部后续处理",
    )

    assert result.lead.deleted_at is not None
    assert return_request.status == ReturnV12Status.CANCELLED.value
    assert return_request.review_note == "客户要求终止全部后续处理"
    assert task.status == "CANCELLED"
    assert reward.status == RewardStatus.CANCELLED.value
    assert reward.cancelled_at == result.lead.deleted_at
    assert reward.exception_reason == "LEAD_DELETED: 客户要求终止全部后续处理"


def test_operation_deletion_settles_reward_that_was_already_effective(db):
    operator = User(display_name="删除结算运营", status="ACTIVE")
    receiver = Company(code="CF914-DUE-REC", name="到期接收方", status="ACTIVE")
    supplier = Company(code="CF914-DUE-SUP", name="到期供资方", status="ACTIVE")
    db.add_all([operator, receiver, supplier])
    db.flush()
    claimed_at = datetime.now(timezone.utc) - timedelta(hours=49)
    lead = _lead(db, status="FOLLOWING", submitter_user_id=operator.id)
    lead.supplier_company_id = supplier.id
    assignment = Assignment(
        lead_id=lead.id,
        company_id=receiver.id,
        receiver_company_id=receiver.id,
        supplier_company_id=supplier.id,
        assigned_by=operator.id,
        assigned_at=claimed_at - timedelta(hours=1),
        claimed_at=claimed_at,
        points_price=100,
        claim_points=100,
        status="FOLLOWING",
    )
    db.add(assignment)
    db.flush()
    lead.current_assignment_id = assignment.id
    reward = SupplierLeadReward(
        assignment_id=assignment.id,
        lead_id=lead.id,
        supplier_company_id=supplier.id,
        receiver_company_id=receiver.id,
        claim_points=100,
        reward_points=30,
        reward_ratio_bps=3000,
        rule_version=1,
        status=RewardStatus.OBSERVING.value,
    )
    db.add(reward)
    db.flush()

    result = delete_operation_lead(
        db,
        lead_id=lead.id,
        principal=_operation(operator.id),
        reason="确认有效后客户要求终止联系",
    )

    assert result.lead.deleted_at is not None
    assert reward.status == RewardStatus.SETTLED.value
    assert reward.ledger_id is not None
    supplier_account = db.scalar(
        select(PointsAccount).where(PointsAccount.company_id == supplier.id)
    )
    assert supplier_account is not None and supplier_account.balance == 30
    assert assignment.status == "RELEASED"
    assert lead.current_assignment_id is None


def test_operation_deletion_releases_pending_claim_point_reservation(db):
    operator = User(display_name="删除预留运营", status="ACTIVE")
    receiver = Company(code="CF914-RESERVE", name="预留积分接收方", status="ACTIVE")
    db.add_all([operator, receiver])
    db.flush()
    db.add(PointsAccount(company_id=receiver.id, balance=500))
    lead = _lead(db, status="DISPATCHED", submitter_user_id=operator.id)
    assignment = Assignment(
        lead_id=lead.id,
        company_id=receiver.id,
        receiver_company_id=receiver.id,
        assigned_by=operator.id,
        assigned_at=datetime.now(timezone.utc),
        points_price=100,
        claim_points=100,
        status="PENDING_CLAIM",
    )
    db.add(assignment)
    db.flush()
    lead.current_assignment_id = assignment.id
    assert points_available_for_dispatch(db, receiver.id) == (500, 100, 400)

    delete_operation_lead(
        db,
        lead_id=lead.id,
        principal=_operation(operator.id),
        reason="删除未领取的误录客资",
    )

    assert assignment.status == "RELEASED"
    assert assignment.release_reason == "LEAD_DELETED"
    assert lead.current_assignment_id is None
    assert points_available_for_dispatch(db, receiver.id) == (500, 0, 500)


def test_operation_deletion_waits_for_live_notification_delivery_lease(db):
    operator = User(display_name="删除消息运营", status="ACTIVE")
    receiver = Company(code="CF914-DELIVERY", name="消息发送接收方", status="ACTIVE")
    db.add_all([operator, receiver])
    db.flush()
    lead = _lead(db, status="DISPATCHED", submitter_user_id=operator.id)
    assignment = Assignment(
        lead_id=lead.id,
        company_id=receiver.id,
        receiver_company_id=receiver.id,
        assigned_by=operator.id,
        points_price=100,
        status="PENDING_CLAIM",
    )
    db.add(assignment)
    db.flush()
    lead.current_assignment_id = assignment.id
    outbox = NotificationOutbox(
        event_key=f"delete-live-delivery:{assignment.id}",
        event_type="V12_ASSIGNMENT_REMINDER",
        aggregate_type="assignment",
        aggregate_id=assignment.id,
        payload={
            "business_ids": {
                "lead_id": lead.id,
                "assignment_id": assignment.id,
            }
        },
        status="PROCESSING",
        next_attempt_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db.add(outbox)
    db.commit()

    try:
        delete_operation_lead(
            db,
            lead_id=lead.id,
            principal=_operation(operator.id),
            reason="发送中的提醒结束后再删除",
        )
    except AppError as exc:
        assert exc.code == "LEAD_NOTIFICATION_DELIVERY_IN_PROGRESS"
        assert exc.status_code == 409
        db.rollback()
    else:
        raise AssertionError("live notification delivery must block deletion")

    assert db.get(Lead, lead.id).deleted_at is None
    assert db.get(Assignment, assignment.id).status == "PENDING_CLAIM"
    assert db.get(NotificationOutbox, outbox.id).status == "PROCESSING"


def _legacy_verification_context(db):
    role = db.scalar(select(Role).where(Role.code == "TELESALES"))
    if role is None:
        role = Role(code="TELESALES", name="电销")
        db.add(role)
    telesales = User(
        username="legacy-verification-914",
        display_name="旧版核验电销",
        status="ACTIVE",
    )
    db.add(telesales)
    db.flush()
    db.add(UserRole(user_id=telesales.id, role_id=role.id))
    lead = _lead(db, status="IMPORTED")
    lead.region_code = "310100"
    lead.category_code = "OLD_RENOVATION"
    publish_template(db, code="CF914-LEGACY", name="旧版核验", schema={"fields": []})
    db.commit()
    task = create_tasks(
        db,
        lead_ids=[lead.id],
        assignee_user_id=telesales.id,
        assigned_by="operation-914",
        template_code="CF914-LEGACY",
    )[0]
    db.commit()
    return lead, task, telesales


def test_deleted_lead_cannot_be_changed_by_a_stale_legacy_verification_submit(db):
    lead, task, telesales = _legacy_verification_context(db)
    task.status = "IN_PROGRESS"
    task.started_at = datetime.now(timezone.utc)
    db.commit()
    # Load both rows before the competing transaction so this session is stale.
    assert db.get(Lead, lead.id).deleted_at is None
    assert db.get(VerificationTask, task.id).status == "IN_PROGRESS"
    deleted_at = datetime.now(timezone.utc)
    with Session(db.get_bind()) as other:
        other.execute(
            update(Lead).where(Lead.id == lead.id).values(deleted_at=deleted_at)
        )
        other.execute(
            update(VerificationTask)
            .where(VerificationTask.id == task.id)
            .values(status="CANCELLED")
        )
        other.commit()

    try:
        submit_verification(
            db,
            task,
            Principal(
                user_id=telesales.id,
                display_name=telesales.display_name,
                company_id=None,
                role_codes=frozenset({"TELESALES"}),
                permission_codes=frozenset({"verification.submit"}),
                session_version=1,
            ),
            {
                "result": "QUALIFIED",
                "answers": {},
                "corrections": {},
                "note": "不应覆盖删除结果",
            },
        )
    except AppError as exc:
        assert exc.code == "VERIFICATION_TASK_NOT_FOUND"
        db.rollback()
    else:
        raise AssertionError("deleted lead must block stale verification submission")

    assert db.get(Lead, lead.id).deleted_at is not None
    assert db.get(VerificationTask, task.id).status == "CANCELLED"
    assert db.scalar(
        select(VerificationSubmission.id).where(VerificationSubmission.task_id == task.id)
    ) is None


def test_deleted_lead_cannot_receive_a_task_from_a_stale_create_request(db):
    lead, _task, telesales = _legacy_verification_context(db)
    second_lead = _lead(db, status="IMPORTED")
    db.commit()
    assert db.get(Lead, second_lead.id).deleted_at is None
    with Session(db.get_bind()) as other:
        other.execute(
            update(Lead)
            .where(Lead.id == second_lead.id)
            .values(deleted_at=datetime.now(timezone.utc))
        )
        other.commit()

    created = create_tasks(
        db,
        lead_ids=[second_lead.id],
        assignee_user_id=telesales.id,
        assigned_by="operation-914",
        template_code="CF914-LEGACY",
    )

    assert created == []
    assert db.scalar(
        select(VerificationTask.id).where(VerificationTask.lead_id == second_lead.id)
    ) is None


def test_operation_reopens_closed_lead_for_a_new_assignment_round_without_reusing_old_assignment(db):
    operator = User(display_name="运营", status="ACTIVE")
    company = Company(code="CF914-R", name="原接收方", status="ACTIVE")
    db.add_all([operator, company])
    db.flush()
    lead = _lead(db, status="CLOSED")
    old_assignment = Assignment(
        lead_id=lead.id,
        company_id=company.id,
        assigned_by=operator.id,
        points_price=100,
        status="RETURNED",
    )
    db.add(old_assignment)
    db.flush()
    lead.current_assignment_id = old_assignment.id

    reopened = reopen_closed_lead(
        db,
        lead_id=lead.id,
        principal=_operation(operator.id),
        reason="客户重新主动咨询",
    )

    assert reopened.status == "READY_DISPATCH"
    assert reopened.current_assignment_id is None
    assert reopened.pending_reason == "REOPENED_FOR_REDISPATCH"
    assert db.get(Assignment, old_assignment.id).status == "RETURNED"


def test_historical_rework_query_uses_disposition_audit_when_current_pending_reason_was_lost(db):
    operator = User(display_name="运营", status="ACTIVE")
    db.add(operator)
    db.flush()
    lead = _lead(db, status="DRAFT")
    task = VerificationTask(
        lead_id=lead.id,
        task_type="PRE_DISPATCH_VERIFY",
        status="RELEASED",
        verification_conclusion="INFO_INCOMPLETE",
    )
    db.add(task)
    db.flush()
    db.add(
        VerificationSubmission(
            task_id=task.id,
            lead_id=lead.id,
            result="INFO_INCOMPLETE",
            submitted_by=operator.id,
        )
    )
    db.add(
        AuditLog(
            actor_user_id=operator.id,
            actor_role_codes=["OPERATION"],
            action="V12_PRE_DISPATCH_DISPOSITION",
            resource_type="lead",
            resource_id=lead.id,
            after_json={"pending_reason": "PRE_DISPATCH_REWORK_REQUIRED"},
            metadata_json={},
        )
    )
    db.flush()
    lead.pending_reason = None

    history, total = list_historical_rework_leads(db, page_no=1, page_size=20)
    assert total == 1
    assert [item["lead"].id for item in history] == [lead.id]
    assert history[0]["latest_task_id"] == task.id
    restored = restore_historical_rework_lead(
        db,
        lead_id=lead.id,
        principal=_operation(operator.id),
        reason="恢复历史待补充入口",
    )
    assert restored.pending_reason == "PRE_DISPATCH_REWORK_REQUIRED"


def test_historical_rework_query_also_keeps_current_pending_rows_without_audit(db):
    lead = _lead(db, status="DRAFT")
    lead.pending_reason = "PRE_DISPATCH_REWORK_REQUIRED"
    db.flush()

    history, total = list_historical_rework_leads(db, page_no=1, page_size=20)

    assert total == 1
    assert [item["lead"].id for item in history] == [lead.id]
    assert history[0]["rework_pending"] is True


def test_historical_rework_query_is_stably_paginated_in_database(db):
    leads = []
    for index in range(3):
        lead = _lead(db, status="DRAFT")
        lead.customer_name = f"分页历史客户{index}"
        lead.pending_reason = "PRE_DISPATCH_REWORK_REQUIRED"
        leads.append(lead)
    db.flush()

    first, first_total = list_historical_rework_leads(db, page_no=1, page_size=2)
    second, second_total = list_historical_rework_leads(db, page_no=2, page_size=2)

    assert first_total == second_total == 3
    assert len(first) == 2
    assert len(second) == 1
    assert {item["lead"].id for item in [*first, *second]} == {
        lead.id for lead in leads
    }


def test_operation_provisions_missing_owner_login(db):
    company = Company(
        code="CF914-A",
        name="负责人治理公司",
        status="ACTIVE",
        contact_phone_encrypted=encrypt_text("13900139914"),
        contact_phone_hash=hash_phone("13900139914"),
    )
    old_owner = User(
        display_name="微信负责人",
        company_id=None,
        status="ACTIVE",
    )
    db.add_all([company, old_owner])
    db.flush()
    owner_role = db.scalar(select(Role).where(Role.code == "FRANCHISE_OWNER"))
    assert owner_role is not None
    old_owner.company_id = company.id
    db.add_all(
        [
            UserRole(user_id=old_owner.id, role_id=owner_role.id),
            WechatIdentity(openid="cf914-old-owner", user_id=old_owner.id),
        ]
    )
    company.primary_user_id = old_owner.id
    db.flush()

    provisioned, initial_password = provision_owner_credentials(
        db,
        company_id=company.id,
        user_id=old_owner.id,
        username=None,
        password=None,
    )
    assert provisioned.username == "13900139914"
    assert verify_password(initial_password, provisioned.password_hash)

    assert db.scalar(select(WechatIdentity).where(WechatIdentity.user_id == old_owner.id)) is not None
