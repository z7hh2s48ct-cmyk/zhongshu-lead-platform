from __future__ import annotations

from pathlib import Path


SOURCE = Path("apps/admin/public/v12-operations.js")


def _source() -> str:
    return SOURCE.read_text(encoding="utf-8")


def test_unified_desktop_settings_manage_only_internal_role_accounts() -> None:
    source = _source()

    assert "INTERNAL_ROLE_OPTIONS" in source
    for role in ("SUPER_ADMIN", "OPERATION", "TELESALES"):
        assert role in source
    assert "function internalUsers" in source
    assert "内部账号" in source
    internal_section = source[source.index("const INTERNAL_ROLE_OPTIONS") : source.index("async function companies")]
    assert "FRANCHISE_OWNER" not in internal_section
    assert "FRANCHISE_EMPLOYEE" not in internal_section


def test_unified_desktop_settings_call_internal_lifecycle_apis() -> None:
    source = _source()

    for marker in (
        "api('/users')",
        "api('/users',{method:'POST'",
        "`/users/${encodeURIComponent(user.id)}/roles`",
        "`/users/${encodeURIComponent(user.id)}/${enabling?'enable':'disable'}`",
        "`/users/${encodeURIComponent(user.id)}/reset-password`",
        "`/users/${encodeURIComponent(user.id)}/mark-test`",
        "method:'DELETE'",
        "role_codes:[role]",
        "new_password",
    ):
        assert marker in source


def test_internal_user_creation_relies_on_one_time_generated_password() -> None:
    source = _source()
    creation = source[source.index("function internalUserModal") : source.index("function internalRoleModal")]
    credentials = source[source.index("function showInternalUserCredentials") : source.index("function internalUserModal")]

    assert "initial-password" not in creation
    assert "password:" not in creation
    assert "initial_password" in credentials
    assert "copy-internal-user-password" in credentials
    assert "复制账号与密码" in credentials
    assert "账号已创建，但初始密码未返回" in credentials
    assert "请输入姓名" in creation
    assert "登录账号至少输入 2 个字符" in creation
    assert "单选，仅限平台内部角色" in creation
    assert "8 位以上初始密码" in creation
    assert creation.index("showInternalUserCredentials(created)") < creation.index("await internalUsers()")
    assert "账号已创建，但账号列表刷新失败" in creation


def test_internal_user_actions_provide_busy_state_and_length_only_reset_policy() -> None:
    source = _source()
    action = source[source.index("async function runInternalUserAction") : source.index("function showInternalUserCredentials")]
    reset = source[source.index("function resetInternalUserPassword") : source.index("async function internalUsers")]

    assert ".disabled=true" in action
    assert ".disabled=false" in action
    assert "await refreshAfterSuccess(internalUsers,success)" in action
    assert "toast(error.message,true)" in action
    assert "请输入8-128位密码，不限制字符组合" not in source
    assert "密码需为 8-128 位" in reset
    assert "minLength:8" in reset
    assert "至少12位" not in source


def test_internal_user_delete_requires_a_second_confirmation() -> None:
    source = _source()
    section = source[source.index("function internalUserLifecycleConfirmation") : source.index("async function internalUsers")]

    assert "输入完整登录账号" in section
    assert "confirm_username" in section
    assert "操作原因" in section
    assert "只允许删除已停用、无业务数据的测试账号" in section
