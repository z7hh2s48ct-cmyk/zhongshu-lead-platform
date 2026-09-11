from __future__ import annotations

from pathlib import Path


def test_v12_workbench_shows_platform_managed_company_profile() -> None:
    html = Path("apps/h5/public/v12-workbench.html").read_text(encoding="utf-8")
    js = Path("apps/h5/public/v12-workbench.js").read_text(encoding="utf-8")

    assert "/v1.2/company/capabilities" in js
    assert "/v1.2/company/service-areas" in js
    assert "LEAD_SUPPLIER" in js
    assert "CAPABILITY_META" not in js
    assert 'id="company-capabilities"' not in js
    assert 'id="wb-profile-logout"' in js
    assert 'id="profile-username"' in js
    assert 'id="profile-password"' in js
    assert "账户与安全" in js
    assert "经营区域" in js
    assert "供资暂未开通" in js
    assert "供资功能未开通，请联系平台管理员。" not in js
    assert "company.profile.manage" in js
    assert "WORKBENCH_REPORT_PERMISSIONS" in js
    assert "defaultWorkbenchView" in js
    assert "canView" in js
    assert "points.own.read" in js
    assert "data-capability-request" not in js
    assert "service-area-edit" not in js
    assert "申请/更新服务区域" not in js
    assert "v12-workbench.js?v=20260911-feedback-911" in html


def test_v12_operations_exposes_company_detail_and_platform_configuration() -> None:
    html = Path("apps/admin/public/v12-operations.html").read_text(encoding="utf-8")
    js = Path("apps/admin/public/v12-operations.js").read_text(encoding="utf-8")

    assert "/v1.2/admin/companies/${encodeURIComponent(company.id)}/profile" in js
    assert "/v1.2/admin/companies/${encodeURIComponent(companyId)}/capabilities/${encodeURIComponent(capabilityCode)}" in js
    assert "/auth/companies/${encodeURIComponent(company.id)}/invites" in js
    assert "company.profile.review" in js
    assert "data-company-detail" in js
    assert "data-company-lifecycle" in js
    assert "删除测试主体" in js
    assert "confirmation_code" not in js
    assert "status:enabling?'ACTIVE':'DISABLED',reason" in js
    assert "负责人绑定" in js
    assert "接收客资" in js
    assert "提供客资" in js
    assert "新建加盟商主体" in js
    assert "function bindServiceRegionBuilder" in js
    assert "${prefix}-selected-regions" in js
    assert "bindServiceRegionBuilder('new-franchise',cities)" in js
    assert "bindServiceRegionBuilder('company-service-area',cities,activeAreas)" in js
    assert "添加整市" in js
    assert "添加区县" in js
    assert "添加乡镇" in js
    assert "/master-data/regions?parent_code=" in js
    assert "province_code:province.code" in js
    assert "district_codes" in js
    assert "region_codes" in js
    assert "serve_all_districts:false" in js
    assert "/v1.2/admin/companies/${encodeURIComponent(company.id)}/service-areas" in js
    assert "data-company-service-areas" in js
    assert "const companyRecord={...company,...detail}" in js
    assert "editCompany(companyRecord)" in js
    assert "拒绝领取" in js
    assert "发起退回" in js
    assert "确认无效" in js
    assert "测试主体" in js
    assert "data-company-wechat-unbind" in js
    assert "data-company-test-delete" in js
    assert "detail.status==='DISABLED'&&detail.is_test&&isSuperAdmin()" in js
    assert "data-company-mark-test" in js
    assert "data-company-enable" in js
    assert "/wechat-binding/unbind" in js
    assert "v12-operations.js?v=20260911-feedback-911-complete" in html
    assert "加盟商能力与服务区域审核申请" not in js


def test_v12_operations_exposes_test_company_cleanup_actions() -> None:
    html = Path("apps/admin/public/v12-operations.html").read_text(encoding="utf-8")
    js = Path("apps/admin/public/v12-operations.js").read_text(encoding="utf-8")

    assert "/wechat-binding/unbind" in js
    assert "deleteTestCompany" in js
    assert "companyLifecycleConfirmation" in js
    assert "/purge-preview" in js
    assert "/mark-test" in js
    assert "confirm_name" in js
    assert "confirm_phrase" in js
    assert "scope_token:preview.scope_token" in js
    assert "影响预览" in js
    assert "永久删除测试数据" in js
    assert "输入加盟商完整名称" in js
    assert "停用只负责业务隔离" in js
    assert "解绑负责人微信" in js
    assert "删除测试数据" in js
    assert "积分账户、充值与积分流水" in js
    assert "不因已产生业务或已派发而阻止删除" in js
    assert "已派给其他加盟商的测试客资也会一并清理" in js
    assert "原成员账号会停用" in js
    assert "确认永久删除" in js
    delete_flow = js[js.index("function deleteTestCompany") : js.index("function configureCompanyCapability")]
    assert delete_flow.count("method:'DELETE'") == 1
    assert "v12-operations.js?v=20260911-feedback-911-complete" in html


def test_company_detail_modal_is_responsive_without_visible_scrollbars() -> None:
    html = Path("apps/admin/public/v12-operations.html").read_text(encoding="utf-8")
    css = Path("apps/admin/public/v12-operations.css").read_text(encoding="utf-8")
    js = Path("apps/admin/public/v12-operations.js").read_text(encoding="utf-8")

    assert "document.body.classList.add('ops-modal-open')" in js
    assert "document.body.classList.remove('ops-modal-open')" in js
    assert ".ops-modal-open{overflow:hidden}" in css
    assert ".ops-overlay:has(.ops-company-detail)" in css
    assert "scrollbar-width:none" in css
    assert ".ops-modal:has(.ops-company-detail)" in css
    assert "overflow:visible" in css
    assert ".ops-company-detail>*{min-width:0}" in css
    assert ".ops-company-invite-history .ops-table{min-width:0" in css
    assert "v12-operations.css?v=20260829-company-detail-fluid" in html


def test_company_invitation_has_a_dedicated_h5_confirmation_page() -> None:
    auth = Path("apps/api/src/routers/auth.py").read_text(encoding="utf-8")
    html = Path("apps/h5/public/invite.html").read_text(encoding="utf-8")
    js = Path("apps/h5/public/invite.js").read_text(encoding="utf-8")

    assert "/h5/invite.html#invite={raw}" in auth
    assert "/auth/invites/preview" in js
    assert "/auth/invites/confirm-start" in js
    assert "authorization_url" in js
    assert "history.replaceState" in js
    assert "确认并微信绑定" in html


def test_company_invitation_link_modal_is_not_closed_after_rendering() -> None:
    js = Path("apps/admin/public/v12-operations.js").read_text(encoding="utf-8")

    assert "if(await onSubmit(raw)!==false)closeModal()" in js
    invitation_start = js.index("function createCompanyInvite")
    invitation_end = js.index("function showCompanyInvite", invitation_start)
    assert "return false;" in js[invitation_start:invitation_end]


def test_stage6_company_review_and_internal_collaboration_actions_are_role_scoped() -> None:
    admin = Path("apps/admin/public/v12-operations.js").read_text(encoding="utf-8")
    franchise = Path("apps/h5/public/v12-workbench.js").read_text(encoding="utf-8")

    assert "data-company-profile-approve" not in admin
    assert "一键审核" not in admin
    assert "平台统一配置。" in admin
    assert "/account-directory" in franchise
    assert "/internal-assignee" in franchise
    assert "公司内部直接分配，无需运营审批" in franchise
    assert "负责人自己跟进" in franchise


def test_stage7_return_and_redispatch_actions_keep_the_business_closure_explicit() -> None:
    admin = Path("apps/admin/public/v12-operations.js").read_text(encoding="utf-8")
    franchise = Path("apps/h5/public/v12-workbench.js").read_text(encoding="utf-8")

    assert "FOLLOWUP_INVALID_REQUIRES_RETURN" in franchise
    assert "error.code=j.code" in franchise
    assert "下一步：发起退回" in franchise
    assert "data-return-evidence" in franchise
    assert "补充证据" in franchise
    assert "资金影响" in admin
    assert "x.status==='REVIEWING'&&verification.conclusion" in admin
    assert "RETURNED_RECEIVER_EXCLUDED" in admin
    assert "return_receiver_override" in admin


def test_stage8_finance_governance_stays_inside_the_unified_workbench() -> None:
    admin = Path("apps/admin/public/v12-operations.js").read_text(encoding="utf-8")

    for marker in (
        "/points/adjust",
        "/points/ledgers/${encodeURIComponent(ledgerId)}/reverse",
        "/points/reconciliation/${encodeURIComponent(companyId)}",
        "/points/packages?active_only=false",
        "/points/price-rules",
        "/v1.2/supplier-rewards",
        "ledger.type",
        "financeRewardPage",
        "收款核验与凭证说明",
        "结算说明",
        "对账异常",
        "无需第二位超级管理员复核",
    ):
        assert marker in admin

    assert "function rewards" not in admin
