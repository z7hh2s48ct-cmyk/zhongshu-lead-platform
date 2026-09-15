from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from apps.api.src.core import reward_models_v12  # noqa: F401
from apps.api.src.core.errors import AppError
from apps.api.src.core.models import (
    AssignmentEvent,
    FollowUp,
    Notification,
    NotificationOutbox,
    PointsAccount,
    PointsLedger,
    ReturnRequest,
)
from apps.api.src.core.time import as_utc
from apps.api.src.services.followup_service import add_followup
from apps.api.src.services.notification_v12 import (
    drain_due_supplier_reward_settlement_notified,
)
from apps.api.src.services.points_service import change_points
from apps.api.src.services.return_v12 import (
    _final_review_return,
    create_or_update_return_draft,
    direct_invalid_return,
    submit_return_request,
)
from apps.api.src.services.supplier_reward_v12 import (
    reverse_supplier_reward,
    run_due_supplier_reward_settlement,
    settle_supplier_reward,
)
from apps.api.tests.test_v12_return_workflow import (
    _evidence,
    _principal,
    _workflow_setup,
)


def _case(db, *, elapsed=timedelta(hours=48), status="WAITING_CLAIM"):
    setup = _workflow_setup(db, lead_status="FOLLOWING")
    now = datetime.now(timezone.utc)
    assignment, reward = setup["assignment"], setup["reward"]
    assignment.claimed_at = now - elapsed
    assignment.assigned_at = assignment.claimed_at - timedelta(hours=1)
    assignment.appeal_deadline_at = assignment.claimed_at + timedelta(hours=48)
    reward.status = status
    reward.observed_at = None if status == "WAITING_CLAIM" else assignment.claimed_at
    reward.reward_due_at = (
        None if status == "WAITING_CLAIM" else now + timedelta(days=3)
    )
    reward.rule_snapshot_json = {"version": 7, "fixed_points": 30}
    db.commit()
    return setup, now


def _return(db, setup, *, status, submitted=True):
    request = ReturnRequest(
        assignment_id=setup["assignment"].id,
        lead_id=setup["lead"].id,
        company_id=setup["receiver"].id,
        status=status,
        reason_code="EMPTY_NUMBER",
        description="接收方请求平台核实客资",
        submitted_by=setup["receiver_user"].id,
        submitted_at=setup["assignment"].appeal_deadline_at - timedelta(minutes=1)
        if submitted
        else None,
    )
    db.add(request)
    db.commit()
    return request


def test_reward_waits_until_exact_48_hours_even_with_manual_settlement(db):
    setup, now = _case(db, elapsed=timedelta(hours=48) - timedelta(microseconds=1))
    assert run_due_supplier_reward_settlement(db, as_of=now)["settled"] == 0
    with pytest.raises(AppError) as error:
        settle_supplier_reward(
            db, reward_id=setup["reward"].id, as_of=now, require_due=False
        )
    assert error.value.code == "REWARD_NOT_DUE"
    assert setup["reward"].ledger_id is None


@pytest.mark.parametrize("status", ["WAITING_CLAIM", "OBSERVING"])
def test_48_hours_auto_confirms_and_settles_existing_rewards_once(db, status):
    setup, now = _case(db, status=status)
    reward = setup["reward"]
    result = run_due_supplier_reward_settlement(db, as_of=now)
    db.commit()
    assert result["settled"] == 1
    assert reward.status == "SETTLED"
    assert as_utc(reward.reward_due_at) == as_utc(
        setup["assignment"].claimed_at
    ) + timedelta(hours=48)
    assert reward.rule_snapshot_json == {"version": 7, "fixed_points": 30}
    assert setup["assignment"].status == "FOLLOWING"
    assert setup["lead"].current_follow_status == "CONTACTED"
    assert db.scalar(select(func.count(FollowUp.id))) == 0
    event = db.scalar(
        select(AssignmentEvent).where(
            AssignmentEvent.event_type == "V12_ASSIGNMENT_AUTO_CONFIRMED"
        )
    )
    assert event is not None and event.actor_user_id is None
    assert (
        event.payload["effective_at"]
        == as_utc(setup["assignment"].appeal_deadline_at).isoformat()
    )
    assert (
        run_due_supplier_reward_settlement(db, as_of=now + timedelta(days=2))["settled"]
        == 0
    )
    repeated = settle_supplier_reward(db, reward_id=reward.id, as_of=now)
    db.commit()
    assert repeated.idempotent
    assert (
        db.scalar(
            select(func.count(PointsLedger.id)).where(
                PointsLedger.ledger_type == "REWARD"
            )
        )
        == 1
    )
    assert (
        db.scalar(
            select(func.count(AssignmentEvent.id)).where(
                AssignmentEvent.event_type == "V12_ASSIGNMENT_AUTO_CONFIRMED"
            )
        )
        == 1
    )


def test_unsubmitted_return_draft_does_not_block_automatic_settlement(db):
    setup, now = _case(db)
    _return(db, setup, status="DRAFT", submitted=False)
    assert run_due_supplier_reward_settlement(db, as_of=now)["settled"] == 1


@pytest.mark.parametrize(
    "status", ["VERIFYING", "REVIEWING", "NEED_MORE_EVIDENCE", "APPROVED"]
)
def test_formal_return_blocks_auto_confirmation_and_credit(db, status):
    setup, now = _case(db)
    _return(db, setup, status=status)
    setup["assignment"].status = (
        "RETURN_PENDING" if status != "APPROVED" else "RETURNED"
    )
    db.commit()
    assert (
        run_due_supplier_reward_settlement(db, as_of=now + timedelta(days=10))[
            "settled"
        ]
        == 0
    )
    assert setup["reward"].ledger_id is None
    assert (
        db.scalar(
            select(func.count(AssignmentEvent.id)).where(
                AssignmentEvent.event_type == "V12_ASSIGNMENT_AUTO_CONFIRMED"
            )
        )
        == 0
    )


@pytest.mark.parametrize(
    "invalid_context",
    [
        "not_current",
        "correction",
        "released",
        "unclaimed",
        "wrong_supplier",
        "wrong_assignment_supplier",
    ],
)
def test_ineligible_assignment_context_cannot_credit_supplier(db, invalid_context):
    setup, now = _case(db)
    if invalid_context == "not_current":
        setup["lead"].current_assignment_id = None
    elif invalid_context == "correction":
        setup["lead"].pending_reason = "CORRECTION_REVIEW_REQUIRED"
    elif invalid_context == "released":
        setup["assignment"].status = "RELEASED"
    elif invalid_context == "unclaimed":
        setup["assignment"].claimed_at = None
    elif invalid_context == "wrong_assignment_supplier":
        setup["assignment"].supplier_company_id = setup["receiver"].id
    else:
        setup["reward"].supplier_company_id = setup["receiver"].id
    db.commit()
    assert run_due_supplier_reward_settlement(db, as_of=now)["settled"] == 0
    assert setup["reward"].ledger_id is None


def test_early_phone_confirmation_credits_supplier_immediately(db):
    setup, now = _case(db, elapsed=timedelta(hours=1))
    db.autoflush = False
    add_followup(
        db,
        assignment=setup["assignment"],
        principal=_principal(setup["receiver_user"], "followup.own.manage"),
        status="DEAL",
        note="已电话核实需求有效",
        next_followup_at=None,
    )
    db.commit()
    assert setup["assignment"].status == "COMPLETED"
    assert setup["lead"].current_follow_status == "DEAL"
    assert setup["reward"].status == "SETTLED"
    assert setup["reward"].ledger_id is not None
    assert (
        db.scalar(
            select(func.count(AssignmentEvent.id)).where(
                AssignmentEvent.event_type == "V12_ASSIGNMENT_AUTO_CONFIRMED"
            )
        )
        == 0
    )


def test_rejected_overdue_return_settles_without_waiting_old_three_workdays(
    db, monkeypatch
):
    setup, now = _case(db, status="OBSERVING")
    request = _return(db, setup, status="REVIEWING")
    setup["assignment"].status = "RETURN_PENDING"
    setup["reward"].status = "FROZEN"
    setup["reward"].reward_due_at = now + timedelta(days=3)
    db.commit()
    db.autoflush = False
    _final_review_return(
        db,
        return_id=request.id,
        principal=_principal(setup["reviewer"], "return.final.review"),
        decision="REJECT",
        note="核实后客资有效，驳回退回申请",
        require_verification=False,
    )
    db.commit()
    assert request.status == "REJECTED"
    assert setup["reward"].status == "SETTLED"
    assert setup["reward"].ledger_id is not None


def test_automatic_settlement_includes_supplier_notification(db):
    setup, now = _case(db)
    result = drain_due_supplier_reward_settlement_notified(db, as_of=now)
    db.commit()
    assert result["settled"] == 1
    assert (
        db.scalar(
            select(Notification.id).where(
                Notification.company_id == setup["supplier"].id,
                Notification.scene == "V12_SUPPLIER_REWARD_SETTLED",
            )
        )
        is not None
    )


def _early_paid_return(db):
    setup, _ = _case(db, elapsed=timedelta(hours=1))
    db.autoflush = False
    principal = _principal(
        setup["receiver_user"], "followup.own.manage", "return.own.manage"
    )
    add_followup(
        db,
        assignment=setup["assignment"],
        principal=principal,
        status="DEAL",
        note="已电话核实需求有效",
        next_followup_at=None,
    )
    db.commit()
    assert setup["reward"].status == "SETTLED"
    request = create_or_update_return_draft(
        db,
        assignment_id=setup["assignment"].id,
        principal=principal,
        reason_code="EMPTY_NUMBER",
        description="领取后复核发现号码异常，申请退回",
    )
    _evidence(db, request, principal, "CHAT_SCREENSHOT")
    submit_return_request(db, return_id=request.id, principal=principal)
    db.commit()
    assert request.status == "VERIFYING"
    assert setup["reward"].status == "SETTLED"
    return setup, request


@pytest.mark.parametrize("spent", [0, 25])
def test_approved_return_reverses_early_reward_and_refunds_claim_once(
    db, monkeypatch, spent
):
    setup, request = _early_paid_return(db)
    reward = setup["reward"]
    if spent:
        change_points(
            db,
            company_id=setup["supplier"].id,
            delta=-spent,
            ledger_type="ADJUST",
            business_type="TEST_SPEND",
            business_id=reward.id,
            idempotency_key=f"early-spend:{reward.id}",
            created_by=setup["reviewer"].id,
            point_kind="SUPPLY",
        )
        db.commit()
    principal = _principal(setup["reviewer"], "return.final.review")
    # The 48-hour limit applies to submission, not the later final review.
    monkeypatch.setattr(
        "apps.api.src.services.return_v12._now",
        lambda: datetime.now(timezone.utc) + timedelta(days=3),
    )
    result = direct_invalid_return(
        db,
        return_id=request.id,
        principal=principal,
        note="经证据核实客资无效，同意退回",
    )
    db.commit()
    assert request.status == "APPROVED"
    assert reward.status == "REVERSED"
    original = db.get(PointsLedger, reward.ledger_id)
    reversal = db.get(PointsLedger, reward.reversal_ledger_id)
    assert reversal.delta == -original.delta == -30
    assert reversal.related_ledger_id == original.id
    assert reversal.metadata_json["reason_code"] == "RETURN_APPROVED"
    assert reversal.metadata_json["return_request_id"] == request.id
    assert result.refund_ledger.delta == 100
    supplier_account = db.scalar(
        select(PointsAccount).where(
            PointsAccount.company_id == setup["supplier"].id
        )
    )
    assert supplier_account.balance == 0
    assert supplier_account.supply_balance == -spent
    assert (
        db.scalar(
            select(PointsAccount.balance).where(
                PointsAccount.company_id == setup["receiver"].id
            )
        )
        == 1000
    )
    assert setup["assignment"].status == "RETURNED"
    assert setup["lead"].status == "CLOSED"
    repeated = direct_invalid_return(
        db,
        return_id=request.id,
        principal=principal,
        note="重复审核不重复扣回",
    )
    db.commit()
    assert repeated.idempotent
    for business_type in ("V12_RETURN_REFUND", "V12_SUPPLIER_REWARD_REVERSAL"):
        assert (
            db.scalar(
                select(func.count(PointsLedger.id)).where(
                    PointsLedger.business_type == business_type
                )
            )
            == 1
        )
    assert (
        db.scalar(
            select(func.count(NotificationOutbox.id)).where(
                NotificationOutbox.aggregate_id == reward.id,
                NotificationOutbox.event_type == "V12_SUPPLIER_REWARD_REVERSED",
            )
        )
        == 1
    )


def test_rejected_return_keeps_early_reward_paid(db):
    setup, request = _early_paid_return(db)
    request.status = "REVIEWING"
    db.commit()
    _final_review_return(
        db,
        return_id=request.id,
        principal=_principal(setup["reviewer"], "return.final.review"),
        decision="REJECT",
        note="客资确认有效，驳回退回",
        require_verification=False,
    )
    db.commit()
    assert request.status == "REJECTED"
    assert setup["reward"].status == "SETTLED"
    assert setup["reward"].reversal_ledger_id is None
    assert setup["assignment"].status == "COMPLETED"
    supplier_account = db.scalar(
        select(PointsAccount).where(
            PointsAccount.company_id == setup["supplier"].id
        )
    )
    assert supplier_account.balance == 0
    assert supplier_account.supply_balance == 30
    assert (
        db.scalar(
            select(func.count(PointsLedger.id)).where(
                PointsLedger.business_type == "V12_SUPPLIER_REWARD"
            )
        )
        == 1
    )


def test_return_reversal_requires_actual_approved_formal_return(db):
    setup, request = _early_paid_return(db)
    with pytest.raises(AppError) as error:
        reverse_supplier_reward(
            db,
            reward_id=setup["reward"].id,
            reason_code="RETURN_APPROVED",
            note="未经终审不能直接冲回奖励",
            reversed_by=setup["reviewer"].id,
        )
    assert error.value.code == "REWARD_RETURN_NOT_APPROVED"
    assert setup["reward"].status == "SETTLED"


def test_reversal_failure_rolls_back_refund_and_return_approval(db, monkeypatch):
    setup, request = _early_paid_return(db)

    def fail_reversal(session, **kwargs):
        assert (
            session.scalar(
                select(PointsLedger.id).where(
                    PointsLedger.business_type == "V12_RETURN_REFUND"
                )
            )
            is not None
        )
        raise AppError("TEST_REVERSAL_FAILED", "模拟冲回失败", 500)

    monkeypatch.setattr(
        "apps.api.src.services.supplier_reward_v12.change_points",
        fail_reversal,
    )
    with pytest.raises(AppError, match="TEST_REVERSAL_FAILED"):
        direct_invalid_return(
            db,
            return_id=request.id,
            principal=_principal(setup["reviewer"], "return.final.review"),
            note="测试退款和冲回必须一并完成",
        )
    db.rollback()
    assert request.status == "VERIFYING"
    assert request.refund_ledger_id is None
    assert setup["reward"].status == "SETTLED"
    assert setup["reward"].reversal_ledger_id is None
    assert setup["assignment"].status == "RETURN_PENDING"
    assert (
        db.scalar(
            select(PointsAccount.balance).where(
                PointsAccount.company_id == setup["receiver"].id
            )
        )
        == 900
    )
    supplier_account = db.scalar(
        select(PointsAccount).where(
            PointsAccount.company_id == setup["supplier"].id
        )
    )
    assert supplier_account.balance == 0
    assert supplier_account.supply_balance == 30
    assert (
        db.scalar(
            select(func.count(PointsLedger.id)).where(
                PointsLedger.business_type.in_(
                    ["V12_RETURN_REFUND", "V12_SUPPLIER_REWARD_REVERSAL"]
                )
            )
        )
        == 0
    )
    assert (
        db.scalar(
            select(func.count(NotificationOutbox.id)).where(
                NotificationOutbox.event_type == "V12_SUPPLIER_REWARD_REVERSED"
            )
        )
        == 0
    )


@pytest.mark.parametrize(
    ("ledger_type", "point_kind", "expected_customer", "expected_supply"),
    [
        ("REWARD", "SUPPLY", 0, -15),
        ("RETURN", "CUSTOMER", 10, -25),
        ("RECHARGE", "CUSTOMER", 10, -25),
    ],
)
def test_credit_can_reduce_reversal_debt_without_allowing_more_spending(
    db, ledger_type, point_kind, expected_customer, expected_supply
):
    setup, request = _early_paid_return(db)
    supplier_id = setup["supplier"].id
    change_points(
        db,
        company_id=supplier_id,
        delta=-25,
        ledger_type="ADJUST",
        business_type="TEST_SPEND",
        business_id=request.id,
        idempotency_key=f"spend:{request.id}",
        created_by=setup["reviewer"].id,
        point_kind="SUPPLY",
    )
    db.commit()
    direct_invalid_return(
        db,
        return_id=request.id,
        principal=_principal(setup["reviewer"], "return.final.review"),
        note="退回成立，冲回已花费奖励",
    )
    db.commit()
    credit = change_points(
        db,
        company_id=supplier_id,
        delta=10,
        ledger_type=ledger_type,
        business_type="TEST_RECOVER_DEBT",
        business_id=request.id,
        idempotency_key=f"credit:{request.id}",
        created_by=setup["reviewer"].id,
        point_kind=point_kind,
    )
    db.commit()
    assert credit.balance_after == (
        expected_customer if point_kind == "CUSTOMER" else expected_supply
    )
    supplier_account = db.scalar(
        select(PointsAccount).where(PointsAccount.company_id == supplier_id)
    )
    assert supplier_account.balance == expected_customer
    assert supplier_account.supply_balance == expected_supply
    with pytest.raises(AppError) as error:
        change_points(
            db,
            company_id=supplier_id,
            delta=-1,
            ledger_type="ADJUST",
            business_type="TEST_OVERSPEND",
            business_id=request.id,
            idempotency_key=f"overspend:{request.id}",
            created_by=setup["reviewer"].id,
            point_kind="SUPPLY",
        )
    assert error.value.code == "POINTS_INSUFFICIENT"
