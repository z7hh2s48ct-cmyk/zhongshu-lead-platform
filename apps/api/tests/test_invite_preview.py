from pathlib import Path

from apps.api.src.schemas.company import CompanyCreateBody
from apps.api.src.services.auth_service import create_company_invite
from apps.api.src.services.company_service import create_company


ROOT = Path(__file__).resolve().parents[3]


def test_invite_preview_returns_only_safe_company_summary(api_client) -> None:
    client, factory = api_client
    with factory() as session:
        company = create_company(
            session,
            CompanyCreateBody(
                code="SH-PREVIEW",
                name="上海预览加盟商",
                owner_name="李老板",
                region_codes=["310100"],
                capabilities=[{"category_code": "VILLA_DECORATION", "brand_code": "ZHONGSHU"}],
            ),
        )
        _, token, _ = create_company_invite(session, company.id, None, 24)
        session.commit()
    response = client.get("/api/v1/auth/invites/preview", params={"invite": token})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["company_name"] == "上海预览加盟商"
    assert data["owner_name"] == "李老板"
    assert data["region_codes"] == ["310100"]
    assert data["capability_codes"] == ["VILLA_DECORATION"]
    assert "contact_phone" not in data
    assert "points" not in data


def test_invite_preview_accepts_post_body_without_query_token(api_client) -> None:
    client, factory = api_client
    with factory() as session:
        company = create_company(
            session,
            CompanyCreateBody(code="SH-PREVIEW-POST", name="上海POST预览加盟商", owner_name="王老板"),
        )
        _, token, _ = create_company_invite(session, company.id, None, 24)
        session.commit()

    response = client.post("/api/v1/auth/invites/preview", json={"invite": token})

    assert response.status_code == 200, response.text
    assert response.json()["data"]["company_name"] == "上海POST预览加盟商"


def test_invite_page_clears_query_before_preview_and_prefers_fragment_token() -> None:
    html = (ROOT / "apps" / "h5" / "public" / "invite.html").read_text(encoding="utf-8")
    script = (ROOT / "apps" / "h5" / "public" / "invite.js").read_text(encoding="utf-8")

    assert 'name="referrer" content="no-referrer"' in html
    assert "location.hash" in script
    assert "history.replaceState" in script
    assert script.index("history.replaceState") < script.index("/auth/invites/preview")
    assert "/auth/invites/preview?invite=" not in script
    assert "method:'POST'" in script
    assert "JSON.stringify({invite:" in script


def test_production_proxy_rate_limits_preview_without_logging_legacy_query_token() -> None:
    for relative_path in (
        "infra/nginx/production.conf.template",
        "infra/nginx/baota-proxy.conf.template",
    ):
        config = (ROOT / relative_path).read_text(encoding="utf-8")
        preview = config[
            config.index("location = /api/v1/auth/invites/preview") :
            config.index("location /api/")
        ]

        assert "limit_req zone=auth_login_limit" in preview
        assert "limit_req_status 429" in preview
        assert "access_log off" in preview


def test_invite_page_refreshes_script_for_wechat_rebind() -> None:
    html = (ROOT / "apps" / "h5" / "public" / "invite.html").read_text(encoding="utf-8")
    assert 'invite.js?v=20260930-feedback-929' in html
