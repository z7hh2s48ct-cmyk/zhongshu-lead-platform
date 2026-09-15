from __future__ import annotations

from datetime import datetime, timedelta, timezone

from apps.api.src.core.models import Assignment, Company, Lead, PointsAccount, PointsLedger
from apps.api.src.core.models_v12 import SupplierLeadReward
from apps.api.src.core.security import encrypt_text, hash_phone
from apps.api.src.services.reconciliation_v12 import reconcile_v12


def test_reconciliation_rejects_corrupted_intermediate_balance_snapshot(db) -> None:
    company = Company(code="SEQ-GATE", name="流水序列测试公司", status="ACTIVE")
    db.add(company)
    db.flush()
    account = PointsAccount(company_id=company.id, balance=30)
    db.add(account)
    db.flush()
    now = datetime.now(timezone.utc)
    db.add_all(
        [
            PointsLedger(
                account_id=account.id,
                company_id=company.id,
                ledger_type="RECHARGE",
                delta=10,
                balance_after=999,
                business_type="TEST",
                business_id="sequence-1",
                idempotency_key="sequence-1",
                metadata_json={},
                created_at=now,
            ),
            PointsLedger(
                account_id=account.id,
                company_id=company.id,
                ledger_type="RECHARGE",
                delta=20,
                balance_after=30,
                business_type="TEST",
                business_id="sequence-2",
                idempotency_key="sequence-2",
                metadata_json={},
                created_at=now + timedelta(seconds=1),
            ),
        ]
    )
    db.flush()

    report = reconcile_v12(db, require_completed_backfill=False)
    codes = {item["code"] for item in report.errors}
    assert "POINTS_RECONCILIATION_MISMATCH" in codes
    assert report.metrics["points_ledger_sequence_errors"] == 1
    assert report.valid is False


def test_reconciliation_accepts_independent_customer_and_supply_wallets(db) -> None:
    company = Company(code="DUAL-WALLET", name="双积分对账公司", status="ACTIVE")
    db.add(company)
    db.flush()
    account = PointsAccount(company_id=company.id, balance=0, supply_balance=30)
    db.add(account)
    db.flush()
    db.add(
        PointsLedger(
            account_id=account.id,
            company_id=company.id,
            ledger_type="ADJUST",
            point_kind="SUPPLY",
            delta=30,
            balance_after=30,
            business_type="MANUAL_ADJUSTMENT",
            business_id="dual-wallet-supply",
            idempotency_key="dual-wallet-supply",
            metadata_json={},
        )
    )
    db.flush()

    report = reconcile_v12(db, require_completed_backfill=False)

    assert "POINTS_RECONCILIATION_MISMATCH" not in {item["code"] for item in report.errors}
    assert report.metrics["points_account_mismatches"] == 0
    assert report.metrics["points_ledger_sequence_errors"] == 0


def test_reconciliation_rejects_ledger_posted_to_another_company_account(db) -> None:
    account_company = Company(code="ACC-OWNER", name="账户归属公司", status="ACTIVE")
    ledger_company = Company(code="LEDGER-OWNER", name="流水归属公司", status="ACTIVE")
    db.add_all([account_company, ledger_company])
    db.flush()
    account = PointsAccount(company_id=account_company.id, balance=20)
    db.add(account)
    db.flush()
    db.add(
        PointsLedger(
            account_id=account.id,
            company_id=ledger_company.id,
            ledger_type="RECHARGE",
            delta=20,
            balance_after=20,
            business_type="TEST",
            business_id="wrong-account-owner",
            idempotency_key="wrong-account-owner",
            metadata_json={},
        )
    )
    db.flush()

    report = reconcile_v12(db, require_completed_backfill=False)
    codes = {item["code"] for item in report.errors}
    assert "POINTS_LEDGER_ACCOUNT_MISMATCH" in codes
    assert report.metrics["points_ledger_account_mismatches"] == 1
    assert report.valid is False


def test_reconciliation_rejects_unknown_supplier_reward_status(db) -> None:
    supplier = Company(code="REWARD-SUP", name="供应公司", status="ACTIVE")
    receiver = Company(code="REWARD-REC", name="接收公司", status="ACTIVE")
    lead = Lead(
        id="unknown-reward-status-lead",
        customer_name="奖励状态测试客户",
        phone_encrypted=encrypt_text("13800138009"),
        phone_hash=hash_phone("13800138009"),
        status="QUALIFIED",
        raw_payload={},
    )
    db.add_all([supplier, receiver, lead])
    db.flush()
    assignment = Assignment(
        id="unknown-reward-status-assignment",
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
    db.add(
        SupplierLeadReward(
            id="unknown-reward-status",
            lead_id=lead.id,
            assignment_id=assignment.id,
            supplier_company_id=supplier.id,
            receiver_company_id=receiver.id,
            status="SETTLEDD",
            claim_points=100,
            reward_ratio_bps=3000,
            reward_points=30,
            rule_version=1,
        )
    )
    db.flush()

    report = reconcile_v12(db, require_completed_backfill=False)
    codes = {item["code"] for item in report.errors}
    assert "UNKNOWN_SUPPLIER_REWARD_STATUS" in codes
    assert report.metrics["unknown_supplier_reward_statuses"] == ["SETTLEDD"]
    assert report.valid is False
