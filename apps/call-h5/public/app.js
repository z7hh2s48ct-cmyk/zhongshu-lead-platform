const API = '/api/v1';
const app = document.querySelector('#app');
const toastEl = document.querySelector('#toast');

let me = null;
let submittedHistoryState = null;

const TASK_KIND = {
  PRE_DISPATCH: {
    label: '前置核验',
    listPath: '/v1.2/pre-dispatch-verifications/tasks',
    detailPath: (id) => `/v1.2/pre-dispatch-verifications/tasks/${encodeURIComponent(id)}`,
    conclusions: {
      QUALIFIED: '信息合格', INFO_INCOMPLETE: '信息不全', UNVERIFIABLE: '无法核验', INVALID: '明确无效', DUPLICATE: '重复客资',
    },
  },
  RETURN: {
    label: '退回核验',
    listPath: '/v1.2/return-verifications/tasks',
    detailPath: (id) => `/v1.2/return-verifications/tasks/${encodeURIComponent(id)}`,
    conclusions: { SUPPORT_RETURN: '支持退回', DOES_NOT_SUPPORT_RETURN: '不支持退回', INCONCLUSIVE: '信息不足' },
  },
};

const TELESALES_HOME_CONTRACT = {
  metrics: ['待开始', '核验中', '已提交'],
  primaryActions: ['开始核验', '继续核验'],
  detail: ['一键拨号', '核验说明', '填写结果'],
};
const contactLabels = {
  CONNECTED: '已接通', NO_ANSWER: '无人接听', EMPTY_NUMBER: '空号', OUT_OF_SERVICE: '停机', WRONG_PERSON: '非本人', REFUSED: '拒接/拒访', OTHER: '其他',
};
const returnReasonLabels = {
  EMPTY_NUMBER: '空号或停机', OUT_OF_SERVICE_REGION: '超出服务区域', DUPLICATE_TO_RECEIVER: '接收方重复客资', NON_HOUSING_CONSULTATION: '非建房装修咨询',
};
const statusLabels = { ASSIGNED: '待开始', IN_PROGRESS: '核验中', SUBMITTED: '已提交' };
const HISTORY_PAGE_SIZE = 50;

const esc = (value = '') => String(value ?? '').replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
const icon = (name) => window.ZSIconSystem?.svg?.(name) || '';
const fmt = (value) => value ? new Date(value).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '--';
const statusLabel = (value) => statusLabels[value] || '待确认';
const statusClass = (value) => value === 'SUBMITTED' ? 'done' : value === 'IN_PROGRESS' ? 'doing' : 'pending';
const evidenceCount = (request = {}) => Object.values(request.evidence_summary || {}).reduce((sum, count) => sum + Number(count || 0), 0);
function returnEvidenceChoice(item, selectable = true) {
  const name = item.original_name || item.type || '退回证据';
  const url = item.access_token ? `${API}/v1.2/return-evidences/${encodeURIComponent(item.id)}/download?token=${encodeURIComponent(item.access_token)}` : '';
  const isImage = item.type === 'CHAT_SCREENSHOT' || String(item.mime_type || '').startsWith('image/');
  const isAudio = item.type === 'CALL_RECORDING' || String(item.mime_type || '').startsWith('audio/');
  const typeLabel = { CHAT_SCREENSHOT: '沟通截图', CALL_RECORDING: '通话录音' }[item.type] || '文件证据';
  const preview = !url ? '<p class="evidence-error">证据链接暂不可用，请刷新后重试。</p>' : isImage ? `<a href="${esc(url)}" target="_blank" rel="noopener"><img data-return-evidence-media src="${esc(url)}" alt="${esc(name)}" loading="lazy"></a>` : isAudio ? `<audio data-return-evidence-media controls preload="metadata" src="${esc(url)}">当前浏览器不支持播放录音。</audio>` : `<a class="btn small outline" href="${esc(url)}" target="_blank" rel="noopener">查看文件</a>`;
  const selection = selectable ? `<label class="choice"><input type="checkbox" name="verification_evidence" value="${esc(item.id)}"> 本次核验采用这份证据</label>` : '';
  return `<article class="return-evidence"><b>${esc(name)}</b><small class="muted">${esc(typeLabel)} · 上传人 ${esc(item.uploaded_by_name || '未记录')} · ${esc(fmt(item.created_at))}</small>${preview}<p class="evidence-error" data-return-evidence-error hidden>证据暂时无法预览，可刷新后重试。</p>${selection}</article>`;
}
function bindReturnEvidencePreviewErrors(root=document) {
  root.querySelectorAll('[data-return-evidence-media]').forEach((media) => media.addEventListener('error', () => {
    const message = media.closest('.return-evidence')?.querySelector('[data-return-evidence-error]');
    if (message) message.hidden = false;
  }));
}
const greetingName = (value) => {
  const name = String(value || '').trim();
  return name.length > 6 ? `${name.slice(0, 6)}…` : name;
};

function toast(message, type = '') {
  toastEl.textContent = message;
  toastEl.className = `toast show ${type}`;
  setTimeout(() => { toastEl.className = 'toast'; }, 2400);
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body !== undefined && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  const response = await fetch(API + path, { ...options, headers, credentials: 'include' });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || payload.code !== 'OK') {
    const error = new Error(payload.message || '请求失败');
    error.code = payload.code;
    throw error;
  }
  return payload.data;
}

function taskPath(kind, taskId, action = '') {
  const root = TASK_KIND[kind].detailPath(taskId);
  return action ? `${root}/${action}` : root;
}

function copyWithSelection(value) {
  const field = document.createElement('textarea');
  field.value = value;
  field.setAttribute('readonly', '');
  field.style.position = 'fixed';
  field.style.top = '-9999px';
  document.body.appendChild(field);
  field.focus();
  field.select();
  try {
    return document.execCommand('copy');
  } finally {
    field.remove();
  }
}

async function copyPhone(phone) {
  const value = String(phone || '').trim();
  if (!value) {
    toast('号码暂不可复制', 'error');
    return;
  }
  try {
    if (navigator.clipboard?.writeText && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
    } else if (!copyWithSelection(value)) {
      throw new Error('copy failed');
    }
    toast('号码已复制');
  } catch {
    toast('复制失败，请手动复制号码', 'error');
  }
}

function nav(active) {
  const items = [['home', 'home', '首页'], ['verify', 'phone', '核验'], ['records', 'history', '记录'], ['profile', 'user', '我的']];
  return `<nav class="bottom" aria-label="底部导航">${items.map(([route, iconName, label]) => `<button class="nav ${active === route ? 'active' : ''}" data-route="${route}"><i>${icon(iconName)}</i><span>${label}</span></button>`).join('')}</nav>`;
}

function shell(content, active = 'home', title = '电销工作台') {
  return `<div class="shell"><header class="top"><div class="brand"><img src="./logo.png" alt="合家美宅"><div>合家美宅<small>${esc(title)} · 仅处理运营派发任务</small></div></div><button class="btn small outline icon-btn" id="refresh">${icon('rotate-ccw')}<span>刷新</span></button></header><main class="content">${content}</main>${nav(active)}</div>`;
}

function bind(refreshHandler = route) {
  document.querySelectorAll('[data-route]').forEach((node) => { node.onclick = () => go(node.dataset.route); });
  document.querySelectorAll('[data-history-back]').forEach((node) => { node.onclick = () => (history.length > 1 ? history.back() : go('verify')); });
  document.querySelector('#refresh')?.addEventListener('click', refreshHandler);
}

function go(routeName) {
  const next = `#/${routeName}`;
  if (location.hash === next) route(); else location.hash = next;
}

function emptyState(title, description) {
  return `<div class="empty"><b>${esc(title)}</b><span>${esc(description)}</span></div>`;
}

async function auth() {
  try {
    me = await api('/auth/me');
    if (redirectWrongWorkbenchRole()) return false;
    return true;
  } catch (error) {
    if (['AUTH_REQUIRED', 'AUTH_INVALID'].includes(error.code)) renderLogin();
    else renderLoadError(error.message);
    return false;
  }
}

function redirectWrongWorkbenchRole() {
  const roles = new Set(me?.roles || []);
  if (roles.has('TELESALES')) return false;
  if (roles.has('SUPER_ADMIN') || roles.has('OPERATION')) {
    location.replace('/h5/admin/');
    return true;
  }
  if (roles.has('FRANCHISE_OWNER') || roles.has('FRANCHISE_EMPLOYEE')) {
    location.replace('/h5/');
    return true;
  }
  renderAccessDenied();
  return true;
}

function renderAccessDenied() {
  zsSetSafeHtml(app, `<section class="login"><div class="panel login-card"><h1>无权访问</h1><p>当前账号没有可用的电销角色，请联系管理员核对。</p></div></section>`);
}

function renderLoadError(message = '页面加载失败') {
  zsSetSafeHtml(app, `<section class="login"><div class="panel login-card"><h1>暂时无法加载</h1><p>${esc(message)}</p><button class="btn primary block" id="retry-route">重试</button></div></section>`);
  document.querySelector('#retry-route').onclick = route;
}

function renderInvalidLink() {
  zsSetSafeHtml(app, `<section class="login"><div class="panel login-card"><h1>链接已失效</h1><p>该任务链接已迁移或不完整，请从本人任务列表重新进入。</p><button class="btn primary block" id="return-call-home">返回电销首页</button></div></section>`);
  document.querySelector('#return-call-home').onclick = () => go('home');
}

function renderLogin(message = '') {
  zsSetSafeHtml(app, `<section class="login"><div class="login-brand"><span class="login-kicker">合家美宅 · 内部登录</span><img src="./logo.png" alt="合家美宅"><h1>电销工作台</h1><p>仅显示运营人员已派发给您的前置核验和退回事实核验任务。</p></div><div class="panel login-card"><div class="login-card-head"><span>内部账号登录</span><h2>欢迎回来</h2><p>登录后只进入电销任务工作台。</p></div>${message ? `<p class="error-text">${esc(message)}</p>` : ''}<form id="call-login-form" novalidate><div class="login-field"><label for="user">登录账号</label><div class="login-input-wrap"><i>${icon('user')}</i><input id="user" name="username" autocomplete="username" placeholder="请输入电销账号" required></div></div><div class="login-field"><label for="pass">登录密码</label><div class="login-input-wrap"><i>${icon('lock')}</i><input id="pass" name="password" type="password" autocomplete="current-password" placeholder="请输入登录密码" required></div></div><button id="login" class="btn primary block login-submit" type="submit">登录工作台</button></form><p class="login-security">完整号码仅在您开始本人任务后显示；拨号必须由您主动点击。</p></div></section>`);
  document.querySelector('#call-login-form').onsubmit = async (event) => {
    event.preventDefault();
    const button = document.querySelector('#login');
    button.disabled = true;
    try {
      await api('/auth/login', { method: 'POST', body: JSON.stringify({ username: document.querySelector('#user').value.trim(), password: document.querySelector('#pass').value }) });
      location.hash = '#/home';
      await route();
    } catch (error) {
      button.disabled = false;
      toast(error.message, 'error');
    }
  };
}

async function loadTasks(status = '') {
  const query = `?page=1&page_size=200${status ? `&status=${encodeURIComponent(status)}` : ''}`;
  const [preDispatch, returns] = await Promise.all([
    api(`${TASK_KIND.PRE_DISPATCH.listPath}${query}`),
    api(`${TASK_KIND.RETURN.listPath}${query}&mine=true`),
  ]);
  const rank = { IN_PROGRESS: 0, ASSIGNED: 1, SUBMITTED: 2 };
  return [
    ...(preDispatch.items || []).map((item) => ({ ...item, task_kind: 'PRE_DISPATCH', display_status: item.status })),
    ...(returns.items || []).map((item) => ({ ...item, task_kind: 'RETURN', display_status: item.status })),
  ].sort((left, right) => (rank[left.display_status] ?? 9) - (rank[right.display_status] ?? 9) || String(right.submitted_at || right.assigned_at || right.created_at || '').localeCompare(String(left.submitted_at || left.assigned_at || left.created_at || '')));
}

function newSubmittedHistoryState() {
  return {
    ownerId: me?.id || null,
    itemsById: new Map(),
    nextPage: { PRE_DISPATCH: 1, RETURN: 1 },
    total: { PRE_DISPATCH: 0, RETURN: 0 },
    loaded: { PRE_DISPATCH: 0, RETURN: 0 },
    done: { PRE_DISPATCH: false, RETURN: false },
    inFlight: null,
  };
}

function submittedHistoryView(state = submittedHistoryState) {
  const items = [...state.itemsById.values()].sort((left, right) => String(right.submitted_at || '').localeCompare(String(left.submitted_at || '')));
  return {
    items,
    hasMore: !state.done.PRE_DISPATCH || !state.done.RETURN,
  };
}

function isSubmittedHistoryRoute() {
  const routeName = (location.hash.replace(/^#\/?/, '') || 'home').split('?')[0].split('/')[0];
  return routeName === 'records';
}

function submittedHistoryRequestIsCurrent(state) {
  return Boolean(state && submittedHistoryState === state && state.ownerId === (me?.id || null) && isSubmittedHistoryRoute());
}

async function loadSubmittedHistory() {
  if (!submittedHistoryState || submittedHistoryState.ownerId !== (me?.id || null)) {
    submittedHistoryState = newSubmittedHistoryState();
  }
  if (submittedHistoryState.inFlight) return submittedHistoryState.inFlight;

  const state = submittedHistoryState;
  state.inFlight = (async () => {
    const pendingKinds = ['PRE_DISPATCH', 'RETURN'].filter((kind) => !state.done[kind]);
    let pages;
    try {
      pages = await Promise.all(pendingKinds.map(async (kind) => {
        const page = state.nextPage[kind];
        const mine = kind === 'RETURN' ? '&mine=true' : '';
        const payload = await api(`${TASK_KIND[kind].listPath}?page=${page}&page_size=${HISTORY_PAGE_SIZE}&submitted_history=true${mine}`);
        return { kind, page, payload };
      }));
    } catch (error) {
      if (!submittedHistoryRequestIsCurrent(state)) return null;
      throw error;
    }
    if (!submittedHistoryRequestIsCurrent(state)) return null;
    for (const { kind, page, payload } of pages) {
      const pageItems = payload.items || [];
      let added = 0;
      pageItems.forEach((item) => {
        const key = `${kind}:${item.id}`;
        if (!state.itemsById.has(key)) added += 1;
        state.itemsById.set(key, { ...item, task_kind: kind, display_status: 'SUBMITTED' });
      });
      state.total[kind] = Number(payload.total || 0);
      state.loaded[kind] += added;
      state.nextPage[kind] = page + 1;
      state.done[kind] = state.loaded[kind] >= state.total[kind] || pageItems.length === 0;
    }
    return submittedHistoryView(state);
  })();

  try {
    return await state.inFlight;
  } finally {
    state.inFlight = null;
  }
}

function metric(items, statuses) { return items.filter((item) => statuses.includes(item.status)).length; }

function taskDescription(task) {
  return task.task_kind === 'PRE_DISPATCH' ? task.lead?.need_summary || '请核对客户资料完整性与真实性。' : task.return_request?.description || '请结合退回申请和已提交证据核验事实。';
}

function taskCard(task) {
  const lead = task.lead || {};
  const request = task.return_request || {};
  const displayStatus = task.display_status || task.status;
  const typeFact = task.task_kind === 'RETURN' ? `退回原因：${returnReasonLabels[request.reason_code] || '待确认'} · 证据 ${evidenceCount(request)} 份` : '资料不全，等待电话事实核验';
  const deadline = task.due_at || request.appeal_deadline_at;
  return `<article class="task" data-task-kind="${task.task_kind}" data-task="${task.id}" tabindex="0"><div class="row"><h3>${esc(lead.customer_name || '待核验客户')}</h3><span class="badge ${statusClass(displayStatus)}">${esc(statusLabel(displayStatus))}</span></div><p class="task-meta">${esc(TASK_KIND[task.task_kind].label)} · ${esc(lead.city || '')} ${esc(lead.district || '')}</p><dl class="task-facts"><div><dt>任务说明</dt><dd>${esc(typeFact)}</dd></div><div><dt>处理参考时间</dt><dd>${fmt(deadline)}</dd></div></dl><p>${esc(taskDescription(task))}</p></article>`;
}

function callHomeGreeting() {
  const name = String(me?.display_name || '电销人员').trim();
  return `<section class="call-home-greeting"><div><p>内部电销工作台</p><h1>${esc(greetingName(name))}，上午好</h1></div><div class="call-home-avatar" aria-label="${esc(name)}">${esc(name.slice(0, 1))}</div></section>`;
}

function callHomeHero({ value, actionLabel, hasDoing }) {
  return `<section class="hero callHomeHero"><div><small>今日待核验</small><strong>${Number(value || 0)}</strong><span>运营派发的本人任务</span></div><button class="btn gold" data-route="verify">${icon(hasDoing ? 'user-check' : 'phone')}<span>${esc(actionLabel)}</span></button></section>`;
}

function callHomeMetrics(items) {
  return `<section class="metrics callHomeMetrics" aria-label="个人任务统计">${items.map(([labelText, value, route]) => `<button type="button" class="metric" data-route="${route}"><span>${esc(labelText)}</span><b>${Number(value || 0)}</b></button>`).join('')}</section>`;
}

function homeTaskRow(task) {
  const lead = task.lead || {};
  const customer = lead.customer_name || '待核验客户';
  const place = [lead.city, lead.district].filter(Boolean).join(' · ') || '地区待补充';
  return `<article class="home-task-row" data-task-kind="${task.task_kind}" data-task="${task.id}" tabindex="0"><span class="home-task-avatar">${esc(String(customer).slice(0, 1))}</span><span class="home-task-copy"><b>${esc(customer)}</b><small>${esc(place)} · ${esc(TASK_KIND[task.task_kind].label)}</small></span><span class="badge ${statusClass(task.status)}">${esc(statusLabel(task.status))}</span>${icon('chevron-right')}</article>`;
}

function homeTaskList(tasks) {
  if (!tasks.length) return '';
  return `<section class="card home-task-section"><div class="row section-head"><h2>最近任务</h2><button class="btn small outline" data-route="verify">查看全部</button></div><div class="home-task-list">${tasks.slice(0, 3).map(homeTaskRow).join('')}</div></section>`;
}

function bindTaskCards() {
  document.querySelectorAll('[data-task]').forEach((node) => {
    const open = () => go(`task/${node.dataset.taskKind}/${node.dataset.task}`);
    node.onclick = open;
    node.onkeydown = (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); open(); } };
  });
}

async function home() {
  if (!await auth()) return;
  const tasks = await loadTasks();
  const actionable = tasks.filter((item) => item.status !== 'SUBMITTED');
  const hasDoing = actionable.some((item) => item.status === 'IN_PROGRESS');
  zsSetSafeHtml(app, shell(`${callHomeGreeting()}${callHomeHero({ value: actionable.length, actionLabel: hasDoing ? '继续核验' : '开始核验', hasDoing })}${callHomeMetrics([['待开始', metric(tasks, ['ASSIGNED']), 'verify?status=ASSIGNED'], ['核验中', metric(tasks, ['IN_PROGRESS']), 'verify?status=IN_PROGRESS'], ['已提交', metric(tasks, ['SUBMITTED']), 'records']])}${homeTaskList(actionable)}`));
  bind();
  bindTaskCards();
}

async function verify() {
  if (!await auth()) return;
  const query = new URLSearchParams(location.hash.split('?')[1] || '');
  const status = query.get('status') || '';
  const items = (await loadTasks(status)).filter((item) => item.status !== 'SUBMITTED');
  zsSetSafeHtml(app, shell(`<h1>核验任务</h1><div class="filters" role="tablist">${[['', '全部'], ['ASSIGNED', '待开始'], ['IN_PROGRESS', '核验中']].map(([value, label]) => `<button class="btn small ${status === value ? 'primary' : 'outline'}" data-filter="${value}">${label}</button>`).join('')}</div>${items.length ? items.map(taskCard).join('') : emptyState('暂无符合条件的任务', '这里只显示运营已派发给您的任务。')}`, 'verify', '核验任务'));
  bind();
  bindTaskCards();
  document.querySelectorAll('[data-filter]').forEach((node) => { node.onclick = () => { location.hash = `#/verify${node.dataset.filter ? `?status=${node.dataset.filter}` : ''}`; }; });
}

async function loadDialStats() {
  try {
    return await api('/v1.2/pre-dispatch-verifications/dial-stats');
  } catch (error) {
    return null; // 统计加载失败不阻塞记录页。
  }
}

function dialStatsCards(stats) {
  if (!stats) return '';
  const entries = [['今日', stats.today], ['本周', stats.week], ['本月', stats.month]];
  return `<section class="metrics callDialStats" aria-label="我的通话量统计">${entries.map(([label, value]) => `<div class="metric"><span>${esc(label)}拨打</span><b>${Number(value?.dials || 0)}</b><small>提交核验 ${Number(value?.submitted || 0)} 次</small></div>`).join('')}</section>`;
}

async function records() {
  if (!await auth()) return;
  if (!isSubmittedHistoryRoute()) return;
  if (!submittedHistoryState || submittedHistoryState.ownerId !== (me?.id || null)) {
    submittedHistoryState = newSubmittedHistoryState();
  }
  const state = submittedHistoryState;
  const needsFirstPage = (
    state.nextPage.PRE_DISPATCH === 1
    && state.nextPage.RETURN === 1
  );
  const statsPromise = loadDialStats();
  const historyData = needsFirstPage ? await loadSubmittedHistory() : submittedHistoryView();
  state.dialStats = await statsPromise;
  renderSubmittedHistory(historyData, state);
}

function renderSubmittedHistory(historyData, state = submittedHistoryState) {
  if (!historyData || !submittedHistoryRequestIsCurrent(state)) return;
  const items = historyData.items.filter((item) => item.submitted_at);
  const loadMore = historyData.hasMore ? '<button class="btn outline block" id="load-more-records">加载更多记录</button>' : '';
  zsSetSafeHtml(app, shell(`<h1>核验记录</h1><p class="muted">已提交的内容只保留事实结论，后续业务处置由运营人员完成。</p>${dialStatsCards(state.dialStats)}${items.length ? `${items.map(taskCard).join('')}${loadMore}` : emptyState('暂无已提交记录', '完成核验并提交后，记录会保留在这里。')}`, 'records', '核验记录'));
  bind(() => { submittedHistoryState = null; route(); });
  bindTaskCards();
  document.querySelector('#load-more-records')?.addEventListener('click', async (event) => {
    event.currentTarget.disabled = true;
    try {
      renderSubmittedHistory(await loadSubmittedHistory(), state);
    } catch (error) {
      if (!submittedHistoryRequestIsCurrent(state)) return;
      event.currentTarget.disabled = false;
      toast(error.message || '加载失败', 'error');
    }
  });
}

function taskFacts(kind, data) {
  const lead = data.lead || {};
  if (kind === 'PRE_DISPATCH') return [['任务类型', '前置核验'], ['处理参考时间', fmt(data.due_at)], ['客户需求', lead.need_summary || '--'], ['下一步', data.submitted_at ? '已提交运营处置' : '完成电话事实核验']];
  const request = data.return_request || {};
  return [['任务类型', '退回核验'], ['处理参考时间', fmt(data.due_at)], ['退回原因', returnReasonLabels[request.reason_code] || '待确认'], ['证据数量', `${evidenceCount(request)} 份`], ['下一步', data.submitted_at ? '已提交运营终审' : '完成退回事实核验']];
}

function taskForm(kind, data = {}) {
  const conclusions = TASK_KIND[kind].conclusions;
  const availableEvidence = kind === 'RETURN' ? (data.return_request?.available_evidences || []) : [];
  const evidenceChoices = availableEvidence.length ? `<div class="form"><label>加盟商提交的退回证据</label><p class="muted">可直接查看图片、播放录音或打开文件；预览失败不影响填写和提交核验结果。</p><div class="return-evidence-list">${availableEvidence.map(item => returnEvidenceChoice(item, true)).join('')}</div></div>` : '';
  const evidenceUpload = kind === 'RETURN' ? '<div class="form"><label>上传新的核验证据</label><input id="verification-evidence-files" type="file" multiple accept="image/jpeg,image/png,image/webp,audio/mpeg,audio/wav,audio/mp4,audio/aac"><small class="muted">支持 JPG、PNG、WEBP、MP3、WAV、M4A、AAC；提交时自动上传并绑定。</small></div>' : '';
  return `<section class="card" id="result-form"><h2>填写结果</h2><div class="form"><label>联系结果 *</label><select id="contact_result" class="select">${Object.entries(contactLabels).map(([value, label]) => `<option value="${value}">${label}</option>`).join('')}</select></div><div class="form"><label>事实结论 *</label><div class="radio-grid">${Object.entries(conclusions).map(([value, label], index) => `<label class="choice"><input type="radio" name="conclusion" value="${value}" ${index === 0 ? 'checked' : ''}> ${label}</label>`).join('')}</div></div>${evidenceChoices}${evidenceUpload}<div class="form"><label>核验备注 *</label><textarea id="note" class="textarea" placeholder="记录客户说明和核验依据"></textarea></div><button id="submit" class="btn primary block">提交核验结果</button></section>`;
}

async function task(kind, id) {
  if (!TASK_KIND[kind] || !await auth()) return;
  const data = await api(taskPath(kind, id));
  const displayStatus=data.submitted_at?'SUBMITTED':data.status;
  const lead = data.lead || {};
  const details = taskFacts(kind, data).map(([label, value]) => `<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`).join('');
  const canContact = data.status === 'IN_PROGRESS';
  const availableEvidence = kind === 'RETURN' ? (data.return_request?.available_evidences || []) : [];
  const readonlyReturnEvidence = availableEvidence.length ? `<section class="card"><h2>加盟商提交的退回证据</h2><p class="muted">开始核验前可先查看；预览失败不影响继续处理。</p><div class="return-evidence-list">${availableEvidence.map((item) => returnEvidenceChoice(item, false)).join('')}</div></section>` : '';
  const verificationEvidenceItems = data.verification_info?.evidences || [];
  const verificationEvidence = verificationEvidenceItems.length ? `<div class="return-evidence-list">${verificationEvidenceItems.map((item) => returnEvidenceChoice(item, false)).join('')}</div>` : '';
  const action = data.status === 'ASSIGNED' ? `${readonlyReturnEvidence}<section class="card"><h2>开始核验</h2><p class="muted">该任务已由运营派发给您。开始后可查看完整手机号；参考时间不限制继续处理。</p><button class="btn primary block" id="start">开始核验</button></section>` : data.status === 'IN_PROGRESS' ? taskForm(kind, data) : `<section class="card"><h2>已提交结论</h2><dl class="detail"><div><dt>联系结果</dt><dd>${esc(contactLabels[data.contact_result] || '待确认')}</dd></div><div><dt>事实结论</dt><dd>${esc(TASK_KIND[kind].conclusions[data.conclusion] || '待确认')}</dd></div><div><dt>核验备注</dt><dd>${esc(data.verification_info?.note || '暂无核验备注')}</dd></div></dl>${readonlyReturnEvidence}${verificationEvidence ? `<h3>本次核验证据</h3>${verificationEvidence}` : ''}<p class="muted">结论已经提交运营人员处置，不能由电销人员直接改变客资状态。</p></section>`;
  const contactActions = canContact ? `<div class="detail-actions"><button id="dial" class="btn gold">${icon('phone')}<span>一键拨号</span></button><button id="copy-phone" class="btn outline">复制号码</button></div>` : '';
  const guide = canContact ? '<section class="quick-guide"><b>核验说明</b><span>拨号由您主动确认；桌面端可复制号码，只提交事实结论，不决定派发、退款或终审。</span><a href="#result-form">填写结果</a></section>' : '';
  zsSetSafeHtml(app, shell(`<button class="btn small outline" data-history-back>返回</button><section class="detail-hero"><div><p class="eyebrow">${esc(TASK_KIND[kind].label)}</p><h1>${esc(lead.customer_name || '待核验客户')}</h1><span class="badge ${statusClass(displayStatus)}">${esc(statusLabel(displayStatus))}</span></div>${contactActions}</section>${guide}<section class="card compact"><dl class="detail"><div><dt>手机号</dt><dd><strong>${esc(lead.phone || lead.phone_masked || '--')}</strong></dd></div><div><dt>地区</dt><dd>${esc(lead.city || '--')} ${esc(lead.district || '')}</dd></div>${details}</dl></section>${action}`, data.submitted_at ? 'records' : 'verify', '核验详情'));
  bind();
  bindReturnEvidencePreviewErrors(app);
  bindTaskActions(kind, id, lead.phone);
}

function bindTaskActions(kind, id, phone) {
  document.querySelector('#start')?.addEventListener('click', async () => { try { await api(taskPath(kind, id, 'start'), { method: 'POST' }); toast('已开始核验'); task(kind, id); } catch (error) { toast(error.message, 'error'); } });
  document.querySelector('#dial')?.addEventListener('click', async () => { try { const result = await api(taskPath(kind, id, 'dial'), { method: 'POST' }); location.href = result.tel_url; } catch (error) { toast(error.message, 'error'); } });
  document.querySelector('#copy-phone')?.addEventListener('click', () => copyPhone(phone));
  document.querySelector('#submit')?.addEventListener('click', () => submit(kind, id));
}

async function submit(kind, id) {
  const note = document.querySelector('#note').value.trim();
  if (note.length < 2) { toast('请填写至少 2 个字的核验备注', 'error'); return; }
  try {
    const evidence_ids = [...document.querySelectorAll('input[name=verification_evidence]:checked')].map((item) => item.value);
    const files = [...(document.querySelector('#verification-evidence-files')?.files || [])];
    for (const file of files) {
      const form = new FormData();
      form.append('evidence_type', file.type.startsWith('audio/') ? 'CALL_RECORDING' : 'CHAT_SCREENSHOT');
      form.append('file', file, file.name);
      const uploaded = await api(taskPath(kind, id, 'evidence'), { method: 'POST', body: form });
      evidence_ids.push(uploaded.id);
    }
    await api(taskPath(kind, id, 'submit'), { method: 'POST', body: JSON.stringify({ contact_result: document.querySelector('#contact_result').value, conclusion: document.querySelector('input[name=conclusion]:checked').value, note, evidence_ids }) });
    submittedHistoryState = null;
    toast('事实核验已提交运营处置');
    task(kind, id);
  } catch (error) { toast(error.message, 'error'); }
}

async function profile() {
  if (!await auth()) return;
  zsSetSafeHtml(app, shell(`<h1>我的工作台</h1><section class="card"><div class="brand"><img src="./logo.png" alt="合家美宅"><div>${esc(me.display_name)}<small>电销人员</small></div></div><dl class="detail"><div><dt>工作范围</dt><dd>仅查看和处理运营派发给您的任务；不具备自主领取、转派、派发、终审、退款或积分操作权限。</dd></div></dl></section><section class="card"><button id="logout" class="btn danger block">退出登录</button></section>`, 'profile', '个人中心'));
  bind();
  document.querySelector('#logout').onclick = async () => { try { await api('/auth/logout', { method: 'POST' }); me = null; submittedHistoryState = null; location.hash = '#/home'; await route(); } catch (error) { toast(`退出失败：${error.message}`, 'error'); } };
}

async function route() {
  const parts = (location.hash.replace(/^#\/?/, '') || 'home').split('?')[0].split('/');
  try {
    if (parts[0] === 'home') return await home();
    if (parts[0] === 'verify') return await verify();
    if (parts[0] === 'records') return await records();
    if (parts[0] === 'task' && parts[1] && parts[2]) return await task(parts[1], parts[2]);
    if (parts[0] === 'profile') return await profile();
    return renderInvalidLink();
  } catch (error) { toast(error.message || '加载失败', 'error'); }
}

window.addEventListener('hashchange', route);
route();
