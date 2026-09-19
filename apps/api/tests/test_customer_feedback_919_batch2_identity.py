"""2026-09-19 S9：供资方与接收方身份互相隐藏（供客积分接口）。"""

from __future__ import annotations

from apps.api.src.core.models import Company, User
from apps.api.src.services.followup_service import add_followup
from apps.api.src.services.rbac import assign_role
from apps.api.tests.test_v12_return_workflow import (
    _principal,
    _submit_and_verify,
    _workflow_setup,
)


def _identity_case(db):
    setup = _workflow_setup(db)
    supplier_user = User(
        display_name="供资负责人",
        status="ACTIVE",
        company_id=setup["supplier"].id,
    )
    db.add(supplier_user)
    db.flush()
    assign_role(db, supplier_user, "FRANCHISE_OWNER")
    # 触发一次提前确认，让奖励进入可展示状态。
    add_followup(
        db,
        assignment=setup["assignment"],
        principal=_principal(setup["receiver_user"], "followup.own.manage"),
        status="DEAL",
        note="客户确认有效，提前确认完成",
        next_followup_at=None,
    )
    db.commit()
    return setup, supplier_user


def test_supplier_view_hides_receiver_identity(api_client):
    from apps.api.src.core.auth import get_current_principal
    from apps.api.src.main import app

    client, factory = api_client
    with factory() as db:
        setup, supplier_user = _identity_case(db)
        supplier = db.get(Company, setup["supplier"].id)
        supplier_name = supplier.name
        receiver_name = db.get(Company, setup["receiver"].id).name
        principal = _principal(supplier_user, "supplier.reward.own.read")
    app.dependency_overrides[get_current_principal] = lambda: principal
    try:
        listed = client.get("/api/v1/v1.2/supplier-rewards")
        assert listed.status_code == 200, listed.text
        items = listed.json()["data"]["items"]
        assert items, "应能查询到本公司的供客奖励"
        assert items[0]["receiver_company_name"] is None
        assert items[0]["receiver_company_id"] is None
        assert items[0]["supplier_company_name"] == supplier_name
        # 客户信息按既有权限正常返回，不受身份隐藏影响。
        assert items[0]["customer_name"]

        reward_id = items[0]["id"]
        detail = client.get(f"/api/v1/v1.2/supplier-rewards/{reward_id}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["data"]["receiver_company_name"] is None
        assert detail.json()["data"]["receiver_company_name"] != receiver_name
    finally:
        app.dependency_overrides.pop(get_current_principal, None)


def test_platform_viewer_keeps_full_counterparty_trace(api_client):
    from apps.api.src.core.auth import get_current_principal
    from apps.api.src.main import app

    client, factory = api_client
    with factory() as db:
        setup, _ = _identity_case(db)
        reviewer = setup["reviewer"]
        receiver_name = db.get(Company, setup["receiver"].id).name
        principal = _principal(reviewer, "*")
    app.dependency_overrides[get_current_principal] = lambda: principal
    try:
        listed = client.get("/api/v1/v1.2/supplier-rewards")
        assert listed.status_code == 200, listed.text
        items = listed.json()["data"]["items"]
        assert items
        assert items[0]["receiver_company_name"] == receiver_name
    finally:
        app.dependency_overrides.pop(get_current_principal, None)
