from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest


H5 = Path("apps/h5/public/v12-workbench.js")
ADMIN = Path("apps/admin/public/v12-operations.js")


def run_evidence(scenario: str) -> dict:
    """Run the real form handlers with synthetic files and in-memory API responses."""
    source = H5.read_text(encoding="utf-8")
    handlers = source[
        source.index("async function uploadEvidenceBatch") : source.index(
            "async function businessReport()"
        )
    ]
    harness = """
const events=[],messages=[];
let html='',closed=0,navigated=0,allowed=true;
const esc=value=>String(value??'');
const deadlineNotice=()=>'',deadlineButtonAttributes=()=>'';
const deadlineState=()=>({allowed});
const refreshDeadlineControls=()=>{};
const toast=(message,error=false)=>messages.push({message,error});
const closeSheet=()=>{closed++};
const go=()=>{navigated++};
const zsSetSafeHtml=(node,value)=>{node.innerHTML=value};
const screenshot={name:'synthetic-screenshot.png'};
const recording={name:'synthetic-recording.mp3'};
const form={elements:{chat_screenshots:{files:[]},call_recording:{files:[]}}};
const nodes={
 '#evidence-form':form,'#upload-evidence':{},'#submit-return':{},
 '#submit-saved-evidence':{},'#evidence-progress':{},'#evidence-file-results':{},
};
const document={querySelector:selector=>nodes[selector]};
const openSheet=(_,body,bind)=>{html=body;bind()};
class FormData {
 constructor(){this.values={}}
 append(key,value){this.values[key]=value}
 get(key){return this.values[key]}
}
const confirmed={status:'VERIFYING',submitted_at:'2026-09-12T01:00:00Z',verification_task_id:'test-task'};
let upload=async()=>({}),submit=async()=>confirmed,read=async()=>({supplementary_evidence_count:1});
const api=async(path,options={})=>{
 if(path.endsWith('/evidence')){const file=options.body.get('file');events.push(`upload:${file.name}`);return upload(file)}
 if(path.endsWith('/submit')){events.push('submit');return submit()}
 events.push('read');return read();
};
const click=()=>form.onsubmit({preventDefault(){}});
const select=(screenshots=[screenshot],audio=[])=>{
 form.elements.chat_screenshots.files=screenshots;
 form.elements.call_recording.files=audio;
 form.onchange?.();
};
const result=()=>({events,messages,closed,navigated,html,
 progress:nodes['#evidence-progress'].textContent,
 disabled:nodes['#submit-return'].disabled,
 savedHidden:nodes['#submit-saved-evidence'].hidden});
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", harness + handlers + scenario],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(completed.stdout)


def test_one_click_uploads_selected_files_and_formally_submits() -> None:
    outcome = run_evidence("""
evidence('test-return',{},{});
select([screenshot],[recording]);
const enabledAfterSelection=!nodes['#submit-return'].disabled;
await click();
console.log(JSON.stringify({...result(),enabledAfterSelection}));
""")
    assert outcome["events"] == [
        "upload:synthetic-screenshot.png", "upload:synthetic-recording.mp3", "submit"
    ]
    assert outcome["enabledAfterSelection"]
    assert outcome["closed"] == outcome["navigated"] == 1
    assert 'id="upload-evidence"' not in outcome["html"]
    assert 'id="submit-return"' in outcome["html"]


def test_saved_draft_submits_without_reuploading_evidence() -> None:
    outcome = run_evidence("""
evidence('test-return',{CHAT_SCREENSHOT:1},{});
await click();console.log(JSON.stringify(result()));
""")
    assert outcome["events"] == ["submit"]
    assert outcome["closed"] == 1


def test_submission_failure_preserves_uploaded_files_for_retry() -> None:
    outcome = run_evidence("""
let attempts=0;
submit=async()=>{if(++attempts===1)throw new Error('测试网络中断');return confirmed};
evidence('test-return',{},{});select();await click();
const failed=result();await click();
console.log(JSON.stringify({...result(),failed}));
""")
    assert outcome["failed"]["closed"] == 0
    assert "未确认提交成功" in outcome["failed"]["progress"]
    assert not outcome["failed"]["disabled"]
    assert outcome["events"] == ["upload:synthetic-screenshot.png", "submit", "read", "submit"]
    assert outcome["closed"] == 1


def test_partial_upload_failure_stops_submission_and_retries_only_failed_files() -> None:
    outcome = run_evidence("""
let failedOnce=false;
upload=async file=>{if(file===recording&&!failedOnce){failedOnce=true;throw new Error('测试上传失败')}};
evidence('test-return',{},{});select([screenshot],[recording]);await click();
const failed=result();await click();
console.log(JSON.stringify({...result(),failed}));
""")
    assert "未提交" in outcome["failed"]["progress"]
    assert outcome["failed"]["closed"] == 0
    assert not outcome["failed"]["savedHidden"]
    assert outcome["events"] == [
        "upload:synthetic-screenshot.png", "upload:synthetic-recording.mp3",
        "upload:synthetic-recording.mp3", "submit",
    ]
    assert outcome["closed"] == 1


def test_partial_upload_can_explicitly_submit_only_saved_evidence() -> None:
    outcome = run_evidence("""
upload=async file=>{if(file===recording)throw new Error('测试上传失败')};
evidence('test-return',{},{});select([screenshot],[recording]);await click();
await nodes['#submit-saved-evidence'].onclick();
console.log(JSON.stringify(result()));
""")
    assert outcome["events"] == [
        "upload:synthetic-screenshot.png", "upload:synthetic-recording.mp3", "submit"
    ]
    assert outcome["closed"] == 1


def test_all_uploads_failed_never_submits_or_reports_success() -> None:
    outcome = run_evidence("""
upload=async()=>{throw new Error('测试上传失败')};
evidence('test-return',{},{});select();await click();
console.log(JSON.stringify(result()));
""")
    assert outcome["events"] == ["upload:synthetic-screenshot.png"]
    assert outcome["closed"] == 0
    assert outcome["savedHidden"]
    assert "未提交" in outcome["progress"]
    assert not any(not message["error"] for message in outcome["messages"])


@pytest.mark.parametrize("response", [
    {"status": "DRAFT"},
    {"status": "DRAFT", "submitted_at": "2026-09-12T01:00:00Z", "verification_task_id": "test-task"},
    {"status": "VERIFYING", "submitted_at": None, "verification_task_id": "test-task"},
    {"status": "VERIFYING", "submitted_at": "2026-09-12T01:00:00Z", "verification_task_id": None},
])
def test_success_message_requires_a_confirmed_formal_submission(response: dict) -> None:
    outcome = run_evidence(
        "submit=async()=>(" + json.dumps(response) + ");\n"
        + "evidence('test-return',{CHAT_SCREENSHOT:1},{});await click();"
        + "console.log(JSON.stringify(result()));"
    )
    assert outcome["events"] == ["submit"]
    assert outcome["closed"] == 0
    assert "未确认提交成功" in outcome["progress"]
    assert outcome["messages"][-1]["error"]


def test_double_click_is_ignored_until_submission_finishes() -> None:
    outcome = run_evidence("""
let release,started;
const submitting=new Promise(resolve=>{started=resolve});
submit=async()=>{started();await new Promise(resolve=>{release=resolve});return confirmed};
evidence('test-return',{},{});select();const pending=click();await submitting;
await click();const busyDisabled=nodes['#submit-return'].disabled;
release();await pending;console.log(JSON.stringify({...result(),busyDisabled}));
""")
    assert outcome["events"] == ["upload:synthetic-screenshot.png", "submit"]
    assert outcome["busyDisabled"]
    assert outcome["closed"] == 1


def test_deadline_is_checked_before_upload_and_again_before_submit() -> None:
    expired = run_evidence("""
allowed=false;evidence('test-return',{},{});select();await click();
console.log(JSON.stringify(result()));
""")
    assert expired["events"] == []
    during_upload = run_evidence("""
upload=async()=>{allowed=false};evidence('test-return',{},{});select();await click();
console.log(JSON.stringify(result()));
""")
    assert during_upload["events"] == ["upload:synthetic-screenshot.png"]
    assert during_upload["closed"] == 0
    assert "未提交" in during_upload["progress"]


def test_supplement_requires_confirmed_new_evidence_and_retries_failed_read() -> None:
    outcome = run_evidence("""
let reads=0;
read=async()=>{if(++reads===1)throw new Error('测试读取失败');return {supplementary_evidence_count:1}};
evidence('test-return',{CHAT_SCREENSHOT:1},{status:'NEED_MORE_EVIDENCE'});
select();await click();const failed=result();await click();
console.log(JSON.stringify({...result(),failed}));
""")
    assert outcome["failed"]["closed"] == 0
    assert "未提交" in outcome["failed"]["progress"]
    assert outcome["events"] == ["upload:synthetic-screenshot.png", "read", "read", "submit"]
    assert outcome["closed"] == 1
    duplicate = run_evidence("""
read=async()=>({supplementary_evidence_count:0});
evidence('test-return',{CHAT_SCREENSHOT:1},{status:'NEED_MORE_EVIDENCE'});select();await click();
console.log(JSON.stringify(result()));
""")
    assert "submit" not in duplicate["events"]
    assert "新增" in duplicate["progress"]


def test_return_draft_label_does_not_change_other_lead_draft_labels() -> None:
    for path in (H5, ADMIN):
        source = path.read_text(encoding="utf-8")
        assert "DRAFT:'待完善'" in source
        assert "status==='DRAFT'?'待提交'" in source
        start = source.index("async function returns()")
        end = source.index("function finalReview" if path == ADMIN else "function rewardExplanation", start)
        return_pages = source[start:end]
        assert "returnStatusLabel(x.status)" in return_pages
        assert "x.submitted_at||x.created_at" not in return_pages
        assert "尚未提交" in return_pages


def test_supplement_retry_recognizes_a_submission_whose_response_was_lost() -> None:
    outcome = run_evidence("""
let submitted=false;
read=async()=>submitted?confirmed:{supplementary_evidence_count:1};
submit=async()=>{submitted=true;throw new Error('测试响应丢失')};
evidence('test-return',{}, {status:'NEED_MORE_EVIDENCE'});select();await click();
const failed=result();await click();
console.log(JSON.stringify({...result(),failed}));
""")
    assert outcome["failed"]["closed"] == 0
    assert outcome["events"] == ["upload:synthetic-screenshot.png", "read", "submit", "read"]
    assert outcome["closed"] == outcome["navigated"] == 1


@pytest.mark.parametrize("original_status", ["DRAFT", "NEED_MORE_EVIDENCE"])
@pytest.mark.parametrize("advanced_status", ["NEED_MORE_EVIDENCE", "APPROVED", "REJECTED"])
def test_uncertain_submit_recovers_a_record_that_has_already_advanced(original_status, advanced_status) -> None:
    scenario = "const originalStatus=" + json.dumps(original_status) + ";const advancedStatus=" + json.dumps(advanced_status) + ";"
    outcome = run_evidence(scenario + """
let attempted=false;
read=async()=>attempted?{...confirmed,status:advancedStatus,verification_task_id:'new-test-task'}:{supplementary_evidence_count:1};
submit=async()=>{attempted=true;throw new Error('测试响应丢失')};
evidence('test-return',{}, {status:originalStatus,verification_task_id:'old-test-task'});
select();await click();await click();console.log(JSON.stringify(result()));
""")
    assert outcome["events"].count("upload:synthetic-screenshot.png") == 1
    assert outcome["events"].count("submit") == 1
    assert outcome["closed"] == outcome["navigated"] == 1
    assert "状态已更新" in outcome["messages"][-1]["message"]


def test_old_supplement_round_does_not_count_as_a_successful_retry() -> None:
    outcome = run_evidence("""
read=async()=>({...confirmed,status:'NEED_MORE_EVIDENCE',verification_task_id:'old-test-task',supplementary_evidence_count:1});
let attempts=0;
submit=async()=>{if(++attempts===1)throw new Error('测试提交失败');return confirmed};
evidence('test-return',{}, {status:'NEED_MORE_EVIDENCE',verification_task_id:'old-test-task'});
select();await click();await click();console.log(JSON.stringify(result()));
""")
    assert outcome["events"].count("submit") == 2
    assert outcome["closed"] == 1
