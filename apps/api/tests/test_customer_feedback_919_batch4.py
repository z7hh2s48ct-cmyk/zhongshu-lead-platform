"""2026-09-19 第四批服务端验收：S1 供客积分日常提现 + S3 收款资料与变更申请。

最低提现额度与手续费待定，本期不实现、不默认零费用。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from apps.api.src.core.errors import AppError
from apps.api.src.core.models import PointsAccount, PointsLedger, SupplyPointsWithdrawal, SystemConfig, User
from apps.api.src.core.security import decrypt_text
from apps.api.src.services.rbac import assign_role
from apps.api.src.services.supply_withdrawal import (
    available_supply_points,
    cancel_withdrawal,
    confirm_offline_payment,
    create_withdrawal,
    record_offline_payment,
    request_payment_change,
    review_payment_change,
    review_withdrawal,
)
from apps.api.tests.test_v12_return_workflow import _principal, _workflow_setup


def _setup_wallet(db, *, supply_balance: int = 1000) -> dict:
    setup = _workflow_setup(db)
    account = db.scalar(
        select(PointsAccount).where(PointsAccount.company_id == setup["receiver"].id)
    )
    account.supply_balance = supply_balance
    account.frozen_supply_points = 0
    db.commit()
    return setup, account


def _seed_rate(db, *, cents_per_point: int = 2) -> SystemConfig:
    config = SystemConfig(
        domain="supply_termination",
        key="cashout_rate",
        value_json={"cash_cents_per_point": cents_per_point},
        version=1,
        status="PUBLISHED",
        effective_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db.add(config)
    db.commit()
    return config


def _apply(db, setup, *, points: int = 300) -> SupplyPointsWithdrawal:
    return create_withdrawal(
        db,
        company_id=setup["receiver"].id,
        requested_by=setup["receiver_user"].id,
        points_requested=points,
        payee_name="张三",
        payee_account="6222020200112233445",
        payment_method="BANK",
    )


def test_withdrawal_full_flow_freezes_then_writes_off_once(db):
    setup, account = _setup_wallet(db)
    _seed_rate(db, cents_per_point=2)
    item = _apply(db, setup, points=300)
    assert item.status == "PENDING_REVIEW"

    reviewed = review_withdrawal(
        db,
        withdrawal_id=item.id,
        reviewed_by=setup["reviewer"].id,
        decision="APPROVE",
        review_note="核对额度与比例",
    )
    db.commit()
    # 审核冻结：不提前扣账，只锁定额度并锁定比例快照。
    assert reviewed.status == "APPROVED_PENDING_PAYMENT"
    assert reviewed.cash_amount_cents_snapshot == 600
    assert reviewed.cash_cents_per_point_snapshot == 2
    assert db.get(PointsAccount, account.id).supply_balance == 1000
    assert db.get(PointsAccount, account.id).frozen_supply_points == 300

    paid = record_offline_payment(
        db,
        withdrawal_id=item.id,
        recorded_by=setup["reviewer"].id,
        external_reference="TXN-WD-001",
        amount_cents=600,
    )
    assert paid.status == "PAID_PENDING_WRITE_OFF"

    confirmed = confirm_offline_payment(
        db,
        withdrawal_id=item.id,
        confirmed_by=setup["reviewer"].id,
        idempotency_key="confirm-wd-1",
    )
    db.commit()
    assert confirmed.status == "PAID"
    # 确认付款后只扣一次。
    assert db.get(PointsAccount, account.id).supply_balance == 700
    assert db.get(PointsAccount, account.id).frozen_supply_points == 0
    ledger = db.scalar(
        select(PointsLedger).where(PointsLedger.business_type == "SUPPLY_WITHDRAWAL")
    )
    assert ledger is not None and ledger.delta == -300
    again = confirm_offline_payment(
        db,
        withdrawal_id=item.id,
        confirmed_by=setup["reviewer"].id,
        idempotency_key="confirm-wd-1",
    )
    assert again.status == "PAID"
    assert db.get(PointsAccount, account.id).supply_balance == 700


def test_withdrawal_reject_exceeds_available_and_release_on_reject(db):
    setup, account = _setup_wallet(db)
    _seed_rate(db)
    with pytest.raises(AppError) as error:
        _apply(db, setup, points=5000)
    assert error.value.code == "WITHDRAWAL_AMOUNT_EXCEEDS_AVAILABLE"

    item = _apply(db, setup, points=300)
    review_withdrawal(
        db,
        withdrawal_id=item.id,
        reviewed_by=setup["reviewer"].id,
        decision="APPROVE",
        review_note="同意",
    )
    db.commit()
    assert db.get(PointsAccount, account.id).frozen_supply_points == 300
    # 在途冻结占用额度：相同余额下不能再申请超过剩余额度。
    availability = available_supply_points(db, company_id=setup["receiver"].id)
    assert availability["available"] == 400
    # 第二笔申请走驳回：冻结必须释放。
    item2 = _apply(db, setup, points=200)
    rejected = review_withdrawal(
        db,
        withdrawal_id=item2.id,
        reviewed_by=setup["reviewer"].id,
        decision="REJECT",
        review_note="资料不齐",
    )
    db.commit()
    assert rejected.status == "REJECTED"
    assert db.get(PointsAccount, account.id).frozen_supply_points == 300


def test_withdrawal_review_requires_published_rate(db):
    setup, _ = _setup_wallet(db)
    item = _apply(db, setup, points=100)
    with pytest.raises(AppError) as error:
        review_withdrawal(
            db,
            withdrawal_id=item.id,
            reviewed_by=setup["reviewer"].id,
            decision="APPROVE",
            review_note="未配置比例",
        )
    assert error.value.code == "SUPPLY_TERMINATION_RATE_NOT_CONFIGURED"


def test_payment_change_requires_review_and_only_before_payment(db):
    setup, _ = _setup_wallet(db)
    _seed_rate(db)
    item = _apply(db, setup, points=200)
    review_withdrawal(
        db,
        withdrawal_id=item.id,
        reviewed_by=setup["reviewer"].id,
        decision="APPROVE",
        review_note="同意",
    )
    changed = request_payment_change(
        db,
        withdrawal_id=item.id,
        requested_by=setup["receiver_user"].id,
        payee_name="李四",
        payee_account="6222020200998877665",
        payment_method="BANK",
        reason="原账户停用",
    )
    db.commit()
    assert changed.change_status == "PENDING"
    with pytest.raises(AppError):
        record_offline_payment(
            db,
            withdrawal_id=item.id,
            recorded_by=setup["reviewer"].id,
            external_reference="TXN-WD-002",
            amount_cents=400,
        )
    approved = review_payment_change(
        db,
        withdrawal_id=item.id,
        reviewed_by=setup["reviewer"].id,
        decision="APPROVE",
    )
    db.commit()
    assert approved.change_status == "APPROVED"
    assert decrypt_text(approved.payee_name_encrypted) == "李四"

    record_offline_payment(
        db,
        withdrawal_id=item.id,
        recorded_by=setup["reviewer"].id,
        external_reference="TXN-WD-003",
        amount_cents=400,
    )
    with pytest.raises(AppError):
        request_payment_change(
            db,
            withdrawal_id=item.id,
            requested_by=setup["receiver_user"].id,
            payee_name="王五",
            payee_account="6222020200111111111",
            payment_method="BANK",
            reason="已付款后不能变更",
        )


def test_withdrawal_cancel_releases_frozen_points(db):
    setup, account = _setup_wallet(db)
    _seed_rate(db)
    item = _apply(db, setup, points=150)
    review_withdrawal(
        db,
        withdrawal_id=item.id,
        reviewed_by=setup["reviewer"].id,
        decision="APPROVE",
        review_note="同意",
    )
    db.commit()
    assert db.get(PointsAccount, account.id).frozen_supply_points == 150
    cancelled = cancel_withdrawal(
        db,
        withdrawal_id=item.id,
        cancelled_by=setup["receiver_user"].id,
        note="临时不需要提现",
    )
    db.commit()
    assert cancelled.status == "CANCELLED"
    assert db.get(PointsAccount, account.id).frozen_supply_points == 0


def test_withdrawal_http_endpoints(api_client):
    from apps.api.src.core.auth import get_current_principal
    from apps.api.src.main import app

    client, factory = api_client
    with factory() as db:
        setup, account = _setup_wallet(db)
        _seed_rate(db, cents_per_point=1)
        owner_principal = _principal(
            setup["receiver_user"], "points.own.read"
        )
        admin_principal = _principal(setup["reviewer"], "*")
        employee = User(
            display_name="无提现权员工", status="ACTIVE", company_id=setup["receiver"].id
        )
        db.add(employee)
        db.flush()
        assign_role(db, employee, "FRANCHISE_EMPLOYEE")
        employee_principal = _principal(employee, "assignment.employee.read")
    app.dependency_overrides[get_current_principal] = lambda: owner_principal
    try:
        forbidden = client.post(
            "/api/v1/v1.2/supply-withdrawals",
            json={"points_requested": 100, "payee_name": "张三", "payee_account": "123", "payment_method": "BANK"},
        )
        # employee principal 有公司上下文之前先验证员工无权：切换到员工再试
    finally:
        app.dependency_overrides.pop(get_current_principal, None)

    app.dependency_overrides[get_current_principal] = lambda: employee_principal
    try:
        employee_forbidden = client.post(
            "/api/v1/v1.2/supply-withdrawals",
            json={"points_requested": 100, "payee_name": "张三", "payee_account": "123", "payment_method": "BANK"},
        )
        assert employee_forbidden.status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_principal, None)

    app.dependency_overrides[get_current_principal] = lambda: owner_principal
    try:
        before = client.get("/api/v1/v1.2/supply-withdrawals/available").json()["data"]
        created = client.post(
            "/api/v1/v1.2/supply-withdrawals",
            json={"points_requested": 400, "payee_name": "张三", "payee_account": "6222020200112233445", "payment_method": "BANK"},
        )
        assert created.status_code == 200, created.text
        withdrawal_id = created.json()["data"]["id"]
        after = client.get("/api/v1/v1.2/supply-withdrawals/available").json()["data"]
        assert after["in_flight"] == before["in_flight"] + 400
        assert after["available"] == before["available"] - 400
    finally:
        app.dependency_overrides.pop(get_current_principal, None)

    app.dependency_overrides[get_current_principal] = lambda: admin_principal
    try:
        reviewed = client.post(
            f"/api/v1/v1.2/admin/supply-withdrawals/{withdrawal_id}/review",
            json={"decision": "APPROVE", "review_note": "同意"},
        )
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["data"]["status"] == "APPROVED_PENDING_PAYMENT"
        recorded = client.post(
            f"/api/v1/v1.2/admin/supply-withdrawals/{withdrawal_id}/record-transfer",
            json={"external_reference": "TXN-WD-HTTP", "amount_cents": 400},
        )
        assert recorded.status_code == 200, recorded.text
        confirmed = client.post(
            f"/api/v1/v1.2/admin/supply-withdrawals/{withdrawal_id}/confirm",
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["data"]["status"] == "PAID"
    finally:
        app.dependency_overrides.pop(get_current_principal, None)
