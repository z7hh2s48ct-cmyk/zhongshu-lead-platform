from __future__ import annotations

from sqlalchemy import func, select

from apps.api.src.core.models import AuditLog, User


STRONG_PASSWORD = "Internal-User9!"


def _login(client, username: str, password: str) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    token = response.cookies.get("access_token")
    assert token
    return token


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _data(response):
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["code"] == "OK"
    return payload["data"]


def _create_internal_user(client, admin_token: str, username: str) -> dict:
    return _data(
        client.post(
            "/api/v1/users",
            headers=_bearer(admin_token),
            json={
                "username": username,
                "password": STRONG_PASSWORD,
                "display_name": "原始姓名",
                "role_codes": ["OPERATION"],
            },
        )
    )


def _name_audits(factory, user_id: str) -> list[AuditLog]:
    with factory() as db:
        return list(
            db.scalars(
                select(AuditLog)
                .where(
                    AuditLog.resource_id == user_id,
                    AuditLog.action == "USER_DISPLAY_NAME_UPDATE",
                )
                .order_by(AuditLog.created_at)
            ).all()
        )


def test_superadmin_updates_internal_user_display_name_and_audits_once(api_client) -> None:
    client, factory = api_client
    admin_token = _login(client, "admin", "Admin123!")
    created = _create_internal_user(client, admin_token, "rename_target")

    response = client.patch(
        f"/api/v1/users/{created['id']}",
        headers=_bearer(admin_token),
        json={"display_name": "新姓名"},
    )

    updated = _data(response)
    assert updated["display_name"] == "新姓名"
    assert updated["id"] == created["id"]
    assert updated["username"] == created["username"]
    assert updated["roles"] == created["roles"]
    assert updated["company_id"] == created["company_id"]
    assert updated["status"] == created["status"]
    assert updated["session_version"] == created["session_version"]

    with factory() as db:
        user = db.get(User, created["id"])
        assert user is not None
        assert user.display_name == "新姓名"
        assert user.username == "rename_target"
        assert user.company_id is None
        assert user.status == "ACTIVE"
        assert user.session_version == created["session_version"]
        assert sorted(role.code for role in user.roles) == ["OPERATION"]

    audits = _name_audits(factory, created["id"])
    assert len(audits) == 1
    assert audits[0].before_json == {"display_name": "原始姓名"}
    assert audits[0].after_json == {"display_name": "新姓名"}

    _data(
        client.patch(
            f"/api/v1/users/{created['id']}",
            headers=_bearer(admin_token),
            json={"display_name": "新姓名"},
        )
    )
    assert len(_name_audits(factory, created["id"])) == 1


def test_display_name_update_rejects_invalid_or_extra_fields(api_client) -> None:
    client, factory = api_client
    admin_token = _login(client, "admin", "Admin123!")
    created = _create_internal_user(client, admin_token, "rename_validation")

    for body in (
        {"display_name": ""},
        {"display_name": "   "},
        {"display_name": "x" * 65},
        {"display_name": "新姓名", "username": "changed_username"},
    ):
        response = client.patch(
            f"/api/v1/users/{created['id']}",
            headers=_bearer(admin_token),
            json=body,
        )
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "VALIDATION_ERROR"

    with factory() as db:
        user = db.get(User, created["id"])
        assert user is not None
        assert user.display_name == "原始姓名"
        audit_count = db.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.resource_id == created["id"],
                AuditLog.action == "USER_DISPLAY_NAME_UPDATE",
            )
        )
        assert audit_count == 0


def test_only_superadmin_can_update_internal_user_display_name(api_client) -> None:
    client, _ = api_client
    admin_token = _login(client, "admin", "Admin123!")
    operation_token = _login(client, "operation", "Operation123!")
    created = _create_internal_user(client, admin_token, "rename_forbidden")

    response = client.patch(
        f"/api/v1/users/{created['id']}",
        headers=_bearer(operation_token),
        json={"display_name": "越权修改"},
    )

    assert response.status_code == 403


def test_display_name_update_uses_internal_account_scope(api_client) -> None:
    client, factory = api_client
    admin_token = _login(client, "admin", "Admin123!")
    with factory() as db:
        franchise_user = db.scalar(select(User).where(User.username == "franchise_demo"))
        assert franchise_user is not None
        franchise_user_id = franchise_user.id

    response = client.patch(
        f"/api/v1/users/{franchise_user_id}",
        headers=_bearer(admin_token),
        json={"display_name": "不应更新"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "INTERNAL_USER_REQUIRED"
