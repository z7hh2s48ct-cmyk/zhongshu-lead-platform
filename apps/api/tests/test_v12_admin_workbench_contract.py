from __future__ import annotations

from pathlib import Path


WORKBENCH = Path("apps/admin/public/v12-operations.js")
ENTRY = Path("apps/admin/public/v12-operations.html")
DESIGN = Path("DESIGN.md")


def test_desktop_workbench_uses_role_specific_navigation_and_lower_left_entrypoints() -> None:
    source = WORKBENCH.read_text(encoding="utf-8")

    assert "ADMIN_VIEW_CONTRACT" in source
    assert "SUPER_ADMIN:['overview','leads','supplements','publicPool','companies','finance']" in source
    assert "OPERATION:['overview','leads','supplements','publicPool','telesales','dispatch','companies']" in source
    assert 'data-account-center' in source
    assert 'data-account-tool' in source
    assert 'data-account-settings' not in source
    assert "S.me?.username||'当前账号'" in source
    assert "ops-account-zone" in source
    assert "ops-personal-menu" not in source
    assert "ops-top" not in source[source.index("function shell"):source.index("function firstAllowedView")]
    assert "${setting}" not in source[source.index("function shell"):source.index("function firstAllowedView")]


def test_desktop_workbench_uses_the_unified_brand_without_a_visible_version_suffix() -> None:
    entry = ENTRY.read_text(encoding="utf-8")
    source = WORKBENCH.read_text(encoding="utf-8")

    assert "客资管理平台" in entry
    assert "客资管理平台" in source
    assert "合家美宅 · 客资运营台" not in entry
    assert "V1.2 统一工作台" not in source


def test_admin_audit_result_is_derived_from_event_status() -> None:
    source = WORKBENCH.read_text(encoding="utf-8")
    audit_section = source[source.index("function auditResult") : source.index("function latestItem")]

    assert "function auditResult" in audit_section
    assert "action.endsWith('_FAILED')" in audit_section
    assert "badge(result.status)" in audit_section
    assert "['操作结果','已完成']" not in audit_section


def test_design_navigation_terms_match_the_compact_desktop_sidebar() -> None:
    design = DESIGN.read_text(encoding="utf-8")
    super_admin = design[
        design.index("### Super-admin information architecture") :
        design.index("### Operations-admin information architecture")
    ]
    operation = design[
        design.index("### Operations-admin information architecture") :
        design.index("### Telesales information architecture")
    ]

    for label in ("1. 首页", "2. 客资", "3. 公海池", "4. 加盟商", "5. 资金"):
        assert label in super_admin
    for label in ("1. 首页", "2. 客资", "3. 公海池", "4. 电销", "5. 派发", "6. 加盟商"):
        assert label in operation
    for stale_label in ("客资总览", "今日待办", "客资中心", "电销核验协同", "派发中心", "加盟商审核"):
        assert stale_label not in super_admin + operation
