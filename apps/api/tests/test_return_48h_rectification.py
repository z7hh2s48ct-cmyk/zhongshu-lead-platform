from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from starlette.requests import Request

from apps.api.src.core.enums import AssignmentStatus, EvidenceType
from apps.api.src.core.errors import AppError
from apps.api.src.core.models import VerificationTask
from apps.api.src.core.time import as_utc
from apps.api.src.core.v12_enums import LeadV12Status, ReturnV12Status
from apps.api.src.routers import v12_returns as router
from apps.api.src.schemas.v12_returns import ReturnDraftV12Body, ReturnFinalReviewBody, ReturnVerificationSubmitBody
from apps.api.src.services import return_v12 as service
from apps.api.tests.test_v12_return_workflow import _evidence, _principal, _submit_and_verify, _workflow_setup


@pytest.mark.parametrize("hours,allowed", [(47.999, True), (48, False), (49, False)])
def test_initial_return_uses_continuous_48_hours_even_with_old_deadline(db, monkeypatch, hours, allowed):
    setup = _workflow_setup(db)
    claimed_at = datetime(2026, 9, 11, 2, tzinfo=timezone.utc)  # Friday 10:00 in China.
    now = claimed_at + timedelta(hours=hours)
    assignment = setup["assignment"]
    assignment.claimed_at = claimed_at
    assignment.appeal_deadline_at = claimed_at + timedelta(days=5)
    reward_due = setup["reward"].reward_due_at
    db.commit()
    monkeypatch.setattr(service, "_now", lambda: now)
    owner = _principal(setup["receiver_user"], "return.own.manage")
    kwargs = dict(assignment_id=assignment.id, principal=owner, reason_code="EMPTY_NUMBER", description="联系后确认客户为空号")
    if allowed:
        draft = service.create_or_update_return_draft(db, **kwargs)
        assert as_utc(draft.appeal_deadline_at) == claimed_at + timedelta(hours=48)
    else:
        with pytest.raises(AppError, match="48") as error:
            service.create_or_update_return_draft(db, **kwargs)
        assert error.value.code == "RETURN_WINDOW_EXPIRED"
    assert setup["reward"].reward_due_at == reward_due


def test_expired_existing_draft_cannot_save_upload_or_first_submit(db, monkeypatch):
    setup = _workflow_setup(db)
    owner = _principal(setup["receiver_user"], "return.own.manage")
    kwargs = dict(assignment_id=setup["assignment"].id, principal=owner, reason_code="EMPTY_NUMBER", description="联系后确认客户为空号")
    draft = service.create_or_update_return_draft(db, **kwargs)
    _evidence(db, draft, owner, EvidenceType.CHAT_SCREENSHOT.value)
    db.commit()
    deadline = as_utc(setup["assignment"].claimed_at) + timedelta(hours=48)
    monkeypatch.setattr(service, "_now", lambda: deadline)
    for operation in (
        lambda: service.create_or_update_return_draft(db, **kwargs),
        lambda: service.prepare_return_evidence_upload(db, request=draft, principal=owner),
    ):
        with pytest.raises(AppError) as error:
            operation()
        assert error.value.code == "RETURN_WINDOW_EXPIRED"
    result = service.submit_return_request(db, return_id=draft.id, principal=owner)
    assert result.expired and result.request.status == ReturnV12Status.EXPIRED.value
    assert db.scalar(select(func.count(VerificationTask.id))) == 0


@pytest.mark.parametrize("decision,conclusion", [("REJECT", "DOES_NOT_SUPPORT_RETURN"), ("APPROVE", "SUPPORT_RETURN")])
def test_completed_lead_can_return_and_rejection_preserves_completion(db, decision, conclusion):
    setup = _workflow_setup(db)
    setup["assignment"].status = AssignmentStatus.COMPLETED.value
    setup["lead"].status = LeadV12Status.CLOSED.value
    setup["lead"].current_follow_status = "DEAL"
    db.commit()
    request, _ = _submit_and_verify(db, setup, conclusion=conclusion)
    reviewer = _principal(setup["reviewer"], "return.review")
    result = service.final_review_return(db, return_id=request.id, principal=reviewer, decision=decision, note="依据核验事实作出最终决定")
    assert setup["lead"].status == LeadV12Status.CLOSED.value
    if decision == "REJECT":
        assert setup["assignment"].status == AssignmentStatus.COMPLETED.value
        assert setup["lead"].current_follow_status == "DEAL"
        assert result.refund_ledger is None
    else:
        assert setup["assignment"].status == AssignmentStatus.RETURNED.value
        assert result.refund_ledger.delta == 100


def test_supplement_requires_new_content_and_preserves_on_time_submission(db, monkeypatch):
    setup = _workflow_setup(db)
    request, _ = _submit_and_verify(db, setup, conclusion="INCONCLUSIVE")
    original_deadline = as_utc(request.appeal_deadline_at)
    reviewer = _principal(setup["reviewer"], "return.review")
    service.final_review_return(db, return_id=request.id, principal=reviewer, decision="NEED_MORE", note="请补充新的通话证据")
    owner = _principal(setup["receiver_user"], "return.own.manage")
    db.commit()
    monkeypatch.setattr(service, "_now", lambda: original_deadline + timedelta(days=5))
    for duplicate in (False, True):
        if duplicate:
            _evidence(db, request, owner, EvidenceType.CALL_RECORDING.value)
        with pytest.raises(AppError) as error:
            service.submit_return_request(db, return_id=request.id, principal=owner)
        assert error.value.code == "RETURN_SUPPLEMENT_REQUIRED"
        assert service.return_request_to_dict(db, request, include_evidence=True)["supplementary_evidence_count"] == 0
    new_evidence = _evidence(db, request, owner, EvidenceType.CALL_RECORDING.value)
    new_evidence.sha256 = "b" * 64
    db.flush()
    assert service.return_request_to_dict(db, request, include_evidence=True)["supplementary_evidence_count"] == 1
    result = service.submit_return_request(db, return_id=request.id, principal=owner)
    assert not result.expired
    assert result.request.status == ReturnV12Status.VERIFYING.value
    assert as_utc(result.request.appeal_deadline_at) == original_deadline


@pytest.mark.parametrize("value", ["  \t\n ", " 一 "])
@pytest.mark.parametrize("model,kwargs,field", [
    (ReturnDraftV12Body, {"reason_code": "EMPTY_NUMBER"}, "description"),
    (ReturnFinalReviewBody, {"decision": "REJECT"}, "note"),
    (ReturnVerificationSubmitBody, {"contact_result": "CONNECTED", "conclusion": "INCONCLUSIVE"}, "note"),
])
def test_return_text_requires_nonblank_content(model, kwargs, field, value):
    with pytest.raises(ValidationError):
        model(**kwargs, **{field: value})


@pytest.mark.parametrize("name", ['客户沟通截图.png', '沟通 "材料".png', 'proof\r\nX-Test: bad.png', 'evidence 😀.png'])
def test_evidence_download_encodes_original_filename_without_breaking_auth(db, monkeypatch, name):
    setup = _workflow_setup(db)
    owner = _principal(setup["receiver_user"], "return.own.manage")
    draft = service.create_or_update_return_draft(db, assignment_id=setup["assignment"].id, principal=owner, reason_code="EMPTY_NUMBER", description="客户为空号提交证据")
    evidence = _evidence(db, draft, owner, EvidenceType.CHAT_SCREENSHOT.value)
    evidence.original_name = name
    db.commit()
    monkeypatch.setattr(router, "decode_file_access_token", lambda _: {"sub": evidence.id, "uid": owner.user_id})
    monkeypatch.setattr(router, "get_storage", lambda: SimpleNamespace(read=lambda _: b"image-content"))
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    request.state.request_id = "filename-test"
    response = router.download_return_evidence_v12(evidence.id, "test-token", request, owner, db)
    header = response.headers["content-disposition"]
    assert response.body == b"image-content"
    assert "filename*=UTF-8''" in header
    assert "\r" not in unquote(header) and "\n" not in unquote(header)
    assert all(ord(char) < 128 for char in header)
    monkeypatch.setattr(router, "decode_file_access_token", lambda _: {"sub": evidence.id, "uid": "another-user"})
    with pytest.raises(AppError) as error:
        router.download_return_evidence_v12(evidence.id, "test-token", request, owner, db)
    assert error.value.code == "FILE_TOKEN_MISMATCH"
