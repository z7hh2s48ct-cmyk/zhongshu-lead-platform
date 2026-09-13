const API = '/api/v1';
const app = document.querySelector('#app');
const toastBox = document.querySelector('#toast');
const sheetRoot = document.querySelector('#sheet-root');
const S = { me: null, view: 'home', fundData: null, sheetSequence: 0 };

const ROLE_META = {
  SUPER_ADMIN: {
    name: '超级管理员',
    title: '平台治理工作台',
    tabs: [['home', 'home', '首页'], ['governance', 'settings', '治理'], ['funds', 'coins', '资金'], ['profile', 'user', '我的']],
  },
  OPERATION: {
    name: '运营管理员',
    title: '运营移动工作台',
    tabs: [['home', 'home', '首页'], ['leads', 'list', '客资'], ['dispatch', 'hand-claim', '派发'], ['exceptions', 'rotate-ccw', '异常'], ['profile', 'user', '我的']],
  },
};
const STATUS = { PENDING: '待审核', PENDING_REVIEW: '待初审', PENDING_OPERATION_DISPOSITION: '运营待处置', DRAFT: '待完善', READY_DISPATCH: '待派发', PENDING_CLAIM: '待领取', ASSIGNED: '待开始', IN_PROGRESS: '核验中', SUBMITTED: '等待运营处置', REVIEWING: '待终审', NEED_MORE_EVIDENCE: '待补证', FROZEN: '冻结奖励', OBSERVING: '观察期', SETTLED: '已结算', ACTIVE: '已启用', INACTIVE: '已停用', APPROVED: '已通过', REJECTED: '已驳回', RECHARGE: '充值', ADJUST: '调账', CLAIM: '领取扣减', RETURN: '退回返还', REWARD: '奖励结算', REVERSE: '冲正' };
const icon = name => window.ZSIconSystem?.svg?.(name) || '';
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const label = value => STATUS[value] || '待确认';
const fmt = value => value ? new Date(value).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '--';
const shortId = (value, prefix = '记录') => value ? `${prefix}-${String(value).replace(/-/g, '').slice(-8).toUpperCase()}` : '--';
const isDone = status => ['ACTIVE', 'APPROVED', 'SETTLED'].includes(status);
const isBad = status => ['REJECTED', 'FROZEN'].includes(status);
const badge = status => `<span class="badge ${isDone(status) ? 'ok' : isBad(status) ? 'bad' : ''}">${esc(label(status))}</span>`;
const greetingName = value => {
  const name = String(value || '').trim();
  return name.length > 6 ? `${name.slice(0, 6)}…` : name;
};
const currentRole = () => Object.keys(ROLE_META).find(role => S.me?.roles?.includes(role)) || '';
const roleMeta = () => ROLE_META[currentRole()];

function identityLabel() {
  const meta = roleMeta();
  return S.me.display_name === meta.name ? meta.name : `${S.me.display_name} · ${meta.name}`;
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  const response = await fetch(`${API}${path}`, { ...options, headers, credentials: 'include' });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || payload.code !== 'OK') {
    const error = new Error(payload.message || '请求失败');
    error.code = payload.code;
    throw error;
  }
  return payload.data;
}

function toast(message, error = false) {
  toastBox.textContent = message;
  toastBox.className = `platform-toast show ${error ? 'error' : ''}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { toastBox.className = 'platform-toast'; }, 2400);
}

function closeSheet() { S.sheetSequence++; sheetRoot.innerHTML = ''; }

function openSheet(title, body, bind) {
  S.sheetSequence++;
  zsSetSafeHtml(sheetRoot, `<div class="sheet-mask"><section class="sheet"><header class="sheet-head"><h2>${esc(title)}</h2><button class="btn small" id="sheet-close">关闭</button></header>${body}</section></div>`);
  document.querySelector('#sheet-close').onclick = closeSheet;
  bind?.();
}

function routeName() {
  const requested = location.hash.replace(/^#\/?/, '').split('?')[0] || 'home';
  if (roleMeta()?.tabs.some(([name]) => name === requested)) return requested;
  return '';
}

function go(view) {
  if (!roleMeta()?.tabs.some(([name]) => name === view)) return;
  location.hash = `#/${view}`;
}

function goDesktop(view, id = '') {
  const query = new URLSearchParams({ view });
  if (id) query.set('id', id);
  location.href = `/admin/v12-operations.html?${query}`;
}

function nav() {
  const tabs = roleMeta().tabs;
  return `<nav class="platform-bottom" aria-label="底部导航" style="--tabs:${tabs.length}">${tabs.map(([view, iconName, text]) => `<button class="nav ${S.view === view ? 'active' : ''}" data-go="${view}"><i>${icon(iconName)}</i><span>${text}</span></button>`).join('')}</nav>`;
}

function shell(content) {
  const meta = roleMeta();
  zsSetSafeHtml(app, `<div class="platform-shell"><header class="platform-top"><div class="platform-brand"><img src="/admin/logo.png" alt="合家美宅"><div>合家美宅<small>${esc(identityLabel())}</small></div></div><div class="platform-top-actions"><button class="btn small" id="refresh">${icon('rotate-ccw')}<span>刷新</span></button><button class="btn small" id="logout">${icon('log-out')}<span>退出</span></button></div></header><main class="platform-main">${content}</main>${nav()}</div>`);
  document.querySelectorAll('[data-go]').forEach(node => { node.onclick = () => go(node.dataset.go); });
  document.querySelector('#refresh').onclick = render;
  document.querySelector('#logout').onclick = logout;
}

function empty(title, description, action = '') {
  return `<div class="empty"><b>${esc(title)}</b><p>${esc(description)}</p>${action}</div>`;
}

function pageTitle(title, description) {
  return `<section class="page-title"><h1>${esc(title)}</h1><p>${esc(description)}</p></section>`;
}

function todo(title, description, view, count) {
  return `<button class="todo" data-go="${view}"><span><b>${esc(title)}${count == null ? '' : ` · ${Number(count)} 项`}</b><p>${esc(description)}</p></span>${icon('chevron-right')}</button>`;
}

function metric(text, value, view) {
  return `<button class="metric" data-go="${view}"><b>${Number(value || 0)}</b><span>${esc(text)}</span></button>`;
}

function platformHomeGreeting() {
  const meta = roleMeta();
  const name = String(S.me?.display_name || meta.name).trim();
  return `<section class="platform-home-greeting"><div><p>${esc(meta.name)}</p><h1>${esc(greetingName(name))}，上午好</h1></div><div class="platform-home-avatar" aria-label="${esc(name)}">${esc(name.slice(0, 1))}</div></section>`;
}

function platformHomeHero({ labelText, value, description, actionLabel, view }) {
  return `<section class="hero platformHomeHero"><div><small>${esc(labelText)}</small><strong>${Number(value || 0)}</strong><span>${esc(description)}</span></div><button class="btn gold" data-go="${view}">${esc(actionLabel)}</button></section>`;
}

function platformHomeMetrics(items) {
  return `<section class="metrics platformHomeMetrics" aria-label="工作概览">${items.map(([labelText, value, view]) => metric(labelText, value, view)).join('')}</section>`;
}

function platformHomeTasks(items) {
  const pending = items.filter(([, , , value]) => Number(value || 0) > 0);
  if (!pending.length) return '';
  return `<section class="card platform-home-tasks"><div class="section-head"><h2>待处理事项</h2></div>${pending.map(item => todo(...item)).join('')}</section>`;
}

function count(items, states) { return (items || []).filter(item => states.includes(item.status)).length; }

async function home() {
  const [report, preDispatch, returns] = await Promise.all([
    api('/v1.2/reports/overview'),
    api('/v1.2/pre-dispatch-verifications/tasks?page=1&page_size=200'),
    api('/v1.2/returns?page=1&page_size=200'),
  ]);
  const tasks = preDispatch.items || [];
  const returnItems = returns.items || [];
  if (currentRole() === 'SUPER_ADMIN') {
    const pendingOperation = count(tasks, ['SUBMITTED']);
    const reviewing = count(returnItems, ['REVIEWING']);
    const waitingDispatch = Number(report.leads?.by_status?.READY_DISPATCH || 0);
    shell(`${platformHomeGreeting()}${platformHomeHero({ labelText: '风险待办', value: pendingOperation + reviewing, description: '待终审与待处置优先', actionLabel: '进入治理', view: 'governance' })}${platformHomeMetrics([['待处置', pendingOperation, 'governance'], ['待终审', reviewing, 'governance'], ['待派发', waitingDispatch, 'governance']])}${platformHomeTasks([['运营待处置', '依据电销事实决定后续流转。', 'governance', pendingOperation], ['退回终审', '审查退回申请与积分处理。', 'governance', reviewing]])}`);
    return;
  }
  const pendingReview = Number(report.leads?.by_status?.PENDING_REVIEW || 0);
  const waitingDispatch = Number(report.leads?.by_status?.READY_DISPATCH || 0);
  const pendingDisposition = count(tasks, ['SUBMITTED']);
  const returnReview = count(returnItems, ['REVIEWING']);
  const priority = pendingDisposition || pendingReview || waitingDispatch || returnReview;
  shell(`${platformHomeGreeting()}${platformHomeHero({ labelText: '优先处理', value: priority, description: '初审、处置、派发按序进行', actionLabel: '处理客资', view: 'leads' })}${platformHomeMetrics([['待初审', pendingReview, 'leads'], ['待处置', pendingDisposition, 'leads'], ['待派发', waitingDispatch, 'dispatch']])}${platformHomeTasks([['客资初审', '核对资料、重复和服务区域。', 'leads', pendingReview], ['电销结论', '依据电销事实决定流转。', 'leads', pendingDisposition], ['待派发', '确认接收公司后完成派发。', 'dispatch', waitingDispatch], ['退回终审', '审查退回申请与积分处理。', 'exceptions', returnReview]])}`);
}

function companyCard(company) {
  return `<article class="item"><div class="item-top"><div><h3>${esc(company.name)}</h3><small>${esc(company.code)} · 当前积分 ${Number(company.points_balance || 0)}</small></div>${badge(company.status)}</div><div class="item-actions"><button class="btn small" data-company-desktop="${esc(company.id)}">桌面治理</button></div></article>`;
}

async function governance() {
  const page = await api('/companies?page=1&page_size=50');
  const items = page.items || [];
  shell(`${pageTitle('治理', '查看账号和加盟商公司事项；批量账号操作留在桌面。')}<section class="card"><div class="section-head"><div><h2>待办</h2><p>公司状态决定是否参与业务接收和资金处理。</p></div><button class="btn small" id="governance-desktop">桌面治理</button></div>${items.length ? `<div class="list">${items.map(companyCard).join('')}</div>` : empty('暂无加盟商公司', '创建加盟商后会显示在这里。')}</section>`);
  document.querySelector('#governance-desktop').onclick = () => goDesktop('companies');
  document.querySelectorAll('[data-company-desktop]').forEach(button => { button.onclick = () => goDesktop('companies', button.dataset.companyDesktop); });
}

function fundCompanyCard(company) {
  return `<article class="item"><div class="item-top"><div><h3>${esc(company.name)}</h3><small>${esc(company.code)} · 当前积分 ${Number(company.points_balance || 0)}</small></div>${badge(company.status)}</div><div class="item-actions"><button class="btn small" data-reconcile="${esc(company.id)}">核对</button><button class="btn small" data-adjust="${esc(company.id)}">调账</button><button class="btn small primary" data-recharge="${esc(company.id)}">充值</button></div></article>`;
}

async function funds() {
  const [companiesPage, packages, ledgerPage, rewardsPage] = await Promise.all([
    api('/companies?page=1&page_size=50'),
    api('/points/packages?active_only=true'),
    api('/points/ledgers?page=1&page_size=20'),
    api('/v1.2/supplier-rewards?page=1&page_size=100'),
  ]);
  const companies = companiesPage.items || [];
  const ledgers = ledgerPage.items || [];
  const frozen = count(rewardsPage.items, ['FROZEN']);
  S.fundData = { companies, packages, ledgers };
  const ledgerRows = ledgers.map(ledger => {
    const type = ledger.ledger_type || ledger.type;
    const reversible = ['RECHARGE', 'ADJUST'].includes(type);
    return `<article class="item"><div class="item-top"><div><h3>${esc(label(type))} · ${ledger.delta > 0 ? '+' : ''}${Number(ledger.delta || 0)} 积分</h3><small>${fmt(ledger.created_at)} · 余额 ${Number(ledger.balance_after || 0)}</small></div>${badge(type)}</div>${reversible ? `<div class="item-actions"><button class="btn small danger" data-reverse-ledger="${esc(ledger.id)}">冲正</button></div>` : ''}</article>`;
  }).join('');
  shell(`${pageTitle('资金', '单笔操作无需第二位超级管理员复核，但必须保留公司、金额或积分、凭证说明和审计。')}<section class="card"><div class="section-head"><div><h2>待办</h2><p>先核对账目，再进行充值、调账或冲正。</p></div></div>${todo('待核对账目', '逐公司比对账户快照、不可变流水和顺序异常。', 'funds', companies.length)}${todo('冻结奖励', '冻结奖励不会入账，应核对关联业务后处理。', 'funds', frozen)}</section><section class="card"><div class="section-head"><div><h2>公司积分</h2><p>充值、调账和账目核对均会写入审计。</p></div></div><div class="list">${companies.map(fundCompanyCard).join('') || empty('暂无公司', '暂无可操作的积分账户。')}</div></section><section class="card"><div class="section-head"><div><h2>最近流水</h2><p>领取、退回和奖励必须在各自业务流程中处理。</p></div></div><div class="list">${ledgerRows || empty('暂无积分流水', '资金变化会以不可变流水显示。')}</div></section>`);
  document.querySelectorAll('[data-reconcile]').forEach(button => { button.onclick = () => reconcile(button.dataset.reconcile); });
  document.querySelectorAll('[data-adjust]').forEach(button => { button.onclick = () => adjustmentSheet(button.dataset.adjust); });
  document.querySelectorAll('[data-recharge]').forEach(button => { button.onclick = () => rechargeSheet(button.dataset.recharge); });
  document.querySelectorAll('[data-reverse-ledger]').forEach(button => { button.onclick = () => reversalSheet(button.dataset.reverseLedger); });
}

async function reconcile(companyId) {
  const requestId = ++S.sheetSequence;
  try {
    const result = await api(`/points/reconciliation/${encodeURIComponent(companyId)}`);
    if (requestId !== S.sheetSequence) return;
    openSheet(result.balanced ? '账目核对一致' : '发现账目差异', `<div class="detail-grid">${[['流水期末余额', result.expected_closing_balance], ['账户快照余额', result.snapshot_balance], ['余额差异', result.difference], ['流水顺序异常', result.sequence_error_count]].map(([key, value]) => `<div class="detail"><small>${key}</small><b>${esc(value)}</b></div>`).join('')}</div><p class="notice">${result.balanced ? '余额、流水和顺序均已核对一致。' : '请停止继续人工资金写入，并在桌面审计中依据不可变流水排查差异。'}</p>`);
  } catch (error) { if (requestId === S.sheetSequence) toast(error.message, true); }
}

function rechargeSheet(companyId) {
  const company = S.fundData.companies.find(item => item.id === companyId);
  const packages = S.fundData.packages || [];
  const options = packages.map(item => `<option value="${esc(item.id)}" data-cash="${Number(item.cash_amount_cents)}">${esc(item.name)} · ${Number(item.total_points)} 积分</option>`).join('');
  if (!options) { toast('请先在桌面资金页配置可用充值档位', true); return; }
  openSheet(`为${company?.name || '加盟商'}充值`, `<form class="form" id="recharge-form"><div class="notice">请先完成线下收款核验。提交后写入不可变流水和审计。</div><div class="field"><label for="recharge-package">充值档位 *</label><select class="select" id="recharge-package">${options}</select></div><div class="field"><label for="recharge-reference">外部收款凭据号 *</label><input class="input" id="recharge-reference" minlength="3" maxlength="128"></div><div class="field"><label for="recharge-note">收款核验与凭证说明 *</label><textarea class="textarea" id="recharge-note" minlength="3" maxlength="500"></textarea></div><label class="check"><input type="checkbox" id="recharge-confirmed"> 我已核实本笔线下款项</label><div class="sheet-actions"><button type="button" class="btn" id="recharge-cancel">取消</button><button class="btn primary" id="recharge-submit">确认充值</button></div></form>`, () => {
    document.querySelector('#recharge-cancel').onclick = closeSheet;
    const form = document.querySelector('#recharge-form');
    form.onsubmit = async event => {
      event.preventDefault();
      const reference = document.querySelector('#recharge-reference').value.trim();
      const note = document.querySelector('#recharge-note').value.trim();
      if (reference.length < 3 || note.length < 3 || !document.querySelector('#recharge-confirmed').checked) { toast('请完整填写凭据、说明并确认已核实收款', true); return; }
      const button = document.querySelector('#recharge-submit');
      button.disabled = true;
      try {
        const selected = document.querySelector('#recharge-package').selectedOptions[0];
        await api('/points/recharge', { method: 'POST', body: JSON.stringify({ company_id: companyId, package_id: selected.value, cash_amount_cents: Number(selected.dataset.cash), external_reference: reference, note, confirmed: true, idempotency_key: `h5-recharge-${crypto.randomUUID()}` }) });
      } catch (error) { button.disabled = false; toast(error.message, true); return; }
      toast('积分已充值入账');
      if (form.isConnected) {
        closeSheet();
        try { await funds(); } catch { toast('积分已充值入账，列表刷新失败，请手动刷新查看', true); }
      }
    };
  });
}

function adjustmentSheet(companyId) {
  const company = S.fundData.companies.find(item => item.id === companyId);
  openSheet(`为${company?.name || '加盟商'}人工调账`, `<form class="form" id="adjustment-form"><div class="notice">调整会生成不可变流水。请填写正负积分值及可复核的原因或凭证说明。</div><div class="field"><label for="adjustment-delta">调整积分 *</label><input class="input" id="adjustment-delta" type="number" inputmode="numeric" placeholder="正数增加，负数扣减"></div><div class="field"><label for="adjustment-reason">调账原因及凭证说明 *</label><textarea class="textarea" id="adjustment-reason" minlength="3" maxlength="500"></textarea></div><div class="sheet-actions"><button type="button" class="btn" id="adjustment-cancel">取消</button><button class="btn primary" id="adjustment-submit">确认调账</button></div></form>`, () => {
    document.querySelector('#adjustment-cancel').onclick = closeSheet;
    const form = document.querySelector('#adjustment-form');
    form.onsubmit = async event => {
      event.preventDefault();
      const delta = Number(document.querySelector('#adjustment-delta').value);
      const reason = document.querySelector('#adjustment-reason').value.trim();
      if (!Number.isInteger(delta) || delta === 0 || reason.length < 3) { toast('请填写非零整数积分和至少 3 个字符的说明', true); return; }
      const button = document.querySelector('#adjustment-submit'); button.disabled = true;
      try {
        await api('/points/adjust', { method: 'POST', body: JSON.stringify({ company_id: companyId, delta, reason, idempotency_key: `h5-adjust-${crypto.randomUUID()}` }) });
      } catch (error) { button.disabled = false; toast(error.message, true); return; }
      toast('积分调账已入账');
      if (form.isConnected) {
        closeSheet();
        try { await funds(); } catch { toast('积分调账已入账，列表刷新失败，请手动刷新查看', true); }
      }
    };
  });
}

function reversalSheet(ledgerId) {
  openSheet('确认人工流水冲正', `<form class="form" id="reversal-form"><div class="notice">仅能冲正人工充值和人工调账。领取、退回和奖励必须通过各自业务流程处理。</div><div class="field"><label for="reversal-reason">冲正原因及凭证说明 *</label><textarea class="textarea" id="reversal-reason" minlength="3" maxlength="500"></textarea></div><div class="sheet-actions"><button type="button" class="btn" id="reversal-cancel">取消</button><button class="btn danger" id="reversal-submit">确认冲正</button></div></form>`, () => {
    document.querySelector('#reversal-cancel').onclick = closeSheet;
    const form = document.querySelector('#reversal-form');
    form.onsubmit = async event => {
      event.preventDefault();
      const reason = document.querySelector('#reversal-reason').value.trim();
      if (reason.length < 3) { toast('请填写至少 3 个字符的冲正说明', true); return; }
      const button = document.querySelector('#reversal-submit'); button.disabled = true;
      try {
        await api(`/points/ledgers/${encodeURIComponent(ledgerId)}/reverse`, { method: 'POST', body: JSON.stringify({ reason, idempotency_key: `h5-reverse-${crypto.randomUUID()}` }) });
      } catch (error) { button.disabled = false; toast(error.message, true); return; }
      toast('人工流水已冲正');
      if (form.isConnected) {
        closeSheet();
        try { await funds(); } catch { toast('人工流水已冲正，列表刷新失败，请手动刷新查看', true); }
      }
    };
  });
}

function operationLeadCard(lead) {
  return `<article class="item"><div class="item-top"><div><h3>${esc(lead.customer_name || '待处理客资')}</h3><small>${esc(lead.city || '')} ${esc(lead.district || '')} · ${esc(lead.phone_masked || '--')}</small></div>${badge(lead.status)}</div><p>${esc(lead.need_summary || '请补充客户需求说明。')}</p><div class="item-actions"><button class="btn small primary" data-lead-desktop="${esc(lead.id)}">单条处理</button></div></article>`;
}

async function leads() {
  const [platformReview, platformDisposition, supplierReview, supplierDisposition, tasks] = await Promise.all([
    api('/v1.2/platform/leads?status=PENDING_REVIEW&page=1&page_size=10'),
    api('/v1.2/platform/leads?status=PENDING_OPERATION_DISPOSITION&page=1&page_size=10'),
    api('/v1.2/admin/supplier-leads?status=PENDING_REVIEW&review_status=PENDING&page=1&page_size=10'),
    api('/v1.2/admin/supplier-leads?status=PENDING_OPERATION_DISPOSITION&review_status=PENDING&page=1&page_size=10'),
    api('/v1.2/pre-dispatch-verifications/tasks?page=1&page_size=100'),
  ]);
  const items = [
    ...(platformReview.items || []),
    ...(supplierReview.items || []),
    ...(platformDisposition.items || []),
    ...(supplierDisposition.items || []),
  ];
  const pendingDisposition = count(tasks.items, ['SUBMITTED']);
  shell(`${pageTitle('客资', '移动端用于查看并处理单条事项；批量审核仍留在桌面。')}<section class="card"><div class="section-head"><div><h2>待办</h2><p>初审、前置电销和运营处置均由运营管理员负责。</p></div></div>${todo('待初审', '核对资料完整性、重复线索和服务区域。', 'leads', count(items, ['PENDING_REVIEW', 'DRAFT']))}${todo('电销结论', '事实结论由电销提交，下一步由运营决定。', 'leads', pendingDisposition)}</section><section class="card"><div class="section-head"><div><h2>待处理客资</h2><button class="btn small" id="leads-desktop">桌面批量处理</button></div><div class="list">${items.map(operationLeadCard).join('') || empty('暂无待处理客资', '新的平台客资或加盟商供资会显示在这里。')}</div></section>`);
  document.querySelector('#leads-desktop').onclick = () => goDesktop('leads');
  document.querySelectorAll('[data-lead-desktop]').forEach(button => { button.onclick = () => goDesktop('leads', button.dataset.leadDesktop); });
}

async function dispatch() {
  const pool = await api('/v1.2/dispatch-pool?page=1&page_size=20');
  shell(`${pageTitle('派发', '仅确认单条派发；批量派发和复杂候选比对留在桌面。')}<section class="card"><div class="section-head"><div><h2>待办</h2><p>派发前先核对服务区域、公司状态和可用积分。</p></div></div>${todo('待派发', '选择接收公司并留存派发确认。', 'dispatch', pool.total)}</section><section class="card"><div class="section-head"><h2>派发池</h2><button class="btn small" id="dispatch-desktop">桌面批量处理</button></div><div class="list">${(pool.items || []).map(lead => `<article class="item"><div class="item-top"><div><h3>${esc(lead.customer_name || '待派发客资')}</h3><small>${esc(lead.city || '')} ${esc(lead.district || '')} · ${esc(lead.phone_masked || '--')}</small></div>${badge(lead.status || 'READY_DISPATCH')}</div><p>${esc(lead.need_summary || '请核对接收公司。')}</p><div class="item-actions"><button class="btn small primary" data-dispatch-desktop="${esc(lead.id)}">确认派发</button></div></article>`).join('') || empty('暂无待派发客资', '客资审核通过后会进入派发池。')}</div></section>`);
  document.querySelector('#dispatch-desktop').onclick = () => goDesktop('dispatch');
  document.querySelectorAll('[data-dispatch-desktop]').forEach(button => { button.onclick = () => goDesktop('dispatch', button.dataset.dispatchDesktop); });
}

async function exceptions() {
  const [returns, tasks] = await Promise.all([api('/v1.2/returns?page=1&page_size=20'), api('/v1.2/return-verifications/tasks?page=1&page_size=100')]);
  const returnItems = returns.items || [];
  shell(`${pageTitle('异常', '退回、超期与公司异常均应保留可追溯的处理记录。')}<section class="card"><div class="section-head"><div><h2>待办</h2><p>电销只给出事实结论；运营负责终审和后续处理。</p></div></div>${todo('退回终审', '核验完成后，决定退回、补证或驳回。', 'exceptions', count(returnItems, ['REVIEWING']))}${todo('待补证', '需要加盟商补充证据后再终审。', 'exceptions', count(returnItems, ['NEED_MORE_EVIDENCE']))}${todo('电话核验', '查看运营派发和超时的核验任务。', 'exceptions', count(tasks.items, ['ASSIGNED', 'IN_PROGRESS', 'SUBMITTED']))}</section><section class="card"><div class="section-head"><h2>退回申诉</h2><button class="btn small" id="exceptions-desktop">桌面终审</button></div><div class="list">${returnItems.map(item => `<article class="item"><div class="item-top"><div><h3>${esc(shortId(item.id, '退回'))}</h3><small>${fmt(item.submitted_at || item.created_at)} · ${esc(label(item.reason_code))}</small></div>${badge(item.status)}</div><div class="item-actions"><button class="btn small primary" data-return-desktop="${esc(item.id)}">查看终审</button></div></article>`).join('') || empty('暂无异常申诉', '加盟商提交退回申请后会显示在这里。')}</div></section>`);
  document.querySelector('#exceptions-desktop').onclick = () => goDesktop('returns');
  document.querySelectorAll('[data-return-desktop]').forEach(button => { button.onclick = () => goDesktop('returns', button.dataset.returnDesktop); });
}

async function profile() {
  const notifications = await api('/notifications?page=1&page_size=20');
  const unread = (notifications.items || []).filter(item => !item.read_at).length;
  shell(`${pageTitle('我的', '身份、消息、密码帮助和次级设置集中在这里。')}<section class="card"><div class="detail-grid"><div class="detail"><small>姓名</small><b>${esc(S.me.display_name)}</b></div><div class="detail"><small>角色</small><b>${esc(roleMeta().name)}</b></div><div class="detail"><small>未读提醒</small><b>${unread} 条</b></div><div class="detail"><small>账号</small><b>${esc(S.me.username)}</b></div></div></section><section class="card"><div class="section-head"><div><h2>行动提醒</h2><p>消息不占用底栏，只显示对当前角色有行动价值的提醒。</p></div></div><div class="list">${(notifications.items || []).slice(0, 5).map(item => `<article class="item"><div class="item-top"><div><h3>${esc(item.title || '系统提醒')}</h3><small>${fmt(item.created_at)}</small></div>${item.read_at ? badge('APPROVED') : badge('PENDING')}</div><p>${esc(item.body || '请进入对应业务页面处理。')}</p></article>`).join('') || empty('暂无行动提醒', '有需要处理的业务事项时会显示在这里。')}</div></section><section class="card"><div class="item-actions"><button class="btn" id="password-help">密码帮助</button><button class="btn danger" id="profile-logout">退出登录</button></div></section>`);
  document.querySelector('#password-help').onclick = () => openSheet('密码帮助', '<p class="notice">如需重置密码，请联系超级管理员或在桌面“治理”中按账号审计流程处理。</p>');
  document.querySelector('#profile-logout').onclick = logout;
}

async function logout() {
  try {
    await api('/auth/logout', { method: 'POST' });
    S.me = null;
    location.hash = '#/home';
    renderLogin();
  } catch (error) {
    toast(`退出失败：${error.message}`, true);
  }
}

function renderLogin(message = '') {
  zsSetSafeHtml(app, `<section class="login"><div class="login-card"><img src="/admin/logo.png" alt="合家美宅"><h1>平台工作台</h1><p>仅超级管理员和运营管理员可登录。登录后会进入与角色匹配的移动首页。</p>${message ? `<p class="notice">${esc(message)}</p>` : ''}<form class="form" id="login-form"><div class="field"><label for="username">登录账号</label><input class="input" id="username" autocomplete="username" required></div><div class="field"><label for="password">登录密码</label><input class="input" id="password" type="password" autocomplete="current-password" required></div><button class="btn primary" id="login-submit">登录工作台</button></form></div></section>`);
  document.querySelector('#login-form').onsubmit = async event => {
    event.preventDefault();
    const button = document.querySelector('#login-submit'); button.disabled = true;
    try {
      await api('/auth/login', { method: 'POST', body: JSON.stringify({ username: document.querySelector('#username').value.trim(), password: document.querySelector('#password').value }) });
      await initialize();
    } catch (error) { button.disabled = false; toast(error.message, true); }
  };
}

function renderAccessDenied() {
  const role = S.me?.roles?.[0];
  const target = role === 'TELESALES' ? '/h5/call/' : '/h5/';
  const targetName = role === 'TELESALES' ? '电销工作台' : '加盟商工作台';
  zsSetSafeHtml(app, `<section class="login"><div class="login-card"><img src="/admin/logo.png" alt="合家美宅"><h1>无权访问</h1><p>当前账号不具备平台移动工作台权限，已阻止进入无关业务页面。</p><button class="btn primary" id="return-role-home">返回${targetName}</button></div></section>`);
  document.querySelector('#return-role-home').onclick = () => { location.href = target; };
}

function renderLoadError(message = '页面加载失败') {
  zsSetSafeHtml(app, `<section class="login"><div class="login-card"><img src="/admin/logo.png" alt="合家美宅"><h1>暂时无法加载</h1><p>${esc(message)}</p><button class="btn primary" id="retry-initialize">重试</button></div></section>`);
  document.querySelector('#retry-initialize').onclick = initialize;
}

function renderInvalidLink() {
  zsSetSafeHtml(app, `<section class="login"><div class="login-card"><img src="/admin/logo.png" alt="合家美宅"><h1>链接已失效</h1><p>该页面链接已迁移或不完整，请从当前角色首页重新进入。</p><button class="btn primary" id="return-platform-home">返回首页</button></div></section>`);
  document.querySelector('#return-platform-home').onclick = () => go('home');
}

async function render() {
  closeSheet();
  if (!S.me) return renderLogin();
  if (!roleMeta()) return renderAccessDenied();
  S.view = routeName();
  if (!S.view) return renderInvalidLink();
  shell('<div class="loading">加载中…</div>');
  try {
    const views = { home, governance, funds, leads, dispatch, exceptions, profile };
    await views[S.view]();
  } catch (error) {
    shell(`<div class="error">${esc(error.message)}</div>`);
    toast(error.message, true);
  }
}

async function initialize() {
  try {
    S.me = await api('/auth/me');
    if (!roleMeta()) { renderAccessDenied(); return; }
    await render();
  } catch (error) {
    if (['AUTH_REQUIRED', 'AUTH_INVALID'].includes(error.code)) renderLogin();
    else renderLoadError(error.message);
  }
}

window.addEventListener('hashchange', render);
initialize();
