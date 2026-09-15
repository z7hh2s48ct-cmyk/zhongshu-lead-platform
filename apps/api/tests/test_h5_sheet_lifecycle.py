from __future__ import annotations

import json
from pathlib import Path
import subprocess


SOURCE = Path("apps/h5/public/v12-workbench.js")


def fragment(source: str, start: str, end: str) -> str:
    return source[source.index(start) : source.index(end)]


def run_js(source: str) -> dict:
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", source],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_slow_lead_detail_cannot_replace_a_newer_sheet() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    helpers = fragment(source, "let sheetIntent=", "function nav()")
    lead_detail = fragment(source, "async function leadDetail(", "function workbenchPager(")
    outcome = run_js(
        """
const sheet={innerHTML:''};
const document={querySelector:selector=>selector==='#sheet-close'?{onclick:null}:null};
const esc=value=>String(value??''),readableLabel=value=>value,fmt=value=>value;
const zsSetSafeHtml=(node,html)=>{node.innerHTML=html};
let resolveLead;
const api=()=>new Promise(resolve=>{resolveLead=resolve});
"""
        + helpers
        + lead_detail
        + """
const pending=leadDetail('old');
openSheet('new sheet','new input');
resolveLead({id:'old',customer_name:'old customer',status:'PENDING'});
await pending;
console.log(JSON.stringify({keptNew:sheet.innerHTML.includes('new sheet'),html:sheet.innerHTML}));
"""
    )
    assert outcome["keptNew"] is True


def test_slow_lead_details_keep_the_newest_request_and_respect_manual_close() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    helpers = fragment(source, "let sheetIntent=", "function nav()")
    lead_detail = fragment(source, "async function leadDetail(", "function workbenchPager(")
    outcome = run_js(
        """
const sheet={innerHTML:''};
const closeButton={onclick:null};
const document={querySelector:selector=>selector==='#sheet-close'?closeButton:null};
const esc=value=>String(value??''),readableLabel=value=>value,fmt=value=>value;
const zsSetSafeHtml=(node,html)=>{node.innerHTML=html};
const pending=new Map();
const api=path=>new Promise(resolve=>pending.set(path,resolve));
"""
        + helpers
        + lead_detail
        + """
const first=leadDetail('A');
const second=leadDetail('B');
pending.get('/v1.2/supplier/leads/B')({id:'B',customer_name:'newest',status:'PENDING'});
await second;
pending.get('/v1.2/supplier/leads/A')({id:'A',customer_name:'stale',status:'PENDING'});
await first;
const newestKept=sheet.innerHTML.includes('newest')&&!sheet.innerHTML.includes('stale');
const closed=leadDetail('C');
closeSheet();
pending.get('/v1.2/supplier/leads/C')({id:'C',customer_name:'closed request',status:'PENDING'});
await closed;
console.log(JSON.stringify({newestKept,stayedClosed:sheet.innerHTML===''}));
"""
    )
    assert outcome == {"newestKept": True, "stayedClosed": True}


def test_page_navigation_invalidates_a_pending_detail_request() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    helpers = fragment(source, "let sheetIntent=", "function nav()")
    go = fragment(source, "function go(", "function item(")
    render = fragment(source, "async function render(", "async function home(")
    lead_detail = fragment(source, "async function leadDetail(", "function workbenchPager(")
    outcome = run_js(
        """
const sheet={innerHTML:''};
const document={querySelector:selector=>selector==='#sheet-close'?{onclick:null}:null};
const location={href:'https://example.test/?view=leads'};
const history={replaceState:()=>{}};
const S={view:'leads',id:'',page:1};
const esc=value=>String(value??''),readableLabel=value=>value,fmt=value=>value;
const zsSetSafeHtml=(node,html)=>{node.innerHTML=html};
const canView=()=>true,defaultWorkbenchView=()=>'',toast=()=>{},shell=()=>{};
const home=async()=>{},profile=async()=>{},leadCenter=async()=>{},points=async()=>{};
const assignments=async()=>{},returns=async()=>{},businessReport=async()=>{};
const rewards=async()=>{},notifications=async()=>{};
let resolveLead;
const api=()=>new Promise(resolve=>{resolveLead=resolve});
"""
        + helpers
        + go
        + render
        + lead_detail
        + """
const detail=leadDetail('A');
go('profile');
await Promise.resolve();
resolveLead({id:'A',customer_name:'stale after navigation',status:'PENDING'});
await detail;
console.log(JSON.stringify({view:S.view,stayedClosed:sheet.innerHTML===''}));
"""
    )
    assert outcome == {"view": "profile", "stayedClosed": True}


def test_slow_password_submit_cannot_close_or_refresh_from_replaced_form() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    change_password = fragment(
        source, "function changeProfilePassword(", "async function profile()"
    )
    outcome = run_js(
        """
const S={me:{has_password:true}};
const nodes={
 '#profile-password-form':{isConnected:true},
 '#profile-password-submit':{disabled:false,isConnected:true},
 '#profile-password-current':{value:'old-password'},
 '#profile-password-next':{value:'new-password'},
 '#profile-password-confirm':{value:'new-password'},
};
const document={querySelector:key=>nodes[key]};
const esc=value=>String(value??'');
let sheetValue='',profileCalls=0,resolveChange;
const openSheet=(title,html,bind)=>{sheetValue=title;bind()};
const closeSheet=()=>{sheetValue=''};
const toast=()=>{};
const profile=async()=>{profileCalls+=1};
const api=async path=>{
 if(path==='/auth/change-password')return new Promise(resolve=>{resolveChange=resolve});
 if(path==='/auth/me')return {has_password:true};
 throw new Error(path);
};
"""
        + change_password
        + """
changeProfilePassword();
const pending=nodes['#profile-password-form'].onsubmit({preventDefault(){}});
nodes['#profile-password-form'].isConnected=false;
nodes['#profile-password-submit'].isConnected=false;
sheetValue='new sheet';
resolveChange({});
await pending;
console.log(JSON.stringify({sheetValue,profileCalls}));
"""
    )
    assert outcome == {"sheetValue": "new sheet", "profileCalls": 0}


def test_profile_mutation_success_is_not_reported_as_refresh_failure() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    profile_mutations = fragment(
        source, "function changeProfileUsername(", "async function profile()"
    )
    outcome = run_js(
        """
const S={me:{has_password:true,username:'old-user'}};
const nodes={
 '#profile-username-form':{isConnected:true},
 '#profile-username-submit':{disabled:false},
 '#profile-current-password':{value:'old-password'},
 '#profile-new-username':{value:'new-user'},
 '#profile-password-form':{isConnected:true},
 '#profile-password-submit':{disabled:false},
 '#profile-password-current':{value:'old-password'},
 '#profile-password-next':{value:'new-password'},
 '#profile-password-confirm':{value:'new-password'},
};
const document={querySelector:key=>nodes[key]};
const esc=value=>String(value??'');
const messages=[];
const toast=(text,error=false)=>messages.push({text,error});
const openSheet=(_title,_html,bind)=>bind();
const closeSheet=form=>{form.isConnected=false};
const profile=async()=>{throw new Error('profile refresh failed')};
const api=async path=>{
 if(path==='/auth/change-username'||path==='/auth/change-password')return {};
 if(path==='/auth/me')throw new Error('account refresh failed');
 throw new Error(path);
};
"""
        + profile_mutations
        + """
changeProfileUsername();
await nodes['#profile-username-form'].onsubmit({preventDefault(){}});
const username={messages:messages.splice(0),disabled:nodes['#profile-username-submit'].disabled,
 connected:nodes['#profile-username-form'].isConnected};
changeProfilePassword();
await nodes['#profile-password-form'].onsubmit({preventDefault(){}});
const password={messages:messages.splice(0),disabled:nodes['#profile-password-submit'].disabled,
 connected:nodes['#profile-password-form'].isConnected};
console.log(JSON.stringify({username,password}));
"""
    )
    for name in ("username", "password"):
        result = outcome[name]
        assert result["disabled"] is True
        assert result["connected"] is False
        assert result["messages"][-1]["error"] is True
        assert "已成功，请刷新查看" in result["messages"][-1]["text"]


def test_supply_save_success_is_not_reported_as_refresh_failure() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    save_supply = fragment(source, "async function saveSupplyLead(", "function supplyIdentityView(")
    outcome = run_js(
        """
const form={isConnected:true,dataset:{},querySelectorAll:()=>[{disabled:false}]};
const document={querySelector:key=>key==='#supply-form'?form:null};
const supplyPayload=()=>({phone:'13800138000'});
const validateSupplySubmission=()=>({}),validateSupplyDraft=()=>({});
const hasSupplyDraftContent=()=>true,showSupplyErrors=()=>{};
const api=async()=>({id:'lead-1'});
const messages=[],toast=(text,error=false)=>messages.push({text,error});
const closeSheet=owner=>{owner.isConnected=false};
const clearSupplyIntent=()=>{};
const leads=async()=>{throw new Error('list refresh failed')};
"""
        + save_supply
        + """
await saveSupplyLead(null,false);
console.log(JSON.stringify({messages,connected:form.isConnected,busy:form.dataset.busy}));
"""
    )
    assert outcome["connected"] is False
    assert outcome["busy"] == "1"
    assert outcome["messages"][-1]["error"] is True
    assert "已成功，请刷新查看" in outcome["messages"][-1]["text"]


def test_supply_delete_success_is_not_reported_as_refresh_failure() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    delete_supply = fragment(
        source, "function confirmSupplyLeadDeletion(", "async function leads()"
    )
    outcome = run_js(
        """
const owner={disabled:false,isConnected:true};
const nodes={'#cancel-supply-delete':{},'#confirm-supply-delete':owner};
const document={querySelector:key=>nodes[key]};
const openSheet=(_title,_html,bind)=>bind();
const closeSheet=node=>{node.isConnected=false};
const api=async()=>({});
const leads=async()=>{throw new Error('list refresh failed')};
const messages=[],toast=(text,error=false)=>messages.push({text,error});
"""
        + delete_supply
        + """
confirmSupplyLeadDeletion('lead-1');
await owner.onclick({currentTarget:owner});
console.log(JSON.stringify({messages,connected:owner.isConnected,disabled:owner.disabled}));
"""
    )
    assert outcome["connected"] is False
    assert outcome["disabled"] is True
    assert outcome["messages"][-1]["error"] is True
    assert "已成功，请刷新查看" in outcome["messages"][-1]["text"]


def test_township_lookup_keeps_the_latest_district_selection() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    loader = fragment(
        source, "async function loadSupplyTownships(", "function filterSupplyRegionOptions("
    )
    handler = fragment(
        source,
        "districtSelect.onchange=async event=>{",
        "document.querySelector('#supply-save-draft')",
    )
    outcome = run_js(
        """
const supplyState={townships:[{code:'original'}],districts:[],cities:[]};
const districtSelect={value:'A',isConnected:true};
const citySelect={value:''};
const townshipSelect={innerHTML:''};
const esc=value=>String(value??'');
const zsSetSafeHtml=(node,html)=>{node.innerHTML=html};
const filterSupplyRegionOptions=()=>{};
const pending=new Map();
const api=path=>new Promise(resolve=>pending.set(path,resolve));
"""
        + loader
        + handler
        + """
const first=districtSelect.onchange({target:{value:'A'}});
districtSelect.value='B';
const second=districtSelect.onchange({target:{value:'B'}});
pending.get('/master-data/regions?parent_code=B&level=TOWNSHIP')([{code:'B1',name:'B township'}]);
await second;
pending.get('/master-data/regions?parent_code=A&level=TOWNSHIP')([{code:'A1',name:'A township'}]);
await first;
console.log(JSON.stringify({html:townshipSelect.innerHTML,state:supplyState.townships}));
"""
    )
    assert "B township" in outcome["html"]
    assert "A township" not in outcome["html"]
    assert outcome["state"] == [{"code": "original"}]


def test_all_sheet_transitions_keep_the_original_owner() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    guarded_functions = {
        "changeProfileUsername": "async function profile()",
        "changeProfilePassword": "async function profile()",
        "saveSupplyLead": "function supplyIdentityView()",
        "confirmSupplyLeadDeletion": "async function leads()",
        "claim": "function refuseAssignment(",
        "refuseAssignment": "async function manageInternalAssignment(",
        "manageInternalAssignment": "function followupDraft(",
        "followupDraft": "function returnDraft(",
        "returnDraft": "async function uploadEvidenceBatch(",
    }
    for name, end in guarded_functions.items():
        block = fragment(source, f"function {name}(", end)
        assert ".isConnected" in block, name


def test_every_async_sheet_loader_reserves_and_checks_an_intent() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    loaders = {
        "openSupplyForm": "function launchSupplyForm(",
        "editSupplyLead": "async function reviseSupplyLead(",
        "reviseSupplyLead": "function confirmSupplyLeadDeletion(",
        "leadDetail": "function workbenchPager(",
        "assignmentDetail": "async function claim(",
        "manageInternalAssignment": "function followupDraft(",
        "returnDetail": "function rewardExplanation(",
        "rewardDetail": "async function notifications(",
    }
    for name, end in loaders.items():
        block = fragment(source, f"async function {name}(", end)
        assert "beginSheetIntent" in block, name
        assert ",intent)" in block, name
