"""S7 存量回刷：把滞留在派发池/公海池的缺县客资转回电销补县。

背景：v1.2.6 上线 S7（县级硬门槛 + 缺县转电销）之前，初审合格的市级客资
直接进入待派发/公海池；上线后这些存量客资被派发硬门槛挡住，而状态机没有
从池内回到电销核验的通道。客户已确认口径"未派发转电销补县"，本脚本按该
口径一次性回刷。
"""

from __future__ import annotations

from datetime import datetime, timezone

from apps.api.src.core.models import AuditLog, Lead
from apps.api.src.core.security import encrypt_text, fingerprint_phone, hash_phone
from apps.api.src.core.v12_enums import LeadSourceKind, LeadV12Status
from scripts.backfill_county_missing_pool_leads import apply_backfill, collect_targets


_SEQ = 0


def _make_lead(
    db,
    *,
    status: str,
    region_code: str | None,
    source_kind: str = LeadSourceKind.SUPPLIER_H5.value,
    current_assignment_id: str | None = None,
    name: str | None = None,
) -> Lead:
    global _SEQ
    _SEQ += 1
    phone = f"1380000{_SEQ:04d}"
    lead = Lead(
        source_type=source_kind,
        source_kind=source_kind,
        customer_name=name or f"回刷测试{_SEQ}",
        phone_encrypted=encrypt_text(phone),
        phone_hash=hash_phone(phone),
        phone_fingerprint=fingerprint_phone(phone),
        consent_confirmed=True,
        city="杭州市",
        region_code=region_code,
        status=status,
        review_status="APPROVED",
        current_assignment_id=current_assignment_id,
        raw_payload={},
        imported_at=datetime.now(timezone.utc),
    )
    db.add(lead)
    db.flush()
    return lead


def test_collect_targets_only_picks_county_missing_unassigned_pool_leads(db):
    missing_city = _make_lead(db, status=LeadV12Status.READY_DISPATCH.value, region_code="330100")
    missing_city_public = _make_lead(db, status=LeadV12Status.PUBLIC_POOL.value, region_code="330100")
    with_district = _make_lead(db, status=LeadV12Status.READY_DISPATCH.value, region_code="330102")
    assigned = _make_lead(
        db,
        status=LeadV12Status.READY_DISPATCH.value,
        region_code="330100",
        current_assignment_id="assignment-1",
    )
    closed = _make_lead(db, status=LeadV12Status.CLOSED.value, region_code="330100")
    in_review = _make_lead(db, status=LeadV12Status.PENDING_REVIEW.value, region_code="330100")
    no_region = _make_lead(db, status=LeadV12Status.READY_DISPATCH.value, region_code=None)

    targets = collect_targets(db)
    target_ids = {lead.id for lead in targets}

    assert {missing_city.id, missing_city_public.id, no_region.id} == target_ids
    assert with_district.id not in target_ids
    assert assigned.id not in target_ids
    assert closed.id not in target_ids
    assert in_review.id not in target_ids


def test_collect_targets_does_not_mutate_leads(db):
    _make_lead(db, status=LeadV12Status.READY_DISPATCH.value, region_code="330100")

    collect_targets(db)

    lead = db.query(Lead).first()
    assert lead.status == LeadV12Status.READY_DISPATCH.value
    assert lead.pending_reason is None


def test_apply_backfill_flips_status_and_writes_reason(db):
    missing_city = _make_lead(db, status=LeadV12Status.READY_DISPATCH.value, region_code="330100")
    missing_city_public = _make_lead(db, status=LeadV12Status.PUBLIC_POOL.value, region_code="330100")
    with_district = _make_lead(db, status=LeadV12Status.READY_DISPATCH.value, region_code="330102")

    targets = collect_targets(db)
    summary = apply_backfill(db, targets)
    db.flush()

    assert summary["converted"] == 2
    for lead in (missing_city, missing_city_public):
        db.refresh(lead)
        assert lead.status == LeadV12Status.PENDING_TELESALES_VERIFY.value
        assert lead.pending_reason == "DISTRICT_PENDING_VERIFY"
    db.refresh(with_district)
    assert with_district.status == LeadV12Status.READY_DISPATCH.value
    assert with_district.pending_reason is None
    audits = db.query(AuditLog).filter(AuditLog.action == "SYSTEM_COUNTY_MISSING_POOL_BACKFILL").all()
    assert {audit.resource_id for audit in audits} == {missing_city.id, missing_city_public.id}


def test_apply_backfill_is_idempotent(db):
    _make_lead(db, status=LeadV12Status.READY_DISPATCH.value, region_code="330100")

    first = apply_backfill(db, collect_targets(db))
    db.flush()
    second_targets = collect_targets(db)

    assert first["converted"] == 1
    assert second_targets == []
