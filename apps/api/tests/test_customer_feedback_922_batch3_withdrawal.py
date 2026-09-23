"""2026-09-22/23 客户反馈第三批之一（D2/D3，对应反馈第 3 条）：

- D2：最低提现额度与手续费率由超级管理员自行设定（SystemConfig 版本化发布），
  默认最低 100 积分、手续费率 0%；手续费在审核时快照，实际应付 = 现金 - 手续费；
- D3：审核/付款等管理操作收紧为仅超级管理员。
"""

from __future__ import annotations

import pytest

from apps.api.src.core.errors import AppError
from apps.api.src.services.supply_withdrawal import (
    confirm_offline_payment,
    create_withdrawal,
    get_withdrawal_policy,
    publish_withdrawal_policy,
    record_offline_payment,
    review_withdrawal,
)
from apps.api.src.services.rbac import assign_role
from apps.api.tests.test_customer_feedback_919_batch4 import (
    _apply,
    _seed_rate,
    _setup_wallet,
)
from apps.api.tests.test_v12_return_workflow import _principal, _workflow_setup
from apps.api.src.routers.supply_withdrawal import _can_manage_withdrawals


def test_default_policy_is_min_100_and_zero_fee(db) -> None:
    policy = get_withdrawal_policy(db)
    assert policy == {"min_withdrawal_points": 100, "fee_rate_bp": 0}


def test_create_below_default_minimum_rejected(db) -> None:
    setup, _ = _setup_wallet(db)
    with pytest.raises(AppError) as error:
        _apply(db, setup, points=50)
    assert error.value.code == "WITHDRAWAL_BELOW_MINIMUM"


def test_published_policy_relaxes_minimum(db) -> None:
    setup, _ = _setup_wallet(db)
    operator = setup["operator"]
    principal = _principal(operator, "*")
    value = publish_withdrawal_policy(
        db, min_withdrawal_points=10, fee_rate_bp=0, principal=principal
    )
    db.commit()
    assert value["min_withdrawal_points"] == 10
    item = _apply(db, setup, points=50)
    assert item.status == "PENDING_REVIEW"


def test_review_snapshots_fee_and_payment_uses_payable(db) -> None:
    setup, _ = _setup_wallet(db)
    _seed_rate(db, cents_per_point=2)
    principal = _principal(setup["operator"], "*")
    # 10% 手续费：300 积分 × 2 分/积分 = 600 分现金，手续费 60，应付 540。
    publish_withdrawal_policy(
        db, min_withdrawal_points=100, fee_rate_bp=1000, principal=principal
    )
    item = _apply(db, setup, points=300)
    reviewed = review_withdrawal(
        db,
        withdrawal_id=item.id,
        reviewed_by=setup["reviewer"].id,
        decision="APPROVE",
        review_note="核对额度、比例与手续费",
    )
    db.commit()

    assert reviewed.cash_amount_cents_snapshot == 600
    assert reviewed.fee_rate_bp_snapshot == 1000
    assert reviewed.fee_cents_snapshot == 60

    with pytest.raises(AppError) as error:
        record_offline_payment(
            db,
            withdrawal_id=item.id,
            recorded_by=setup["reviewer"].id,
            external_reference="PAY-001",
            amount_cents=600,
        )
    assert error.value.code == "WITHDRAWAL_PAYMENT_AMOUNT_MISMATCH"

    record_offline_payment(
        db,
        withdrawal_id=item.id,
        recorded_by=setup["reviewer"].id,
        external_reference="PAY-001",
        amount_cents=540,
    )
    confirm_offline_payment(
        db,
        withdrawal_id=item.id,
        confirmed_by=setup["reviewer"].id,
        idempotency_key="withdrawal:PAY-001",
    )
    assert item.status == "PAID"


def test_manage_permissions_tightened_to_super_admin(db) -> None:
    setup, _ = _setup_wallet(db)
    readonly = _principal(setup["operator"], "reward.read")
    admin = _principal(setup["reviewer"], "*")
    assert _can_manage_withdrawals(readonly) is False
    assert _can_manage_withdrawals(admin) is True


def test_publish_policy_rejects_invalid_values(db) -> None:
    principal = _principal(_workflow_setup(db, suffix="FB923W")["operator"], "*")
    with pytest.raises(AppError):
        publish_withdrawal_policy(
            db, min_withdrawal_points=0, fee_rate_bp=0, principal=principal
        )
    with pytest.raises(AppError):
        publish_withdrawal_policy(
            db, min_withdrawal_points=100, fee_rate_bp=20000, principal=principal
        )
