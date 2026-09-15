from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from starlette.requests import Request

from apps.api.src.core.enums import EvidenceType
from apps.api.src.core.models import AssignmentEvent, ReturnRequest, VerificationTask
from apps.api.src.core.time import as_utc
from apps.api.src.core.v12_enums import ReturnV12Status
from apps.api.src.routers.v12_returns import list_returns_v12
from apps.api.src.services import return_v12 as service
from apps.api.tests.test_v12_return_workflow import _evidence, _principal, _workflow_setup


def _request() -> Request:
    request = Request({"type": "http", "method": "GET", "path": "/v1.2/returns", "headers": []})
    request.state.request_id = "feedback-912"
    return request


def _draft(db, setup) -> ReturnRequest:
    owner = _principal(setup["receiver_user"], "return.own.manage")
    return service.create_or_update_return_draft(
        db,
        assignment_id=setup["assignment"].id,
        principal=owner,
        reason_code="EMPTY_NUMBER",
        description="联系后确认客户为空号",
    )


def _submit(db, setup, draft: ReturnRequest) -> ReturnRequest:
    owner = _principal(setup["receiver_user"], "return.own.manage")
    _evidence(db, draft, owner, EvidenceType.CHAT_SCREENSHOT.value)
    service.submit_return_request(db, return_id=draft.id, principal=owner)
    return draft


def _ids(response: dict) -> set[str]:
    return {item["id"] for item in response["data"]["items"]}


def _list(db, principal, *, status: str | None = None) -> dict:
    return list_returns_v12(
        _request(),
        principal,
        db,
        status=status,
        company_id=None,
        page_no=1,
        page_size=20,
    )


def test_admin_default_queue_only_contains_formally_submitted_returns(db) -> None:
    draft_setup = _workflow_setup(db, suffix="-QUEUE-DRAFT")
    submitted_setup = _workflow_setup(db, suffix="-QUEUE-SUBMITTED")
    expired_setup = _workflow_setup(db, suffix="-QUEUE-EXPIRED")
    draft = _draft(db, draft_setup)
    submitted = _submit(db, submitted_setup, _draft(db, submitted_setup))
    expired = _draft(db, expired_setup)
    expired.status = ReturnV12Status.EXPIRED.value
    db.commit()

    admin = _principal(submitted_setup["operator"], "return.read")
    default_result = _list(db, admin)
    assert _ids(default_result) == {submitted.id}

    draft_result = _list(db, admin, status="DRAFT")
    assert _ids(draft_result) == {draft.id}


def test_franchise_default_list_keeps_own_draft_and_company_scope(db) -> None:
    own_setup = _workflow_setup(db, suffix="-OWN-DRAFT")
    other_setup = _workflow_setup(db, suffix="-OTHER-DRAFT")
    own_draft = _draft(db, own_setup)
    other_draft = _draft(db, other_setup)
    db.commit()

    owner = _principal(own_setup["receiver_user"], "return.own.manage")
    result = _list(db, owner)
    assert own_draft.id in _ids(result)
    assert other_draft.id not in _ids(result)


def test_expire_unsubmitted_return_drafts_is_idempotent_and_audited(db, monkeypatch) -> None:
    now = datetime(2026, 9, 12, 9, tzinfo=timezone.utc)
    expired_setup = _workflow_setup(db, suffix="-JOB-EXPIRED")
    active_setup = _workflow_setup(db, suffix="-JOB-ACTIVE")
    submitted_setup = _workflow_setup(db, suffix="-JOB-SUBMITTED")
    supplement_setup = _workflow_setup(db, suffix="-JOB-SUPPLEMENT")

    expired = _draft(db, expired_setup)
    active = _draft(db, active_setup)
    submitted = _submit(db, submitted_setup, _draft(db, submitted_setup))
    supplement = _submit(db, supplement_setup, _draft(db, supplement_setup))
    supplement.status = ReturnV12Status.NEED_MORE_EVIDENCE.value

    expired_setup["assignment"].claimed_at = now - timedelta(hours=49)
    active_setup["assignment"].claimed_at = now - timedelta(hours=47)
    submitted_setup["assignment"].claimed_at = now - timedelta(days=5)
    supplement_setup["assignment"].claimed_at = now - timedelta(days=5)
    db.commit()
    monkeypatch.setattr(service, "_now", lambda: now)

    first = service.expire_unsubmitted_return_drafts(db, batch_size=100)
    db.commit()
    second = service.expire_unsubmitted_return_drafts(db, batch_size=100)
    db.commit()

    assert first == {"scanned": 1, "expired": 1}
    assert second == {"scanned": 0, "expired": 0}
    assert db.get(ReturnRequest, expired.id).status == ReturnV12Status.EXPIRED.value
    assert as_utc(db.get(ReturnRequest, expired.id).appeal_deadline_at) == now - timedelta(hours=1)
    assert db.get(ReturnRequest, active.id).status == ReturnV12Status.DRAFT.value
    assert db.get(ReturnRequest, submitted.id).status == ReturnV12Status.VERIFYING.value
    assert db.get(ReturnRequest, supplement.id).status == ReturnV12Status.NEED_MORE_EVIDENCE.value
    assert db.scalar(select(func.count(VerificationTask.id)).where(VerificationTask.return_request_id == expired.id)) == 0
    events = db.scalars(
        select(AssignmentEvent).where(
            AssignmentEvent.assignment_id == expired_setup["assignment"].id,
            AssignmentEvent.event_type == "V12_RETURN_DRAFT_EXPIRED",
        )
    ).all()
    assert len(events) == 1
    assert events[0].payload["return_request_id"] == expired.id
    assert events[0].payload["reason"] == "UNSUBMITTED_48H_WINDOW_EXPIRED"


def test_expire_return_drafts_skips_deleted_lead_and_continues_batch(db, monkeypatch) -> None:
    now = datetime(2026, 9, 15, 9, tzinfo=timezone.utc)
    deleted_setup = _workflow_setup(db, suffix="-JOB-DELETED")
    active_setup = _workflow_setup(db, suffix="-JOB-CONTINUE")
    deleted = _draft(db, deleted_setup)
    active = _draft(db, active_setup)
    deleted_setup["assignment"].claimed_at = now - timedelta(hours=50)
    active_setup["assignment"].claimed_at = now - timedelta(hours=49)
    deleted_setup["lead"].deleted_at = now - timedelta(minutes=1)
    db.commit()
    monkeypatch.setattr(service, "_now", lambda: now)

    result = service.expire_unsubmitted_return_drafts(db, batch_size=100)

    assert result == {"scanned": 1, "expired": 1}
    assert db.get(ReturnRequest, deleted.id).status == ReturnV12Status.DRAFT.value
    assert db.get(ReturnRequest, active.id).status == ReturnV12Status.EXPIRED.value
