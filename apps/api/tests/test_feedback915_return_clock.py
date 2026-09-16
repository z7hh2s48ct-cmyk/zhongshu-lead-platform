from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from apps.api.src.core.enums import EvidenceType
from apps.api.src.core.errors import AppError
from apps.api.src.core.models import Assignment, AssignmentEvent, FollowUp, PointsLedger, VerificationTask
from apps.api.src.core.time import as_utc
from apps.api.src.services import return_v12 as service
from apps.api.src.services.supplier_reward_v12 import (
    confirmation_sql_expressions, resolve_effective_confirmation, run_due_supplier_reward_settlement,
)
from apps.api.tests.test_v12_return_workflow import _evidence, _principal, _workflow_setup
from apps.api.tests.test_return_concurrency_postgres import postgres_factory  # noqa: F401


def _submit_near_deadline(db, monkeypatch):
    setup = _workflow_setup(db)
    claimed = datetime.now(timezone.utc) - timedelta(hours=47, minutes=59)
    submitted = claimed + timedelta(hours=47, minutes=59)
    setup['assignment'].claimed_at = claimed
    db.commit()
    monkeypatch.setattr(service, '_now', lambda: submitted)
    owner = _principal(setup['receiver_user'], 'return.own.manage')
    draft = service.create_or_update_return_draft(db, assignment_id=setup['assignment'].id,
        principal=owner, reason_code='EMPTY_NUMBER', description='联系客户未接听，申请重新核实')
    assert getattr(setup['assignment'], 'appeal_paused_at', None) is None
    _evidence(db, draft, owner, EvidenceType.CHAT_SCREENSHOT.value)
    assert getattr(setup['assignment'], 'appeal_paused_at', None) is None
    result = service.submit_return_request(db, return_id=draft.id, principal=owner)
    db.commit()
    return setup, result.request, owner, submitted


def test_formal_return_pauses_and_rejection_resumes_only_remaining_minute(db, monkeypatch):
    setup, request, owner, submitted = _submit_near_deadline(db, monkeypatch)
    assignment = setup['assignment']
    assert as_utc(assignment.appeal_paused_at) == submitted
    assert assignment.appeal_remaining_seconds == 60
    reviewed = submitted + timedelta(days=4)
    assert run_due_supplier_reward_settlement(db, as_of=reviewed)['settled'] == 0
    request.status = 'REVIEWING'
    task = db.get(VerificationTask, request.verification_task_id)
    task.status = 'SUBMITTED'
    task.submitted_at = submitted
    task.verification_conclusion = 'DOES_NOT_SUPPORT_RETURN'
    db.commit()
    monkeypatch.setattr(service, '_now', lambda: reviewed)
    service._final_review_return(db, return_id=request.id,
        principal=_principal(setup['reviewer'], 'return.review'), decision='REJECT',
        note='重新核实后客户电话正常')
    db.commit()
    assert assignment.appeal_paused_at is None
    assert as_utc(assignment.appeal_deadline_at) == reviewed + timedelta(minutes=1)
    assert setup['reward'].status == 'OBSERVING'
    assert run_due_supplier_reward_settlement(db, as_of=reviewed + timedelta(seconds=59))['settled'] == 0
    assert run_due_supplier_reward_settlement(db, as_of=reviewed + timedelta(minutes=1))['settled'] == 1


def test_rejected_return_can_be_submitted_again_with_new_evidence_without_losing_history(db, monkeypatch):
    setup, request, owner, submitted = _submit_near_deadline(db, monkeypatch)
    request.status = 'REVIEWING'
    task = db.get(VerificationTask, request.verification_task_id)
    task.status = 'SUBMITTED'
    task.submitted_at = submitted
    task.verification_conclusion = 'DOES_NOT_SUPPORT_RETURN'
    db.commit()
    reviewed = submitted + timedelta(days=1)
    monkeypatch.setattr(service, '_now', lambda: reviewed)
    service._final_review_return(db, return_id=request.id,
        principal=_principal(setup['reviewer'], 'return.review'), decision='REJECT',
        note='第一次核验后暂不支持退回')
    db.commit()
    old_task_id = request.verification_task_id
    old_description = request.description
    old_task = db.get(VerificationTask, old_task_id)
    old_task.assignee_user_id = setup['telesales'].id
    db.commit()
    monkeypatch.setattr(service, '_now', lambda: reviewed + timedelta(seconds=20))
    draft = service.create_or_update_return_draft(db, assignment_id=setup['assignment'].id,
        principal=owner, reason_code='EMPTY_NUMBER', description='再次联系仍有问题，补充录音重新申请')
    with pytest.raises(AppError) as error:
        service.submit_return_request(db, return_id=draft.id, principal=owner)
    assert error.value.code == 'RETURN_SUPPLEMENT_REQUIRED'
    evidence = _evidence(db, draft, owner, EvidenceType.CALL_RECORDING.value)
    evidence.sha256 = 'b' * 64
    db.flush()
    result = service.submit_return_request(db, return_id=draft.id, principal=owner)
    assert result.task.id != old_task_id
    assert setup['assignment'].appeal_remaining_seconds == 40
    assert db.scalar(select(func.count(AssignmentEvent.id)).where(
        AssignmentEvent.assignment_id == setup['assignment'].id,
        AssignmentEvent.event_type == 'V12_RETURN_REJECTED')) == 1
    assert db.scalar(select(func.count(PointsLedger.id)).where(
        PointsLedger.business_type == 'V12_SUPPLIER_REWARD')) == 0
    viewer = _principal(setup['telesales'], 'verification.task.read', 'lead.phone.read')
    previous = service.return_verification_task_to_dict(db, old_task, viewer, include_verification_info=True)
    assert previous['return_request']['description'] == old_description
    assert previous['return_request']['status'] == 'REJECTED'
    assert evidence.id not in {row['id'] for row in previous['return_request']['available_evidences']}
    from apps.api.src.routers.v12_returns import _can_read_return
    assert not _can_read_return(db, viewer, request)
    assert not _can_read_return(db, viewer, request, evidence_id=evidence.id)


def test_paused_and_resumed_reporting_matches_settlement(db, monkeypatch):
    setup, request, _, submitted = _submit_near_deadline(db, monkeypatch)
    assignment = setup['assignment']
    expressions = confirmation_sql_expressions(dialect_name=db.bind.dialect.name)
    def reported():
        return db.execute(select(expressions.effective_at, expressions.policy)
            .where(Assignment.id == assignment.id)).one()
    assert reported() == (None, None)
    assert resolve_effective_confirmation(db, assignment, as_of=submitted + timedelta(days=4)).effective_at is None
    task = db.get(VerificationTask, request.verification_task_id)
    task.status, task.verification_conclusion = 'SUBMITTED', 'DOES_NOT_SUPPORT_RETURN'
    request.status = 'REVIEWING'
    db.commit()
    reviewed = submitted + timedelta(days=1)
    monkeypatch.setattr(service, '_now', lambda: reviewed)
    service._final_review_return(db, return_id=request.id,
        principal=_principal(setup['reviewer'], 'return.review'), decision='REJECT', note='核验后暂不支持退回')
    db.commit()
    effective_at, policy = reported()
    expected = reviewed + timedelta(minutes=1)
    assert as_utc(effective_at) == expected
    assert policy == 'CLAIM_48H'
    assert resolve_effective_confirmation(db, assignment, as_of=expected - timedelta(microseconds=1)).effective_at is None
    assert resolve_effective_confirmation(db, assignment, as_of=expected).effective_at == expected


def test_return_clock_on_postgresql(postgres_factory, monkeypatch):
    with postgres_factory() as db:
        db.autoflush = False
        test_formal_return_pauses_and_rejection_resumes_only_remaining_minute(db, monkeypatch)


def test_reporting_clock_on_postgresql(postgres_factory, monkeypatch):
    with postgres_factory() as db:
        db.autoflush = False
        test_paused_and_resumed_reporting_matches_settlement(db, monkeypatch)


def test_manual_confirmation_during_resumed_window_settles_immediately(db, monkeypatch):
    setup, request, _, submitted = _submit_near_deadline(db, monkeypatch)
    task = db.get(VerificationTask, request.verification_task_id)
    task.status, task.verification_conclusion = 'SUBMITTED', 'DOES_NOT_SUPPORT_RETURN'
    request.status = 'REVIEWING'
    db.commit()
    reviewed = submitted + timedelta(days=1)
    monkeypatch.setattr(service, '_now', lambda: reviewed)
    service._final_review_return(db, return_id=request.id,
        principal=_principal(setup['reviewer'], 'return.review'), decision='REJECT', note='核验后恢复跟进')
    confirmed = reviewed + timedelta(seconds=10)
    db.add(FollowUp(assignment_id=setup['assignment'].id, company_id=setup['receiver'].id,
        status='DEAL', created_by=setup['receiver_user'].id, created_at=confirmed))
    db.flush()
    assert resolve_effective_confirmation(db, setup['assignment'], as_of=confirmed).policy == 'MANUAL_CONFIRMED'
    expr = confirmation_sql_expressions(dialect_name=db.bind.dialect.name)
    assert db.scalar(select(expr.policy).where(Assignment.id == setup['assignment'].id)) == 'MANUAL_CONFIRMED'
    assert run_due_supplier_reward_settlement(db, as_of=confirmed)['settled'] == 1
