"""Exercise supplier reward and return races against real PostgreSQL locks."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event

from sqlalchemy import event, func, select

from apps.api.src.core import reward_models_v12  # noqa: F401
from apps.api.src.core.enums import EvidenceType, PointsLedgerType
from apps.api.src.core.models import (
    Assignment,
    AssignmentEvent,
    FollowUp,
    NotificationOutbox,
    PointsAccount,
    PointsLedger,
    ReturnRequest,
)
from apps.api.src.core.models_v12 import SupplierLeadReward
from apps.api.src.services import return_v12, supplier_reward_v12
from apps.api.src.services.followup_service import add_followup
from apps.api.src.services.points_service import change_points
from apps.api.src.services.return_v12 import (
    create_or_update_return_draft,
    final_review_return,
    submit_return_request,
)
from apps.api.src.services.supplier_reward_v12 import settle_supplier_reward
from apps.api.tests.test_return_concurrency_postgres import (
    postgres_factory as postgres_factory,
)
from apps.api.tests.test_v12_return_workflow import (
    _evidence,
    _principal,
    _submit_and_verify,
    _workflow_setup,
)


def _setup_case(postgres_factory):
    with postgres_factory() as db:
        db.autoflush = False
        setup = _workflow_setup(db, lead_status="FOLLOWING")
        deadline = datetime(2026, 9, 13, 4, 0, tzinfo=timezone.utc)
        assignment = setup["assignment"]
        reward = setup["reward"]
        assignment.claimed_at = deadline - timedelta(hours=48)
        assignment.assigned_at = assignment.claimed_at - timedelta(hours=1)
        assignment.appeal_deadline_at = deadline
        reward.status = "WAITING_CLAIM"
        reward.observed_at = None
        reward.appeal_deadline_at = deadline
        reward.reward_due_at = deadline
        db.commit()
        return {
            "assignment_id": assignment.id,
            "reward_id": reward.id,
            "supplier_company_id": setup["supplier"].id,
            "owner": _principal(setup["receiver_user"], "return.own.manage"),
            "deadline": deadline,
        }


def _create_legal_return_draft(postgres_factory, case):
    with postgres_factory() as db:
        db.autoflush = False
        request = create_or_update_return_draft(
            db,
            assignment_id=case["assignment_id"],
            principal=case["owner"],
            reason_code="EMPTY_NUMBER",
            description="多次联系后确认号码异常，需要申请退回",
        )
        _evidence(db, request, case["owner"], EvidenceType.CHAT_SCREENSHOT.value)
        request_id = request.id
        db.commit()
        return request_id


def _is_assignment_lock(statement: str) -> bool:
    normalized = " ".join(statement.upper().split())
    return "FROM ASSIGNMENTS" in normalized and "FOR UPDATE" in normalized


def _is_points_account_lock(statement: str) -> bool:
    normalized = " ".join(statement.upper().split())
    return "FROM POINTS_ACCOUNTS" in normalized and "FOR UPDATE" in normalized


def _counts(db, case):
    return {
        "ledgers": db.scalar(
            select(func.count(PointsLedger.id)).where(
                PointsLedger.company_id == case["supplier_company_id"],
                PointsLedger.business_type == "V12_SUPPLIER_REWARD",
                PointsLedger.business_id == case["reward_id"],
            )
        ),
        "events": db.scalar(
            select(func.count(AssignmentEvent.id)).where(
                AssignmentEvent.assignment_id == case["assignment_id"],
                AssignmentEvent.event_type == "V12_ASSIGNMENT_AUTO_CONFIRMED",
            )
        ),
    }


def _setup_early_settled_return(postgres_factory, monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(return_v12, "_now", lambda value=None: value or now)
    with postgres_factory() as db:
        db.autoflush = False
        setup = _workflow_setup(db, lead_status="FOLLOWING")
        assignment = setup["assignment"]
        reward = setup["reward"]
        assignment.claimed_at = now - timedelta(hours=1)
        assignment.assigned_at = assignment.claimed_at - timedelta(hours=1)
        assignment.appeal_deadline_at = assignment.claimed_at + timedelta(hours=48)
        reward.status = "WAITING_CLAIM"
        reward.observed_at = None
        reward.appeal_deadline_at = assignment.appeal_deadline_at
        reward.reward_due_at = assignment.appeal_deadline_at
        add_followup(
            db,
            assignment=assignment,
            principal=_principal(setup["receiver_user"], "followup.own.manage"),
            status="DEAL",
            note="接收方提前确认客资有效",
            next_followup_at=None,
        )
        request, _ = _submit_and_verify(db, setup)
        case = {
            "assignment_id": assignment.id,
            "request_id": request.id,
            "reward_id": reward.id,
            "supplier_company_id": setup["supplier"].id,
            "receiver_company_id": setup["receiver"].id,
            "reviewer": _principal(setup["reviewer"], "return.review"),
        }
        db.commit()
        return case


def test_two_sessions_settle_same_due_reward_once(postgres_factory):
    case = _setup_case(postgres_factory)
    first_locked = Event()
    second_attempted = Event()

    def settle_first():
        with postgres_factory() as db:
            db.autoflush = False
            connection = db.connection()
            paused = False

            def pause_after_assignment_lock(
                conn, cursor, statement, parameters, context, executemany
            ):
                nonlocal paused
                if _is_assignment_lock(statement) and not paused:
                    paused = True
                    first_locked.set()
                    assert second_attempted.wait(3), (
                        "second settlement did not contend on Assignment"
                    )

            event.listen(
                connection, "after_cursor_execute", pause_after_assignment_lock
            )
            result = settle_supplier_reward(
                db,
                reward_id=case["reward_id"],
                as_of=case["deadline"] + timedelta(seconds=1),
            )
            db.commit()
            return result.idempotent

    def settle_second():
        assert first_locked.wait(3), (
            "first settlement did not acquire the Assignment lock"
        )
        with postgres_factory() as db:
            db.autoflush = False
            connection = db.connection()

            def signal_assignment_attempt(
                conn, cursor, statement, parameters, context, executemany
            ):
                if _is_assignment_lock(statement):
                    second_attempted.set()

            event.listen(connection, "before_cursor_execute", signal_assignment_attempt)
            result = settle_supplier_reward(
                db,
                reward_id=case["reward_id"],
                as_of=case["deadline"] + timedelta(seconds=1),
            )
            db.commit()
            return result.idempotent

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(settle_first)
        second = executor.submit(settle_second)
        outcomes = [first.result(timeout=10), second.result(timeout=10)]

    assert sorted(outcomes) == [False, True]
    with postgres_factory() as db:
        db.autoflush = False
        reward = db.get(SupplierLeadReward, case["reward_id"])
        assert reward.status == "SETTLED"
        assert _counts(db, case) == {"ledgers": 1, "events": 1}


def test_return_lock_before_deadline_blocks_settlement_and_wins(
    postgres_factory, monkeypatch
):
    case = _setup_case(postgres_factory)
    monkeypatch.setattr(
        return_v12, "_now", lambda value=None: case["deadline"] - timedelta(seconds=1)
    )
    return_id = _create_legal_return_draft(postgres_factory, case)
    return_locked = Event()
    settlement_attempted = Event()

    def submit_return_first():
        with postgres_factory() as db:
            db.autoflush = False
            connection = db.connection()
            paused = False

            def pause_after_assignment_lock(
                conn, cursor, statement, parameters, context, executemany
            ):
                nonlocal paused
                if _is_assignment_lock(statement) and not paused:
                    paused = True
                    return_locked.set()
                    assert settlement_attempted.wait(3), (
                        "settlement did not contend on Assignment"
                    )

            event.listen(
                connection, "after_cursor_execute", pause_after_assignment_lock
            )
            result = submit_return_request(
                db, return_id=return_id, principal=case["owner"]
            )
            db.commit()
            return result.request.status

    def settle_second():
        assert return_locked.wait(3), (
            "return submission did not acquire the Assignment lock"
        )
        with postgres_factory() as db:
            db.autoflush = False
            connection = db.connection()

            def signal_assignment_attempt(
                conn, cursor, statement, parameters, context, executemany
            ):
                if _is_assignment_lock(statement):
                    settlement_attempted.set()

            event.listen(connection, "before_cursor_execute", signal_assignment_attempt)
            result = settle_supplier_reward(
                db,
                reward_id=case["reward_id"],
                as_of=case["deadline"] + timedelta(seconds=1),
            )
            db.commit()
            return result.frozen

    with ThreadPoolExecutor(max_workers=2) as executor:
        return_future = executor.submit(submit_return_first)
        settlement_future = executor.submit(settle_second)
        assert return_future.result(timeout=10) == "VERIFYING"
        assert settlement_future.result(timeout=10) is True

    with postgres_factory() as db:
        db.autoflush = False
        assert db.get(ReturnRequest, return_id).status == "VERIFYING"
        assert db.get(SupplierLeadReward, case["reward_id"]).status == "FROZEN"
        assert _counts(db, case) == {"ledgers": 0, "events": 0}


def test_settlement_lock_at_deadline_wins_over_late_first_return(
    postgres_factory, monkeypatch
):
    case = _setup_case(postgres_factory)
    monkeypatch.setattr(
        return_v12, "_now", lambda value=None: case["deadline"] - timedelta(seconds=1)
    )
    return_id = _create_legal_return_draft(postgres_factory, case)
    monkeypatch.setattr(
        return_v12, "_now", lambda value=None: case["deadline"] + timedelta(seconds=1)
    )
    settlement_locked = Event()
    return_attempted = Event()

    def settle_first():
        with postgres_factory() as db:
            db.autoflush = False
            connection = db.connection()
            paused = False

            def pause_after_assignment_lock(
                conn, cursor, statement, parameters, context, executemany
            ):
                nonlocal paused
                if _is_assignment_lock(statement) and not paused:
                    paused = True
                    settlement_locked.set()
                    assert return_attempted.wait(3), (
                        "late return did not contend on Assignment"
                    )

            event.listen(
                connection, "after_cursor_execute", pause_after_assignment_lock
            )
            result = settle_supplier_reward(
                db,
                reward_id=case["reward_id"],
                as_of=case["deadline"] + timedelta(seconds=1),
            )
            db.commit()
            return result.reward.status

    def submit_return_second():
        assert settlement_locked.wait(3), (
            "settlement did not acquire the Assignment lock"
        )
        with postgres_factory() as db:
            db.autoflush = False
            connection = db.connection()

            def signal_assignment_attempt(
                conn, cursor, statement, parameters, context, executemany
            ):
                if _is_assignment_lock(statement):
                    return_attempted.set()

            event.listen(connection, "before_cursor_execute", signal_assignment_attempt)
            result = submit_return_request(
                db, return_id=return_id, principal=case["owner"]
            )
            db.commit()
            return result.expired, result.request.status

    with ThreadPoolExecutor(max_workers=2) as executor:
        settlement_future = executor.submit(settle_first)
        return_future = executor.submit(submit_return_second)
        assert settlement_future.result(timeout=10) == "SETTLED"
        assert return_future.result(timeout=10) == (True, "EXPIRED")

    with postgres_factory() as db:
        db.autoflush = False
        assert db.get(ReturnRequest, return_id).status == "EXPIRED"
        assert db.get(SupplierLeadReward, case["reward_id"]).status == "SETTLED"
        assert _counts(db, case) == {"ledgers": 1, "events": 1}


def test_batch_skips_busy_assignment_while_manual_deal_waits_on_same_supplier_account(
    postgres_factory,
    monkeypatch,
):
    deadline = datetime(2026, 9, 13, 4, 0, tzinfo=timezone.utc)
    with postgres_factory() as db:
        db.autoflush = False
        setup_a = _workflow_setup(db, lead_status="FOLLOWING", suffix="-A")
        setup_b = _workflow_setup(db, lead_status="FOLLOWING", suffix="-B")
        shared_supplier_id = setup_a["supplier"].id
        setup_b["lead"].supplier_company_id = shared_supplier_id
        setup_b["assignment"].supplier_company_id = shared_supplier_id
        setup_b["reward"].supplier_company_id = shared_supplier_id
        db.add(PointsAccount(company_id=shared_supplier_id, balance=0, version=1))
        for setup, elapsed in (
            (setup_a, timedelta(hours=49)),
            (setup_b, timedelta(hours=48)),
        ):
            assignment = setup["assignment"]
            reward = setup["reward"]
            assignment.claimed_at = deadline - elapsed
            assignment.assigned_at = assignment.claimed_at - timedelta(hours=1)
            assignment.appeal_deadline_at = assignment.claimed_at + timedelta(hours=48)
            reward.status = "WAITING_CLAIM"
            reward.observed_at = None
            reward.appeal_deadline_at = assignment.appeal_deadline_at
            reward.reward_due_at = assignment.appeal_deadline_at
        case_a = {
            "assignment_id": setup_a["assignment"].id,
            "reward_id": setup_a["reward"].id,
        }
        case_b = {
            "assignment_id": setup_b["assignment"].id,
            "reward_id": setup_b["reward"].id,
            "owner": _principal(setup_b["receiver_user"], "followup.own.manage"),
        }
        db.commit()

    batch_holds_account = Event()
    manual_attempted_account = Event()
    original_settle = supplier_reward_v12.settle_supplier_reward

    def observe_first_batch_settlement(*args, **kwargs):
        result = original_settle(*args, **kwargs)
        if kwargs["reward_id"] == case_a["reward_id"]:
            batch_holds_account.set()
            assert manual_attempted_account.wait(3), (
                "manual DEAL did not wait on supplier PointsAccount"
            )
        return result

    monkeypatch.setattr(
        supplier_reward_v12, "settle_supplier_reward", observe_first_batch_settlement
    )

    def settle_due_batch():
        with postgres_factory() as db:
            db.autoflush = False
            result = supplier_reward_v12.run_due_supplier_reward_settlement(
                db,
                as_of=deadline + timedelta(seconds=1),
            )
            db.commit()
            return result

    def confirm_b_manually():
        assert batch_holds_account.wait(3), (
            "batch did not settle A and retain the supplier account lock"
        )
        with postgres_factory() as db:
            db.autoflush = False
            connection = db.connection()

            def signal_account_attempt(
                conn, cursor, statement, parameters, context, executemany
            ):
                if _is_points_account_lock(statement):
                    manual_attempted_account.set()

            event.listen(connection, "before_cursor_execute", signal_account_attempt)
            assignment = db.get(Assignment, case_b["assignment_id"])
            add_followup(
                db,
                assignment=assignment,
                principal=case_b["owner"],
                status="DEAL",
                note="已人工确认客户需求有效",
                next_followup_at=None,
            )
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as executor:
        batch_future = executor.submit(settle_due_batch)
        manual_future = executor.submit(confirm_b_manually)
        batch_result = batch_future.result(timeout=10)
        manual_future.result(timeout=10)

    assert batch_result["settled"] == 1
    assert batch_result["skipped"] == 1
    assert batch_result["failed"] == 0
    with postgres_factory() as db:
        db.autoflush = False
        assert (
            supplier_reward_v12.run_due_supplier_reward_settlement(
                db,
                as_of=deadline + timedelta(minutes=1),
            )["scanned"]
            == 0
        )
        rewards = db.scalars(
            select(SupplierLeadReward).where(
                SupplierLeadReward.id.in_([case_a["reward_id"], case_b["reward_id"]])
            )
        ).all()
        assert {reward.status for reward in rewards} == {"SETTLED"}
        assert (
            db.scalar(
                select(func.count(PointsLedger.id)).where(
                    PointsLedger.company_id == shared_supplier_id,
                    PointsLedger.business_type == "V12_SUPPLIER_REWARD",
                )
            )
            == 2
        )
        assert (
            db.scalar(
                select(func.count(FollowUp.id)).where(
                    FollowUp.assignment_id == case_b["assignment_id"],
                    FollowUp.status == "DEAL",
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(NotificationOutbox.id)).where(
                    NotificationOutbox.event_type == "V12_SUPPLIER_REWARD_SETTLED",
                    NotificationOutbox.aggregate_id.in_(
                        [case_a["reward_id"], case_b["reward_id"]]
                    ),
                )
            )
            == 2
        )
        assert (
            db.scalar(
                select(func.count(AssignmentEvent.id)).where(
                    AssignmentEvent.event_type == "V12_ASSIGNMENT_AUTO_CONFIRMED",
                    AssignmentEvent.assignment_id.in_(
                        [case_a["assignment_id"], case_b["assignment_id"]]
                    ),
                )
            )
            == 1
        )


def test_concurrent_return_approvals_refund_and_reverse_early_reward_once(
    postgres_factory,
    monkeypatch,
):
    case = _setup_early_settled_return(postgres_factory, monkeypatch)
    first_locked = Event()
    second_attempted = Event()

    def approve_first():
        with postgres_factory() as db:
            db.autoflush = False
            connection = db.connection()
            paused = False

            def pause_after_assignment_lock(
                conn, cursor, statement, parameters, context, executemany
            ):
                nonlocal paused
                if _is_assignment_lock(statement) and not paused:
                    paused = True
                    first_locked.set()
                    assert second_attempted.wait(3), (
                        "second approval did not contend on Assignment"
                    )

            event.listen(
                connection, "after_cursor_execute", pause_after_assignment_lock
            )
            result = final_review_return(
                db,
                return_id=case["request_id"],
                principal=case["reviewer"],
                decision="APPROVE",
                note="证据和电销结论均支持退回",
            )
            db.commit()
            return result.idempotent

    def approve_second():
        assert first_locked.wait(3), (
            "first approval did not acquire the Assignment lock"
        )
        with postgres_factory() as db:
            db.autoflush = False
            connection = db.connection()

            def signal_assignment_attempt(
                conn, cursor, statement, parameters, context, executemany
            ):
                if _is_assignment_lock(statement):
                    second_attempted.set()

            event.listen(connection, "before_cursor_execute", signal_assignment_attempt)
            result = final_review_return(
                db,
                return_id=case["request_id"],
                principal=case["reviewer"],
                decision="APPROVE",
                note="并发重复终审不得重复退款或冲正",
            )
            db.commit()
            return result.idempotent

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(approve_first)
        second = executor.submit(approve_second)
        outcomes = [first.result(timeout=10), second.result(timeout=10)]

    assert sorted(outcomes) == [False, True]
    with postgres_factory() as db:
        db.autoflush = False
        request = db.get(ReturnRequest, case["request_id"])
        reward = db.get(SupplierLeadReward, case["reward_id"])
        supplier_account = db.scalar(
            select(PointsAccount).where(
                PointsAccount.company_id == case["supplier_company_id"]
            )
        )
        receiver_account = db.scalar(
            select(PointsAccount).where(
                PointsAccount.company_id == case["receiver_company_id"]
            )
        )
        assert request.status == "APPROVED"
        assert reward.status == "REVERSED"
        assert supplier_account.balance == 0
        assert receiver_account.balance == 1000
        assert (
            db.scalar(
                select(func.count(PointsLedger.id)).where(
                    PointsLedger.company_id == case["receiver_company_id"],
                    PointsLedger.business_type == "V12_RETURN_REFUND",
                    PointsLedger.business_id == case["request_id"],
                )
            )
            == 1
        )
        reversal = db.scalar(
            select(PointsLedger).where(
                PointsLedger.company_id == case["supplier_company_id"],
                PointsLedger.business_type == "V12_SUPPLIER_REWARD_REVERSAL",
                PointsLedger.business_id == case["reward_id"],
            )
        )
        assert reversal is not None and reversal.delta == -30
        assert reversal.metadata_json["reason_code"] == "RETURN_APPROVED"
        assert (
            db.scalar(
                select(func.count(AssignmentEvent.id)).where(
                    AssignmentEvent.assignment_id == case["assignment_id"],
                    AssignmentEvent.event_type == "V12_RETURN_APPROVED",
                )
            )
            == 1
        )


def test_return_approval_reverses_exactly_after_concurrent_supplier_spend(
    postgres_factory,
    monkeypatch,
):
    case = _setup_early_settled_return(postgres_factory, monkeypatch)
    spend_holds_account = Event()
    approval_attempted_account = Event()

    def spend_reward_points():
        with postgres_factory() as db:
            db.autoflush = False
            connection = db.connection()
            paused = False

            def pause_after_account_lock(
                conn, cursor, statement, parameters, context, executemany
            ):
                nonlocal paused
                if _is_points_account_lock(statement) and not paused:
                    paused = True
                    spend_holds_account.set()
                    assert approval_attempted_account.wait(3), (
                        "approval did not wait on supplier PointsAccount"
                    )

            event.listen(connection, "after_cursor_execute", pause_after_account_lock)
            change_points(
                db,
                company_id=case["supplier_company_id"],
                delta=-25,
                ledger_type=PointsLedgerType.ADJUST.value,
                business_type="TEST_CONCURRENT_REWARD_SPEND",
                business_id=case["reward_id"],
                idempotency_key=f"test-concurrent-spend:{case['reward_id']}",
                created_by=case["reviewer"].user_id,
            )
            db.commit()

    def approve_return():
        assert spend_holds_account.wait(3), (
            "supplier spend did not acquire the PointsAccount lock"
        )
        with postgres_factory() as db:
            db.autoflush = False
            connection = db.connection()

            def signal_supplier_account_attempt(
                conn, cursor, statement, parameters, context, executemany
            ):
                if _is_points_account_lock(statement) and case[
                    "supplier_company_id"
                ] in str(parameters):
                    approval_attempted_account.set()

            event.listen(
                connection, "before_cursor_execute", signal_supplier_account_attempt
            )
            result = final_review_return(
                db,
                return_id=case["request_id"],
                principal=case["reviewer"],
                decision="APPROVE",
                note="退回批准时供应奖励已经被部分使用",
            )
            db.commit()
            return result.idempotent

    with ThreadPoolExecutor(max_workers=2) as executor:
        spend_future = executor.submit(spend_reward_points)
        approval_future = executor.submit(approve_return)
        spend_future.result(timeout=10)
        assert approval_future.result(timeout=10) is False

    with postgres_factory() as db:
        db.autoflush = False
        supplier_account = db.scalar(
            select(PointsAccount).where(
                PointsAccount.company_id == case["supplier_company_id"]
            )
        )
        receiver_account = db.scalar(
            select(PointsAccount).where(
                PointsAccount.company_id == case["receiver_company_id"]
            )
        )
        reward = db.get(SupplierLeadReward, case["reward_id"])
        assert supplier_account.balance == -25
        assert receiver_account.balance == 1000
        assert reward.status == "REVERSED"
        assert (
            db.scalar(
                select(func.count(PointsLedger.id)).where(
                    PointsLedger.company_id == case["supplier_company_id"],
                    PointsLedger.business_type == "TEST_CONCURRENT_REWARD_SPEND",
                    PointsLedger.business_id == case["reward_id"],
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(PointsLedger.id)).where(
                    PointsLedger.company_id == case["supplier_company_id"],
                    PointsLedger.business_type == "V12_SUPPLIER_REWARD_REVERSAL",
                    PointsLedger.business_id == case["reward_id"],
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(PointsLedger.id)).where(
                    PointsLedger.company_id == case["receiver_company_id"],
                    PointsLedger.business_type == "V12_RETURN_REFUND",
                    PointsLedger.business_id == case["request_id"],
                )
            )
            == 1
        )


def test_batch_skips_account_locked_by_return_then_settles_next_run(
    postgres_factory,
    monkeypatch,
):
    return_case = _setup_early_settled_return(postgres_factory, monkeypatch)
    now = datetime.now(timezone.utc)
    with postgres_factory() as db:
        db.autoflush = False
        due_setup = _workflow_setup(db, lead_status="FOLLOWING", suffix="-DUE")
        due_setup["lead"].supplier_company_id = return_case["supplier_company_id"]
        due_setup["assignment"].supplier_company_id = return_case["supplier_company_id"]
        due_setup["reward"].supplier_company_id = return_case["supplier_company_id"]
        due_setup["assignment"].claimed_at = now - timedelta(hours=49)
        due_setup["assignment"].appeal_deadline_at = now - timedelta(hours=1)
        due_setup["reward"].status = "WAITING_CLAIM"
        due_setup["reward"].observed_at = None
        due_setup["reward"].appeal_deadline_at = now - timedelta(hours=1)
        due_setup["reward"].reward_due_at = now - timedelta(hours=1)
        due_reward_id = due_setup["reward"].id
        db.commit()

    return_holds_accounts = Event()
    batch_attempted_account = Event()

    def approve_return_while_holding_accounts():
        with postgres_factory() as db:
            db.autoflush = False
            connection = db.connection()
            paused = False

            def pause_after_accounts_lock(
                conn, cursor, statement, parameters, context, executemany
            ):
                nonlocal paused
                if _is_points_account_lock(statement) and not paused:
                    paused = True
                    return_holds_accounts.set()
                    assert batch_attempted_account.wait(3), (
                        "batch did not try the locked supplier account"
                    )

            event.listen(connection, "after_cursor_execute", pause_after_accounts_lock)
            result = final_review_return(
                db,
                return_id=return_case["request_id"],
                principal=return_case["reviewer"],
                decision="APPROVE",
                note="退回事务持有账户锁时批次应跳过",
            )
            db.commit()
            return result.request.status

    def run_batch_while_account_is_busy():
        assert return_holds_accounts.wait(3), (
            "return approval did not acquire both account locks"
        )
        with postgres_factory() as db:
            db.autoflush = False
            connection = db.connection()

            def signal_supplier_account_attempt(
                conn, cursor, statement, parameters, context, executemany
            ):
                if _is_points_account_lock(statement) and return_case[
                    "supplier_company_id"
                ] in str(parameters):
                    batch_attempted_account.set()

            event.listen(
                connection, "before_cursor_execute", signal_supplier_account_attempt
            )
            result = supplier_reward_v12.run_due_supplier_reward_settlement(
                db, as_of=now
            )
            db.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        return_future = executor.submit(approve_return_while_holding_accounts)
        batch_future = executor.submit(run_batch_while_account_is_busy)
        busy_result = batch_future.result(timeout=10)
        assert return_future.result(timeout=10) == "APPROVED"

    assert busy_result["settled"] == 0
    assert busy_result["skipped"] == 1
    assert busy_result["failed"] == 0
    with postgres_factory() as db:
        db.autoflush = False
        retry_result = supplier_reward_v12.run_due_supplier_reward_settlement(
            db, as_of=now
        )
        db.commit()
        assert retry_result["settled"] == 1
        assert retry_result["skipped"] == 0
        assert db.get(SupplierLeadReward, due_reward_id).status == "SETTLED"
        assert db.get(SupplierLeadReward, return_case["reward_id"]).status == "REVERSED"
        supplier_account = db.scalar(
            select(PointsAccount).where(
                PointsAccount.company_id == return_case["supplier_company_id"]
            )
        )
        assert supplier_account.balance == 30
        assert (
            db.scalar(
                select(func.count(PointsLedger.id)).where(
                    PointsLedger.company_id == return_case["supplier_company_id"],
                    PointsLedger.business_type == "V12_SUPPLIER_REWARD",
                    PointsLedger.business_id == due_reward_id,
                )
            )
            == 1
        )
