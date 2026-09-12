from __future__ import annotations

import re
from pathlib import Path


def _references(path: Path) -> list[str]:
    content = path.read_text(encoding="utf-8")
    return re.findall(r'(?:src|href)=["\']([^"\']+)["\']', content)


def _assert_local_references_exist(html: Path) -> None:
    content = html.read_text(encoding="utf-8")
    assert "https://" not in content and "http://" not in content
    for reference in _references(html):
        if reference.startswith(("#", "data:", "/api/")):
            continue
        path = reference.split("?", 1)[0]
        if path.startswith("/h5/"):
            target = Path("apps/h5/public") / path.removeprefix("/h5/")
        else:
            target = html.parent / path.lstrip("./")
        assert target.exists(), f"missing {reference} from {html}"


def test_formal_desktop_and_h5_shells_expose_their_workbench_scripts() -> None:
    admin = Path("apps/admin/public/v12-operations.html").read_text(encoding="utf-8")
    h5 = Path("apps/h5/public/v12-workbench.html").read_text(encoding="utf-8")

    assert "v12-operations.js" in admin
    assert "v12-workbench.js" in h5
    assert not Path("apps/admin/public/index.html").exists()
    assert not Path("apps/h5/public/index.html").exists()


def test_unified_operations_and_h5_shells_are_self_contained() -> None:
    _assert_local_references_exist(Path("apps/admin/public/v12-operations.html"))
    _assert_local_references_exist(Path("apps/h5/public/v12-workbench.html"))


def test_unified_operations_lead_ui_distinguishes_platform_and_supplier_flow() -> None:
    js = Path("apps/admin/public/v12-operations.js").read_text(encoding="utf-8")
    assert "/v1.2/platform/leads" in js
    assert "/v1.2/admin/supplier-leads" in js
    assert "/master-data/region-tree" in js
    assert "READY_DISPATCH" in js
    assert "data-platform-pre-dispatch" in js
    assert "手机号必填且必须为 11 位有效号码" in js
    assert "运营补充资料后再处理" in js
    assert "/v1.2/reports/leads" in js
    assert "leadReportFilters" in js
    assert "assignmentStatusFilter" in js
    assert "加盟商来源才可退回加盟商补正" in js


def test_platform_lead_district_options_use_dom_nodes_not_html_injection() -> None:
    js = Path("apps/admin/public/v12-operations.js").read_text(encoding="utf-8")

    assert "function replacePlatformSelectOptions" in js
    assert "select.replaceChildren(...options)" in js
    assert "node.textContent=name" in js
    assert ".innerHTML=platformSelectOptions" not in js


def test_supplier_submission_routes_only_missing_location_to_telesales() -> None:
    source = Path("apps/admin/public/v12-operations.js").read_text(encoding="utf-8")
    supplier_queue = source[
        source.index("const rows=leads.map") : source.index("const sourceOptions")
    ]

    for decision in (
        "PENDING_REVIEW",
        "PENDING_TELESALES_VERIFY",
        "data-supplier-pre-dispatch",
        "分配电销核实",
    ):
        assert decision in supplier_queue
    assert "/admin/leads/${encodeURIComponent(leadId)}/pre-dispatch-verification" in source
    assert "pre_dispatch_reason" not in source
    assert "data-review=" not in source
    assert "分配电销核验" in source
    assert "加盟商客资在当地暂无其他合格接收方时先进入公海池" in source
    assert "加盟商来源会直接进入待电销核实" not in source


def test_supplier_h5_supports_capability_upload_list_and_detail() -> None:
    js = Path("apps/h5/public/v12-workbench.js").read_text(encoding="utf-8")
    assert "/v1.2/company/capabilities" in js
    assert "/v1.2/supplier/leads" in js
    assert "LEAD_SUPPLIER" in js
    assert "consent_confirmed" in js
    assert "supply-submit" in js
    assert "加盟商工作台" in js
    assert "供资" in js
    assert "上传第一条客资" in js
    assert "客户知晓其联系方式和需求将用于业务对接" in js
    assert "请根据运营说明补正资料后重新提交" in js
    assert "PUBLIC_POOL:'等待当地接收方'" in js
    assert "当地暂时没有其他可接收加盟商" in js
    assert "'PUBLIC_POOL','READY_DISPATCH'" in js
    assert "HMAC" not in js
    assert "90/180/365" not in js
    assert "/verification/tasks" not in js


def test_supplier_h5_validates_before_creating_a_submission_draft() -> None:
    js = Path("apps/h5/public/v12-workbench.js").read_text(encoding="utf-8")
    save_form = js.split("async function saveSupplyLead", 1)[1].split("async function openSupplyForm", 1)[0]

    assert "validateSupplySubmission(payload)" in save_form
    assert save_form.index("validateSupplySubmission(payload)") < save_form.index("api('/v1.2/supplier/leads'")
    assert "supply-form-error" in js
    assert "data-supply-field" in js
    assert "aria-invalid" in js
    assert "normalizeSupplyPhone" in js
    validation = js.split("function validateSupplySubmission", 1)[1].split(
        "function showSupplyErrors", 1
    )[0]
    assert "errors.phone" in validation
    assert "errors.consent_confirmed" in validation
    assert "errors.customer_name" not in validation
    assert "errors.city" not in validation
    assert "errors.need_summary" not in validation
    assert "暂不确定，提交后由电销补充" in js


def test_admin_and_h5_share_the_same_budget_unit_conversion() -> None:
    shared = Path("apps/h5/public/business-units.js").read_text(encoding="utf-8")
    admin = Path("apps/admin/public/v12-operations.js").read_text(encoding="utf-8")
    h5 = Path("apps/h5/public/v12-workbench.js").read_text(encoding="utf-8")

    assert "export function amountToWan" in shared
    assert "export function wanToAmount" in shared
    for source in (admin, h5):
        assert "from '/h5/business-units.js'" in source
        assert "amount/10000" not in source
        assert "amount*10000" not in source


def test_supplier_h5_exposes_draft_cleanup_and_rejected_lead_revision() -> None:
    js = Path("apps/h5/public/v12-workbench.js").read_text(encoding="utf-8")
    router = Path("apps/api/src/routers/v12_lead_supply.py").read_text(encoding="utf-8")

    assert re.search(r"method:\s*['\"]DELETE['\"]", js)
    assert "/revise" in js
    assert '@router.delete("/supplier/leads/{lead_id}")' in router
    assert '@router.post("/supplier/leads/{lead_id}/revise")' in router


def test_supplier_h5_explains_operation_requested_rework_before_resubmission() -> None:
    source = Path("apps/h5/public/v12-workbench.js").read_text(encoding="utf-8")

    assert "PRE_DISPATCH_REWORK_REQUIRED" in source
    assert "根据运营说明补正" in source
    assert "PENDING_TELESALES_VERIFY" in source
    assert "PENDING_OPERATION_DISPOSITION" in source


def test_platform_lead_ui_shows_creator_and_uses_controlled_correction_without_delete() -> None:
    admin = Path("apps/admin/public/v12-operations.js").read_text(encoding="utf-8")
    router = Path("apps/api/src/routers/v12_lead_supply.py").read_text(encoding="utf-8")

    assert "submitter_name" in admin
    assert "data-platform-correction" in admin
    assert "/correction" in admin
    assert '@router.post("/platform/leads/{lead_id}/correction")' in router
    assert "data-platform-delete" not in admin
    assert 'id="platform-lead-township"' in admin
    assert "platformTownships" in admin
    assert "region_code:township?.code||district?.code||city?.code||null" in admin


def test_platform_and_supplier_lead_forms_can_select_township_master_data() -> None:
    admin = Path("apps/admin/public/v12-operations.js").read_text(encoding="utf-8")
    h5 = Path("apps/h5/public/v12-workbench.js").read_text(encoding="utf-8")

    for source in (admin, h5):
        assert "/master-data/regions?parent_code=" in source
        assert "TOWNSHIP" in source
    assert 'id="supply-township"' in h5
    assert "loadSupplyTownships" in h5
    assert "region_code:townshipCode||districtCode||cityCode" in h5


def test_candidate_company_picker_uses_actionable_cards_instead_of_a_wide_table() -> None:
    admin = Path("apps/admin/public/v12-operations.js").read_text(encoding="utf-8")
    css = Path("apps/admin/public/v12-operations.css").read_text(encoding="utf-8")
    candidate_block = admin[admin.index("async function candidates") : admin.index("function dispatchOne")]

    assert "ops-candidate-grid" in candidate_block
    assert "ops-candidate-card" in candidate_block
    assert "data-dispatch" in candidate_block
    assert "table(['接收公司'" not in candidate_block
    assert ".ops-candidate-grid" in css
    assert ".ops-candidate-card" in css


def test_operations_review_exposes_audited_background_full_phone_export() -> None:
    admin = Path("apps/admin/public/v12-operations.js").read_text(encoding="utf-8")

    assert "/v1.2/reports/leads/exports" in admin
    assert "lead.phone.export" in admin
    assert "导出客资完整信息" in admin
    assert "系统记录导出人和全部筛选条件" in admin


def test_operations_workbench_exposes_backup_password_and_return_record_fields() -> None:
    source = Path("apps/admin/public/v12-operations.js").read_text(encoding="utf-8")
    returns_block = source[source.index("async function returns") : source.index("async function returnDetail")]

    assert "设置备用登录密码" in source
    assert "公众号登录之外的备用方式" in source
    assert "AUTH_BACKUP_PASSWORD_SET" in source
    assert "退回记录" in returns_block
    assert "assignment_code" in returns_block
    assert "customer_name" in returns_block
    assert "phone_masked" in returns_block
    assert "作废金额" not in source
    assert "消费金额" not in source


def test_franchise_has_standalone_return_record_and_business_report_entries() -> None:
    source = Path("apps/h5/public/v12-workbench.js").read_text(encoding="utf-8")

    assert "退回记录" in source
    assert 'data-go="returns"' in source
    assert "经营报表" in source
    assert 'data-go="reports"' in source
    assert "消耗积分" in source


def test_supplier_workbench_uses_user_facing_reward_copy() -> None:
    js = Path("apps/h5/public/v12-workbench.js").read_text(encoding="utf-8")

    assert "结算说明" in js
    assert "领取时规则快照" not in js
    assert "JSON.stringify(x.rule_snapshot" not in js
