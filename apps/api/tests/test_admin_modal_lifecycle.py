from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest


ADMIN = Path("apps/admin/public/v12-operations.js")


def run_lifecycle_flow(scenario: str, extra_handlers: str = "") -> dict:
    source = ADMIN.read_text(encoding="utf-8")
    handlers = "\n".join(
        source[source.index(start):source.index(end)]
        for start, end in (
            ("let modalIntent=", "function allowed("),
            ("function openPublicPoolImport(", "async function transferPublicPoolLead("),
            ("function showLeadDetail(", "async function openLeadDetailForSource("),
        )
    )
    harness = r"""
const nodes={},calls=[],pending=[],failures=[],messages=[];
let markup='';
const modalRoot={get innerHTML(){return markup},set innerHTML(value){
  for(const node of Object.values(nodes))node.isConnected=false;
  for(const key of Object.keys(nodes))delete nodes[key];
  markup=value;
}};
const document={body:{classList:{add(){},remove(){}}},querySelector:key=>nodes[key]??={value:'',disabled:false,isConnected:true,focus(){}}};
const zsSetSafeHtml=(node,html)=>{node.innerHTML=html};
const esc=String,encodeURIComponent=globalThis.encodeURIComponent;
const leadDetailBody=x=>`detail:${x.id}`;
const toast=message=>messages.push(message),publicPool=async()=>{},go=()=>{};
const api=(path,options)=>new Promise((resolve,reject)=>{calls.push({path,options});pending.push(resolve);failures.push(reject)});
const finish=(index,value)=>pending[index](value);
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", harness + handlers + extra_handlers + scenario],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    return json.loads(completed.stdout)


def test_completed_old_form_does_not_close_new_modal() -> None:
    outcome = run_lifecycle_flow(r"""
openPublicPoolImport();
const originalForm=nodes['#public-pool-import-form'];
const submission=originalForm.onsubmit({preventDefault(){}});
modal('后打开的弹窗','新内容');
finish(0,{created_count:1,dispatch_pool_count:0,public_pool_count:1,skipped_count:0});
await submission;
console.log(JSON.stringify({html:markup}));
""")
    assert "<h2>后打开的弹窗</h2>" in outcome["html"]


def test_slower_old_detail_does_not_overwrite_newer_detail() -> None:
    outcome = run_lifecycle_flow(r"""
const oldRequest=reviewDetail('old');
const newRequest=reviewDetail('new');
finish(1,{id:'new'});await newRequest;
finish(0,{id:'old'});await oldRequest;
console.log(JSON.stringify({html:markup}));
""")
    assert "detail:new" in outcome["html"]
    assert "detail:old" not in outcome["html"]


def test_navigation_invalidates_pending_detail() -> None:
    outcome = run_lifecycle_flow(r"""
const request=reviewDetail('old');
invalidateModalIntent();
finish(0,{id:'old'});await request;
console.log(JSON.stringify({html:markup}));
""")
    assert outcome["html"] == ""


def test_render_from_browser_navigation_invalidates_pending_detail() -> None:
    source = ADMIN.read_text(encoding="utf-8")
    render = source[source.index("async function render()"):source.index("const totalOf=")]
    outcome = run_lifecycle_flow(r"""
const S={view:'companies'};
const shell=()=>{};
const overview=async()=>{},review=overview,supplements=overview,telesales=overview,
 dispatch=overview,companies=overview,returns=overview,finance=overview,audit=overview,
 fullTrace=overview,settings=overview,internalUsers=overview,calendar=overview,account=overview;
const request=reviewDetail('old');
await render();
finish(0,{id:'old'});await request;
console.log(JSON.stringify({html:markup}));
""", render)
    assert outcome["html"] == ""


@pytest.mark.parametrize("selector,dataset", [
    ("data-platform-edit", "platformEdit"),
    ("data-lead-correction", "leadCorrection"),
])
def test_slow_edit_prefetch_cannot_replace_new_modal(selector: str, dataset: str) -> None:
    source = ADMIN.read_text(encoding="utf-8")
    binding = next(line for line in source.splitlines()
        if f"document.querySelectorAll('[{selector}]').forEach" in line)
    outcome = run_lifecycle_flow(
        "const button={dataset:" + json.dumps({dataset: "old"}) + "};\n" + r"""
document.querySelectorAll=()=>[button];
const openPlatformLeadForm=async item=>modal('旧编辑窗口',item.id);
""" + binding + r"""
const request=button.onclick();
modal('新窗口','正在输入');
finish(0,{id:'old'});await request;
console.log(JSON.stringify({html:markup}));
""")
    assert "<h2>新窗口</h2>" in outcome["html"]


@pytest.mark.parametrize("kind", ["username", "password"])
def test_successful_account_change_is_preserved_if_identity_refresh_fails(kind: str) -> None:
    source = ADMIN.read_text(encoding="utf-8")
    handlers = source[source.index("function changeOwnUsername("):source.index("async function companies(")]
    outcome = run_lifecycle_flow(r"""
const S={me:{username:'synthetic-user',has_password:true}};
const account=async()=>{};
""" + f"changeOwn{kind.title()}();\n" + r"""
for(const key of ['#username-current-password','#new-username','#current-password','#new-password','#confirm-password'])
 document.querySelector(key).value='synthetic-test-value';
""" + f"const form=nodes['#own-{kind}-form'],button=nodes['#own-{kind}-submit'];\n" + r"""
const request=form.onsubmit({preventDefault(){}});
finish(0,{});await Promise.resolve();await Promise.resolve();
failures[1](new Error('合成身份读取失败'));await request;
console.log(JSON.stringify({html:markup,connected:form.isConnected,disabled:button.disabled,messages,calls:calls.length}));
""", handlers)
    assert outcome["html"] == ""
    assert outcome["connected"] is False
    assert outcome["disabled"] is True
    assert outcome["calls"] == 2
    assert "已" in outcome["messages"][-1] and "刷新" in outcome["messages"][-1]


@pytest.mark.parametrize("action", ["reverseLedger", "settle", "settleDue", "reverse"])
def test_financial_action_success_is_not_reopened_if_list_refresh_fails(action: str) -> None:
    source = ADMIN.read_text(encoding="utf-8")
    handlers = source[source.index("function reverseLedger("):source.index("function newPointsPackage(")]
    handlers += source[source.index("function settle("):source.index("function notificationFailureAdvice(")]
    outcome = run_lifecycle_flow(r"""
const finance=async()=>{throw new Error('合成账目读取失败')};
""" + f"{action}('synthetic-id');\n" + r"""
nodes['#action-value'].value='合成操作说明';
const form=nodes['#action-form'],button=nodes['#action-submit'];
const request=form.onsubmit({preventDefault(){}});
finish(0,{});await request;
console.log(JSON.stringify({html:markup,connected:form.isConnected,disabled:button.disabled,messages,calls:calls.length}));
""", handlers)
    assert outcome["html"] == ""
    assert outcome["connected"] is False
    assert outcome["disabled"] is True
    assert outcome["calls"] == 1
    assert "已" in outcome["messages"][-1] and "刷新" in outcome["messages"][-1]


def company_form_handlers(name: str, end: str) -> str:
    source = ADMIN.read_text(encoding="utf-8")
    return r"""
const platformCities=async()=>[];
const serviceRegionBuilderMarkup=()=>'';
const bindServiceRegionBuilder=()=>({regionCodes:()=>['test-city'],primaryCityCode:()=>'test-city'});
const companyDetail=async company=>modal('旧公司详情',company.name);
""" + source[source.index(f"async function {name}("):source.index(end)]


def test_old_service_area_save_cannot_replace_new_modal() -> None:
    outcome = run_lifecycle_flow(r"""
await editCompanyServiceAreas({id:'old',name:'合成公司'},{service_areas:[]});
const request=nodes['#company-service-area-form'].onsubmit({preventDefault(){}});
modal('新窗口','正在输入');
finish(0,{});await request;
console.log(JSON.stringify({html:markup}));
""", company_form_handlers("editCompanyServiceAreas", "function editCompany("))
    assert "<h2>新窗口</h2>" in outcome["html"]


def test_company_created_before_slow_list_refresh_does_not_replace_new_modal() -> None:
    outcome = run_lifecycle_flow(r"""
let finishRefresh;
const companies=()=>new Promise(resolve=>{finishRefresh=resolve});
await openNewFranchiseCompany();
document.querySelector('#new-franchise-name').value='合成公司';
const request=nodes['#new-franchise-form'].onsubmit({preventDefault(){}});
finish(0,{id:'old',name:'合成公司'});
await Promise.resolve();await Promise.resolve();
modal('新窗口','正在输入');
finishRefresh();await request;
console.log(JSON.stringify({html:markup}));
""", company_form_handlers("openNewFranchiseCompany", "async function editCompanyServiceAreas("))
    assert "<h2>新窗口</h2>" in outcome["html"]
