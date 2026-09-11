import json
import subprocess
from pathlib import Path


ADMIN_APP = Path("apps/admin/public/v12-operations.js")
ADMIN_INDEX = Path("apps/admin/public/v12-operations.html")
CALL_APP = Path("apps/call-h5/public/app.js")
CALL_INDEX = Path("apps/call-h5/public/index.html")


def _function_slice(source: str, start: str, end: str) -> str:
    return source[source.index(start) : source.index(end)]


def test_dispatch_pool_always_offers_existing_correction_form_and_marks_verification() -> None:
    source = ADMIN_APP.read_text(encoding="utf-8")
    dispatch = _function_slice(source, "async function dispatch()", "async function candidates")
    correction = _function_slice(
        source,
        "async function openDispatchCorrection",
        "async function candidates",
    )

    assert "修改信息" in dispatch
    assert "有核验信息" in dispatch
    assert "data-dispatch-correction" in dispatch
    assert "can('lead.manual.manage')" in dispatch
    assert "primaryRole()!=='SUPER_ADMIN'" in dispatch
    assert "/v1.2/admin/leads/" in correction
    assert "/v1.2/pre-dispatch-verifications/tasks/" in correction
    assert "openPlatformLeadForm(lead,true" in correction
    assert "verificationInfo" in correction
    assert "refresh:dispatch" in correction


def test_existing_correction_form_renders_read_only_verification_and_uses_caller_refresh() -> None:
    source = ADMIN_APP.read_text(encoding="utf-8")
    form = _function_slice(source, "async function openPlatformLeadForm", "async function readPlatformLeadPayload")
    save = _function_slice(source, "async function savePlatformLead", "async function saveAndAssignNewLeadToTelesales")
    verification = _function_slice(source, "function preDispatchVerificationInfo", "async function openPlatformLeadForm")

    for label in ("电销核验信息", "核验人员", "提交时间", "联系结果", "事实结论", "核验备注"):
        assert label in verification
    assert "esc(info.note" in verification
    assert "preDispatchVerificationInfo(options.verificationInfo)" in form
    assert "savePlatformLead(item,correction,options.refresh,options.forcePublicPool)" in form
    assert "refresh||" in save


def test_operation_disposition_uses_task_detail_and_shows_verification_info() -> None:
    source = ADMIN_APP.read_text(encoding="utf-8")
    tasks = _function_slice(source, "async function telesales()", "function companyQueuePager")

    assert "data-pre-disposition-task" in tasks
    assert "openPreDispatchTask" in tasks
    assert "disposePreDispatch(task)" in tasks
    assert "preDispatchVerificationInfo(task.verification_info)" in tasks


def test_telesales_submitted_record_shows_its_verification_note() -> None:
    source = CALL_APP.read_text(encoding="utf-8")
    history = _function_slice(source, "async function loadSubmittedHistory", "function metric")
    records = _function_slice(source, "async function records()", "function taskFacts")
    task_card = _function_slice(source, "function taskCard(task)", "function callHomeGreeting")
    task = _function_slice(source, "async function task(kind, id)", "function bindTaskActions")

    assert "HISTORY_PAGE_SIZE" in history
    assert "payload.total" in history
    assert "state.nextPage" in history
    assert "submittedHistoryState.inFlight" in history
    assert "pendingKinds" in history
    assert "submitted_history=true" in history
    assert "loadSubmittedHistory()" in records
    assert "load-more-records" in records
    assert "renderSubmittedHistory(historyData, state)" in records
    assert "renderSubmittedHistory(await loadSubmittedHistory(), state)" in records
    assert "item.submitted_at" in records
    assert "task.is_overdue&&!task.submitted_at" in task_card
    assert "核验备注" in task
    assert "data.verification_info?.note" in task
    assert "esc(data.verification_info?.note" in task
    assert "data.submitted_at?'SUBMITTED':data.status" in task
    assert "data.is_overdue&&!data.submitted_at" in task


def test_telesales_history_load_more_only_requests_each_source_next_page() -> None:
    node_script = f"""
const fs = require('fs');
global.document = {{ querySelector: () => ({{}}), querySelectorAll: () => [] }};
global.window = {{ addEventListener: () => {{}}, ZSIconSystem: null, isSecureContext: false }};
global.location = {{ hash: '#/records', href: '', replace: () => {{}} }};
global.history = {{ length: 1, back: () => {{}} }};
global.navigator = {{}};
global.zsSetSafeHtml = () => {{}};
const calls = [];
global.fetch = async (url) => {{
  calls.push(url);
  const parsed = new URL(url, 'http://local');
  const page = Number(parsed.searchParams.get('page'));
  const preDispatch = parsed.pathname.includes('pre-dispatch-verifications');
  const total = preDispatch ? 51 : 1;
  const start = (page - 1) * 50;
  const count = Math.max(0, Math.min(50, total - start));
  const prefix = preDispatch ? 'pre' : 'return';
  const items = Array.from({{ length: count }}, (_, index) => ({{
    id: `${{prefix}}-${{start + index}}`,
    submitted_at: new Date(Date.UTC(2026, 8, 4, 12, 0, 0) - (start + index) * 1000).toISOString(),
  }}));
  return {{ ok: true, json: async () => ({{ code: 'OK', data: {{ items, total }} }}) }};
}};
let source = fs.readFileSync({json.dumps(str(CALL_APP))}, 'utf8');
source = source.slice(0, source.lastIndexOf('route();'));
source += '\\n;globalThis.__historyTest = {{ loadSubmittedHistory, reset: () => {{ submittedHistoryState = null; }} }};';
eval(source);
(async () => {{
  const [first, duplicateFirst] = await Promise.all([
    __historyTest.loadSubmittedHistory(),
    __historyTest.loadSubmittedHistory(),
  ]);
  const callsAfterFirst = calls.length;
  const second = await __historyTest.loadSubmittedHistory();
  const callsAfterSecond = calls.length;
  const third = await __historyTest.loadSubmittedHistory();
  const callsAfterThird = calls.length;
  __historyTest.reset();
  const callsBeforeInvalidInput = calls.length;
  await __historyTest.loadSubmittedHistory(Infinity);
  console.log(JSON.stringify({{
    firstHasMore: first.hasMore,
    firstCount: first.items.length,
    duplicateFirstHasMore: duplicateFirst.hasMore,
    duplicateFirstCount: duplicateFirst.items.length,
    secondHasMore: second.hasMore,
    secondCount: second.items.length,
    callsAfterFirst,
    callsAfterSecond,
    callsAfterThird,
    secondLoadCalls: calls.slice(callsAfterFirst, callsAfterSecond),
    invalidInputCalls: calls.length - callsBeforeInvalidInput,
  }}));
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
    result = subprocess.run(
        ["node", "-e", node_script],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)

    assert payload["firstHasMore"] is True
    assert payload["firstCount"] == 51
    assert payload["duplicateFirstHasMore"] is True
    assert payload["duplicateFirstCount"] == 51
    assert payload["secondHasMore"] is False
    assert payload["secondCount"] == 52
    assert payload["callsAfterFirst"] == 2
    assert payload["callsAfterSecond"] == 3
    assert payload["callsAfterThird"] == 3
    assert len(payload["secondLoadCalls"]) == 1
    assert "pre-dispatch-verifications" in payload["secondLoadCalls"][0]
    assert "page=2" in payload["secondLoadCalls"][0]
    assert payload["invalidInputCalls"] == 2


def test_telesales_history_drops_response_after_session_or_route_changes() -> None:
    node_script = f"""
const fs = require('fs');
const domNode = {{ addEventListener: () => {{}}, focus: () => {{}} }};
global.document = {{ querySelector: () => domNode, querySelectorAll: () => [] }};
global.window = {{ addEventListener: () => {{}}, ZSIconSystem: null, isSecureContext: false }};
global.location = {{ hash: '#/records', href: '', replace: () => {{}} }};
global.history = {{ length: 1, back: () => {{}} }};
global.navigator = {{}};
const renders = [];
global.zsSetSafeHtml = (_node, html) => {{ renders.push(html); }};
const pendingHistory = [];
let markHistoryStarted;
const historyStarted = new Promise((resolve) => {{ markHistoryStarted = resolve; }});
global.fetch = async (url) => {{
  const parsed = new URL(url, 'http://local');
  if (parsed.pathname.endsWith('/auth/me')) {{
    return {{ ok: true, json: async () => ({{ code: 'OK', data: {{ id: 'user-a', roles: ['TELESALES'] }} }}) }};
  }}
  return new Promise((resolve) => {{
    const taskKind = parsed.pathname.includes('pre-dispatch-verifications') ? 'pre' : 'return';
    pendingHistory.push(() => resolve({{
      ok: true,
      json: async () => ({{
        code: 'OK',
        data: {{
          total: 1,
          items: [{{
            id: `${{taskKind}}-a-secret`,
            submitted_at: '2026-09-04T12:00:00Z',
            lead: {{ customer_name: 'A 账号客户秘密' }},
          }}],
        }},
      }}),
    }}));
    if (pendingHistory.length === 2) markHistoryStarted();
  }});
}};
let source = fs.readFileSync({json.dumps(str(CALL_APP))}, 'utf8');
source = source.slice(0, source.lastIndexOf('route();'));
source += `
let abandonedHistoryState = null;
globalThis.__staleHistoryTest = {{
  records,
  switchAway: () => {{
    abandonedHistoryState = submittedHistoryState;
    me = null;
    submittedHistoryState = null;
    location.hash = '#/home';
  }},
  abandonedItemCount: () => abandonedHistoryState?.itemsById.size || 0,
  stateIsNull: () => submittedHistoryState === null,
}};`;
eval(source);
(async () => {{
  const request = __staleHistoryTest.records();
  await historyStarted;
  __staleHistoryTest.switchAway();
  pendingHistory.forEach((resolve) => resolve());
  await request;
  console.log(JSON.stringify({{
    renderCount: renders.length,
    renderedSecret: renders.some((html) => html.includes('A 账号客户秘密')),
    abandonedItemCount: __staleHistoryTest.abandonedItemCount(),
    stateIsNull: __staleHistoryTest.stateIsNull(),
  }}));
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
    result = subprocess.run(
        ["node", "-e", node_script],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)

    assert payload == {
        "renderCount": 0,
        "renderedSecret": False,
        "abandonedItemCount": 0,
        "stateIsNull": True,
    }


def test_changed_frontend_assets_have_feedback_94_cache_busters() -> None:
    admin_index = ADMIN_INDEX.read_text(encoding="utf-8")
    call_index = CALL_INDEX.read_text(encoding="utf-8")

    assert "v12-operations.js?v=20260910-feedback-910" in admin_index
    assert "app.js?v=20260904-verification-info" in call_index
