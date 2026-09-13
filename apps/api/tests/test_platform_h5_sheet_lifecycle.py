from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest


def run_sheet_flow(scenario: str) -> dict:
    source = Path("apps/admin/public/h5/app.js").read_text(encoding="utf-8")
    handlers = source[source.index("function closeSheet("):source.index("function routeName(")]
    handlers += source[source.index("async function reconcile("):source.index("function operationLeadCard(")]
    harness = """
const nodes={},pending=[],messages=[];
let markup='',refreshes=0,refreshFails=false;
const sheetRoot={get innerHTML(){return markup},set innerHTML(value){
 for(const key of Object.keys(nodes)){nodes[key].isConnected=false;delete nodes[key]}
 markup=value;
}};
const document={querySelector:key=>nodes[key]??={value:'',checked:false,isConnected:true,
 selectedOptions:[{value:'test-package',dataset:{cash:'100'}}]}};
const zsSetSafeHtml=(node,html)=>{node.innerHTML=html};
const esc=String,toast=(message,error=false)=>messages.push({message,error});
const funds=async()=>{refreshes++;if(refreshFails)throw new Error('合成读取失败')};
const S={sheetSequence:0,fundData:{companies:[{id:'test-company',name:'测试公司'}],
 packages:[{id:'test-package',cash_amount_cents:100,total_points:10,name:'合成档位'}]}};
const api=(path,options)=>new Promise((resolve,reject)=>pending.push({path,options,resolve,reject}));
const start=(kind)=>{
 ({recharge:rechargeSheet,adjustment:adjustmentSheet,reversal:reversalSheet})[kind]('test-company');
 for(const key of ['#recharge-reference','#recharge-note','#adjustment-reason','#reversal-reason'])
  document.querySelector(key).value='合成测试说明';
 document.querySelector('#recharge-confirmed').checked=true;
 document.querySelector('#adjustment-delta').value='10';
 return nodes[`#${kind}-form`].onsubmit({preventDefault(){}});
};
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", harness + handlers + scenario],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    return json.loads(completed.stdout)


@pytest.mark.parametrize("kind", ["recharge", "adjustment", "reversal"])
@pytest.mark.parametrize("failed", [False, True])
def test_old_fund_action_cannot_clear_new_sheet(kind: str, failed: bool) -> None:
    complete = "reject(new Error('合成失败'))" if failed else "resolve({})"
    outcome = run_sheet_flow(f"const submitted=start({json.dumps(kind)});\n" + """
closeSheet();adjustmentSheet('test-company');
document.querySelector('#adjustment-reason').value='尚未提交的新说明';
""" + f"pending[0].{complete};await submitted;\n" + """
console.log(JSON.stringify({html:markup,draft:nodes['#adjustment-reason']?.value,refreshes}));
""")
    assert 'id="adjustment-form"' in outcome["html"]
    assert outcome["draft"] == "尚未提交的新说明"
    assert outcome["refreshes"] == 0


@pytest.mark.parametrize("kind", ["recharge", "adjustment", "reversal"])
def test_current_fund_action_closes_after_success_and_refreshes(kind: str) -> None:
    outcome = run_sheet_flow(f"const submitted=start({json.dumps(kind)});\n" + """
pending[0].resolve({});await submitted;
console.log(JSON.stringify({html:markup,refreshes,messages,calls:pending.length}));
""")
    assert outcome["html"] == ""
    assert outcome["refreshes"] == outcome["calls"] == 1
    assert not outcome["messages"][-1]["error"]


@pytest.mark.parametrize("kind", ["recharge", "adjustment", "reversal"])
def test_successful_fund_action_is_not_reported_as_failed_when_refresh_fails(kind: str) -> None:
    outcome = run_sheet_flow("refreshFails=true;\n" + f"const submitted=start({json.dumps(kind)});\n"
        + f"const button=nodes['#{kind}-submit'];\n" + """
pending[0].resolve({});await submitted;
console.log(JSON.stringify({html:markup,refreshes,messages,calls:pending.length,disabled:button.disabled}));
""")
    assert outcome["html"] == ""
    assert outcome["calls"] == outcome["refreshes"] == 1
    assert outcome["disabled"] is True
    assert "已" in outcome["messages"][-1]["message"]
    assert "刷新" in outcome["messages"][-1]["message"]
    assert all(item["message"] != "合成读取失败" for item in outcome["messages"])


def test_old_reconciliation_result_does_not_replace_new_operation() -> None:
    outcome = run_sheet_flow("""
const loading=reconcile('test-company');
adjustmentSheet('test-company');
pending[0].resolve({balanced:true});await loading;
console.log(JSON.stringify({html:markup}));
""")
    assert 'id="adjustment-form"' in outcome["html"]


def test_only_latest_reconciliation_request_displays_its_result() -> None:
    outcome = run_sheet_flow("""
const first=reconcile('first-company'),second=reconcile('second-company');
pending[1].resolve({balanced:true,expected_closing_balance:222});await second;
pending[0].resolve({balanced:false,expected_closing_balance:111});await first;
console.log(JSON.stringify({html:markup}));
""")
    assert "账目核对一致" in outcome["html"]
    assert "222" in outcome["html"]
    assert "111" not in outcome["html"]
