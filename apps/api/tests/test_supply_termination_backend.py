from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from apps.api.src.core.auth import Principal
from apps.api.src.core.errors import AppError
from apps.api.src.core.models import (
    Assignment,
    Company,
    CompanyCapability,
    CompanyServiceRegion,
    Lead,
    PointsAccount,
    PointsLedger,
    SystemConfig,
    User,
)
from apps.api.src.core.models_v12 import SupplierLeadReward
from apps.api.src.core.security import encrypt_text, hash_phone
from apps.api.src.schemas.supply_termination import SupplyTerminationPaymentBody
from apps.api.src.services.claim_service import claim_assignment
from apps.api.src.services.dispatch_service import dispatch_lead
from apps.api.src.services.followup_service import add_followup
from apps.api.src.services.points_service import change_points
from apps.api.src.services.company_service import company_to_dict
from apps.api.src.services.supply_termination import (
    _masked_account,
    approve_termination,
    cancel_termination,
    confirm_offline_payment,
    create_termination_request,
    fail_offline_payment,
    prepare_supplier_reward_reversal,
    record_offline_payment,
    reopen_supply_cooperation,
    termination_blockers,
)
from apps.api.src.routers.supply_termination import _masked_payment_reference


def _company(db, code: str = "TERM-001") -> tuple[Company, User]:
    company = Company(code=code, name="终止合作测试加盟商", status="ACTIVE")
    db.add(company)
    db.flush()
    owner = User(
        username=f"owner-{code}",
        display_name="加盟商负责人",
        password_hash="test",
        status="ACTIVE",
        company_id=company.id,
    )
    db.add(owner)
    db.flush()
    company.primary_user_id = owner.id
    db.flush()
    return company, owner


def _publish_rate(db, cents_per_point: int = 25) -> SystemConfig:
    item = SystemConfig(
        domain="supply_termination",
        key="cashout_rate",
        value_json={"cash_cents_per_point": cents_per_point},
        version=1,
        status="PUBLISHED",
        effective_at=datetime.now(timezone.utc),
    )
    db.add(item)
    db.flush()
    return item


def test_short_financial_identifiers_are_fully_masked() -> None:
    assert _masked_account("1234") == "****"
    assert _masked_account("123") == "***"
    assert _masked_account("6222000012345678") == "************5678"
    assert _masked_payment_reference("ABC") == "***"
    assert _masked_payment_reference("1234") == "****"
    assert _masked_payment_reference("BANK-3456") == "*****3456"


def test_payment_registration_rejects_blank_reference_and_future_time() -> None:
    base = {
        "external_reference": "BANK-001",
        "paid_at": datetime.now(timezone.utc),
        "payment_amount_cents": 100,
        "note": "已核对付款",
    }
    with pytest.raises(ValidationError):
        SupplyTerminationPaymentBody.model_validate({**base, "external_reference": "   "})
    with pytest.raises(ValidationError):
        SupplyTerminationPaymentBody.model_validate({
            **base,
            "paid_at": datetime.now(timezone.utc) + timedelta(hours=1),
        })


def test_reward_points_are_kept_separate_from_customer_points(db) -> None:
    company, owner = _company(db)
    change_points(
        db, company_id=company.id, delta=500, ledger_type="RECHARGE",
        business_type="TEST", business_id="customer", idempotency_key="customer-seed-001",
        created_by=owner.id,
    )
    reward = change_points(
        db, company_id=company.id, delta=80, ledger_type="REWARD",
        business_type="V12_SUPPLIER_REWARD", business_id="reward", idempotency_key="reward-seed-001",
        created_by=owner.id, point_kind="SUPPLY",
    )
    account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == company.id))
    assert account.balance == 500
    assert account.supply_balance == 80
    assert reward.point_kind == "SUPPLY"
    assert reward.balance_after == 80
    db.expire(company, ["points_account"])
    company_data = company_to_dict(company, include_finance=True)
    assert company_data["points_balance"] == 500
    assert company_data["customer_points_balance"] == 500
    assert company_data["supply_points_balance"] == 80


def test_termination_requires_published_rate_before_financial_snapshot(db) -> None:
    company, owner = _company(db)
    request = create_termination_request(
        db, company_id=company.id, requested_by=owner.id, reason="不再提供新客资",
        payee_name="张三", payee_account="6222000012345678", payment_method="BANK_TRANSFER",
    )
    with pytest.raises(AppError) as exc:
        approve_termination(db, request_id=request.id, approved_by=owner.id, review_note="审核通过")
    assert exc.value.code == "SUPPLY_TERMINATION_RATE_NOT_CONFIGURED"
    assert company.status == "ACTIVE"


def test_paid_termination_writes_off_snapshot_only_and_is_idempotent(db) -> None:
    company, owner = _company(db)
    _publish_rate(db, 25)
    change_points(
        db, company_id=company.id, delta=500, ledger_type="RECHARGE",
        business_type="TEST", business_id="customer", idempotency_key="customer-seed-002",
        created_by=owner.id,
    )
    change_points(
        db, company_id=company.id, delta=80, ledger_type="REWARD",
        business_type="V12_SUPPLIER_REWARD", business_id="reward", idempotency_key="reward-seed-002",
        created_by=owner.id, point_kind="SUPPLY",
    )
    request = create_termination_request(
        db, company_id=company.id, requested_by=owner.id, reason="不再提供新客资",
        payee_name="张三", payee_account="6222000012345678", payment_method="BANK_TRANSFER",
    )
    approve_termination(db, request_id=request.id, approved_by=owner.id, review_note="审核通过")
    assert request.general_points_snapshot == 580
    assert request.cash_amount_cents_snapshot == 14500

    with pytest.raises(AppError) as frozen_supply:
        change_points(
            db, company_id=company.id, delta=1, ledger_type="ADJUST",
            business_type="TEST", business_id="frozen-supply-adjust",
            idempotency_key="frozen-supply-adjust", created_by=owner.id,
            point_kind="SUPPLY",
        )
    assert frozen_supply.value.code == "POINTS_FROZEN_FOR_TERMINATION"

    # 审批后收到的新客资积分不属于本次终止结算快照。
    change_points(
        db, company_id=company.id, delta=60, ledger_type="RETURN",
        business_type="TEST", business_id="later", idempotency_key="later-credit-001",
        created_by=owner.id,
    )
    record_offline_payment(
        db, request_id=request.id, recorded_by=owner.id,
        external_reference="BANK-20260915-001", paid_at=datetime.now(timezone.utc),
        payment_amount_cents=14500,
        note="线下已付款",
    )
    # 客户端幂等键即使曾被同公司其他业务使用，也不能跳过本结算单核销。
    change_points(
        db, company_id=company.id, delta=1, ledger_type="ADJUST",
        business_type="TEST", business_id="idempotency-collision",
        idempotency_key="termination-pay-001:customer", created_by=owner.id,
    )
    first = confirm_offline_payment(
        db, request_id=request.id, confirmed_by=owner.id,
        idempotency_key="termination-pay-001",
    )
    second = confirm_offline_payment(
        db, request_id=request.id, confirmed_by=owner.id,
        idempotency_key="termination-pay-001",
    )
    account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == company.id))
    assert first.id == second.id
    assert account.balance == 61
    assert account.supply_balance == 0
    assert company.status == "ACTIVE"
    assert company.supplier_cooperation_status == "TERMINATED"
    writeoffs = db.scalars(select(PointsLedger).where(PointsLedger.business_id == request.id)).all()
    assert sorted((item.point_kind, item.delta) for item in writeoffs) == [("CUSTOMER", -500), ("SUPPLY", -80)]

    reopen_supply_cooperation(db, company_id=company.id, reopened_by=owner.id, note="恢复供资")
    assert company.supplier_cooperation_status == "ACTIVE"
    assert request.status == "TERMINATED"


def test_pending_or_terminated_cooperation_blocks_supply_wallet_debits(db) -> None:
    company, owner = _company(db)
    change_points(
        db, company_id=company.id, delta=100, ledger_type="REWARD",
        business_type="V12_SUPPLIER_REWARD", business_id="reward", idempotency_key="reward-seed-003",
        created_by=owner.id, point_kind="SUPPLY",
    )
    request = create_termination_request(
        db, company_id=company.id, requested_by=owner.id, reason="不再提供新客资",
        payee_name="张三", payee_account="6222000012345678", payment_method="BANK_TRANSFER",
    )
    _publish_rate(db)
    approve_termination(db, request_id=request.id, approved_by=owner.id, review_note="审核通过")
    with pytest.raises(AppError) as exc:
        change_points(
            db, company_id=company.id, delta=-1, ledger_type="ADJUST",
            business_type="TEST", business_id="spend", idempotency_key="spend-supply-001",
            created_by=owner.id, point_kind="SUPPLY",
        )
    assert exc.value.code == "POINTS_FROZEN_FOR_TERMINATION"


def test_terminated_supplier_cooperation_keeps_receiving_claiming_and_followup(db) -> None:
    company, owner = _company(db, "TERM-RECEIVER")
    company.supplier_cooperation_status = "TERMINATED"
    db.add(CompanyServiceRegion(company_id=company.id, region_code="310101", active=True))
    db.add(CompanyCapability(company_id=company.id, category_code="RENOVATION", active=True))
    change_points(
        db, company_id=company.id, delta=200, ledger_type="RECHARGE",
        business_type="TEST", business_id="receiver-balance",
        idempotency_key="terminated-receiver-balance", created_by=owner.id,
    )
    lead = Lead(
        customer_name="终止供资后接收客户",
        phone_encrypted=encrypt_text("13800138088"),
        phone_hash=hash_phone("13800138088"),
        city="上海市",
        region_code="310101",
        category_code="RENOVATION",
        status="QUALIFIED",
    )
    db.add(lead)
    db.flush()
    operator = Principal(
        user_id=owner.id,
        display_name="运营",
        company_id=None,
        role_codes=frozenset({"OPERATION"}),
        permission_codes=frozenset({"lead.dispatch"}),
        session_version=1,
    )
    assignment = dispatch_lead(
        db, lead_id=lead.id, company_id=company.id, principal=operator,
        idempotency_key="terminated-receiver-dispatch",
    )
    owner_principal = Principal(
        user_id=owner.id,
        display_name=owner.display_name,
        company_id=company.id,
        role_codes=frozenset({"FRANCHISE_OWNER"}),
        permission_codes=frozenset({"assignment.own.claim", "followup.own.manage"}),
        session_version=1,
    )
    claimed, _ = claim_assignment(
        db, assignment.id, owner_principal, "terminated-receiver-claim",
    )
    followup = add_followup(
        db, assignment=claimed, principal=owner_principal,
        status="CONTACTED", note="已联系客户", next_followup_at=None,
    )

    assert assignment.status == "FOLLOWING"
    assert followup.status == "CONTACTED"
    assert company.supplier_cooperation_status == "TERMINATED"
    assert company.status == "ACTIVE"


def test_early_settled_reward_blocks_with_record_id_until_return_window_closes(db) -> None:
    supplier, owner = _company(db, "TERM-EARLY-SUP")
    receiver, _ = _company(db, "TERM-EARLY-REC")
    lead = Lead(
        customer_name="提前确认客资", phone_encrypted="encrypted", phone_hash="term-early-phone",
        status="COMPLETED", source_kind="SUPPLIER_H5", supplier_company_id=supplier.id,
    )
    db.add(lead)
    db.flush()
    assignment = Assignment(
        lead_id=lead.id, company_id=receiver.id, receiver_company_id=receiver.id,
        supplier_company_id=supplier.id, status="COMPLETED", points_price=100,
        claim_points=100, lead_snapshot={}, assigned_by=owner.id,
        claimed_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    db.add(assignment)
    db.flush()
    reward = SupplierLeadReward(
        lead_id=lead.id, assignment_id=assignment.id, supplier_company_id=supplier.id,
        receiver_company_id=receiver.id, status="SETTLED", claim_points=100,
        reward_ratio_bps=3000, reward_points=30, rule_version=1,
        settled_at=datetime.now(timezone.utc),
    )
    db.add(reward)
    db.flush()
    ledger = change_points(
        db, company_id=supplier.id, delta=30, ledger_type="REWARD",
        business_type="V12_SUPPLIER_REWARD", business_id=reward.id,
        idempotency_key="term-early-reward", created_by=owner.id, point_kind="SUPPLY",
    )
    reward.ledger_id = ledger.id
    blockers = termination_blockers(db, supplier.id)
    early = next(item for item in blockers if item["code"] == "EARLY_REWARD_RETURN_WINDOW")
    assert early["record_ids"] == [reward.id]
    assert early["count"] == 1 and early["truncated"] is False


def test_owner_can_cancel_before_payment_without_writing_off_points(db) -> None:
    company, owner = _company(db, "TERM-CANCEL")
    _publish_rate(db)
    change_points(
        db, company_id=company.id, delta=100, ledger_type="RECHARGE",
        business_type="TEST", business_id="seed", idempotency_key="cancel-seed-001",
        created_by=owner.id,
    )
    item = create_termination_request(
        db, company_id=company.id, requested_by=owner.id, reason="暂停提供客资",
        payee_name="张三", payee_account="6222000012345678", payment_method="BANK_TRANSFER",
    )
    approve_termination(db, request_id=item.id, approved_by=owner.id, review_note="通过")
    with pytest.raises(AppError) as frozen_zero_supply:
        change_points(
            db, company_id=company.id, delta=1, ledger_type="ADJUST",
            business_type="TEST", business_id="zero-snapshot-supply",
            idempotency_key="zero-snapshot-supply", created_by=owner.id,
            point_kind="SUPPLY",
        )
    assert frozen_zero_supply.value.code == "POINTS_FROZEN_FOR_TERMINATION"
    cancelled = cancel_termination(
        db, company_id=company.id, cancelled_by=owner.id, note="继续合作",
    )
    assert cancelled.id == item.id and cancelled.status == "CANCELLED"
    assert company.supplier_cooperation_status == "ACTIVE"
    account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == company.id))
    assert account.balance == 100 and account.frozen_customer_points == 0
    assert db.scalar(select(PointsLedger).where(PointsLedger.business_type == "SUPPLY_TERMINATION")) is None


def test_payment_can_be_corrected_then_failed_without_points_writeoff(db) -> None:
    company, owner = _company(db, "TERM-PAY-FAIL")
    _publish_rate(db)
    change_points(
        db, company_id=company.id, delta=100, ledger_type="RECHARGE",
        business_type="TEST", business_id="seed", idempotency_key="pay-fail-seed-001",
        created_by=owner.id,
    )
    item = create_termination_request(
        db, company_id=company.id, requested_by=owner.id, reason="停止提供客资",
        payee_name="张三", payee_account="6222000012345678", payment_method="BANK_TRANSFER",
    )
    approve_termination(db, request_id=item.id, approved_by=owner.id, review_note="通过")
    with pytest.raises(AppError) as mismatch:
        record_offline_payment(
            db, request_id=item.id, recorded_by=owner.id, external_reference="BANK-WRONG-AMOUNT",
            paid_at=datetime.now(timezone.utc), payment_amount_cents=2499, note="金额错误",
        )
    assert mismatch.value.code == "SUPPLY_TERMINATION_PAYMENT_AMOUNT_MISMATCH"
    record_offline_payment(
        db, request_id=item.id, recorded_by=owner.id, external_reference="BANK-WRONG",
        paid_at=datetime.now(timezone.utc), note="首次登记", proof_url="https://example.invalid/old-proof",
        payment_amount_cents=2500,
    )
    record_offline_payment(
        db, request_id=item.id, recorded_by=owner.id, external_reference="BANK-CORRECT",
        paid_at=datetime.now(timezone.utc), note="更正流水", proof_url="https://example.invalid/new-proof",
        payment_amount_cents=2500,
    )
    failed = fail_offline_payment(db, request_id=item.id, failed_by=owner.id, note="银行退票")
    account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == company.id))
    assert failed.status == "PAYMENT_FAILED"
    assert [event["event"] for event in failed.payment_history_json] == ["RECORDED", "CORRECTED", "FAILED"]
    assert failed.payment_external_reference is None and failed.payment_proof_url is None
    assert account.balance == 100 and account.frozen_customer_points == 100
    assert company.supplier_cooperation_status == "TERMINATION_PENDING"
    assert db.scalar(select(PointsLedger).where(PointsLedger.business_type == "SUPPLY_TERMINATION")) is None
    retried = record_offline_payment(
        db, request_id=item.id, recorded_by=owner.id, external_reference="BANK-RETRY",
        paid_at=datetime.now(timezone.utc), note="退票后重新付款", payment_amount_cents=2500,
    )
    assert retried.status == "PAID_PENDING_WRITE_OFF"


def test_reward_reversal_invalidates_unpaid_snapshot_and_stops_after_payment(db) -> None:
    company, owner = _company(db, "TERM-REVERSAL")
    _publish_rate(db)
    change_points(
        db, company_id=company.id, delta=30, ledger_type="REWARD",
        business_type="V12_SUPPLIER_REWARD", business_id="reward-old",
        idempotency_key="termination-reversal-seed", created_by=owner.id, point_kind="SUPPLY",
    )
    item = create_termination_request(
        db, company_id=company.id, requested_by=owner.id, reason="停止提供客资",
        payee_name="张三", payee_account="6222000012345678", payment_method="BANK_TRANSFER",
    )
    approve_termination(db, request_id=item.id, approved_by=owner.id, review_note="通过")
    prepare_supplier_reward_reversal(db, company_id=company.id, reward_id="reward-old")
    account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == company.id))
    assert item.status == "NEED_MORE" and item.general_points_snapshot is None
    assert account.frozen_supply_points == 0

    create_termination_request(
        db, company_id=company.id, requested_by=owner.id, reason="重新清算",
        payee_name="张三", payee_account="6222000012345678", payment_method="BANK_TRANSFER",
    )
    approve_termination(db, request_id=item.id, approved_by=owner.id, review_note="再次通过")
    record_offline_payment(
        db, request_id=item.id, recorded_by=owner.id, external_reference="BANK-REVERSAL-LOCK",
        paid_at=datetime.now(timezone.utc), payment_amount_cents=750, note="已登记付款",
    )
    with pytest.raises(AppError) as blocked:
        prepare_supplier_reward_reversal(db, company_id=company.id, reward_id="reward-old")
    assert blocked.value.code == "REWARD_REVERSAL_PAYMENT_RECORDED"
    assert item.status == "PAID_PENDING_WRITE_OFF"


def _login(client, username: str, password: str) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.cookies.get('access_token')}"}


def test_current_status_is_readable_by_employee_but_application_is_owner_only(api_client) -> None:
    client, _ = api_client
    owner = _login(client, "franchise_demo", "Franchise123!")
    employee = _login(client, "franchise_employee_demo", "Employee123!")
    current = client.get("/api/v1/v1.2/supply-termination/current", headers=employee)
    assert current.status_code == 200, current.text
    assert current.json()["data"]["cooperation_status"] == "ACTIVE"
    assert current.json()["data"]["customer_points_balance"] is None

    denied = client.post(
        "/api/v1/v1.2/supply-termination/requests", headers=employee,
        json={"reason": "不再提供新客资", "payee_name": "张三", "payee_account": "6222000012345678", "payment_method": "BANK_TRANSFER"},
    )
    assert denied.status_code == 403
    submitted = client.post(
        "/api/v1/v1.2/supply-termination/requests", headers=owner,
        json={"reason": "不再提供新客资", "payee_name": "张三", "payee_account": "6222000012345678", "payment_method": "BANK_TRANSFER"},
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["data"]["status"] == "REQUESTED"
    employee_state = client.get("/api/v1/v1.2/supply-termination/current", headers=employee).json()["data"]
    employee_current = employee_state["request"]
    assert employee_current["payee_account_masked"].endswith("5678")
    assert employee_current["payee_account"] is None
    assert employee_current["payment_external_reference"] is None
    assert employee_current["payment_proof_url"] is None
    assert employee_current["payment_history"] == []
    assert len(employee_state["history"]) == 1
    assert employee_state["history"][0]["id"] == submitted.json()["data"]["id"]
    assert employee_state["history"][0]["payee_account"] is None

    operation = _login(client, "operation", "Operation123!")
    operation_list = client.get("/api/v1/v1.2/supply-terminations", headers=operation)
    assert operation_list.status_code == 200, operation_list.text
    assert operation_list.json()["data"]["items"][0]["payee_account"] is None
    denied_payment = client.post(
        f"/api/v1/v1.2/supply-terminations/{submitted.json()['data']['id']}/payment/fail",
        headers=operation,
        json={"note": "运营无财务权限"},
    )
    assert denied_payment.status_code == 403
