from __future__ import annotations

from pathlib import Path


def _login(client, username: str, password: str) -> None:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text


def test_role_entrypoints_use_only_v12_role_workbenches(api_client) -> None:
    client, _ = api_client

    anonymous_call = client.get("/h5/call/", follow_redirects=False)
    assert anonymous_call.status_code == 302
    assert anonymous_call.headers["location"] == "/h5/call/index.html"
    legacy_call = client.get("/call/", follow_redirects=False)
    assert legacy_call.status_code == 302
    assert legacy_call.headers["location"] == "/h5/call/"
    legacy_detail = client.get("/call/task/legacy-id", follow_redirects=False)
    assert legacy_detail.status_code == 302
    assert legacy_detail.headers["location"] == "/h5/call/index.html#/invalid-link"
    legacy_supplier = client.get("/h5/supplier.html", follow_redirects=False)
    assert legacy_supplier.status_code == 302
    assert legacy_supplier.headers["location"] == "/h5/v12-workbench.html?view=leads&id=supply"
    legacy_platform_leads = client.get("/admin/v12-leads.html", follow_redirects=False)
    assert legacy_platform_leads.status_code == 302
    assert legacy_platform_leads.headers["location"] == "/admin/v12-operations.html?view=leads"
    legacy_platform_detail = client.get(
        "/admin/v12-leads.html?id=lead-123&status=DRAFT&source=PLATFORM_MANUAL",
        follow_redirects=False,
    )
    assert legacy_platform_detail.status_code == 302
    assert legacy_platform_detail.headers["location"] == (
        "/admin/v12-operations.html?view=leads&id=lead-123&status=DRAFT&source=PLATFORM_MANUAL"
    )

    _login(client, "telesales", "Telesales123!")
    telesales_h5 = client.get("/h5/", follow_redirects=False)
    assert telesales_h5.headers["location"] == "/h5/call/"
    telesales_admin = client.get("/admin/", follow_redirects=False)
    assert telesales_admin.headers["location"] == "/h5/call/"

    client.post("/api/v1/auth/logout")
    _login(client, "operation", "Operation123!")
    operation_h5 = client.get("/h5/", follow_redirects=False)
    assert operation_h5.headers["location"] == "/h5/admin/"
    operation_admin = client.get("/h5/admin/", follow_redirects=False)
    assert operation_admin.status_code == 200
    assert "平台工作台" in operation_admin.text
    assert "./app.js?v=20260913-reward-48h-modal" in operation_admin.text
    operation_call = client.get("/h5/call/", follow_redirects=False)
    assert operation_call.headers["location"] == "/h5/admin/"

    client.post("/api/v1/auth/logout")
    _login(client, "franchise_demo", "Franchise123!")
    franchise_h5 = client.get("/h5/", follow_redirects=False)
    assert franchise_h5.headers["location"] == "/h5/v12-workbench.html"
    franchise_admin = client.get("/admin/", follow_redirects=False)
    assert franchise_admin.headers["location"] == "/h5/v12-workbench.html"


def test_platform_lead_work_is_migrated_into_operations_without_legacy_page() -> None:
    operations_source = Path("apps/admin/public/v12-operations.js").read_text(encoding="utf-8")

    assert "/v1.2/platform/leads" in operations_source
    assert "data-platform-pre-dispatch" in operations_source
    assert "运营补充资料后再处理" in operations_source
    assert "async function openLeadDetail(id)" in operations_source
    assert "await platformDetail(id)" in operations_source
    assert "await reviewDetail(id)" in operations_source
    assert not Path("apps/admin/public/v12-leads.html").exists()
    assert not Path("apps/admin/public/v12-leads.js").exists()
    assert not Path("apps/admin/public/v12-leads.css").exists()
