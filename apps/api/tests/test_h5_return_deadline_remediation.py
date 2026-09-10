from __future__ import annotations

import json
from pathlib import Path
import subprocess


SOURCE = Path("apps/h5/public/v12-workbench.js")


def run_js(source: str) -> dict:
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", source],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


def test_deadline_rejects_exact_boundary_and_refreshes_open_button() -> None:
    source = SOURCE.read_text()
    helpers = source[source.index("function deadlineState("):source.index("const num=")]
    outcome = run_js(helpers + """
const now = Date.parse('2026-09-12T10:00:00Z');
const button = {dataset:{actionDeadline:'2026-09-12T10:00:00Z'},disabled:false};
const text = {dataset:{deadline:'2026-09-12T10:00:00Z'},textContent:''};
const document = {querySelectorAll:selector=>selector==='[data-action-deadline]'?[button]:[text]};
refreshDeadlineControls(now);
console.log(JSON.stringify({before:deadlineState('2026-09-12T10:00:00Z',now-1).allowed,
 at:deadlineState('2026-09-12T10:00:00Z',now).allowed,disabled:button.disabled,text:text.textContent,
 missing:deadlineState(null,now).allowed}));
""")
    assert outcome == {"before": True, "at": False, "disabled": True, "text": "已截止", "missing": False}


def test_pagination_handles_twenty_first_record_and_multiple_status_pages() -> None:
    source = SOURCE.read_text()
    helpers = source[source.index("function workbenchPager("):source.index("async function assignments()")]
    outcome = run_js("const S={page:1};" + helpers + """
const one = workbenchPager([{total:21},{total:41}]);
S.page=3;
const last = workbenchPager([{total:21},{total:41}]);
console.log(JSON.stringify({one,last}));
""")
    assert "第 1 / 3 页" in outcome["one"]
    assert 'id="records-next" disabled' in outcome["last"]
    assert "第 3 / 3 页" in outcome["last"]


def test_return_evidence_requires_new_material_only_for_supplement_round() -> None:
    source = SOURCE.read_text()
    helpers = source[source.index("function evidence(returnId,"):source.index("async function businessReport()")]
    outcome = run_js("const esc=x=>String(x??'');let html='';const openSheet=(_,body)=>{html=body};" + helpers + """
evidence('r',{CHAT_SCREENSHOT:1},{status:'NEED_MORE_EVIDENCE',supplementary_evidence_count:0});
const oldOnly = /id="submit-return"[^>]*disabled/.test(html);
evidence('r',{CHAT_SCREENSHOT:2},{status:'NEED_MORE_EVIDENCE',supplementary_evidence_count:1});
const newEvidence = /id="submit-return"[^>]*disabled/.test(html);
console.log(JSON.stringify({oldOnly,newEvidence}));
""")
    assert outcome == {"oldOnly": True, "newEvidence": False}


def test_formal_workbench_wires_deadlines_draft_resume_and_completed_returns() -> None:
    source = SOURCE.read_text()
    assert "claim_deadline_at" not in source
    assert "deadlineNotice(x.expires_at" in source
    assert "'CLAIMED','FOLLOWING','RETURN_PENDING','COMPLETED'" in source
    assert "['CLAIMED','FOLLOWING','COMPLETED'].includes(x.status)" in source
    assert "['DRAFT','NEED_MORE_EVIDENCE'].includes(x.status)" in source
    assert "returnStatusLabel(x.status)" in source
    assert "setInterval(refreshDeadlineControls,1000)" in source
    returns = source[source.index("async function returns()"):source.index("async function returnDetail(")]
    assert "workbenchPager([d])" in returns
    assert "bindWorkbenchPager(returns)" in returns


def test_followup_pages_allow_every_record_in_each_status_to_be_opened() -> None:
    source = SOURCE.read_text()
    functions = source[source.index("function workbenchPager("):source.index("async function assignmentDetail(")]
    outcome = run_js("""
const S={page:1,view:'followups',me:{company_id:'company-1'}};
const counts={CLAIMED:21,FOLLOWING:41,RETURN_PENDING:0,COMPLETED:1};
const visible=[];const next={addEventListener:(_,fn)=>{next.click=fn}};
const document={querySelectorAll:()=>[],querySelector:()=>next};
const isFranchiseOwner=()=>false,can=()=>false,canClaimAssignment=()=>false;
const esc=x=>String(x??''),readableLabel=x=>x,fmt=x=>x;
const deadlineNotice=()=>'',deadlineButtonAttributes=()=>'';
const toast=()=>{},shell=()=>{},item=(title)=>{visible.push(title);return title};
const api=async url=>{const q=new URL(url,'https://example.test').searchParams;
 const status=q.get('status'),page=Number(q.get('page')),total=counts[status];
 return {total,items:Array.from({length:Math.max(0,Math.min(20,total-(page-1)*20))},(_,i)=>({
  id:`${status}-${(page-1)*20+i+1}`,customer_name:`${status}-${(page-1)*20+i+1}`,status
 }))}};
""" + functions + """
await assignments();await next.click();await next.click();
console.log(JSON.stringify({count:visible.length,unique:new Set(visible).size,
 hasTwentyFirst:visible.includes('CLAIMED-21'),hasLast:visible.includes('FOLLOWING-41'),
 completed:visible.includes('COMPLETED-1'),page:S.page}));
""")
    assert outcome == {"count": 63, "unique": 63, "hasTwentyFirst": True, "hasLast": True, "completed": True, "page": 3}
