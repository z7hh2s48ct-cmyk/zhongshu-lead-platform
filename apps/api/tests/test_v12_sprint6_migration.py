from __future__ import annotations

import pytest

from apps.api.src.core.models import Assignment, Company, Lead, PointsAccount, PointsLedger
from apps.api.src.core.models_v12 import SupplierLeadReward, V12MigrationCheckpoint
from apps.api.src.core.security import encrypt_text, hash_phone
from apps.api.src.services.phone_uniqueness import hash_customer_wechat
from apps.api.src.core.state_machine_v12 import (
    UnknownLegacyStatus,
    map_legacy_lead_status,
    try_map_legacy_lead_status,
)
from apps.api.src.core.v12_enums import LeadV12Status, RewardStatus
from apps.api.src.services.migration_v12 import (
    PHONE_FINGERPRINT_CHECKPOINT,
    backfill_phone_fingerprints_batch,
    preview_phone_fingerprint_backfill,
)
from apps.api.src.services.reconciliation_v12 import reconcile_v12
from scripts.migrate_v12_data import _write_exit_code


def _lead(lead_id: str, phone: str, *, encrypted: str | None = None) -> Lead:
    return Lead(
        id=lead_id,
        customer_name=f"客户-{lead_id}",
        phone_encrypted=encrypted or encrypt_text(phone),
        phone_hash=hash_phone(phone),
        status="QUALIFIED",
        raw_payload={},
    )


def test_phone_fingerprint_backfill_is_bounded_resumable_and_idempotent(db) -> None:
    db.add_all([_lead("a-lead", "13800138000"), _lead("b-lead", "13900139000")])
    db.flush()

    first = backfill_phone_fingerprints_batch(db, batch_size=1, secret="F" * 40)
    db.commit()
    assert first.scanned == 1
    assert first.updated == 1
    assert first.complete is False

    second = backfill_phone_fingerprints_batch(db, batch_size=1, secret="F" * 40)
    db.commit()
    assert second.updated == 1
    assert second.complete is True
    assert second.checkpoint_status == "COMPLETED"
    assert all(item.phone_fingerprint for item in db.query(Lead).all())

    third = backfill_phone_fingerprints_batch(db, batch_size=100, secret="F" * 40)
    assert third.scanned == 0
    assert third.complete is True
    checkpoint = db.get(V12MigrationCheckpoint, PHONE_FINGERPRINT_CHECKPOINT)
    assert checkpoint is not None
    assert checkpoint.processed_count == 2
    assert checkpoint.error_count == 0


def test_wechat_only_lead_does_not_require_phone_fingerprint(db) -> None:
    db.add_all([
        Lead(
            id="wechat-only-backfill",
            customer_name="仅微信客户",
            customer_wechat_encrypted=encrypt_text("wx-backfill-929"),
            customer_wechat_hash=hash_customer_wechat("wx-backfill-929"),
            status="PUBLIC_POOL",
            raw_payload={},
        ),
        Lead(
            id="wechat-legacy-empty-phone",
            customer_name="旧版空号码客户",
            phone_encrypted=encrypt_text(""),
            phone_hash=hash_phone(""),
            customer_wechat_encrypted=encrypt_text("wx-legacy-empty-929"),
            customer_wechat_hash=hash_customer_wechat("wx-legacy-empty-929"),
            status="PUBLIC_POOL",
            raw_payload={},
        ),
    ])
    db.flush()

    preview = preview_phone_fingerprint_backfill(db, secret="F" * 40)
    assert preview.scanned == 0
    result = backfill_phone_fingerprints_batch(db, secret="F" * 40)
    assert result.scanned == 0
    assert result.errors == 0
    report = reconcile_v12(db)
    assert report.metrics["leads_missing_phone_fingerprint"] == 0
    assert "PHONE_FINGERPRINT_INCOMPLETE" not in {item["code"] for item in report.errors}


def test_preview_is_read_only_and_row_errors_never_include_plaintext(db) -> None:
    db.add(_lead("bad-lead", "13800138000", encrypted="not-a-fernet-token"))
    db.flush()

    preview = preview_phone_fingerprint_backfill(db, secret="F" * 40)
    assert preview.scanned == 1
    assert preview.errors == 1
    assert preview.error_samples == ({"lead_id": "bad-lead", "reason": "PHONE_DECRYPT_FAILED"},)
    assert db.get(Lead, "bad-lead").phone_fingerprint is None
    assert db.get(V12MigrationCheckpoint, PHONE_FINGERPRINT_CHECKPOINT) is None

    result = backfill_phone_fingerprints_batch(db, secret="F" * 40)
    db.commit()
    assert result.complete is True
    assert result.checkpoint_status == "COMPLETED_WITH_ERRORS"
    checkpoint = db.get(V12MigrationCheckpoint, PHONE_FINGERPRINT_CHECKPOINT)
    assert checkpoint is not None
    assert "13800138000" not in str(checkpoint.metadata_json)


def test_write_exit_code_preserves_failed_checkpoint_on_clean_rerun() -> None:
    totals = {"batches": 1, "scanned": 0, "updated": 0, "errors": 0}
    failed_checkpoint = {"checkpoint_status": "COMPLETED_WITH_ERRORS"}
    clean_checkpoint = {"checkpoint_status": "COMPLETED"}

    assert _write_exit_code(
        fail_on_row_error=True,
        totals=totals,
        last=failed_checkpoint,
    ) == 2
    assert _write_exit_code(
        fail_on_row_error=True,
        totals=totals,
        last=clean_checkpoint,
    ) == 0
    assert _write_exit_code(
        fail_on_row_error=False,
        totals=totals,
        last=failed_checkpoint,
    ) == 0


def test_reconciliation_detects_incomplete_backfill_and_points_mismatch(db) -> None:
    lead = _lead("reconcile-lead", "13700137000")
    company = Company(code="RECON", name="对账测试公司", status="ACTIVE")
    db.add_all([lead, company])
    db.flush()
    account = PointsAccount(company_id=company.id, balance=50)
    db.add(account)
    db.flush()
    db.add(
        PointsLedger(
            account_id=account.id,
            company_id=company.id,
            ledger_type="RECHARGE",
            delta=40,
            balance_after=40,
            business_type="TEST",
            business_id="test",
            idempotency_key="test-ledger",
            metadata_json={},
        )
    )
    db.flush()

    report = reconcile_v12(db)
    codes = {item["code"] for item in report.errors}
    assert "PHONE_FINGERPRINT_INCOMPLETE" in codes
    assert "POINTS_RECONCILIATION_MISMATCH" in codes
    assert report.valid is False


def test_reconciliation_rejects_reward_ledger_with_wrong_business_semantics(db) -> None:
    lead = _lead("reward-ledger-lead", "13500135000")
    supplier = Company(code="SUP-SEM", name="供应公司", status="ACTIVE")
    receiver = Company(code="REC-SEM", name="接收公司", status="ACTIVE")
    db.add_all([lead, supplier, receiver])
    db.flush()
    backfill_phone_fingerprints_batch(db, secret="F" * 40)

    supplier_account = PointsAccount(company_id=supplier.id, balance=30)
    receiver_account = PointsAccount(company_id=receiver.id, balance=0)
    db.add_all([supplier_account, receiver_account])
    db.flush()
    assignment = Assignment(
        id="reward-semantic-assignment",
        lead_id=lead.id,
        company_id=receiver.id,
        status="CLAIMED",
        points_price=100,
        price_version=1,
        lead_snapshot={},
        assigned_by="test-reviewer",
    )
    db.add(assignment)
    db.flush()
    wrong_ledger = PointsLedger(
        account_id=supplier_account.id,
        company_id=supplier.id,
        ledger_type="REWARD",
        delta=30,
        balance_after=30,
        business_type="UNRELATED_BUSINESS",
        business_id="unrelated-id",
        idempotency_key="reward-semantic-wrong-ledger",
        metadata_json={},
    )
    db.add(wrong_ledger)
    db.flush()
    db.add(
        SupplierLeadReward(
            id="reward-semantic-reward",
            lead_id=lead.id,
            assignment_id=assignment.id,
            supplier_company_id=supplier.id,
            receiver_company_id=receiver.id,
            status=RewardStatus.SETTLED.value,
            claim_points=100,
            reward_ratio_bps=3000,
            reward_points=30,
            rule_version=1,
            ledger_id=wrong_ledger.id,
        )
    )
    db.flush()

    report = reconcile_v12(db)
    codes = {item["code"] for item in report.errors}
    assert "POINTS_RECONCILIATION_MISMATCH" not in codes
    assert "REWARD_LEDGER_SEMANTIC_MISMATCH" in codes
    assert report.metrics["reward_ledger_semantic_mismatches"] == 1
    assert report.valid is False


def test_reconciliation_passes_after_clean_backfill(db) -> None:
    db.add(_lead("clean-lead", "13600136000"))
    db.flush()
    result = backfill_phone_fingerprints_batch(db, secret="F" * 40)
    db.commit()
    assert result.complete is True

    report = reconcile_v12(db)
    assert report.valid is True
    assert report.metrics["leads_missing_phone_fingerprint"] == 0


def test_strict_legacy_mapping_refuses_unknown_terminal_fallback() -> None:
    assert try_map_legacy_lead_status(" qualified ") is LeadV12Status.READY_DISPATCH
    assert map_legacy_lead_status("unknown") is LeadV12Status.CLOSED
    with pytest.raises(UnknownLegacyStatus):
        map_legacy_lead_status("unknown", strict=True)
