from __future__ import annotations

import json
from pathlib import Path
import subprocess


ADMIN = Path("apps/admin/public/v12-operations.js")
FRANCHISE_H5 = Path("apps/h5/public/v12-workbench.js")
CALL_H5 = Path("apps/call-h5/public/app.js")


def run_js(source: str) -> dict:
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", source],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    return json.loads(completed.stdout)


def fragment(start: str, end: str) -> str:
    source = ADMIN.read_text(encoding="utf-8")
    return source[source.index(start) : source.index(end)]


def call_fragment(start: str, end: str) -> str:
    source = CALL_H5.read_text(encoding="utf-8")
    return source[source.index(start) : source.index(end)]


def test_return_appeal_timing_distinguishes_paused_and_resumed_countdown() -> None:
    result = run_js(
        "const fmt=value=>value;Date.now=()=>new Date('2026-09-16T10:30:00Z').getTime();"
        + fragment("function remainingAppealTime", "function evidenceList")
        + """
console.log(JSON.stringify({
  paused:returnAppealTiming({appeal_paused_at:'2026-09-15T10:00:00Z',appeal_remaining_seconds:3660}),
  resumed:returnAppealTiming({appeal_resumed_at:'2026-09-15T11:00:00Z',appeal_deadline_at:'2026-09-16T11:00:00Z',appeal_remaining_seconds:3600}),
  normal:returnAppealTiming({appeal_deadline_at:'2026-09-17T10:00:00Z'}),
}));
"""
    )
    assert result["paused"] == "48小时计时已暂停，剩余 1小时1分钟"
    assert "已恢复计时" in result["resumed"]
    assert "剩余 30分钟" in result["resumed"]
    assert result["normal"] == "截止 2026-09-17T10:00:00Z"


def test_supplement_history_failure_is_local_and_returns_empty_history() -> None:
    result = run_js(
        """
const S={supplementHistoryPage:2};
const qs=()=>'?page=2';
const api=async()=>{throw new Error('历史接口暂时不可用')};
"""
        + fragment("async function loadSupplementHistory", "async function review")
        + """
console.log(JSON.stringify(await loadSupplementHistory(true)));
"""
    )
    assert result == {
        "items": [],
        "total": 0,
        "page": 2,
        "page_size": 20,
        "load_error": "历史接口暂时不可用",
    }


def test_evidence_preview_failure_marks_only_the_failed_file() -> None:
    result = run_js(
        """
const media={listeners:{},addEventListener(name,handler){this.listeners[name]=handler},closest(){return card}};
const error={hidden:true};
const card={querySelector:()=>error};
const root={querySelectorAll:()=>[media]};
"""
        + fragment("function bindEvidencePreviewErrors", "function telesalesName")
        + """
bindEvidencePreviewErrors(root);
media.listeners.error();
console.log(JSON.stringify({hidden:error.hidden}));
"""
    )
    assert result == {"hidden": False}


def test_admin_wires_region_redispatch_and_no_answer_requeue_endpoints() -> None:
    source = ADMIN.read_text(encoding="utf-8")
    assert "/correct-region-and-redispatch" in source
    assert "province_code" in source
    assert "city_code" in source
    assert "district_code" in source
    assert "/pre-dispatch-verification/requeue" in source
    assert "未接或拒接，重新分配电销核验" in source
    assert "lead.can_requeue_pre_dispatch" in source
    assert "data-pre-dispatch-requeue" in source
    assert "!lead.can_requeue_pre_dispatch" in source
    assert "REJECTED_CALL" in source
    assert "CALL_REJECTED" in source
    assert "拒绝接听" in source


def test_reverified_lead_requires_qualified_result_before_dispatch_pool() -> None:
    result = run_js(
        fragment("function canApprovePreDispatch", "function disposePreDispatch")
        + """
console.log(JSON.stringify({
  firstRoundIncomplete:canApprovePreDispatch({conclusion:'INFO_INCOMPLETE'}),
  requiredQualified:canApprovePreDispatch({requires_qualified_verification:true,conclusion:'QUALIFIED'}),
  requiredNestedQualified:canApprovePreDispatch({requires_qualified_verification:true,verification_info:{conclusion:'QUALIFIED'}}),
  requiredIncomplete:canApprovePreDispatch({requires_qualified_verification:true,conclusion:'INFO_INCOMPLETE'}),
  requiredUnverifiable:canApprovePreDispatch({requires_qualified_verification:true,conclusion:'UNVERIFIABLE'}),
}));
"""
    )
    assert result == {
        "firstRoundIncomplete": True,
        "requiredQualified": True,
        "requiredNestedQualified": True,
        "requiredIncomplete": False,
        "requiredUnverifiable": False,
    }
    disposition = ADMIN.read_text(encoding="utf-8")
    disposition = disposition[disposition.index("function disposePreDispatch") : disposition.index("function companyQueuePager")]
    assert "${approveOption}" in disposition


def test_overdue_tasks_remain_actionable_without_hard_timeout_copy() -> None:
    source = ADMIN.read_text(encoding="utf-8")
    telesales = source[source.index("async function telesales") : source.index("async function openPreDispatchTask")]
    returns = source[source.index("async function returns") : source.index("async function returnDetail")]
    assert "task.is_overdue" not in telesales
    assert "x.is_overdue" not in returns
    assert "已超时，需运营改派" not in source
    assert "参考时间" in telesales
    assert "参考时间" in returns


def test_region_filter_explains_matching_and_distinguishes_empty_result() -> None:
    source = ADMIN.read_text(encoding="utf-8")
    assert "省/市/区县名称或地区编码（如阜阳/阜阳市）" in source
    assert "未找到符合当前筛选条件的客资" in source
    assert "地区筛选失败" in source


def test_call_h5_keeps_overdue_tasks_actionable_and_previews_return_evidence() -> None:
    source = CALL_H5.read_text(encoding="utf-8")
    task = source[source.index("async function task(kind, id)") : source.index("function bindTaskActions")]
    assert "data.is_overdue" not in task
    assert "任务已超时" not in source
    assert "不能继续处理" not in source
    assert "处理参考时间" in source
    assert "data-return-evidence-media" in source
    assert "证据暂时无法预览" in source


def test_call_h5_readonly_evidence_keeps_metadata_and_media_preview() -> None:
    result = run_js(
        """
const API='/api/v1';
const esc=value=>String(value??'');
const fmt=value=>value||'--';
"""
        + call_fragment("function returnEvidenceChoice", "function bindReturnEvidencePreviewErrors")
        + """
const image=returnEvidenceChoice({id:'img-1',type:'CHAT_SCREENSHOT',original_name:'聊天.png',mime_type:'image/png',access_token:'t',uploaded_by_name:'张三',created_at:'2026-09-16T10:00:00Z'},false);
const audio=returnEvidenceChoice({id:'audio-1',type:'CALL_RECORDING',original_name:'通话.mp3',mime_type:'audio/mpeg',access_token:'t',uploaded_by_name:'李四',created_at:'2026-09-16T11:00:00Z'},false);
const selectable=returnEvidenceChoice({id:'img-2',type:'CHAT_SCREENSHOT',access_token:'t'},true);
console.log(JSON.stringify({
  readonlyHasImage:image.includes('data-return-evidence-media')&&image.includes('<img'),
  readonlyHasMetadata:image.includes('沟通截图')&&image.includes('上传人 张三')&&image.includes('2026-09-16T10:00:00Z'),
  readonlyHasNoCheckbox:!image.includes('name="verification_evidence"'),
  readonlyHasAudio:audio.includes('<audio')&&audio.includes('通话录音')&&audio.includes('上传人 李四'),
  selectableHasCheckbox:selectable.includes('name="verification_evidence"'),
}));
"""
    )
    assert all(result.values())

    task = CALL_H5.read_text(encoding="utf-8")
    task = task[task.index("async function task(kind, id)") : task.index("function bindTaskActions")]
    assert "available_evidences" in task
    assert "开始核验前可先查看" in task
    assert "verification_info?.evidences" in task
    assert "returnEvidenceChoice(item, false)" in task


def test_call_h5_assigned_and_submitted_tasks_render_readonly_evidence() -> None:
    result = run_js(
        """
const API='/api/v1';
const app={querySelectorAll:()=>[]};
const esc=value=>String(value??'');
const fmt=value=>value||'--';
const icon=()=>'';
const statusLabel=value=>value;
const statusClass=()=>'';
const contactLabels={CONNECTED:'已接通'};
const TASK_KIND={RETURN:{label:'退回核验',detailPath:id=>`/tasks/${id}`,conclusions:{SUPPORT_RETURN:'支持退回'}}};
const taskPath=(kind,id)=>TASK_KIND[kind].detailPath(id);
const taskFacts=()=>[];
const taskForm=()=>'';
const auth=async()=>true;
const shell=body=>body;
const bind=()=>{};
const bindTaskActions=()=>{};
let markup='';
const zsSetSafeHtml=(root,html)=>{markup=html};
const available={id:'source-1',type:'CHAT_SCREENSHOT',original_name:'加盟商截图.png',mime_type:'image/png',access_token:'t',uploaded_by_name:'加盟商',created_at:'2026-09-16T10:00:00Z'};
const adopted={id:'adopted-1',type:'CALL_RECORDING',original_name:'核验录音.mp3',mime_type:'audio/mpeg',access_token:'t',uploaded_by_name:'电销员',created_at:'2026-09-16T11:00:00Z'};
const responses=[
  {status:'ASSIGNED',lead:{customer_name:'客户'},return_request:{available_evidences:[available]}},
  {status:'SUBMITTED',submitted_at:'2026-09-16T12:00:00Z',lead:{customer_name:'客户'},return_request:{available_evidences:[available]},verification_info:{note:'已核验',evidences:[adopted]},conclusion:'SUPPORT_RETURN'},
];
const api=async()=>responses.shift();
"""
        + call_fragment("function returnEvidenceChoice", "const greetingName")
        + call_fragment("async function task(kind, id)", "function bindTaskActions")
        + """
await task('RETURN','assigned');
const assigned=markup;
await task('RETURN','submitted');
const submitted=markup;
console.log(JSON.stringify({
  assignedReadonly:assigned.includes('加盟商截图.png')&&assigned.includes('开始核验')&&!assigned.includes('name="verification_evidence"'),
  submittedSource:submitted.includes('加盟商截图.png')&&submitted.includes('<img'),
  submittedAdopted:submitted.includes('核验录音.mp3')&&submitted.includes('<audio'),
  submittedReadonly:!submitted.includes('name="verification_evidence"'),
}));
"""
    )
    assert all(result.values())


def test_franchise_h5_shows_paused_countdown_and_allows_reapply_after_rejection() -> None:
    source = FRANCHISE_H5.read_text(encoding="utf-8")
    assert "48小时计时已暂停" in source
    assert "appeal_remaining_seconds" in source
    detail = source[source.index("async function returnDetail") : source.index("function rewardExplanation")]
    assert "x.status==='REJECTED'" in detail
    assert "再次申请退回" in detail
    assert "requires_new_evidence" in source
    assert "current_round_evidence_count" in source


def test_region_correction_refund_is_not_described_as_invalid_lead() -> None:
    expected_copy = "区域更正，已返还领取积分"
    result = run_js(
        "const fmt=value=>value;"
        + fragment("function traceEffectiveRecognition", "function traceNextStep")
        + """
console.log(JSON.stringify({
  corrected:traceEffectiveRecognition({status:'RETURNED',release_reason:'V12_RETURN_REGION_CORRECTED'}),
  invalid:traceEffectiveRecognition({status:'RETURNED',release_reason:'RETURN_APPROVED'}),
}));
"""
    )
    assert result == {
        "corrected": expected_copy,
        "invalid": "退回审核通过 · 已判无效",
    }
    for source_path in (ADMIN, FRANCHISE_H5):
        source = source_path.read_text(encoding="utf-8")
        assert "V12_RETURN_REGION_CORRECTED" in source
        assert expected_copy in source


def test_region_correction_requires_supported_review_and_reports_redispatch() -> None:
    result = run_js(
        "const can=()=>true;"
        + fragment("function canCorrectReturnRegion", "async function returnDetail")
        + """
console.log(JSON.stringify({
  submitted:canCorrectReturnRegion({reason_code:'OUT_OF_SERVICE_REGION',status:'SUBMITTED'},{}),
  unsupported:canCorrectReturnRegion({reason_code:'OUT_OF_SERVICE_REGION',status:'REVIEWING'},{conclusion:'DOES_NOT_SUPPORT_RETURN'}),
  supported:canCorrectReturnRegion({reason_code:'OUT_OF_SERVICE_REGION',status:'REVIEWING'},{conclusion:'SUPPORT_RETURN'}),
  corrected:returnFundImpact({submitted_at:'2026-09-16T00:00:00Z',status:'APPROVED',refund_points:100,assignment_release_reason:'V12_RETURN_REGION_CORRECTED'}),
  invalid:returnFundImpact({submitted_at:'2026-09-16T00:00:00Z',status:'APPROVED',refund_points:100}),
}));
"""
    )
    assert result == {
        "submitted": False,
        "unsupported": False,
        "supported": True,
        "corrected": "已返还 100 积分，区域已更正并重新入池",
        "invalid": "已返还 100 积分，客资已关闭",
    }
    detail = ADMIN.read_text(encoding="utf-8")
    detail = detail[detail.index("async function returnDetail") : detail.index("async function correctReturnRegionAndRedispatch")]
    assert "请先完成退回核验" in detail


def test_single_evidence_in_active_task_can_be_selected_for_verification() -> None:
    result = run_js(
        "const esc=String,fmt=String,API='/api/v1';"
        "const TASK_KIND={RETURN:{conclusions:{SUPPORT_RETURN:'支持退回'}}},contactLabels={};"
        + call_fragment("function returnEvidenceChoice", "const greetingName")
        + call_fragment("function taskForm", "async function task(kind, id)")
        + """
const html=taskForm('RETURN',{return_request:{available_evidences:[{
  id:'only-evidence',type:'CHAT_SCREENSHOT',original_name:'唯一证据.png',access_token:'test'
}]}});
console.log(JSON.stringify({selectable:html.includes('name="verification_evidence" value="only-evidence"')}));
"""
    )
    assert result["selectable"] is True
