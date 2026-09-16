from concurrent.futures import ThreadPoolExecutor
from threading import Event

from sqlalchemy import event, func, select

from apps.api.src.core.errors import AppError
from apps.api.src.core.models import PointsLedger, SupplyTerminationRequest
from apps.api.src.services.return_v12 import correct_region_and_redispatch
from apps.api.src.services.supply_termination import approve_termination, create_termination_request
from apps.api.tests.test_feedback915_region_refund import _region_case
from apps.api.tests.test_return_concurrency_postgres import postgres_factory  # noqa: F401


def test_region_reward_reversal_and_termination_approval_do_not_deadlock(postgres_factory):
    with postgres_factory() as db:
        setup, _, args = _region_case(db, early_reward=True)
        item = create_termination_request(db, company_id=setup['supplier'].id,
            requested_by=setup['receiver_user'].id, reason='终止客资合作',
            payee_name='测试收款人', payee_account='test-account', payment_method='BANK_TRANSFER')
        request_id, reviewer_id = item.id, setup['reviewer'].id
        db.commit()

    return_locked, termination_contended = Event(), Event()
    first_lock = []

    def refund():
        with postgres_factory() as db:
            db.autoflush = False
            def hold_first_financial_lock(conn, cursor, statement, parameters, context, executemany):
                sql = ' '.join(statement.upper().split())
                if not any(lock in sql for lock in ('FOR UPDATE', 'FOR NO KEY UPDATE')) or first_lock:
                    return
                if 'FROM COMPANIES' in sql or 'FROM POINTS_ACCOUNTS' in sql:
                    first_lock.append('company' if 'FROM COMPANIES' in sql else 'account')
                    return_locked.set()
                    assert termination_contended.wait(3), 'termination never contended'
            event.listen(db.connection(), 'after_cursor_execute', hold_first_financial_lock)
            result = correct_region_and_redispatch(db, **args)
            db.commit()
            return result.refund_ledger.id

    def approve():
        assert return_locked.wait(3), 'return never locked financial context'
        with postgres_factory() as db:
            db.autoflush = False
            def signal_before(conn, cursor, statement, parameters, context, executemany):
                sql = ' '.join(statement.upper().split())
                if first_lock == ['company'] and 'FROM COMPANIES' in sql and any(lock in sql for lock in ('FOR UPDATE', 'FOR NO KEY UPDATE')):
                    termination_contended.set()
            def signal_after(conn, cursor, statement, parameters, context, executemany):
                sql = ' '.join(statement.upper().split())
                if first_lock == ['account'] and 'FROM COMPANIES' in sql and any(lock in sql for lock in ('FOR UPDATE', 'FOR NO KEY UPDATE')):
                    termination_contended.set()
            connection = db.connection()
            event.listen(connection, 'before_cursor_execute', signal_before)
            event.listen(connection, 'after_cursor_execute', signal_after)
            try:
                approve_termination(db, request_id=request_id, approved_by=reviewer_id, review_note='并发审核')
                db.commit()
                return 'APPROVED'
            except AppError as exc:
                db.rollback()
                return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        returned, approved = pool.submit(refund), pool.submit(approve)
        assert returned.result(timeout=12)
        assert approved.result(timeout=12) == 'SUPPLY_TERMINATION_BLOCKED'
    with postgres_factory() as db:
        assert db.get(SupplyTerminationRequest, request_id).status == 'REQUESTED'
        for kind in ('V12_RETURN_REFUND', 'V12_SUPPLIER_REWARD_REVERSAL'):
            assert db.scalar(select(func.count(PointsLedger.id)).where(PointsLedger.business_type == kind)) == 1
