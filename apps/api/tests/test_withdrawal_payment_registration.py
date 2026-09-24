"""问题二·增量1/增量2 回归：提现付款登记金额校验与内部登记编号设计。

对应 2026-09-24 资金修复方案验收：
- 应付 ¥100.00 时提交 10000 分，登记编号填写 ``100`` 不改变金额；金额不一致
  给出期望/提交分值明细，不靠改凭据号绕过。
- 内部登记编号（系统生成可编辑 / 手动填写、全局唯一、保留来源）与外部交易流水
  号拆分存储；待核销阶段可带原因更正并保留修改前后值与操作者；已核销只读。
"""
from __future__ import annotations

import pytest

from apps.api.src.core.errors import AppError
from apps.api.src.services.supply_withdrawal import (
    confirm_offline_payment,
    correct_payment_registration,
    publish_withdrawal_policy,
    record_offline_payment,
    review_withdrawal,
)
from apps.api.tests.test_customer_feedback_919_batch4 import (
    _apply,
    _seed_rate,
    _setup_wallet,
)
from apps.api.tests.test_v12_return_workflow import _principal


def _approved(db, setup, *, points: int = 300, fee_bp: int = 0):
    """申请并审核通过，进入 APPROVED_PENDING_PAYMENT，快照已固定。"""
    principal = _principal(setup["operator"], "*")
    publish_withdrawal_policy(
        db, min_withdrawal_points=100, fee_rate_bp=fee_bp, principal=principal
    )
    item = _apply(db, setup, points=points)
    review_withdrawal(
        db,
        withdrawal_id=item.id,
        reviewed_by=setup["reviewer"].id,
        decision="APPROVE",
        review_note="复核额度、比例与手续费",
    )
    db.commit()
    return item


def test_payable_10000_cents_and_registration_text_does_not_change_amount(db) -> None:
    setup, _ = _setup_wallet(db, supply_balance=5000)
    _seed_rate(db, cents_per_point=2)
    # 5000 积分 × 2 分 = 10000 分（¥100.00），0 手续费。
    item = _approved(db, setup, points=5000)

    # 金额不一致：给出期望与提交分值明细，且不落库为待核销。
    with pytest.raises(AppError) as mismatch:
        record_offline_payment(
            db,
            withdrawal_id=item.id,
            recorded_by=setup["reviewer"].id,
            amount_cents=0,
            registration_no="100",
        )
    assert mismatch.value.code == "WITHDRAWAL_PAYMENT_AMOUNT_MISMATCH"
    assert mismatch.value.details == {"expected_cents": 10000, "submitted_cents": 0}
    db.refresh(item)
    assert item.status == "APPROVED_PENDING_PAYMENT"

    # 登记编号填 100 只写编号字段，不影响应付金额。
    recorded = record_offline_payment(
        db,
        withdrawal_id=item.id,
        recorded_by=setup["reviewer"].id,
        amount_cents=10000,
        registration_no="100",
        registration_no_source="MANUAL",
    )
    assert recorded.payment_amount_cents == 10000
    assert recorded.payment_registration_no == "100"
    assert recorded.status == "PAID_PENDING_WRITE_OFF"


def test_record_payment_generates_system_registration_no(db) -> None:
    setup, _ = _setup_wallet(db)
    _seed_rate(db, cents_per_point=2)
    item = _approved(db, setup, points=300)

    recorded = record_offline_payment(
        db,
        withdrawal_id=item.id,
        recorded_by=setup["reviewer"].id,
        amount_cents=600,
    )
    assert recorded.payment_registration_no.startswith("WD-")
    assert recorded.payment_registration_no_source == "SYSTEM"
    # 渠道未提供外部流水时留空，不伪造。
    assert recorded.payment_external_reference is None
    entry = recorded.payment_history_json[-1]
    assert entry["event"] == "PAYMENT_RECORDED"
    assert entry["registration_no_source"] == "SYSTEM"


def test_manual_registration_no_and_external_reference_stored_separately(db) -> None:
    setup, _ = _setup_wallet(db)
    _seed_rate(db, cents_per_point=2)
    item = _approved(db, setup, points=300)

    recorded = record_offline_payment(
        db,
        withdrawal_id=item.id,
        recorded_by=setup["reviewer"].id,
        amount_cents=600,
        registration_no="REG-100",
        registration_no_source="MANUAL",
        external_reference="BANK-XYZ-001",
    )
    assert recorded.payment_registration_no == "REG-100"
    assert recorded.payment_registration_no_source == "MANUAL"
    assert recorded.payment_external_reference == "BANK-XYZ-001"


def test_edited_system_registration_no_keeps_system_source(db) -> None:
    setup, _ = _setup_wallet(db)
    _seed_rate(db, cents_per_point=2)
    item = _approved(db, setup, points=300)

    recorded = record_offline_payment(
        db,
        withdrawal_id=item.id,
        recorded_by=setup["reviewer"].id,
        amount_cents=600,
        registration_no="WD-EDITED-1",
        registration_no_source="SYSTEM",
    )
    assert recorded.payment_registration_no == "WD-EDITED-1"
    assert recorded.payment_registration_no_source == "SYSTEM"


def test_duplicate_registration_no_rejected(db) -> None:
    setup, _ = _setup_wallet(db, supply_balance=2000)
    _seed_rate(db, cents_per_point=2)
    first = _approved(db, setup, points=300)
    record_offline_payment(
        db,
        withdrawal_id=first.id,
        recorded_by=setup["reviewer"].id,
        amount_cents=600,
        registration_no="REG-DUP",
        registration_no_source="MANUAL",
    )
    second = _approved(db, setup, points=300)

    with pytest.raises(AppError) as error:
        record_offline_payment(
            db,
            withdrawal_id=second.id,
            recorded_by=setup["reviewer"].id,
            amount_cents=600,
            registration_no="REG-DUP",
            registration_no_source="MANUAL",
        )
    assert error.value.code == "WITHDRAWAL_REGISTRATION_NO_DUPLICATE"


def test_correct_registration_in_pending_writeoff_keeps_before_after(db) -> None:
    setup, _ = _setup_wallet(db)
    _seed_rate(db, cents_per_point=2)
    item = _approved(db, setup, points=300)
    record_offline_payment(
        db,
        withdrawal_id=item.id,
        recorded_by=setup["reviewer"].id,
        amount_cents=600,
    )
    original_no = item.payment_registration_no

    corrected = correct_payment_registration(
        db,
        withdrawal_id=item.id,
        corrected_by=setup["reviewer"].id,
        reason="登记编号填写错误，更正为银行回单号",
        registration_no="REG-FIX",
        registration_no_source="MANUAL",
        external_reference="BANK-REAL-9",
    )
    assert corrected.payment_registration_no == "REG-FIX"
    assert corrected.payment_registration_no_source == "MANUAL"
    assert corrected.payment_external_reference == "BANK-REAL-9"
    assert corrected.status == "PAID_PENDING_WRITE_OFF"
    entry = corrected.payment_history_json[-1]
    assert entry["event"] == "PAYMENT_REGISTRATION_CORRECTED"
    assert entry["reason"] == "登记编号填写错误，更正为银行回单号"
    assert entry["corrected_by"] == setup["reviewer"].id
    assert entry["before"]["registration_no"] == original_no
    assert entry["after"]["registration_no"] == "REG-FIX"


def test_correction_can_clear_wrong_external_reference_and_proof(db) -> None:
    setup, _ = _setup_wallet(db)
    _seed_rate(db, cents_per_point=2)
    item = _approved(db, setup, points=300)
    record_offline_payment(
        db,
        withdrawal_id=item.id,
        recorded_by=setup["reviewer"].id,
        amount_cents=600,
        external_reference="WRONG-REFERENCE",
        proof_url="https://example.com/wrong-proof",
    )

    corrected = correct_payment_registration(
        db,
        withdrawal_id=item.id,
        corrected_by=setup["reviewer"].id,
        reason="清除误填的流水和凭证",
        clear_external_reference=True,
        clear_proof_url=True,
    )

    assert corrected.payment_external_reference is None
    assert corrected.payment_proof_url is None
    assert corrected.payment_history_json[-1]["before"]["external_reference"] == "WRONG-REFERENCE"
    assert corrected.payment_history_json[-1]["after"]["external_reference"] is None


def test_correction_requires_reason(db) -> None:
    setup, _ = _setup_wallet(db)
    _seed_rate(db, cents_per_point=2)
    item = _approved(db, setup, points=300)
    record_offline_payment(
        db,
        withdrawal_id=item.id,
        recorded_by=setup["reviewer"].id,
        amount_cents=600,
    )

    with pytest.raises(AppError) as error:
        correct_payment_registration(
            db,
            withdrawal_id=item.id,
            corrected_by=setup["reviewer"].id,
            reason="   ",
            registration_no="REG-FIX",
        )
    assert error.value.code == "WITHDRAWAL_CORRECTION_REASON_REQUIRED"


def test_correction_blocked_after_writeoff(db) -> None:
    setup, _ = _setup_wallet(db)
    _seed_rate(db, cents_per_point=2)
    item = _approved(db, setup, points=300)
    record_offline_payment(
        db,
        withdrawal_id=item.id,
        recorded_by=setup["reviewer"].id,
        amount_cents=600,
        registration_no="REG-WRITEOFF",
        registration_no_source="MANUAL",
    )
    confirmed = confirm_offline_payment(
        db,
        withdrawal_id=item.id,
        confirmed_by=setup["reviewer"].id,
        idempotency_key="withdrawal:REG-WRITEOFF",
    )
    assert confirmed.status == "PAID"

    # 已核销记录只读，不可再更正编号或凭证。
    with pytest.raises(AppError) as error:
        correct_payment_registration(
            db,
            withdrawal_id=item.id,
            corrected_by=setup["reviewer"].id,
            reason="尝试修改已核销记录",
            registration_no="REG-TAMPER",
        )
    assert error.value.code == "WITHDRAWAL_CORRECTION_NOT_ALLOWED"
