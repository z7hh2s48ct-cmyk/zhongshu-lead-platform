from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
ADMIN = ROOT / "apps" / "admin" / "public"
CALL = ROOT / "apps" / "call-h5" / "public"
H5 = ROOT / "apps" / "h5" / "public"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_formal_workbenches_keep_login_at_the_matching_role_entry() -> None:
    operations = read(ADMIN / "v12-operations.js")
    platform_h5 = read(ADMIN / "h5" / "app.js")
    call = read(CALL / "app.js")
    franchise = read(H5 / "v12-workbench.js")

    for source, form_id in (
        (operations, "platform-login-form"),
        (platform_h5, "login-form"),
        (call, "call-login-form"),
        (franchise, "franchise-login-form"),
    ):
        assert f'id="{form_id}"' in source
        assert 'autocomplete="username"' in source
        assert 'autocomplete="current-password"' in source
        assert "demo-login" not in source

    assert "location.replace('/admin/')" in operations
    assert "location.hash = '#/home'" in call
    assert "location.replace('/h5/')" in franchise


def test_platform_login_keeps_only_credentials_and_can_reveal_password() -> None:
    operations = read(ADMIN / "v12-operations.js")
    login_start = operations.index("function renderLogin(")
    login_end = operations.index("function renderNoAccess()")
    login = operations[login_start:login_end]

    assert "使用平台管理员或运营管理员账号登录。" not in login
    assert "请先登录" not in login
    assert "function renderLogin(message" not in operations
    assert "renderLogin(error.message" not in operations
    assert "ops-notice" not in login
    assert 'id="login-password-toggle"' in login
    assert 'aria-label="显示密码"' in login
    assert "passwordInput.type=visible?'text':'password'" in login
    assert "ops-password-input" in login


def test_formal_workbenches_use_business_labels_without_old_page_links() -> None:
    operations = read(ADMIN / "v12-operations.js")
    franchise = read(H5 / "v12-workbench.js")

    for source in (operations, franchise):
        assert "index.html#/" not in source
        assert "supplier.html" not in source
        assert "供应商工作台" not in source
        assert "JSON.stringify(x.rule_snapshot" not in source

    assert "加盟商客资队列" in operations
    assert "供客奖励" in operations
    assert "加盟商工作台" in franchise
    assert "结算说明" in franchise


def test_return_evidence_and_audit_details_remain_user_readable() -> None:
    operations = read(ADMIN / "v12-operations.js")
    franchise = read(H5 / "v12-workbench.js")

    for marker in (
        "截图或录音任一类型满足即可",
        'name="chat_screenshots"',
        'name="call_recording"',
        "CALL_RECORDING",
        "CHAT_SCREENSHOT",
    ):
        assert marker in franchise
    assert "必须同时上传沟通截图和电话录音" not in franchise
    assert "在线查看截图" in operations
    assert "在线播放录音" in operations
    assert "AUDIT_RESOURCE_LABEL" in operations
    assert "recordCode(x.resource_id,'业务')" in operations
