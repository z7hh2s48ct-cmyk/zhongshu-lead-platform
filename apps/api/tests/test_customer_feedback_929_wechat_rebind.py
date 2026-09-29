import pytest
from urllib.parse import parse_qs, urlparse
from sqlalchemy import select

from apps.api.src.core.errors import AppError
from apps.api.src.core.models import Company, InviteToken, User, WechatIdentity
from apps.api.src.schemas.company import CompanyCreateBody
from apps.api.src.services.auth_service import (
    bind_wechat_by_invite,
    create_company_invite,
    create_owner_wechat_rebind_invite,
    login_or_bind_wechat,
)
from apps.api.src.services.company_service import create_company


def _bound_company(db):
    company = create_company(db, CompanyCreateBody(
        code="REBIND929", name="换绑测试加盟商", owner_name="负责人",
        region_codes=["310100"],
        capabilities=[{"category_code": "OLD_RENOVATION", "brand_code": None}],
    ))
    _, raw, _ = create_company_invite(db, company.id, None, 24)
    owner, _ = bind_wechat_by_invite(db, raw, "old-openid-929", "负责人")
    db.commit()
    return company, owner


def test_owner_changes_wechat_without_changing_account_or_company(db) -> None:
    company, owner = _bound_company(db)
    old_identity = db.scalar(select(WechatIdentity).where(WechatIdentity.user_id == owner.id))
    assert old_identity is not None
    old_openid = old_identity.openid
    old_version = owner.session_version

    invite, raw, _ = create_owner_wechat_rebind_invite(
        db, company.id, created_by=owner.id, expires_hours=72
    )
    result_user, _ = login_or_bind_wechat(
        db, openid="new-openid-929", invite_token=raw, expected_company_id=company.id
    )
    db.flush()

    assert result_user.id == owner.id
    assert company.primary_user_id == owner.id
    assert company.status == "ACTIVE"
    assert owner.session_version == old_version + 1
    assert old_identity.openid == "new-openid-929"
    assert db.scalar(select(WechatIdentity).where(WechatIdentity.openid == old_openid)) is None
    assert db.get(InviteToken, invite.id).used_by_user_id == owner.id


def test_rebind_refuses_wechat_already_owned_by_someone_else(db) -> None:
    company, owner = _bound_company(db)
    _, raw, _ = create_owner_wechat_rebind_invite(db, company.id, owner.id, 24)
    other = User(display_name="其他人", status="ACTIVE")
    db.add(other)
    db.flush()
    db.add(WechatIdentity(openid="taken-openid-929", user_id=other.id))
    db.commit()

    with pytest.raises(AppError) as exc:
        login_or_bind_wechat(db, openid="taken-openid-929", invite_token=raw)
    assert exc.value.code == "AUTH_WECHAT_ALREADY_BOUND"
    assert db.scalar(select(WechatIdentity).where(WechatIdentity.user_id == owner.id)).openid == "old-openid-929"


def test_rebind_invite_cannot_bind_another_company_owner(db) -> None:
    company, owner = _bound_company(db)
    _, raw, _ = create_owner_wechat_rebind_invite(db, company.id, owner.id, 24)
    with pytest.raises(AppError) as exc:
        login_or_bind_wechat(db, openid="new-openid-929", invite_token=raw, expected_company_id="other")
    assert exc.value.code == "AUTH_INVITE_INVALID"


def test_rebind_invite_uses_existing_oauth_confirmation_flow(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        company, owner = _bound_company(db)
        company_id, owner_id = company.id, owner.id

    admin_login = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "Admin123!"},
    )
    assert admin_login.status_code == 200
    admin_token = admin_login.cookies.get("access_token")
    created = client.post(
        f"/api/v1/auth/companies/{company_id}/owner-wechat-rebind-invites",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"expires_hours": 24},
    )
    assert created.status_code == 200, created.text
    raw = created.json()["data"]["token"]
    client.cookies.clear()

    preview = client.post("/api/v1/auth/invites/preview", json={"invite": raw})
    assert preview.json()["data"]["purpose"] == "OWNER_WECHAT_REBIND"
    confirmed = client.post(
        "/api/v1/auth/invites/confirm-start",
        json={"invite": raw, "return_url": "/h5/v12-workbench.html"},
    )
    assert confirmed.status_code == 200, confirmed.text
    state = parse_qs(urlparse(confirmed.json()["data"]["authorization_url"]).query)["state"][0]
    bound = client.post(
        "/api/v1/auth/wechat/mock-callback",
        json={"state": state, "openid": "new-http-openid-929", "nickname": "负责人"},
    )
    assert bound.status_code == 200, bound.text
    assert bound.json()["data"]["user_id"] == owner_id
    with factory() as db:
        assert db.scalar(select(WechatIdentity).where(WechatIdentity.user_id == owner_id)).openid == "new-http-openid-929"
