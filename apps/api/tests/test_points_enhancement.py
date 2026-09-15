from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from apps.api.src.core.errors import AppError
from apps.api.src.core.models import Company, Notification, NotificationOutbox, PointsAccount, PointsPackage
from apps.api.src.schemas.company import CompanyCreateBody
from apps.api.src.services.auth_service import create_internal_user
from apps.api.src.services.company_service import create_company
from apps.api.src.services.points_service import (
    account_summary,
    change_points,
    reconcile_points_account,
    run_low_points_warnings,
)


def test_level_entitlements_are_exposed_in_account_summary(db):
    company = create_company(db, CompanyCreateBody(code="P101", name="积分权益公司", level_code="V2"))
    db.add(
        PointsPackage(
            code="V2_PACKAGE",
            name="V2 档位",
            cash_amount_cents=5_000_000,
            base_points=50_000,
            bonus_points=5_000,
            level_code="V2",
            entitlements_json={"客资折扣": "95折", "服务优先级": "优先"},
            version=2,
            status="PUBLISHED",
            effective_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
    )
    db.commit()

    result = account_summary(db, company.id)
    assert result["level_code"] == "V2"
    assert result["level_entitlements"]["客资折扣"] == "95折"
    assert result["level_package"]["version"] == 2
    assert result["low_points"] is True


def test_low_points_warning_is_deduplicated_per_company_per_day(db, monkeypatch):
    import apps.api.src.services.points_service as module

    monkeypatch.setattr(module.settings, "low_points_warning_threshold", 1000)
    company = create_company(db, CompanyCreateBody(code="P102", name="低积分公司"))
    user = create_internal_user(
        db,
        username="low-points-owner",
        password="Owner123!",
        display_name="低积分老板",
        role_code="FRANCHISE_OWNER",
        company_id=company.id,
    )
    company.primary_user_id = user.id
    change_points(
        db,
        company_id=company.id,
        delta=500,
        ledger_type="ADJUST",
        business_type="TEST",
        business_id="seed",
        idempotency_key="seed-low-points",
        created_by=None,
    )
    db.commit()

    day_one = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)
    first = run_low_points_warnings(db, as_of=day_one)
    repeated_same_day = run_low_points_warnings(db, as_of=day_one + timedelta(hours=6))
    db.commit()
    assert first["warned"] == 1
    assert repeated_same_day["warned"] == 0
    notification = db.scalar(
        select(Notification).where(
            Notification.company_id == company.id,
            Notification.scene == "LOW_POINTS",
        )
    )
    outbox = db.scalar(
        select(NotificationOutbox).where(NotificationOutbox.aggregate_type == "points_account")
    )
    assert notification is not None
    assert notification.deep_link == "/h5/v12-workbench.html?view=points"
    assert outbox is not None
    assert outbox.payload["deep_link"] == "/h5/v12-workbench.html?view=points"

    change_points(
        db,
        company_id=company.id,
        delta=1000,
        ledger_type="ADJUST",
        business_type="TEST",
        business_id="recover",
        idempotency_key="recover-points",
        created_by=None,
    )
    db.commit()
    recovered = run_low_points_warnings(db, as_of=day_one + timedelta(days=1))
    assert recovered["warned"] == 0

    change_points(
        db,
        company_id=company.id,
        delta=-700,
        ledger_type="CLAIM",
        business_type="TEST",
        business_id="new-crossing",
        idempotency_key="new-low-crossing",
        created_by=None,
    )
    next_day = run_low_points_warnings(db, as_of=day_one + timedelta(days=2))
    db.commit()
    assert next_day["warned"] == 1
    assert len(db.scalars(select(Notification).where(Notification.company_id == company.id, Notification.scene == "LOW_POINTS")).all()) == 2


def test_points_reconciliation_detects_balanced_ledger(db):
    company = create_company(db, CompanyCreateBody(code="P103", name="积分对账公司"))
    change_points(db, company_id=company.id, delta=2000, ledger_type="RECHARGE", business_type="TEST", business_id="r1", idempotency_key="recon-r1", created_by=None)
    change_points(db, company_id=company.id, delta=-300, ledger_type="CLAIM", business_type="TEST", business_id="c1", idempotency_key="recon-c1", created_by=None)
    db.commit()

    result = reconcile_points_account(db, company.id)
    assert result["balanced"] is True
    assert result["expected_closing_balance"] == 1700
    assert result["snapshot_balance"] == 1700
    assert result["difference"] == 0
    assert result["sequence_error_count"] == 0


def test_supply_points_reconciliation_uses_the_supply_wallet(db):
    company = create_company(db, CompanyCreateBody(code="P104", name="供客积分对账公司"))
    change_points(
        db,
        company_id=company.id,
        delta=80,
        ledger_type="REWARD",
        business_type="TEST",
        business_id="supply-r1",
        idempotency_key="recon-supply-r1",
        created_by=None,
        point_kind="SUPPLY",
    )
    change_points(
        db,
        company_id=company.id,
        delta=-20,
        ledger_type="ADJUST",
        business_type="TEST",
        business_id="supply-a1",
        idempotency_key="recon-supply-a1",
        created_by=None,
        point_kind="SUPPLY",
    )
    db.commit()

    customer = reconcile_points_account(db, company.id, point_kind="CUSTOMER")
    supply = reconcile_points_account(db, company.id, point_kind="SUPPLY")

    assert customer["point_kind"] == "CUSTOMER"
    assert customer["snapshot_balance"] == 0
    assert supply["point_kind"] == "SUPPLY"
    assert supply["balanced"] is True
    assert supply["expected_closing_balance"] == 60
    assert supply["snapshot_balance"] == 60


def test_points_writes_are_blocked_until_historical_split_is_reconciled(db):
    company = create_company(db, CompanyCreateBody(code="P105", name="历史分账异常公司"))
    account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == company.id))
    assert account is not None
    account.points_split_status = "BLOCKED"
    db.commit()

    with pytest.raises(AppError) as exc:
        change_points(
            db,
            company_id=company.id,
            delta=10,
            ledger_type="ADJUST",
            business_type="TEST",
            business_id="blocked-split",
            idempotency_key="blocked-split-write",
            created_by=None,
        )

    assert exc.value.code == "POINTS_SPLIT_RECONCILIATION_REQUIRED"


def test_idempotency_key_reuse_with_different_ledger_semantics_is_rejected(db):
    company = create_company(db, CompanyCreateBody(code="P106", name="积分幂等冲突公司"))
    change_points(
        db,
        company_id=company.id,
        delta=20,
        ledger_type="ADJUST",
        business_type="TEST",
        business_id="first",
        idempotency_key="same-key-different-request",
        created_by=None,
    )

    with pytest.raises(AppError) as exc:
        change_points(
            db,
            company_id=company.id,
            delta=-20,
            ledger_type="TERMINATION_WRITEOFF",
            business_type="SUPPLY_TERMINATION",
            business_id="another-business",
            idempotency_key="same-key-different-request",
            created_by=None,
            allow_frozen=True,
        )

    assert exc.value.code == "POINTS_IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize(
    ("ledger_type", "point_kind"),
    [("REWARD", "CUSTOMER"), ("RECHARGE", "SUPPLY"), ("CLAIM", "SUPPLY"), ("RETURN", "SUPPLY")],
)
def test_business_ledger_type_cannot_write_to_the_wrong_wallet(db, ledger_type, point_kind):
    company = create_company(db, CompanyCreateBody(code=f"KIND-{ledger_type}", name=f"{ledger_type} 钱包约束"))

    with pytest.raises(AppError) as exc:
        change_points(
            db,
            company_id=company.id,
            delta=10,
            ledger_type=ledger_type,
            business_type="TEST",
            business_id=f"wrong-{ledger_type}",
            idempotency_key=f"wrong-wallet-{ledger_type}",
            created_by=None,
            point_kind=point_kind,
        )

    assert exc.value.code == "POINT_KIND_BUSINESS_MISMATCH"


def test_only_reversals_can_explicitly_create_a_negative_balance(db):
    company = create_company(db, CompanyCreateBody(code="NEGATIVE-GUARD", name="负积分约束公司"))

    with pytest.raises(AppError) as exc:
        change_points(
            db,
            company_id=company.id,
            delta=-10,
            ledger_type="ADJUST",
            business_type="TEST",
            business_id="negative-adjust",
            idempotency_key="negative-adjust",
            created_by=None,
            allow_negative=True,
        )

    assert exc.value.code == "POINTS_NEGATIVE_BALANCE_NOT_ALLOWED"


def _login(client, username: str, password: str) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    token = response.cookies.get("access_token")
    assert token
    assert "token" not in response.json()["data"]
    return {"Authorization": f"Bearer {token}"}


def test_manual_recharge_requires_explicit_confirmation_and_enqueues_notice(api_client):
    client, factory = api_client
    admin = _login(client, "admin", "Admin123!")
    packages = client.get("/api/v1/points/packages", headers=admin).json()["data"]
    package = packages[0]
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        assert company
        company_id = company.id

    payload = {
        "company_id": company_id,
        "package_id": package["id"],
        "external_reference": "BANK-P101-CONFIRM",
        "cash_amount_cents": package["cash_amount_cents"],
        "note": "已核对银行流水与收款凭证",
        "idempotency_key": "recharge-p101-confirm",
    }
    rejected = client.post("/api/v1/points/recharge", headers=admin, json=payload)
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "POINTS_RECHARGE_CONFIRM_REQUIRED"

    accepted = client.post("/api/v1/points/recharge", headers=admin, json={**payload, "confirmed": True})
    assert accepted.status_code == 200, accepted.text
    first_ledger = accepted.json()["data"]
    assert first_ledger["delta"] == package["total_points"]

    repeated = client.post("/api/v1/points/recharge", headers=admin, json={**payload, "confirmed": True})
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["data"]["id"] == first_ledger["id"]

    with factory() as db:
        notifications = db.scalars(select(Notification).where(Notification.company_id == company_id, Notification.scene == "POINTS_RECHARGED")).all()
        outbox = db.scalars(select(NotificationOutbox).where(NotificationOutbox.event_type == "POINTS_RECHARGED")).all()
        assert len(notifications) == 1
        assert len(outbox) == 1
        assert notifications[0].deep_link == "/h5/v12-workbench.html?view=points"
        assert outbox[0].payload["deep_link"] == "/h5/v12-workbench.html?view=points"


def test_financial_writes_require_voucher_and_preserve_business_ledger_semantics(api_client):
    client, factory = api_client
    admin = _login(client, "admin", "Admin123!")
    operation = _login(client, "operation", "Operation123!")
    package = client.get("/api/v1/points/packages", headers=admin).json()["data"][0]
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        assert company
        company_id = company.id

    recharge_payload = {
        "company_id": company_id,
        "package_id": package["id"],
        "external_reference": "BANK-P101-VOUCHER",
        "cash_amount_cents": package["cash_amount_cents"],
        "idempotency_key": "recharge-p101-voucher",
        "confirmed": True,
    }
    missing_voucher = client.post("/api/v1/points/recharge", headers=admin, json=recharge_payload)
    assert missing_voucher.status_code == 422
    blank_voucher = client.post(
        "/api/v1/points/recharge",
        headers=admin,
        json={**recharge_payload, "note": "   "},
    )
    assert blank_voucher.status_code == 422

    client.cookies.clear()
    assert client.get("/api/v1/points/packages?active_only=false").status_code == 401
    assert client.get("/api/v1/points/packages?active_only=false", headers=operation).status_code == 403
    assert client.get("/api/v1/points/packages?active_only=false", headers=admin).status_code == 200

    accepted = client.post(
        "/api/v1/points/recharge",
        headers=admin,
        json={**recharge_payload, "note": "已核对收款流水、凭证截图和到账金额"},
    )
    assert accepted.status_code == 200, accepted.text
    recharge_ledger_id = accepted.json()["data"]["id"]

    adjustment_payload = {
        "company_id": company_id,
        "point_kind": "CUSTOMER",
        "delta": 10,
        "reason": "测试人工调账原因",
        "idempotency_key": "adjust-p101-voucher",
    }
    assert client.post(
        "/api/v1/points/adjust",
        headers=admin,
        json={**adjustment_payload, "reason": "   "},
    ).status_code == 422
    assert client.post(
        f"/api/v1/points/ledgers/{recharge_ledger_id}/reverse",
        headers=admin,
        json={"reason": "   ", "idempotency_key": "reverse-p101-blank"},
    ).status_code == 422
    assert client.post("/api/v1/points/adjust", headers=operation, json=adjustment_payload).status_code == 403
    assert client.post("/api/v1/points/recharge", headers=operation, json={**recharge_payload, "note": "越权充值"}).status_code == 403
    assert client.post(
        f"/api/v1/points/ledgers/{recharge_ledger_id}/reverse",
        headers=operation,
        json={"reason": "越权冲正", "idempotency_key": "reverse-p101-operation"},
    ).status_code == 403

    with factory() as db:
        claim_ledger = change_points(
            db,
            company_id=company_id,
            delta=-10,
            ledger_type="CLAIM",
            business_type="ASSIGNMENT",
            business_id="assignment-p101-voucher",
            idempotency_key="claim-p101-voucher",
            created_by=None,
        )
        db.commit()

    protected = client.post(
        f"/api/v1/points/ledgers/{claim_ledger.id}/reverse",
        headers=admin,
        json={"reason": "不能跳过业务退回流程", "idempotency_key": "reverse-p101-claim"},
    )
    assert protected.status_code == 409
    assert protected.json()["code"] == "POINTS_REVERSAL_MANUAL_ONLY"


def test_publishing_new_package_version_closes_previous_active_version(api_client):
    client, factory = api_client
    admin = _login(client, "admin", "Admin123!")
    payload = {
        "code": "V2_VERSIONED",
        "name": "V2版本档",
        "cash_amount_cents": 2_000_000,
        "base_points": 20_000,
        "bonus_points": 2_000,
        "level_code": "V2",
        "entitlements": {"服务优先级": "优先"},
        "publish": True,
    }
    first = client.post("/api/v1/points/packages", headers=admin, json=payload)
    assert first.status_code == 200, first.text
    second = client.post(
        "/api/v1/points/packages",
        headers=admin,
        json={**payload, "name": "V2版本档升级", "bonus_points": 3_000},
    )
    assert second.status_code == 200, second.text
    assert second.json()["data"]["version"] == 2

    with factory() as db:
        versions = db.scalars(
            select(PointsPackage)
            .where(PointsPackage.code == "V2_VERSIONED")
            .order_by(PointsPackage.version)
        ).all()
        assert len(versions) == 2
        assert versions[0].expires_at is not None
        assert versions[1].expires_at is None

    active = client.get("/api/v1/points/packages", headers=admin).json()["data"]
    active_versions = [item for item in active if item["code"] == "V2_VERSIONED"]
    assert len(active_versions) == 1
    assert active_versions[0]["version"] == 2
