from __future__ import annotations

import json
from pathlib import Path
import subprocess


WORKBENCH = Path("apps/h5/public/v12-workbench.js")
ADMIN = Path("apps/admin/public/v12-operations.js")


def _fragment(source: str, start: str, end: str) -> str:
    return source[source.index(start) : source.index(end)]


def _run_claim_flow(scenario: str) -> dict:
    source = WORKBENCH.read_text(encoding="utf-8")
    handlers = _fragment(source, "function normalizedClaimPoints", "function refuseAssignment(")
    harness = r"""
const calls=[],messages=[],sheets=[];
let detail={id:'assignment-1',status:'PENDING_CLAIM',points_price:100};
let postResult={assignment:{points_price:100},ledger:{delta:-100}};
let postError=null,rendered=0;
const nodes={};
const document={querySelector:key=>nodes[key]??={disabled:false,isConnected:true,dataset:{},addEventListener(_name,handler){this.onclick=handler}}};
const esc=String;
let sheetIntent=0;
const beginSheetIntent=()=>++sheetIntent;
const closeSheet=(owner,intent)=>{if(!owner?.isConnected||intent!==sheetIntent)return false;owner.isConnected=false;++sheetIntent;return true};
const openSheet=(title,html,bind,intent=null)=>{
 if(intent===null)++sheetIntent;
 else if(intent!==sheetIntent)return false;
 sheets.push({title,html});bind?.();return true;
};
const toast=(message,error=false)=>messages.push({message,error});
const render=async()=>{rendered++};
let api=async(path,options={})=>{
 calls.push({path,options});
 if(options.method==='POST'){
  if(postError)throw postError;
  return postResult;
 }
 return detail;
};
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", harness + handlers + scenario],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return json.loads(completed.stdout)


def test_claim_rechecks_detail_and_posts_the_price_the_user_saw() -> None:
    outcome = _run_claim_flow(r"""
const button={disabled:false,isConnected:true,dataset:{}};
await claim('assignment-1',button,100);
console.log(JSON.stringify({calls,messages,rendered,button}));
""")
    assert [item["path"] for item in outcome["calls"]] == [
        "/v1.2/assignments/assignment-1",
        "/v1.2/assignments/assignment-1/claim",
    ]
    assert json.loads(outcome["calls"][1]["options"]["body"]) == {"expected_points": 100}
    assert outcome["messages"][-1] == {"message": "领取成功，实际扣除 100 积分", "error": False}
    assert outcome["rendered"] == 1


def test_price_change_requires_an_explicit_second_confirmation() -> None:
    outcome = _run_claim_flow(r"""
detail.points_price=150;
const button={disabled:false,isConnected:true,dataset:{}};
await claim('assignment-1',button,100);
const beforeConfirm=calls.length;
await nodes['#confirm-current-price-claim'].onclick({currentTarget:nodes['#confirm-current-price-claim']});
console.log(JSON.stringify({calls,messages,sheets,beforeConfirm}));
""")
    assert outcome["beforeConfirm"] == 1
    assert "100" in outcome["sheets"][0]["html"]
    assert "150" in outcome["sheets"][0]["html"]
    assert "按最新 150 积分领取" in outcome["sheets"][0]["html"]
    assert json.loads(outcome["calls"][1]["options"]["body"]) == {"expected_points": 150}


def test_conflict_shows_the_new_price_without_automatic_retry() -> None:
    outcome = _run_claim_flow(r"""
postError=Object.assign(new Error('领取积分已更新'),{code:'CLAIM_PRICE_CHANGED',status:409,
 details:{expected_points:100,current_points:180}});
const button={disabled:false,isConnected:true,dataset:{}};
await claim('assignment-1',button,100);
console.log(JSON.stringify({calls,messages,sheets,button}));
""")
    assert len(outcome["calls"]) == 2
    assert "100" in outcome["sheets"][0]["html"]
    assert "180" in outcome["sheets"][0]["html"]
    assert not any(not item["error"] for item in outcome["messages"])
    assert outcome["button"]["disabled"] is False


def test_double_click_does_not_start_two_claims() -> None:
    outcome = _run_claim_flow(r"""
let release;
api=async(path,options={})=>{
 calls.push({path,options});
 if(options.method==='POST')return postResult;
 await new Promise(resolve=>{release=resolve});
 return detail;
};
const button={disabled:false,isConnected:true,dataset:{}};
const first=claim('assignment-1',button,100);
const second=claim('assignment-1',button,100);
release();await Promise.all([first,second]);
console.log(JSON.stringify({calls}));
""")
    assert [item["path"] for item in outcome["calls"]] == [
        "/v1.2/assignments/assignment-1",
        "/v1.2/assignments/assignment-1/claim",
    ]


def test_stale_preflight_cannot_reopen_price_confirmation() -> None:
    outcome = _run_claim_flow(r"""
detail.points_price=150;
let release;
api=async(path,options={})=>{
 calls.push({path,options});
 await new Promise(resolve=>{release=resolve});
 return detail;
};
const button={disabled:false,isConnected:true,dataset:{}};
const pending=claim('assignment-1',button,100);
beginSheetIntent();
release();await pending;
console.log(JSON.stringify({sheets,messages}));
""")
    assert outcome["sheets"] == []
    assert outcome["messages"] == []


def test_stale_price_conflict_cannot_replace_newer_sheet() -> None:
    outcome = _run_claim_flow(r"""
let rejectPost;
api=async(path,options={})=>{
 calls.push({path,options});
 if(options.method==='POST')return new Promise((_resolve,reject)=>{rejectPost=reject});
 return detail;
};
const button={disabled:false,isConnected:true,dataset:{}};
const pending=claim('assignment-1',button,100);
await Promise.resolve();await Promise.resolve();
beginSheetIntent();
rejectPost(Object.assign(new Error('领取积分已更新'),{code:'CLAIM_PRICE_CHANGED',status:409,
 details:{expected_points:100,current_points:180}}));
await pending;
console.log(JSON.stringify({sheets,messages}));
""")
    assert outcome["sheets"] == []


def test_assignment_list_labels_pending_and_historical_points_separately() -> None:
    source = WORKBENCH.read_text(encoding="utf-8")
    assignments = _fragment(source, "async function assignments()", "async function assignmentDetail(")
    assert "x.status==='PENDING_CLAIM'?'当前领取积分':'实际扣除积分'" in assignments


def test_api_keeps_conflict_details_for_price_refresh() -> None:
    source = WORKBENCH.read_text(encoding="utf-8")
    api = _fragment(source, "async function api(", "function toast(")
    script = r"""
const API='/api/v1',S={};
const FormData=globalThis.FormData;
const fetch=async()=>({ok:false,status:409,headers:{get:()=>''},json:async()=>({
 code:'CLAIM_PRICE_CHANGED',message:'领取积分已更新',
 details:{expected_points:100,current_points:150}
})});
""" + api + r"""
try{await api('/test')}catch(error){console.log(JSON.stringify({code:error.code,status:error.status,details:error.details}))}
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert json.loads(completed.stdout) == {
        "code": "CLAIM_PRICE_CHANGED",
        "status": 409,
        "details": {"expected_points": 100, "current_points": 150},
    }


def test_admin_explains_current_claim_price_and_preserves_historical_charges() -> None:
    source = ADMIN.read_text(encoding="utf-8")
    section = _fragment(source, "function leadPointsSettingsSection", "const SUPPLY_TERMINATION_BLOCKER_LABEL")
    assert "客资领取积分（积分/条）" in section
    assert "所有来源" in section
    assert "领取时按后台最新设置生效" in section
    assert "历史已扣费记录保持原金额" in section
    assert "供客积分" in section
    assert "上方未启用统一客资领取积分时" in source
    assert "此处规则继续适用于其他来源" not in source
