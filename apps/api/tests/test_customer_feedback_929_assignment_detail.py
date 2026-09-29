from pathlib import Path
from datetime import datetime, timezone

from apps.api.src.core.enums import AssignmentStatus
from apps.api.src.core.models import Assignment
from apps.api.src.core.security import encrypt_text
from apps.api.src.services.dispatch_v12 import _receiver_duplicate_assignment
from apps.api.src.services.phone_uniqueness import hash_customer_wechat
from apps.api.src.routers.v12_dispatch import _assignment_dict, _assignment_detail_projection
from apps.api.tests.test_v12_dispatch_claim import _lead, dispatch_setup


def test_franchise_assignment_detail_shows_location_and_customer_need() -> None:
    source = Path("apps/h5/public/v12-workbench.js").read_text(encoding="utf-8")
    detail = source.split("async function assignmentDetail(", 1)[1].split(
        "function normalizedClaimPoints(", 1
    )[0]

    assert "['所在地'" in detail
    assert "x.city" in detail
    assert "x.district" in detail
    assert "['客户需求',x.need_summary]" in detail
    assert "['客户微信号',x.customer_wechat||" in detail


def test_assignment_contact_reveals_wechat_only_after_claim(db, dispatch_setup) -> None:
    _, supplier_user, receiver, _, lead = dispatch_setup
    lead.phone_encrypted = None
    lead.phone_hash = None
    lead.phone_fingerprint = None
    lead.customer_wechat_encrypted = encrypt_text("wx_929_customer")
    lead.customer_wechat_hash = hash_customer_wechat("wx_929_customer")
    assignment = Assignment(
        lead_id=lead.id,
        company_id=receiver.id,
        status=AssignmentStatus.PENDING_CLAIM.value,
        points_price=100,
        assigned_by=supplier_user.id,
        assigned_at=datetime.now(timezone.utc),
    )
    db.add(assignment)
    db.flush()

    before = _assignment_dict(assignment, lead)
    after = _assignment_dict(assignment, lead, reveal_phone=True)

    assert before["customer_wechat"] is None
    assert after["customer_wechat"] == "wx_929_customer"
    assert "leads.customer_wechat_encrypted" in str(
        _assignment_detail_projection(assignment.id, receiver.id)
    )


def test_wechat_only_receiver_history_does_not_match_unrelated_null_phones(
    db, dispatch_setup
) -> None:
    _, supplier_user, receiver, _, lead = dispatch_setup
    lead.phone_encrypted = None
    lead.phone_hash = None
    lead.phone_fingerprint = None
    lead.customer_wechat_hash = hash_customer_wechat("wx_one")
    other = _lead(
        db,
        phone="13800138199",
        submitter_id=supplier_user.id,
        supplier_company_id=None,
    )
    other.phone_encrypted = None
    other.phone_hash = None
    other.phone_fingerprint = None
    other.customer_wechat_hash = hash_customer_wechat("wx_two")
    db.add(
        Assignment(
            lead_id=other.id,
            company_id=receiver.id,
            status=AssignmentStatus.CLAIMED.value,
            points_price=100,
            assigned_by=supplier_user.id,
            assigned_at=datetime.now(timezone.utc),
            claimed_at=datetime.now(timezone.utc),
        )
    )
    db.flush()

    assert _receiver_duplicate_assignment(db, lead=lead, company_id=receiver.id) is None
