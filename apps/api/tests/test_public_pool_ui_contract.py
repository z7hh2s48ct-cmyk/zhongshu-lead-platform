from pathlib import Path


WORKBENCH = Path("apps/admin/public/v12-operations.js")


def test_admin_public_pool_is_shared_by_management_and_operations_only() -> None:
    source = WORKBENCH.read_text(encoding="utf-8")

    assert "publicPool:['公海池'" in source
    assert "SUPER_ADMIN:['overview','leads','supplements','publicPool','companies','finance']" in source
    assert "OPERATION:['overview','leads','supplements','publicPool','telesales','dispatch','companies']" in source
    assert "FRANCHISE_OWNER" not in source[source.index("const ADMIN_VIEW_CONTRACT"):source.index("const ROLE_HOME_PRIORITY")]


def test_public_pool_page_exposes_all_phase_one_entry_and_transfer_actions() -> None:
    source = WORKBENCH.read_text(encoding="utf-8")

    assert "/v1.2/public-pool/leads" in source
    assert "/v1.2/public-pool/feishu/import" in source
    assert "/transfer-to-dispatch" in source
    assert "/dedup-override" in source
    assert "新增客户" in source
    assert "直接添加一行" in source
    assert "从飞书客户视图导入" in source
    assert "确认非重复并转入派发池" in source
    assert "整批目标" in source
    assert "const path=S.view==='publicPool'?" in source
    assert "定时同步" not in source
    assert "飞书回写" not in source


def test_public_pool_distinguishes_customer_source_and_supplier_coverage() -> None:
    source = WORKBENCH.read_text(encoding="utf-8")

    assert "客户来源" in source
    assert "OPERATION_ENTRY" in source
    assert "FRANCHISE_SUPPLIED" in source
    assert "运营录入" in source
    assert "加盟商提供" in source
    assert "提供加盟商" in source
    assert "customer_source" in source
    assert "重新匹配并转入派发池" in source


def test_public_pool_audit_actions_have_human_readable_labels() -> None:
    source = WORKBENCH.read_text(encoding="utf-8")

    for action in (
        "V12_PUBLIC_POOL_LEAD_CREATE",
        "V12_PUBLIC_POOL_LEAD_UPDATE",
        "V12_PUBLIC_POOL_TRANSFER",
        "V12_PUBLIC_POOL_TRANSFER_BLOCKED",
        "V12_PUBLIC_POOL_FEISHU_IMPORT",
    ):
        assert f"{action}:" in source
