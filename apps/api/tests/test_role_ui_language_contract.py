from __future__ import annotations

import re
from pathlib import Path


ADMIN = Path("apps/admin/public")
H5 = Path("apps/h5/public")
CALL_H5 = Path("apps/call-h5/public")


def _read(root: Path, filename: str) -> str:
    return (root / filename).read_text(encoding="utf-8")


def test_operations_pages_use_business_language_and_existing_safe_endpoints() -> None:
    source = _read(ADMIN, "v12-operations.js")
    for developer_copy in (
        "旧业务写接口",
        "配置值与场景参数",
        "请输入电销用户 ID",
        "JSON.stringify(x.evidences",
        "JSON.stringify(x.rule_snapshot",
        "奖励比例（基点）",
        "业务ID",
        "请求ID",
        "JSON.stringify(d,null,2)",
    ):
        assert developer_copy not in source
    assert "/admin-meta/telesales-users" in source
    assert "/return-evidences/${encodeURIComponent(item.id)}/download" in source
    assert "派发前置电销核验" in source
    assert "派发退回电话核验" in source
    assert "运营客资领取积分（积分/条）" in source
    assert "加盟商供客积分（积分/条）" in source
    assert "奖励比例（%）" not in source
    assert "esc(label(x.source_kind))" in source
    assert "公司编号" not in source
    for field in (
        "rule?.min_points",
        "rule.max_points",
        "rule?.hard_duplicate_days",
        "rule?.reward_duplicate_days",
        "rule?.historical_suspect_days",
    ):
        assert field in source


def test_company_workbench_uses_user_language_and_real_unread_count() -> None:
    source = _read(H5, "v12-workbench.js")
    for developer_copy in (
        "能力编码",
        "领取与积分扣减原子执行",
        "领取时规则快照",
        "JSON.stringify(x.rule_snapshot",
        "按当前设备本地时间填写，提交时统一转换为标准时间",
    ):
        assert developer_copy not in source
    assert "unread_notifications" in source
    assert "x.read_at?'READ':'UNREAD'" in source
    assert "wb-message-entry" in source
    assert "wb-message-badge" in source
    assert "已读" in source
    assert "未读" in source
    assert "function safeDeepLink" in source
    assert "url.origin!==location.origin" in source
    assert "!url.pathname.startsWith('/h5/')" in source
    assert "url.pathname.startsWith('/admin/')" not in source
    assert "redirectWrongWorkbenchRole" in source


def test_wechat_oauth_failures_use_a_session_independent_page() -> None:
    router = Path("apps/api/src/routers/auth.py").read_text(encoding="utf-8")
    html = _read(H5, "auth-error.html")
    script = _read(H5, "auth-error.js")

    assert "/h5/#/auth-error" not in router
    assert "/h5/auth-error.html?code=" in router
    assert "AUTH_BINDING_REQUIRES_CLEAN_SESSION" in script
    assert "当前浏览器已登录平台账号" in script
    assert "/auth/me" not in script
    assert "auth-error.js" in html


def test_company_workbench_opens_existing_legacy_assignment_message_links() -> None:
    source = _read(H5, "v12-workbench.js")

    assert "function legacyAssignmentLinkToken" in source
    assert "/claims/resolve-link?token=" in source
    assert "searchParams.set('view','assignments')" in source
    assert "history.replaceState(null,'',url)" in source


def test_dispatch_messages_use_the_v12_assignment_detail_route() -> None:
    source = Path("apps/api/src/services/dispatch_service.py").read_text(encoding="utf-8")

    assert 'deep_link = f"/h5/v12-workbench.html?view=assignments&id={assignment.id}"' in source
    assert "/h5/#/link/" not in source


def test_unified_lead_review_explains_outcomes_without_security_or_process_jargon() -> None:
    source = _read(ADMIN, "v12-operations.js")
    assert "HMAC" not in source
    assert "加盟商客资在当地暂无其他合格接收方时先进入公海池" in source
    assert "缺少可派发地区时再分配电销核实" in source
    assert "运营处置电销结论" in source


def test_call_workbench_shows_chinese_role_and_plain_task_language() -> None:
    source = _read(CALL_H5, "app.js")
    assert re.search(r"const\s+TELESALES_HOME_CONTRACT\s*=", source)
    assert "me.roles.join('、')" not in source
    assert "事实后置核验" not in source
    assert "自主领取" in source
    assert "电销人员" in source
    assert "工作范围" in source


def test_role_clients_do_not_disguise_runtime_or_logout_failures_as_login_success() -> None:
    desktop = _read(ADMIN, "v12-operations.js")
    admin_h5 = _read(ADMIN / "h5", "app.js")
    company_h5 = _read(H5, "v12-workbench.js")
    call_h5 = _read(CALL_H5, "app.js")

    for source in (desktop, admin_h5, company_h5, call_h5):
        assert "/auth/logout', { method: 'POST' }).catch(() => {})" not in source
        assert "/auth/logout',{method:'POST'}).catch(()=>{})" not in source
    assert "function renderLoadError" in desktop
    assert "function renderLoadError" in admin_h5
    assert "function renderLoadError" in company_h5
    assert "function renderLoadError" in call_h5
    assert "error.status===404" in desktop
    assert ".catch(()=>({items:[],total:0,unread_total:0}))" not in desktop
    assert "api(`/notifications/${encodeURIComponent(item.id)}/read`,{method:'POST'}).catch(()=>{})" not in desktop


def test_role_clients_do_not_silently_fall_back_to_login_or_home_for_wrong_routes() -> None:
    admin_h5 = _read(ADMIN / "h5", "app.js")
    company_h5 = _read(H5, "v12-workbench.js")
    call_h5 = _read(CALL_H5, "app.js")

    assert "if (!S.me) return renderLogin();" in admin_h5
    assert "if (!roleMeta()) return renderAccessDenied();" in admin_h5
    assert "function renderInvalidLink" in admin_h5
    assert "function redirectWrongWorkbenchRole" in call_h5
    assert "location.replace('/h5/admin/')" in call_h5
    assert "location.replace('/h5/')" in call_h5
    assert "function renderInvalidLink" in call_h5
    assert "return renderInvalidLink();" in call_h5
    assert "function renderInvalidLink" in company_h5
    assert "if(!VIEWS[S.view])return renderInvalidLink" in company_h5
