from apps.api.src.core.enums import EvidenceType
from apps.api.src.core.models import AssignmentEvent
from apps.api.src.services import return_v12 as service
from apps.api.tests.test_v12_return_workflow import (
    _evidence,
    _principal,
    _workflow_setup,
    _submit_and_verify,
)


def test_legacy_history_only_exposes_evidence_available_before_its_submission(db):
    from datetime import timedelta
    from sqlalchemy import select
    from apps.api.src.core.models import ReturnEvidence
    from apps.api.src.routers.v12_returns import _can_read_return

    setup = _workflow_setup(db, suffix="-LEGACY-EVIDENCE")
    request, first_task = _submit_and_verify(db, setup, conclusion="INCONCLUSIVE")
    old_evidences = db.scalars(select(ReturnEvidence).where(ReturnEvidence.return_request_id == request.id)).all()
    for old_evidence in old_evidences:
        old_evidence.created_at = first_task.submitted_at - timedelta(seconds=1)
    service.final_review_return(db, return_id=request.id,
        principal=_principal(setup["reviewer"], "return.review"), decision="NEED_MORE",
        note="请提供新的证明材料")
    owner = _principal(setup["receiver_user"], "return.own.manage")
    later = _evidence(db, request, owner, EvidenceType.CALL_RECORDING.value)
    later.sha256 = "c" * 64
    later.created_at = first_task.submitted_at + timedelta(seconds=1)
    db.flush()
    service.submit_return_request(db, return_id=request.id, principal=owner)
    # These records were created before independent round snapshots existed.
    for item in db.scalars(select(AssignmentEvent).where(
        AssignmentEvent.assignment_id == setup["assignment"].id
    )):
        item.payload = {key: value for key, value in item.payload.items() if key != "return_snapshot"}
    db.commit()
    viewer = _principal(setup["telesales"], "verification.task.read")
    previous = service.return_verification_task_to_dict(db, first_task, viewer, include_verification_info=True)
    details = previous["return_request"]
    assert {item["id"] for item in details["available_evidences"]} == {item.id for item in old_evidences}
    assert details["history_snapshot_unavailable"] is True
    assert details["status"] is None
    assert details["reason_code"] is None
    assert _can_read_return(db, viewer, request, evidence_id=old_evidence.id)
    assert not _can_read_return(db, viewer, request, evidence_id=later.id)
    first_task.submitted_at = None
    db.flush()
    assert not _can_read_return(db, viewer, request, evidence_id=old_evidence.id)


def test_reopened_return_serializer_counts_only_new_round_evidence(db) -> None:
    setup = _workflow_setup(db, suffix="-EVIDENCE-ROUND")
    owner = _principal(setup["receiver_user"], "return.own.manage")
    request = service.create_or_update_return_draft(
        db,
        assignment_id=setup["assignment"].id,
        principal=owner,
        reason_code="EMPTY_NUMBER",
        description="再次申请退回前需要补充新证据",
    )
    original = _evidence(db, request, owner, EvidenceType.CHAT_SCREENSHOT.value)
    db.add(
        AssignmentEvent(
            assignment_id=setup["assignment"].id,
            event_type="V12_RETURN_REOPENED",
            actor_user_id=owner.user_id,
            payload={
                "return_request_id": request.id,
                "evidence_sha256": [original.sha256],
            },
        )
    )
    db.flush()

    before = service.return_request_to_dict(db, request, include_evidence=True)
    assert before["requires_new_evidence"] is True
    assert before["current_round_evidence_count"] == 0

    added = _evidence(db, request, owner, EvidenceType.CALL_RECORDING.value)
    added.sha256 = "b" * 64
    db.flush()
    after = service.return_request_to_dict(db, request, include_evidence=True)
    assert after["requires_new_evidence"] is True
    assert after["current_round_evidence_count"] == 1
    assert after["evidences"][0]["uploaded_by"] == owner.user_id
    assert after["evidences"][0]["uploaded_by_name"] == "接收方负责人"
