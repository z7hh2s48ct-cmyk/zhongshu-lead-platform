from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
ENTRYPOINT = ROOT / "apps" / "api" / "src" / "main.py"
ADMIN_H5 = ROOT / "apps" / "admin" / "public" / "h5"


def test_platform_h5_is_a_real_role_scoped_workbench_not_a_desktop_redirect() -> None:
    entrypoint = ENTRYPOINT.read_text(encoding="utf-8")
    index = (ADMIN_H5 / "index.html").read_text(encoding="utf-8")
    app = (ADMIN_H5 / "app.js").read_text(encoding="utf-8")

    assert '"/h5/admin", ROOT / "apps" / "admin" / "public" / "h5", "h5-admin"' in entrypoint
    assert 'return RedirectResponse(url="/admin/v12-operations.html"' not in entrypoint
    assert 'src="./app.js?v=20260913-modal-lifecycle"' in index
    assert "SUPER_ADMIN" in app
    assert "OPERATION" in app
    assert "ROLE_META" in app
    assert "function identityLabel()" in app
    assert "S.me.display_name === meta.name" in app
    assert "renderAccessDenied" in app
    assert "renderInvalidLink" in app
    assert "'/h5/call/'" in app
    assert "'/h5/'" in app


def test_platform_h5_keeps_each_role_on_its_two_character_bottom_navigation() -> None:
    app = (ADMIN_H5 / "app.js").read_text(encoding="utf-8")

    for label in ("首页", "治理", "资金", "我的", "客资", "派发", "异常"):
        assert label in app
    assert "超级管理员" in app
    assert "运营管理员" in app
    assert "待办" in app
    assert "一键拨号" not in app
    assert "index.html#" not in app


def test_operation_h5_only_queries_leads_in_an_operation_todo_state() -> None:
    app = (ADMIN_H5 / "app.js").read_text(encoding="utf-8")

    for marker in (
        "/v1.2/platform/leads?status=PENDING_REVIEW&page=1&page_size=10",
        "/v1.2/platform/leads?status=PENDING_OPERATION_DISPOSITION&page=1&page_size=10",
        "/v1.2/admin/supplier-leads?status=PENDING_REVIEW&review_status=PENDING&page=1&page_size=10",
        "/v1.2/admin/supplier-leads?status=PENDING_OPERATION_DISPOSITION&review_status=PENDING&page=1&page_size=10",
    ):
        assert marker in app
    assert "api('/v1.2/platform/leads?page=1&page_size=20')" not in app
    assert "api('/v1.2/admin/supplier-leads?page=1&page_size=20')" not in app


def test_superadmin_h5_funds_reuses_the_audited_financial_write_contracts() -> None:
    app = (ADMIN_H5 / "app.js").read_text(encoding="utf-8")

    for marker in (
        "/points/reconciliation/",
        "/points/recharge",
        "/points/adjust",
        "/points/ledgers/${encodeURIComponent(ledgerId)}/reverse",
        "收款核验与凭证说明",
        "调账原因及凭证说明",
        "冲正原因及凭证说明",
        "crypto.randomUUID()",
    ):
        assert marker in app
