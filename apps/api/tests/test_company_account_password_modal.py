from __future__ import annotations

import json
from pathlib import Path
import subprocess


ADMIN = Path("apps/admin/public/v12-operations.js")


def run_reset_flow(scenario: str) -> dict:
    source = ADMIN.read_text(encoding="utf-8")
    form_handlers = source[source.index("function closeModal("):source.index("function allowed(")]
    password_handlers = source[
        source.index("function resetCompanyAccountPassword("):source.index("async function dispatch(")
    ]
    harness = """
const nodes={},messages=[],calls=[],copied=[],refreshed=[];
const modalRoot={innerHTML:''};
const document={body:{classList:{add(){},remove(){}}},
 querySelector:key=>nodes[key]??={value:'',focus(){}}};
const zsSetSafeHtml=(node,html)=>{node.innerHTML=html};
const esc=String,isSuperAdmin=()=>true;
const toast=(message,error=false)=>messages.push({message,error});
const companyAccounts=(...args)=>refreshed.push(args);
let resetFailure=false,copyFailure=false;
const api=async(path,options)=>{
 calls.push({path,body:JSON.parse(options.body)});
 if(resetFailure)throw new Error('重置失败');
 return {initial_password:'SyntheticOnly9'};
};
const navigator={clipboard:{writeText:async value=>{
 if(copyFailure)throw new Error('剪贴板不可用');
 copied.push(value);
}}};
const submit=()=>nodes['#action-form'].onsubmit({preventDefault(){}});
const result=()=>({html:modalRoot.innerHTML,messages:[...messages],calls:[...calls],
 copied:[...copied],refreshed:[...refreshed],
 disabled:nodes['#action-submit'].disabled});
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", harness + form_handlers + password_handlers + """
resetCompanyAccountPassword('test-company','test-user','测试公司');
nodes['#action-value'].value='用户申请';
""" + scenario],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    return json.loads(completed.stdout)


def test_reset_keeps_new_password_visible_after_copy_until_saved() -> None:
    outcome = run_reset_flow("""
await submit();
const shown=result();
await nodes['#copy-initial-password'].onclick();
const afterCopy=result();
nodes['#initial-password-close'].onclick();
console.log(JSON.stringify({shown,afterCopy,afterSave:result()}));
""")
    assert 'id="initial-password">SyntheticOnly9</b>' in outcome["shown"]["html"]
    assert outcome["afterCopy"]["html"] == outcome["shown"]["html"]
    assert outcome["afterCopy"]["copied"] == ["SyntheticOnly9"]
    assert outcome["afterCopy"]["refreshed"] == []
    assert outcome["afterSave"]["html"] == ""
    assert outcome["afterSave"]["refreshed"] == [["test-company", "测试公司"]]
    assert outcome["shown"]["calls"] == [{
        "path": "/companies/test-company/accounts/test-user/reset-password",
        "body": {"reason": "用户申请"},
    }]


def test_copy_failure_keeps_password_visible_for_manual_copy() -> None:
    outcome = run_reset_flow("""
await submit();copyFailure=true;
await nodes['#copy-initial-password'].onclick();
console.log(JSON.stringify(result()));
""")
    assert 'id="initial-password">SyntheticOnly9</b>' in outcome["html"]
    assert outcome["copied"] == []
    assert outcome["messages"] == [{"message": "浏览器不支持自动复制，请手动复制", "error": True}]


def test_reset_failure_preserves_form_and_allows_retry() -> None:
    outcome = run_reset_flow("""
resetFailure=true;await submit();
console.log(JSON.stringify(result()));
""")
    assert 'id="action-form"' in outcome["html"]
    assert 'id="initial-password"' not in outcome["html"]
    assert outcome["disabled"] is False
    assert outcome["messages"] == [{"message": "重置失败", "error": True}]
