from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest


def run_modal_flow(scenario: str) -> dict:
    source = Path("apps/admin/public/v12-operations.js").read_text(encoding="utf-8")
    handlers = "\n".join(
        source[source.index(start):source.index(end)]
        for start, end in (
            ("let modalIntent=", "function allowed("),
            ("function configureCompanyCapability(", "function copyText("),
            ("function revokeCompanyInvite(", "function serviceRegionBuilderMarkup("),
            ("function changeCompanyAccountStatus(", "function resetCompanyAccountPassword("),
        )
    )
    harness = """
const nodes={},calls=[];
let markup='';
const modalRoot={get innerHTML(){return markup},set innerHTML(value){
 for(const key of Object.keys(nodes)){nodes[key].isConnected=false;delete nodes[key]}
 markup=value;
}};
const document={body:{classList:{add(){},remove(){}}},
 querySelector:key=>nodes[key]??={value:'',isConnected:true,focus(){}}};
const zsSetSafeHtml=(node,html)=>{node.innerHTML=html};
const esc=String,isSuperAdmin=()=>true,toast=()=>{};
const COMPANY_CAPABILITY_LABEL={LEAD_SUPPLIER:'提供客资',LEAD_RECEIVER:'接收客资'};
const api=async(path,options)=>{calls.push({path,options});return {}};
const companyDetail=async()=>modal('加盟商详情','详情内容');
const companyAccounts=async()=>modal('账号与人员','账号列表');
const company={id:'test-company'};
const submit=()=>nodes['#action-form'].onsubmit({preventDefault(){}});
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", harness + handlers + scenario],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    return json.loads(completed.stdout)


@pytest.mark.parametrize(("action", "title"), [
    ("configureCompanyCapability(company.id,'LEAD_SUPPLIER',true,company)", "加盟商详情"),
    ("configureCompanyCapability(company.id,'LEAD_SUPPLIER',false,company)", "加盟商详情"),
    ("configureCompanyCapability(company.id,'LEAD_RECEIVER',true,company)", "加盟商详情"),
    ("configureCompanyCapability(company.id,'LEAD_RECEIVER',false,company)", "加盟商详情"),
    ("revokeCompanyInvite('test-invite',company)", "加盟商详情"),
    ("changeCompanyAccountStatus(company.id,'test-user','ACTIVE','测试公司')", "账号与人员"),
    ("changeCompanyAccountStatus(company.id,'test-user','DISABLED','测试公司')", "账号与人员"),
])
def test_completed_company_action_keeps_follow_up_modal(action: str, title: str) -> None:
    outcome = run_modal_flow(action + ";\n" + """
nodes['#action-value'].value='管理员核实';
await submit();
console.log(JSON.stringify({html:markup,calls}));
""")
    assert f"<h2>{title}</h2>" in outcome["html"]
    assert len(outcome["calls"]) == 1


@pytest.mark.parametrize("keep_open", [False, True])
def test_plain_action_preserves_existing_close_contract(keep_open: bool) -> None:
    outcome = run_modal_flow("""
actionForm({title:'普通操作'},async()=>""" + ("false" if keep_open else "undefined") + """);
await submit();console.log(JSON.stringify({html:markup}));
""")
    assert bool(outcome["html"]) is keep_open


def test_old_request_completion_does_not_close_a_new_form() -> None:
    outcome = run_modal_flow("""
let finish;
actionForm({title:'原表单'},()=>new Promise(resolve=>{finish=resolve}));
const pending=submit();
actionForm({title:'后打开的表单'},async()=>{});
finish();await pending;
console.log(JSON.stringify({html:markup}));
""")
    assert "<h2>后打开的表单</h2>" in outcome["html"]
