from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from apps.api.src.core.config import get_settings
from apps.api.src.core.database import get_db
from apps.api.src.core.models import (
    Assignment,
    AuditLog,
    Company,
    Lead,
    NotificationOutbox,
    PointsAccount,
    PointsLedger,
    ReturnRequest,
    User,
    VerificationTask,
    VerificationTemplate,
)
from apps.api.src.core.models_v12 import SupplierLeadReward
from apps.api.src.services.bootstrap import seed_reference_data
from apps.api.src.services.superadmin_bootstrap import bootstrap_superadmin


ROOT = Path(__file__).resolve().parents[3]
ROOT_PASSWORD = "E2E-Root-Only9!"


def _upgrade_to_head(database_url: str) -> None:
    previous_database_url = os.environ.get("DATABASE_URL")
    previous_app_env = os.environ.get("APP_ENV")
    try:
        os.environ["APP_ENV"] = "test"
        os.environ["DATABASE_URL"] = database_url
        get_settings.cache_clear()
        config = Config()
        config.set_main_option("script_location", str(ROOT / "migrations"))
        command.upgrade(config, "head")
    finally:
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url
        if previous_app_env is None:
            os.environ.pop("APP_ENV", None)
        else:
            os.environ["APP_ENV"] = previous_app_env
        get_settings.cache_clear()


def _database_url(tmp_path: Path) -> str:
    configured = os.environ.get("V12_E2E_DATABASE_URL", "").strip()
    if not configured:
        return f"sqlite:///{tmp_path / 'v12-production-lifecycle.db'}"
    parsed = make_url(configured)
    database_name = (parsed.database or "").lower()
    if not any(marker in database_name for marker in ("e2e", "test", "ci")):
        raise RuntimeError("V12_E2E_DATABASE_URL 必须指向名称含 e2e/test/ci 的隔离数据库")
    return configured


@pytest.fixture()
def production_lifecycle_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from apps.api.src.core import legacy_guard
    from apps.api.src.main import app, settings
    from apps.api.src.routers import auth as auth_router
    import apps.api.src.integrations.wechat as wechat_module
    import apps.api.src.services.storage as storage_module

    database_url = _database_url(tmp_path)
    _upgrade_to_head(database_url)
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, pool_pre_ping=True, connect_args=connect_args)
    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )

    with factory() as db:
        assert db.scalar(select(func.count(User.id))) == 0
        assert db.scalar(select(func.count(Company.id))) == 0
        assert db.scalar(select(func.count(Lead.id))) == 0
        template = db.scalar(
            select(VerificationTemplate).where(
                VerificationTemplate.code == "PRE_DISPATCH",
                VerificationTemplate.status == "PUBLISHED",
            )
        )
        assert template is not None
        assert template.name == "前置电销核验模板"
        bootstrap_superadmin(
            db,
            username="e2e_root",
            password=ROOT_PASSWORD,
            display_name="E2E 超级管理员",
        )
        db.commit()
        assert db.scalar(select(func.count(User.id))) == 1
        assert db.scalar(select(func.count(Company.id))) == 0
        assert db.scalar(select(func.count(Lead.id))) == 0
        assert db.scalar(select(func.count(PointsLedger.id))) == 0
        seed_reference_data(db)
        db.commit()

    storage_dir = tmp_path / "private-object-storage"
    monkeypatch.setattr(auth_router.settings, "wechat_dev_mock", True)
    # 绑定确认流程需要可构造的授权 URL，但绝不依赖真实微信凭据或外呼
    monkeypatch.setattr(wechat_module.settings, "wechat_app_id", "wx-e2e-only")
    monkeypatch.setattr(
        wechat_module.settings,
        "wechat_oauth_redirect_uri",
        "https://testserver/api/v1/auth/wechat/callback",
    )
    monkeypatch.setattr(wechat_module.settings, "wechat_oauth_scope", "snsapi_base")
    monkeypatch.setattr(legacy_guard.settings, "legacy_write_enabled", False)
    monkeypatch.setattr(storage_module.settings, "object_storage_backend", "local")
    monkeypatch.setattr(storage_module.settings, "object_storage_dir", str(storage_dir))

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    allowed_host = next(
        (host for host in settings.trusted_host_list if host and "*" not in host),
        "localhost",
    )
    client = TestClient(app, base_url=f"http://{allowed_host}")
    try:
        yield client, factory, engine.dialect.name
    finally:
        client.close()
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def _data(response):
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["code"] == "OK"
    assert payload["request_id"]
    return payload["data"]


def _login(client, username: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    _data(response)
    token = response.cookies.get("access_token")
    assert token
    return {"Authorization": f"Bearer {token}"}


def _create_internal_users(client, admin: dict[str, str]) -> None:
    specs = {
        "operation": ("E2E-Operation9!", "E2E 运营", "OPERATION"),
        "telesales": ("E2E-Telesales9!", "E2E 电销", "TELESALES"),
    }
    for username, (password, display_name, role_code) in specs.items():
        _data(
            client.post(
                "/api/v1/users",
                headers=admin,
                json={
                    "username": f"e2e_{username}",
                    "password": password,
                    "display_name": display_name,
                    "role_code": role_code,
                },
            )
        )


def _create_company(client, admin: dict[str, str], code: str, name: str) -> str:
    return _data(
        client.post(
            "/api/v1/companies",
            headers=admin,
            json={"code": code, "name": name, "level_code": "V1"},
        )
    )["id"]


def _state_from_authorization_url(authorization_url: str) -> str:
    return parse_qs(urlparse(authorization_url).query)["state"][0]


def _bind_franchise(
    client,
    admin: dict[str, str],
    company_id: str,
    *,
    openid: str,
    nickname: str,
) -> dict[str, str]:
    invite = _data(
        client.post(
            f"/api/v1/auth/companies/{company_id}/invites",
            headers=admin,
            json={"expires_hours": 24},
        )
    )
    # 负责人邀请必须在不带任何已登录平台会话的微信浏览器中打开。
    client.cookies.clear()
    confirm = _data(
        client.post(
            "/api/v1/auth/invites/confirm-start",
            json={"invite": invite["token"]},
        )
    )
    bound = _data(
        client.post(
            "/api/v1/auth/wechat/mock-callback",
            json={
                "state": _state_from_authorization_url(confirm["authorization_url"]),
                "openid": openid,
                "nickname": nickname,
            },
        )
    )
    assert bound["company_id"] == company_id
    return {"Authorization": f"Bearer {bound['token']}"}


def _create_franchise_employee(
    client,
    operation: dict[str, str],
    company_id: str,
) -> tuple[str, dict[str, str]]:
    username = "e2e_receiver_employee"
    password = "E2E-Employee9!"
    employee = _data(
        client.post(
            f"/api/v1/companies/{company_id}/accounts",
            headers=operation,
            json={
                "username": username,
                "password": password,
                "display_name": "E2E 接收员工",
                "role_code": "FRANCHISE_EMPLOYEE",
            },
        )
    )
    return employee["id"], _login(client, username, password)


def _request_profile(
    client,
    franchise: dict[str, str],
    capability_codes: tuple[str, ...],
) -> list[str]:
    for code in capability_codes:
        item = _data(
            client.post(
                "/api/v1/v1.2/company/capabilities",
                headers=franchise,
                json={"capability_code": code},
            )
        )
        assert item["review_status"] == "PENDING"
    areas = _data(
        client.put(
            "/api/v1/v1.2/company/service-areas",
            headers=franchise,
            json={
                "primary_city_code": "310000",
                "region_codes": ["310000", "310115"],
            },
        )
    )
    return [item["id"] for item in areas]


def _approve_profile(
    client,
    operation: dict[str, str],
    company_id: str,
    capability_codes: tuple[str, ...],
    area_ids: list[str],
) -> None:
    for code in capability_codes:
        item = _data(
            client.post(
                f"/api/v1/v1.2/admin/companies/{company_id}/capabilities/{code}/review",
                headers=operation,
                json={"decision": "APPROVE", "note": "E2E 资料核验通过"},
            )
        )
        assert item["active"] is True
    for area_id in area_ids:
        item = _data(
            client.post(
                f"/api/v1/v1.2/admin/service-areas/{area_id}/review",
                headers=operation,
                json={"decision": "APPROVE", "note": "E2E 服务区域核验通过"},
            )
        )
        assert item["active"] is True


def _recharge(
    client,
    super_admin: dict[str, str],
    company_id: str,
    package_id: str,
    suffix: str,
) -> int:
    ledger = _data(
        client.post(
            "/api/v1/points/recharge",
            headers=super_admin,
            json={
                "company_id": company_id,
                "package_id": package_id,
                "external_reference": f"E2E-{suffix}",
                "cash_amount_cents": 10000,
                "note": "E2E 隔离库充值",
                "idempotency_key": f"e2e-recharge-{suffix}",
                "confirmed": True,
            },
        )
    )
    return int(ledger["balance_after"])


def _submit_supplier_lead(
    client,
    supplier: dict[str, str],
    *,
    phone: str,
    label: str,
) -> str:
    draft = _data(
        client.post(
            "/api/v1/v1.2/supplier/leads",
            headers=supplier,
            json={
                "customer_name": f"E2E {label}客户",
                "phone": phone,
                "province": "上海市",
                "city": "上海市",
                "district": "浦东新区",
                "region_code": "310115",
                "category_code": "OLD_RENOVATION",
                "brand_code": "ZHONGSHU",
                "source_channel": "MANUAL",
                "need_summary": f"E2E {label}全链路验收",
                "consent_confirmed": True,
            },
        )
    )
    lead_id = draft["id"]
    submitted = _data(
        client.post(
            f"/api/v1/v1.2/supplier/leads/{lead_id}/submit",
            headers=supplier,
        )
    )
    assert submitted["lead"]["status"] == "READY_DISPATCH"
    return lead_id


def _dispatch_and_claim(
    client,
    operation: dict[str, str],
    receiver_employee: dict[str, str],
    verified_paths: set[str],
    *,
    lead_id: str,
    receiver_company_id: str,
    employee_user_id: str,
    suffix: str,
) -> tuple[str, str]:
    assignment = _data(
        client.post(
            f"/api/v1/v1.2/dispatch-pool/{lead_id}/dispatch",
            headers=operation,
            json={
                "company_id": receiver_company_id,
                "employee_user_id": employee_user_id,
                "idempotency_key": f"e2e-dispatch-{suffix}",
                "note": "E2E 人工派发",
            },
        )
    )
    replay = _data(
        client.post(
            f"/api/v1/v1.2/dispatch-pool/{lead_id}/dispatch",
            headers=operation,
            json={
                "company_id": receiver_company_id,
                "employee_user_id": employee_user_id,
                "idempotency_key": f"e2e-dispatch-{suffix}",
                "note": "E2E 人工派发重放",
            },
        )
    )
    assert replay["id"] == assignment["id"]
    verified_paths.add("duplicate_dispatch")
    before_claim = _data(
        client.get(
            f"/api/v1/v1.2/assignments/{assignment['id']}",
            headers=receiver_employee,
        )
    )
    assert before_claim["phone"] is None
    claimed = _data(
        client.post(
            f"/api/v1/v1.2/assignments/{assignment['id']}/claim",
            headers=receiver_employee,
        )
    )
    unlocked_phone = claimed["assignment"]["phone"]
    assert len(unlocked_phone) == 11
    assert "*" not in unlocked_phone
    assert claimed["idempotent"] is False
    assert claimed["reward"]["status"] == "WAITING_CLAIM"
    replay_claim = _data(
        client.post(
            f"/api/v1/v1.2/assignments/{assignment['id']}/claim",
            headers=receiver_employee,
        )
    )
    assert replay_claim["idempotent"] is True
    assert replay_claim["ledger"]["id"] == claimed["ledger"]["id"]
    verified_paths.add("duplicate_claim")
    return assignment["id"], claimed["reward"]["id"]


def _run_return_flow(
    client,
    receiver: dict[str, str],
    operation: dict[str, str],
    telesales: dict[str, str],
    telesales_user_id: str,
    *,
    assignment_id: str,
    decision: str,
) -> str:
    draft = _data(
        client.post(
            f"/api/v1/v1.2/returns/assignments/{assignment_id}/draft",
            headers=receiver,
            json={
                "reason_code": "EMPTY_NUMBER",
                "description": "E2E 客户号码为空号，申请后置核验",
            },
        )
    )
    return_id = draft["id"]
    evidence = client.post(
        f"/api/v1/v1.2/returns/{return_id}/evidence",
        headers=receiver,
        data={"evidence_type": "CHAT_SCREENSHOT"},
        files={"file": ("e2e-proof.png", b"\x89PNG\r\n\x1a\ne2e-proof", "image/png")},
    )
    assert _data(evidence)["type"] == "CHAT_SCREENSHOT"
    recording = client.post(
        f"/api/v1/v1.2/returns/{return_id}/evidence",
        headers=receiver,
        data={"evidence_type": "CALL_RECORDING"},
        files={"file": ("e2e-call.mp3", b"ID3e2e-proof", "audio/mpeg")},
    )
    assert _data(recording)["type"] == "CALL_RECORDING"
    submitted = _data(
        client.post(f"/api/v1/v1.2/returns/{return_id}/submit", headers=receiver)
    )
    assert submitted["status"] == "VERIFYING"
    task_id = submitted["verification_task_id"]
    assigned = _data(
        client.post(
            f"/api/v1/v1.2/return-verifications/tasks/{task_id}/assign",
            headers=operation,
            json={
                "assignee_user_id": telesales_user_id,
                "reason": "运营确认需要电话核验退回事实",
            },
        )
    )
    assert assigned["status"] == "ASSIGNED"
    assert assigned["due_at"] is not None
    started = _data(
        client.post(
            f"/api/v1/v1.2/return-verifications/tasks/{task_id}/start",
            headers=telesales,
        )
    )
    assert started["status"] == "IN_PROGRESS"
    verified = _data(
        client.post(
            f"/api/v1/v1.2/return-verifications/tasks/{task_id}/submit",
            headers=telesales,
            json={
                "contact_result": "CONNECTED",
                "conclusion": (
                    "SUPPORT_RETURN" if decision == "APPROVE" else "DOES_NOT_SUPPORT_RETURN"
                ),
                "note": "E2E 电销事实核验已完成",
            },
        )
    )
    assert verified["status"] == "SUBMITTED"
    reviewed = _data(
        client.post(
            f"/api/v1/v1.2/returns/{return_id}/final-review",
            headers=operation,
            json={"decision": decision, "note": f"E2E 终审{decision}"},
        )
    )
    assert reviewed["status"] == ("APPROVED" if decision == "APPROVE" else "REJECTED")
    assert reviewed["reviewed_by"]
    assert reviewed["reviewed_at"]
    assert reviewed["review_note"] == f"E2E 终审{decision}"
    assert reviewed["final_decision_reason"] == f"E2E 终审{decision}"
    return return_id


def _make_reward_due(factory, reward_id: str) -> None:
    with factory() as db:
        reward = db.get(SupplierLeadReward, reward_id)
        assert reward is not None
        due = datetime.now(timezone.utc) - timedelta(minutes=1)
        reward.reward_due_at = due
        reward.appeal_deadline_at = due
        assignment = db.get(Assignment, reward.assignment_id)
        assert assignment is not None
        assignment.reward_due_at = due
        assignment.appeal_deadline_at = due
        db.commit()


def _assert_account_reconciles(db: Session, company_id: str) -> None:
    account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == company_id))
    ledger_total = db.scalar(
        select(func.coalesce(func.sum(PointsLedger.delta), 0)).where(
            PointsLedger.company_id == company_id
        )
    )
    assert account is not None
    assert int(account.balance) == int(ledger_total or 0)


def test_v12_empty_database_to_reward_settlement_lifecycle(
    production_lifecycle_client,
) -> None:
    client, factory, dialect = production_lifecycle_client
    verified_paths: set[str] = set()
    admin = _login(client, "e2e_root", ROOT_PASSWORD)
    _create_internal_users(client, admin)
    operation = _login(client, "e2e_operation", "E2E-Operation9!")
    telesales = _login(client, "e2e_telesales", "E2E-Telesales9!")
    internal_users = _data(client.get("/api/v1/users", headers=admin))
    telesales_user_id = next(item["id"] for item in internal_users if item["username"] == "e2e_telesales")
    supplier_company_id = _create_company(client, admin, "E2E-SUPPLIER", "E2E 供应商")
    receiver_company_id = _create_company(client, admin, "E2E-RECEIVER", "E2E 接收商")
    pending_company_id = _create_company(client, admin, "E2E-PENDING", "E2E 待审接收商")
    supplier = _bind_franchise(
        client,
        admin,
        supplier_company_id,
        openid="e2e-supplier-openid",
        nickname="E2E 供应商负责人",
    )
    receiver = _bind_franchise(
        client,
        admin,
        receiver_company_id,
        openid="e2e-receiver-openid",
        nickname="E2E 接收商负责人",
    )
    receiver_employee_id, receiver_employee = _create_franchise_employee(
        client,
        operation,
        receiver_company_id,
    )
    pending = _bind_franchise(
        client,
        admin,
        pending_company_id,
        openid="e2e-pending-openid",
        nickname="E2E 待审负责人",
    )

    supplier_areas = _request_profile(
        client,
        supplier,
        ("LEAD_SUPPLIER", "LEAD_RECEIVER"),
    )
    receiver_areas = _request_profile(client, receiver, ("LEAD_RECEIVER",))
    _request_profile(client, pending, ("LEAD_RECEIVER",))
    _approve_profile(
        client,
        operation,
        supplier_company_id,
        ("LEAD_SUPPLIER", "LEAD_RECEIVER"),
        supplier_areas,
    )
    _approve_profile(
        client,
        operation,
        receiver_company_id,
        ("LEAD_RECEIVER",),
        receiver_areas,
    )

    package = _data(
        client.post(
            "/api/v1/points/packages",
            headers=admin,
            json={
                "code": "E2E-1000",
                "name": "E2E 隔离库积分包",
                "cash_amount_cents": 10000,
                "base_points": 1000,
                "bonus_points": 0,
                "level_code": "V1",
                "entitlements": {},
                "publish": True,
            },
        )
    )
    assert _recharge(
        client,
        admin,
        supplier_company_id,
        package["id"],
        "supplier",
    ) == 1000

    lead_one = _submit_supplier_lead(
        client,
        supplier,
        phone="13900001001",
        label="奖励结算",
    )
    candidate_data = _data(
        client.get(
            f"/api/v1/v1.2/dispatch-pool/{lead_one}/candidates",
            headers=operation,
        )
    )
    candidates = {item["company_id"]: item for item in candidate_data["candidates"]}
    assert candidates[supplier_company_id]["exclusion_reasons"] == ["SELF_SUPPLY_FORBIDDEN"]
    assert "POINTS_INSUFFICIENT" in candidates[receiver_company_id]["exclusion_reasons"]
    assert "RECEIVER_CAPABILITY_REQUIRED" in candidates[pending_company_id]["exclusion_reasons"]
    verified_paths.update(
        {"self_supply_forbidden", "points_insufficient", "unreviewed_capability"}
    )

    assert _recharge(
        client,
        admin,
        receiver_company_id,
        package["id"],
        "receiver",
    ) == 1000
    refreshed = _data(
        client.get(
            f"/api/v1/v1.2/dispatch-pool/{lead_one}/candidates",
            headers=operation,
        )
    )
    receiver_candidate = next(
        item for item in refreshed["candidates"] if item["company_id"] == receiver_company_id
    )
    assert receiver_candidate["eligible"] is True

    assignment_one, reward_one = _dispatch_and_claim(
        client,
        operation,
        receiver_employee,
        verified_paths,
        lead_id=lead_one,
        receiver_company_id=receiver_company_id,
        employee_user_id=receiver_employee_id,
        suffix="reward",
    )
    cross_company = client.get(
        f"/api/v1/v1.2/assignments/{assignment_one}",
        headers=supplier,
    )
    assert cross_company.status_code == 404
    verified_paths.add("cross_company_isolation")
    followup = _data(
        client.post(
            f"/api/v1/followups/assignments/{assignment_one}",
            headers=receiver_employee,
            json={"status": "CONTACTED", "note": "E2E 已联系客户"},
        )
    )
    assert followup["status"] == "CONTACTED"
    effective_confirmation = _data(
        client.post(
            f"/api/v1/followups/assignments/{assignment_one}",
            headers=receiver_employee,
            json={"status": "DEAL", "note": "E2E 电话确认客资有效"},
        )
    )
    assert effective_confirmation["status"] == "DEAL"
    _make_reward_due(factory, reward_one)
    missing_settlement_note = client.post(
        f"/api/v1/v1.2/admin/supplier-rewards/{reward_one}/settle",
        headers=admin,
    )
    assert missing_settlement_note.status_code == 422
    blank_settlement_note = client.post(
        f"/api/v1/v1.2/admin/supplier-rewards/{reward_one}/settle",
        headers=admin,
        json={"note": "   "},
    )
    assert blank_settlement_note.status_code == 422
    first_settlement = _data(
        client.post(
            f"/api/v1/v1.2/admin/supplier-rewards/{reward_one}/settle",
            headers=admin,
            json={"note": "E2E 核对奖励结算条件"},
        )
    )
    repeated_settlement = _data(
        client.post(
            f"/api/v1/v1.2/admin/supplier-rewards/{reward_one}/settle",
            headers=admin,
            json={"note": "E2E 重复结算幂等核验"},
        )
    )
    assert first_settlement["status"] == "SETTLED"
    assert repeated_settlement["ledger_id"] == first_settlement["ledger_id"]

    lead_two = _submit_supplier_lead(
        client,
        supplier,
        phone="13900001002",
        label="退回通过",
    )
    assignment_two, reward_two = _dispatch_and_claim(
        client,
        operation,
        receiver_employee,
        verified_paths,
        lead_id=lead_two,
        receiver_company_id=receiver_company_id,
        employee_user_id=receiver_employee_id,
        suffix="return-approved",
    )
    return_approved = _run_return_flow(
        client,
        receiver_employee,
        operation,
        telesales,
        telesales_user_id,
        assignment_id=assignment_two,
        decision="APPROVE",
    )

    lead_three = _submit_supplier_lead(
        client,
        supplier,
        phone="13900001003",
        label="退回驳回",
    )
    assignment_three, reward_three = _dispatch_and_claim(
        client,
        operation,
        receiver_employee,
        verified_paths,
        lead_id=lead_three,
        receiver_company_id=receiver_company_id,
        employee_user_id=receiver_employee_id,
        suffix="return-rejected",
    )
    return_rejected = _run_return_flow(
        client,
        receiver_employee,
        operation,
        telesales,
        telesales_user_id,
        assignment_id=assignment_three,
        decision="REJECT",
    )
    resumed_confirmation = _data(
        client.post(
            f"/api/v1/followups/assignments/{assignment_three}",
            headers=receiver_employee,
            json={"status": "DEAL", "note": "E2E 退回复核可用后电话确认有效"},
        )
    )
    assert resumed_confirmation["status"] == "DEAL"
    _make_reward_due(factory, reward_three)
    rejected_reward_settlement = _data(
        client.post(
            f"/api/v1/v1.2/admin/supplier-rewards/{reward_three}/settle",
            headers=admin,
            json={"note": "E2E 冻结奖励结算核验"},
        )
    )
    assert rejected_reward_settlement["status"] == "SETTLED"

    lead_four = _submit_supplier_lead(
        client,
        supplier,
        phone="13900001004",
        label="超期申诉",
    )
    assignment_four, _ = _dispatch_and_claim(
        client,
        operation,
        receiver_employee,
        verified_paths,
        lead_id=lead_four,
        receiver_company_id=receiver_company_id,
        employee_user_id=receiver_employee_id,
        suffix="expired-return",
    )
    with factory() as db:
        assignment = db.get(Assignment, assignment_four)
        assert assignment is not None
        assignment.appeal_deadline_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    expired_return = client.post(
        f"/api/v1/v1.2/returns/assignments/{assignment_four}/draft",
        headers=receiver_employee,
        json={
            "reason_code": "EMPTY_NUMBER",
            "description": "E2E 超过三工作日后不得创建申诉",
        },
    )
    assert expired_return.status_code == 409
    assert expired_return.json()["code"] == "RETURN_WINDOW_EXPIRED"
    verified_paths.add("expired_return")

    legacy_write = client.post("/api/v1/leads", headers=operation, json={})
    assert legacy_write.status_code == 410
    assert legacy_write.json()["code"] == "LEGACY_WRITE_DISABLED"
    verified_paths.add("legacy_write_disabled")

    trace = _data(
        client.get(f"/api/v1/v1.2/trace/{lead_two}", headers=operation)
    )
    assert trace["lead"]["id"] == lead_two
    assert trace["lead"]["customer_name"]
    assert trace["lead"]["phone_masked"]
    assert assignment_two in {item["id"] for item in trace["assignments"]}
    assert return_approved in {item["id"] for item in trace["returns"]}
    assert reward_two in {item["id"] for item in trace["supplier_rewards"]}
    assert trace["assignments"][0]["receiver_company_name"]
    assert trace["returns"][0]["description"]
    assert "evidence_summary" in trace["returns"][0]
    assert "followups" in trace
    assert all("actor_name" in item for item in trace["audit_events"])
    assert all("summary" in item for item in trace["timeline"])

    with factory() as db:
        reward_rows = {
            item.id: item
            for item in db.scalars(
                select(SupplierLeadReward).where(
                    SupplierLeadReward.id.in_([reward_one, reward_two, reward_three])
                )
            ).all()
        }
        assert reward_rows[reward_one].status == "SETTLED"
        assert reward_rows[reward_two].status == "CANCELLED"
        assert reward_rows[reward_three].status == "SETTLED"
        assert db.get(ReturnRequest, return_approved).status == "APPROVED"
        assert db.get(ReturnRequest, return_rejected).status == "REJECTED"
        assert db.get(Assignment, assignment_two).status == "RETURNED"
        assert db.get(Lead, lead_two).status == "CLOSED"
        assert db.get(Assignment, assignment_three).status == "COMPLETED"
        _assert_account_reconciles(db, supplier_company_id)
        _assert_account_reconciles(db, receiver_company_id)
        reward_ledgers = db.scalars(
            select(PointsLedger).where(
                PointsLedger.business_type == "V12_SUPPLIER_REWARD"
            )
        ).all()
        assert {item.business_id for item in reward_ledgers} == {reward_one, reward_three}
        assert db.scalar(select(func.count(NotificationOutbox.id))) >= 1
        assert db.scalar(select(func.count(AuditLog.id))) >= 30
        # 资料完整的供客直接进入派发池；仅两笔退回申诉产生电销核验任务。
        assert db.scalar(select(func.count(VerificationTask.id))) == 2
        assert db.scalar(
            select(func.count(VerificationTask.id)).where(
                VerificationTask.task_type == "PRE_DISPATCH_VERIFY"
            )
        ) == 0
        assert db.scalar(
            select(func.count(VerificationTask.id)).where(
                VerificationTask.task_type == "RETURN_VERIFY"
            )
        ) == 2
        assert db.scalar(select(func.count(User.id))) == 7
        expected_negative_paths = {
            "cross_company_isolation",
            "points_insufficient",
            "self_supply_forbidden",
            "duplicate_dispatch",
            "duplicate_claim",
            "expired_return",
            "unreviewed_capability",
            "legacy_write_disabled",
        }
        assert verified_paths == expected_negative_paths
        report = {
            "valid": True,
            "database_dialect": dialect,
            "schema_source": "alembic",
            "demo_seed_loaded": False,
            "legacy_write_enabled": False,
            "business": {
                "companies": db.scalar(select(func.count(Company.id))),
                "leads": db.scalar(select(func.count(Lead.id))),
                "assignments": db.scalar(select(func.count(Assignment.id))),
                "returns": db.scalar(select(func.count(ReturnRequest.id))),
                "rewards": db.scalar(select(func.count(SupplierLeadReward.id))),
                "points_ledgers": db.scalar(select(func.count(PointsLedger.id))),
                "audit_logs": db.scalar(select(func.count(AuditLog.id))),
                "notification_outbox": db.scalar(select(func.count(NotificationOutbox.id))),
            },
            "negative_paths": sorted(verified_paths),
            "return_paths": ["approved_refund", "rejected_reward_restored"],
            "settled_reward_ids": [reward_one, reward_three],
            "cancelled_reward_id": reward_two,
        }

    evidence_path = os.environ.get("V12_E2E_EVIDENCE_PATH", "").strip()
    if evidence_path:
        target = Path(evidence_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    assert inspect(factory.kw["bind"]).has_table("alembic_version")
