from __future__ import annotations

from sqlalchemy import select

from apps.api.src.core.models import AuditLog, Company


def _login(client, username: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    token = response.cookies.get("access_token")
    assert token
    return {"Authorization": f"Bearer {token}"}


def _data(response):
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["code"] == "OK"
    return payload["data"]


def _create_company(client, admin: dict[str, str]) -> str:
    return _data(
        client.post(
            "/api/v1/companies/simple",
            headers=admin,
            json={
                "name": "账号管理验收加盟商",
                "owner_name": "赵负责人",
                "primary_city_code": "310000",
                "serve_all_districts": True,
            },
        )
    )["id"]


def test_operation_manages_company_accounts_without_receiving_personal_assignment_details(
    api_client,
) -> None:
    client, _ = api_client
    admin = _login(client, "admin", "Admin123!")
    operation = _login(client, "operation", "Operation123!")
    company_id = _create_company(client, admin)

    company_page = _data(client.get("/api/v1/companies?page=1&page_size=20", headers=operation))
    managed_company = next(item for item in company_page["items"] if item["id"] == company_id)
    assert managed_company["name"] == "账号管理验收加盟商"
    assert managed_company["points_balance"] == 0
    assert managed_company["assignment_summary"] == {"total": 0, "by_status": {}}
    assert "internal_assignee_user_id" not in str(managed_company["assignment_summary"])

    owner = _data(
        client.post(
            f"/api/v1/companies/{company_id}/accounts",
            headers=operation,
            json={
                "username": "zhao_owner",
                "display_name": "赵负责人",
                "role_code": "FRANCHISE_OWNER",
            },
        )
    )
    assert owner["role_code"] == "FRANCHISE_OWNER"
    assert len(owner["initial_password"]) == 8
    assert owner["initial_password"].isalnum()

    employee = _data(
        client.post(
            f"/api/v1/companies/{company_id}/accounts",
            headers=operation,
            json={
                "username": "zhao_employee",
                "password": "simple88",
                "display_name": "赵员工",
                "role_code": "FRANCHISE_EMPLOYEE",
            },
        )
    )
    assert "initial_password" not in employee

    employee_session = _login(client, "zhao_employee", "simple88")
    listed = _data(
        client.get(
            f"/api/v1/companies/{company_id}/accounts",
            headers=operation,
        )
    )
    assert [(item["username"], item["role_code"]) for item in listed] == [
        ("zhao_owner", "FRANCHISE_OWNER"),
        ("zhao_employee", "FRANCHISE_EMPLOYEE"),
    ]
    assert all("assignment" not in item for item in listed)

    disabled = _data(
        client.post(
            f"/api/v1/companies/{company_id}/accounts/{employee['id']}/disable",
            headers=operation,
            json={},
        )
    )
    assert disabled["status"] == "DISABLED"
    expired = client.get("/api/v1/auth/me", headers=employee_session)
    assert expired.status_code == 401

    protected = client.post(
        f"/api/v1/companies/{company_id}/accounts/{owner['id']}/disable",
        headers=operation,
        json={},
    )
    assert protected.status_code == 409
    assert protected.json()["code"] == "COMPANY_PRIMARY_ACCOUNT_PROTECTED"


def test_superadmin_company_account_operations_require_reason_and_leave_audit_trail(
    api_client,
) -> None:
    client, factory = api_client
    admin = _login(client, "admin", "Admin123!")
    operation = _login(client, "operation", "Operation123!")
    company_id = _create_company(client, admin)
    owner = _data(
        client.post(
            f"/api/v1/companies/{company_id}/accounts",
            headers=operation,
            json={
                "username": "audit_owner",
                "password": "simple88",
                "display_name": "审计负责人",
                "role_code": "FRANCHISE_OWNER",
            },
        )
    )

    missing_reason = client.post(
        f"/api/v1/companies/{company_id}/accounts",
        headers=admin,
        json={
            "username": "audit_employee",
            "password": "simple88",
            "display_name": "审计员工",
            "role_code": "FRANCHISE_EMPLOYEE",
        },
    )
    assert missing_reason.status_code == 422
    assert missing_reason.json()["code"] == "SUPER_ADMIN_REASON_REQUIRED"

    created = _data(
        client.post(
            f"/api/v1/companies/{company_id}/accounts",
            headers=admin,
            json={
                "username": "audit_employee",
                "password": "simple88",
                "display_name": "审计员工",
                "role_code": "FRANCHISE_EMPLOYEE",
                "reason": "应加盟商负责人书面申请开通账号",
            },
        )
    )
    _data(
        client.post(
            f"/api/v1/companies/{company_id}/accounts/{created['id']}/disable",
            headers=admin,
            json={"reason": "员工离岗，停止访问权限"},
        )
    )

    with factory() as db:
        company = db.get(Company, company_id)
        assert company is not None
        audits = db.scalars(
            select(AuditLog)
            .where(
                AuditLog.company_id == company_id,
                AuditLog.action.in_({"COMPANY_ACCOUNT_CREATE", "COMPANY_ACCOUNT_DISABLE"}),
                AuditLog.actor_role_codes == ["SUPER_ADMIN"],
            )
            .order_by(AuditLog.created_at.asc())
        ).all()
        assert [audit.action for audit in audits] == [
            "COMPANY_ACCOUNT_CREATE",
            "COMPANY_ACCOUNT_DISABLE",
        ]
        assert all(audit.actor_role_codes == ["SUPER_ADMIN"] for audit in audits)
        assert audits[0].before_json is None
        assert audits[0].after_json["username"] == "audit_employee"
        assert audits[0].metadata_json["reason"] == "应加盟商负责人书面申请开通账号"
        assert audits[1].before_json["status"] == "ACTIVE"
        assert audits[1].after_json["status"] == "DISABLED"
        assert audits[1].metadata_json["reason"] == "员工离岗，停止访问权限"
        assert all("password" not in str(audit.metadata_json).lower() for audit in audits)
