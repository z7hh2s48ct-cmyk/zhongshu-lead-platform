from __future__ import annotations

import json
from pathlib import Path
import subprocess


ADMIN = Path("apps/admin/public/v12-operations.js")


def run_js(source: str) -> dict:
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", source],
        capture_output=True, text=True, check=True, timeout=20,
    )
    return json.loads(completed.stdout)


def fragment(start: str, end: str) -> str:
    source = ADMIN.read_text(encoding="utf-8")
    return source[source.index(start):source.index(end)]


def test_national_selection_is_idempotent_and_preserves_primary_city():
    result = run_js("""
const nodes={};
const document={querySelector:key=>nodes[key]??=(
 {value:'',innerHTML:'',querySelectorAll:()=>[]})};
const esc=String,recordCode=String,zsSetSafeHtml=(node,html)=>node.innerHTML=html;
const replacePlatformSelectOptions=()=>{},toast=()=>{};
""" + fragment("function serviceRegionBuilderMarkup", "async function openNewFranchiseCompany") + """
const cities=[{code:'110100',province_code:'11',option_name:'甲市',districts:[]},
 {code:'420100',province_code:'42',option_name:'乙市',districts:[]}];
const builder=bindServiceRegionBuilder('test',cities,[
 {region_code:'110100',region_level:'CITY',active:true},
 {region_code:'420100',region_level:'CITY',active:true,is_primary_city:true}]);
const initialPrimary=builder.primaryCityCode();
nodes['#test-select-national-cities'].onclick();
nodes['#test-select-national-cities'].onclick();
const result={initialPrimary,codes:builder.regionCodes(),primary:builder.primaryCityCode(),
 html:nodes['#test-selected-regions'].innerHTML};
nodes['#test-clear-regions'].onclick();
console.log(JSON.stringify({...result,cleared:builder.regionCodes(),clearedPrimary:builder.primaryCityCode()}));
""")
    assert result["initialPrimary"] == result["primary"] == "420100"
    assert sorted(result["codes"]) == ["110100", "420100"]
    assert "全国" in result["html"]
    assert result["cleared"] == []
    assert result["clearedPrimary"] == ""


def test_entering_supplements_clears_filters_from_other_lead_page():
    source = ADMIN.read_text(encoding="utf-8")
    reset = ""
    if "function resetLeadReportFilters" in source:
        reset = fragment("function resetLeadReportFilters", "function leadReportFilters")
    result = run_js("""
const S={view:'leads',leadCreatedFrom:'2026-09-12',leadCreatedTo:'2026-09-12',
 leadSupplierCompanyId:'old-supplier',leadSubmitterId:'old-user',leadPhone:'old-phone',
 leadRegion:'old-region',leadReceiverCompanyId:'old-receiver',leadStatusFilter:'CLAIMED',
 assignmentStatusFilter:'CLAIMED',leadAssignerId:'old-assigner',leadPendingReason:''};
const location={href:'https://example.test/?view=supplements&source=SUPPLIER_H5'};
const canOpenView=()=>true,firstAllowedView=()=>'',history={replaceState:()=>{}};
""" + reset + fragment("function syncRouteFromUrl", "function go(") + """
syncRouteFromUrl();
const entered={...S};
S.leadPhone='intentional-filter';
syncRouteFromUrl();
console.log(JSON.stringify({entered,samePagePhone:S.leadPhone}));
""")
    entered = result["entered"]
    assert entered["leadPendingReason"] == "PRE_DISPATCH_REWORK_REQUIRED"
    for key in ("leadCreatedFrom", "leadCreatedTo", "leadSupplierCompanyId", "leadSubmitterId",
                "leadPhone", "leadRegion", "leadReceiverCompanyId", "leadStatusFilter",
                "assignmentStatusFilter", "leadAssignerId", "leadSource"):
        assert entered[key] == "", key
    assert result["samePagePhone"] == "intentional-filter"


def test_public_pool_search_and_export_share_the_phone_filter():
    result = run_js("""
const S={publicPoolPhone:' 13800138000 ',publicPoolKeyword:'合成客户'};
const leadDateBoundary=()=>undefined;
""" + fragment("function publicPoolFilters", "const leadFilterOptionName") + """
console.log(JSON.stringify(publicPoolFilters()));
""")
    assert result["phone"] == "13800138000"
    assert result["keyword"] == "合成客户"


def test_invalid_public_pool_phone_shows_error_and_preserves_input():
    result = run_js("""
const nodes={'#public-pool-phone':{value:'123'}};
const document={querySelector:key=>nodes[key]??={value:''}};
const S={},messages=[],toast=(text,error)=>messages.push({text,error});
const publicPool=async()=>{throw new Error('请输入有效的完整手机号')};
""" + fragment("document.querySelector('#public-pool-filter').onsubmit", "document.querySelector('#public-pool-filter-reset').onclick") + """
await nodes['#public-pool-filter'].onsubmit({preventDefault(){}});
console.log(JSON.stringify({messages,phone:nodes['#public-pool-phone'].value}));
""")
    assert result["messages"] == [{"text": "请输入有效的完整手机号", "error": True}]
    assert result["phone"] == "123"


def test_fixed_points_form_saves_both_values_and_keeps_input_on_conflict():
    result = run_js("""
const nodes={
 '#lead-points-form':{}, '#operation-claim-points':{value:'125'},
 '#supplier-provision-points':{value:'45'}, '#lead-points-save':{},
 '#lead-points-message':{textContent:''}};
const document={querySelector:key=>nodes[key]};
const messages=[],calls=[];
const esc=String,toast=(text,error)=>messages.push({text,error});
let failure=false;
const api=async(path,options)=>{calls.push({path,body:JSON.parse(options.body)});
 if(failure)throw new Error('配置已更新，请刷新后重试');
 return {...JSON.parse(options.body),configured:true,version:4}};
""" + fragment("function leadPointsSettingsSection", "async function finance()") + """
const settings={configured:true,operation_claim_points:100,supplier_provision_points:30,version:3};
bindLeadPointsSettings(settings);
await nodes['#lead-points-form'].onsubmit({preventDefault(){}});
failure=true;nodes['#operation-claim-points'].value='150';
await nodes['#lead-points-form'].onsubmit({preventDefault(){}});
console.log(JSON.stringify({calls,messages,value:nodes['#operation-claim-points'].value,
 disabled:nodes['#lead-points-save'].disabled,
 emptyHtml:leadPointsSettingsSection({configured:false,version:0})}));
""")
    assert result["calls"][0]["body"] == {
        "operation_claim_points": 125, "supplier_provision_points": 45, "expected_version": 3,
    }
    assert result["calls"][1]["body"]["expected_version"] == 4
    assert result["value"] == "150"
    assert result["disabled"] is False
    assert result["messages"][-1]["error"] is True
    assert 'value="undefined"' not in result["emptyHtml"]
    assert "首次" in result["emptyHtml"]


def test_fixed_reward_details_show_the_saved_amount_including_zero():
    result = run_js("const esc=String;\n" + fragment("function ruleSummary", "function rewardSection") + """
console.log(JSON.stringify({fixed:ruleSummary({calculation_mode:'FIXED',fixed_points:0}),
 legacy:ruleSummary({ratio_bps:5000,min_points:0})}));
""")
    assert "固定" in result["fixed"] and "0 积分" in result["fixed"]
    assert "%" not in result["fixed"]
    assert "50%" in result["legacy"]


def test_admin_reward_detail_displays_zero_points_as_zero():
    result = run_js("""
const api=async()=>({id:'reward-zero',reward_points:0,claim_points:100,
 rule_snapshot:{calculation_mode:'FIXED',fixed_points:0}});
const esc=String,recordCode=String,label=String,fmt=()=>'',ruleSummary=()=>'';
const beginModalRequest=()=>()=>true;
let html='';const modal=(title,body)=>{html=body};
""" + fragment("async function rewardDetail", "function settle(") + """
await rewardDetail('reward-zero');
console.log(JSON.stringify({html}));
""")
    assert '<small>奖励积分</small><b>0</b>' in result["html"]
