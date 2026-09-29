from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import inspect, select

from apps.api.src.core.auth import Principal
from apps.api.src.core.errors import AppError
from apps.api.src.core.models import Company, Lead, Region, User
from apps.api.src.core.security import encrypt_text, hash_phone
from apps.api.src.core.v12_enums import LeadSourceKind, LeadV12Status
from apps.api.src.services.company_profile_v12 import request_capability, review_capability
from apps.api.src.services.lead_supply_v12 import create_draft, submit_draft
from apps.api.src.services.phone_uniqueness import hash_customer_wechat
from apps.api.src.services.pre_dispatch_v12 import assign_pre_dispatch_task
from apps.api.tests.test_pre_dispatch_verification_http import _login


TASKS_API = "/api/v1/v1.2/pre-dispatch-verifications/tasks"
WECHAT_EXISTS_API = "/api/v1/v1.2/pre-dispatch-verifications/customer-wechat-exists"


def _principal(user_id: str, company_id: str | None = None, *permissions: str) -> Principal:
    return Principal(
        user_id=user_id,
        display_name="9.29 测试用户",
        company_id=company_id,
        role_codes=frozenset(),
        permission_codes=frozenset(permissions),
        session_version=1,
    )


def _identity(db, code: str) -> tuple[Company, User]:
    company = Company(code=code, name=f"测试公司-{code}", status="ACTIVE")
    db.add(company)
    db.flush()
    user = User(display_name="9.29 测试用户", status="ACTIVE", company_id=company.id)
    db.add(user)
    db.flush()
    return company, user


def _approve_supplier(db, company: Company, user: User) -> None:
    request_capability(db, company.id, "LEAD_SUPPLIER")
    review_capability(
        db,
        company_id=company.id,
        capability_code="LEAD_SUPPLIER",
        approve=True,
        reviewed_by=user.id,
    )


def test_wechat_only_lead_can_be_formally_submitted(db) -> None:
    db.add(Region(code="420100", name="武汉市", level="CITY", aliases=[], active=True))
    _, user = _identity(db, "WECHAT-ONLY")
    principal = _principal(user.id, None, "lead.manual.manage")

    lead = create_draft(
        db,
        principal=principal,
        source_kind=LeadSourceKind.PLATFORM_MANUAL,
        values={
            "customer_name": "仅微信客户",
            "phone": "",
            "customer_wechat": "  Wx_Customer-929  ",
            "city": "武汉市",
            "region_code": "420100",
            "need_summary": "仅通过客户微信联系",
            "consent_confirmed": True,
        },
    )

    result = submit_draft(db, lead=lead, principal=principal)

    assert result.decision.value == "CLEAR"
    assert lead.status == LeadV12Status.PUBLIC_POOL.value
    assert lead.phone_encrypted is None
    assert lead.phone_hash is None
    assert lead.phone_tail4 is None
    assert lead.customer_wechat_encrypted
    assert lead.customer_wechat_hash


def test_customer_wechat_is_unique_case_insensitively(db) -> None:
    _, user = _identity(db, "WECHAT-UNIQUE")
    principal = _principal(user.id, None, "lead.manual.manage")
    create_draft(
        db,
        principal=principal,
        source_kind=LeadSourceKind.PLATFORM_MANUAL,
        values={"customer_wechat": "Wx_Unique_929"},
    )

    with pytest.raises(AppError) as exc_info:
        create_draft(
            db,
            principal=principal,
            source_kind=LeadSourceKind.PLATFORM_MANUAL,
            values={"customer_wechat": "wx_unique_929"},
        )

    assert exc_info.value.code == "LEAD_WECHAT_DUPLICATE"


def test_wechat_only_lead_can_be_assigned_and_seen_within_own_tasks(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        operation = db.scalar(select(User).where(User.username == "operation"))
        telesales = db.scalar(select(User).where(User.username == "telesales"))
        other = db.scalar(select(User).where(User.username == "franchise_demo"))
        assert operation is not None and telesales is not None and other is not None

        company, supplier = _identity(db, "WECHAT-TASK")
        _approve_supplier(db, company, supplier)
        lead = create_draft(
            db,
            principal=_principal(supplier.id, company.id, "supplier.lead.manage"),
            source_kind=LeadSourceKind.SUPPLIER_H5,
            values={
                "customer_name": "微信核验客户",
                "customer_wechat": "wx_task_929",
                "consent_confirmed": True,
            },
        )
        submit_draft(
            db,
            lead=lead,
            principal=_principal(supplier.id, company.id, "supplier.lead.manage"),
        )
        assigned = assign_pre_dispatch_task(
            db,
            lead_id=lead.id,
            assignee_user_id=telesales.id,
            assigned_by=operation.id,
            reason="客户只有微信号，转电销在线核验",
        )

        other_lead = Lead(
            source_type="PLATFORM_MANUAL",
            source_kind="PLATFORM_MANUAL",
            customer_name="其他人的同类任务",
            customer_wechat_encrypted=lead.customer_wechat_encrypted,
            customer_wechat_hash=lead.customer_wechat_hash,
            consent_confirmed=True,
            status=LeadV12Status.PENDING_TELESALES_VERIFY.value,
            review_status="PENDING",
            raw_payload={},
        )
        db.add(other_lead)
        db.flush()
        from apps.api.src.core.models import VerificationTask

        other_task = VerificationTask(
            lead_id=other_lead.id,
            task_type="PRE_DISPATCH_VERIFY",
            status="ASSIGNED",
            assignee_user_id=other.id,
        )
        db.add(other_task)
        db.commit()
        task_id = assigned.task.id
        other_task_id = other_task.id

    _login(client, "telesales", "Telesales123!")
    response = client.get(TASKS_API, params={"keyword": "微信核验客户"})
    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]
    assert {item["id"] for item in items} == {task_id}
    assert other_task_id not in {item["id"] for item in items}
    assert items[0]["lead"]["customer_wechat"] is None
    assert items[0]["lead"]["customer_wechat_masked"]

    started = client.post(f"{TASKS_API}/{task_id}/start")
    assert started.status_code == 200, started.text
    assert started.json()["data"]["lead"]["customer_wechat"] == "wx_task_929"


def test_telesales_can_check_customer_wechat_exists_without_lead_details(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        lead = Lead(
            source_type="PLATFORM_MANUAL",
            source_kind="PLATFORM_MANUAL",
            customer_name="其他人负责的客户",
            customer_wechat_encrypted=encrypt_text("wx_global_929"),
            customer_wechat_hash=hash_customer_wechat("wx_global_929"),
            consent_confirmed=True,
            status=LeadV12Status.PENDING_TELESALES_VERIFY.value,
            review_status="PENDING",
            raw_payload={},
        )
        db.add(lead)
        db.commit()

    _login(client, "telesales", "Telesales123!")
    found = client.post(WECHAT_EXISTS_API, json={"customer_wechat": "WX_GLOBAL_929"})
    assert found.status_code == 200, found.text
    assert found.json()["data"] == {"exists": True}
    assert "其他人负责的客户" not in found.text
    missing = client.post(WECHAT_EXISTS_API, json={"customer_wechat": "wx_missing_929"})
    assert missing.status_code == 200, missing.text
    assert missing.json()["data"] == {"exists": False}
    assert client.post(WECHAT_EXISTS_API, json={"customer_wechat": "  "}).status_code == 422

    _login(client, "franchise_demo", "Franchise123!")
    assert client.post(WECHAT_EXISTS_API, json={"customer_wechat": "wx_global_929"}).status_code == 403


def test_telesales_workbench_shows_wechat_and_only_offers_dial_for_phone() -> None:
    source = Path("apps/call-h5/public/app.js").read_text(encoding="utf-8")
    assert "pre-dispatch-verifications/customer-wechat-exists" in source
    assert 'id="wechat-exists-search"' in source
    assert 'lead.customer_wechat || lead.customer_wechat_masked' in source
    assert "canContact && lead.phone" in source


def test_customer_wechat_migration_is_reversible(db, monkeypatch) -> None:
    path = Path(__file__).resolve().parents[3] / "migrations/versions/0029_lead_customer_wechat.py"
    spec = importlib.util.spec_from_file_location("lead_customer_wechat_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    monkeypatch.setattr(module, "op", Operations(MigrationContext.configure(db.connection())))
    module.upgrade()
    columns = {item["name"]: item for item in inspect(db.connection()).get_columns("leads")}
    assert columns["phone_encrypted"]["nullable"] is True
    assert columns["phone_hash"]["nullable"] is True
    assert "customer_wechat_encrypted" in columns
    assert "customer_wechat_hash" in columns

    module.downgrade()
    columns = {item["name"]: item for item in inspect(db.connection()).get_columns("leads")}
    assert "customer_wechat_encrypted" not in columns
    assert "customer_wechat_hash" not in columns
    assert columns["phone_encrypted"]["nullable"] is False
    assert columns["phone_hash"]["nullable"] is False


def test_customer_wechat_migration_refuses_lossy_downgrade(db, monkeypatch) -> None:
    path = Path(__file__).resolve().parents[3] / "migrations/versions/0029_lead_customer_wechat.py"
    spec = importlib.util.spec_from_file_location("lead_customer_wechat_migration_lossy", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    monkeypatch.setattr(module, "op", Operations(MigrationContext.configure(db.connection())))
    db.add(Lead(
        source_type="PLATFORM_MANUAL",
        source_kind="PLATFORM_MANUAL",
        customer_name="只有微信号的客户",
        phone_encrypted=encrypt_text(""),
        phone_hash=hash_phone(""),
        customer_wechat_encrypted=encrypt_text("wx-preserve-929"),
        customer_wechat_hash=hash_customer_wechat("wx-preserve-929"),
        status=LeadV12Status.DRAFT.value,
        raw_payload={},
    ))
    db.flush()

    with pytest.raises(RuntimeError, match="客户微信号"):
        module.downgrade()
    assert "customer_wechat_encrypted" in {item["name"] for item in inspect(db.connection()).get_columns("leads")}
