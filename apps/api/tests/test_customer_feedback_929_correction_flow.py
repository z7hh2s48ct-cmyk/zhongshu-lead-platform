from datetime import datetime, timezone

from sqlalchemy import func, select

from apps.api.src.core.models import Lead, User, VerificationTask
from apps.api.src.core.security import encrypt_text, fingerprint_phone, hash_phone
from apps.api.src.core.v12_enums import LeadSourceKind, LeadV12Status
from apps.api.src.services.lead_supply_v12 import correct_platform_lead
from apps.api.tests.test_v12_lead_supply import _principal


def test_correction_after_completed_telesales_keeps_operation_disposition(db) -> None:
    operator = User(display_name="运营", status="ACTIVE")
    db.add(operator)
    db.flush()
    phone = "13800139293"
    lead = Lead(
        source_type=LeadSourceKind.PLATFORM_MANUAL.value,
        source_kind=LeadSourceKind.PLATFORM_MANUAL.value,
        submitter_user_id=operator.id,
        customer_name="待更正客户",
        phone_encrypted=encrypt_text(phone),
        phone_hash=hash_phone(phone),
        phone_fingerprint=fingerprint_phone(phone),
        city="武汉市",
        district="武昌区",
        region_code="420106",
        consent_confirmed=True,
        status=LeadV12Status.PENDING_OPERATION_DISPOSITION.value,
        review_status="PENDING",
        duplicate_status="CLEAR",
        raw_payload={},
    )
    db.add(lead)
    db.flush()
    task = VerificationTask(
        lead_id=lead.id,
        task_type="PRE_DISPATCH_VERIFY",
        status="SUBMITTED",
        assignee_user_id=operator.id,
        verification_conclusion="QUALIFIED",
        submitted_at=datetime.now(timezone.utc),
    )
    db.add(task)
    db.flush()

    correct_platform_lead(
        db,
        lead_id=lead.id,
        principal=_principal(operator.id, None, "lead.manual.manage"),
        values={"region_code": "110000", "need_summary": "电销确认实际位于北京"},
        reason="依据已完成的电销核验更正地区",
        expected_snapshot_version=lead.snapshot_version,
    )

    assert lead.status == LeadV12Status.PENDING_OPERATION_DISPOSITION.value
    assert task.status == "SUBMITTED"
    assert db.scalar(
        select(func.count(VerificationTask.id)).where(VerificationTask.lead_id == lead.id)
    ) == 1
