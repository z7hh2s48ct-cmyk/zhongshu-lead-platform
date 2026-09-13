import { amountToWan, wanToAmount } from '/h5/business-units.js';

const API='/api/v1',app=document.querySelector('#app'),toastBox=document.querySelector('#toast'),sheet=document.querySelector('#sheet-root');
const S={me:null,view:'home',id:'',page:1,unreadNotifications:0};
const LABEL={DRAFT:'待完善',PENDING:'审核中',PENDING_REVIEW:'平台审核中',PUBLIC_POOL:'等待当地接收方',READY_DISPATCH:'已进入派发',DUPLICATE:'重复信息复核中',PENDING_CLAIM:'待领取',CLAIMED:'已领取',FOLLOWING:'跟进中',RETURN_PENDING:'退回处理中',RETURNED:'已退回',RELEASED:'已释放',EXPIRED:'已过期',CLOSED:'已关闭',COMPLETED:'已完成',UNCONTACTED:'未联系',CONTACTED:'已联系',INTERESTED:'有意向',NOT_INTERESTED:'无意向',DEAL:'电话确认有效',INVALID:'需要修改',SUBMITTED:'已提交',VERIFYING:'核验中',REVIEWING:'待终审',NEED_MORE_EVIDENCE:'待补证',APPROVED:'审核通过',REJECTED:'需要修改',CLEAR:'未发现重复',HARD_DUPLICATE:'近期已有相同客户',REWARD_DUPLICATE:'已有相同客户记录',HISTORICAL_SUSPECT:'历史记录待确认',OVERRIDDEN:'已人工确认',OBSERVING:'待结算',FROZEN:'暂缓结算',SETTLED:'已结算',CANCELLED:'已取消',REVERSED:'已调整',WAITING_CLAIM:'等待有效确认',READ:'已读',UNREAD:'未读',EMPTY_NUMBER:'空号或停机',OUT_OF_SERVICE_REGION:'超出服务区域',DUPLICATE_TO_RECEIVER:'接收方重复客户',NON_HOUSING_CONSULTATION:'非建房装修咨询',ASSIGNED:'待处理',IN_PROGRESS:'核验中',SUPPORT_RETURN:'支持退回',DOES_NOT_SUPPORT_RETURN:'不支持退回',INCONCLUSIVE:'信息不足',UNCLAIMED_TIMEOUT:'未在领取截止前确认接收',CLAIM_TIMEOUT:'未在领取截止前确认接收',REFUSED_CLAIM:'接收方拒绝领取',RETURN_APPROVED:'退回审核通过',V12_RETURN_APPROVED:'退回审核通过'};
const ROLE_HOME_CONTRACT={FRANCHISE_OWNER:'加盟商工作台',FRANCHISE_EMPLOYEE:'加盟商工作台'};
const ROLE_HOME_PRIORITY=['FRANCHISE_OWNER','FRANCHISE_EMPLOYEE'];
const FRANCHISE_NAV={
  FRANCHISE_OWNER:[['home','home','首页'],['assignments','hand-claim','接收'],['leads','plus','供资'],['followups','clipboard-check','跟进'],['profile','user','我的']],
  FRANCHISE_EMPLOYEE:[['home','home','首页'],['assignments','hand-claim','接收'],['followups','clipboard-check','跟进'],['leads','plus','供资'],['profile','user','我的']],
};
const VIEWS={home:['首页','home'],leads:['供资','plus'],points:['积分','coins'],notifications:['消息','bell'],profile:['我的','user'],assignments:['接收','hand-claim'],followups:['跟进','clipboard-check'],returns:['退回记录','rotate-ccw'],reports:['经营报表','chart-no-axes-combined'],rewards:['奖励','award']};
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=v=>v?new Date(v).toLocaleString('zh-CN'):'--';
function deadlineState(value,now=Date.now()+(S.serverTimeOffset||0)){
  const deadline=value?Date.parse(value):NaN;
  if(!Number.isFinite(deadline))return {allowed:false,text:'截止时间待确认'};
  const remaining=deadline-now;
  if(remaining<=0)return {allowed:false,text:'已截止'};
  const minutes=Math.ceil(remaining/60000);
  return {allowed:true,text:`剩余 ${Math.floor(minutes/60)} 小时 ${minutes%60} 分钟`};
}
function deadlineNotice(value,label){return `${esc(label)} ${esc(fmt(value))} · <span data-deadline="${esc(value||'')}">${esc(deadlineState(value).text)}</span>`}
function deadlineButtonAttributes(value){return `data-action-deadline="${esc(value||'')}" ${deadlineState(value).allowed?'':'disabled'}`}
function refreshDeadlineControls(now=Date.now()+(S.serverTimeOffset||0)){
  document.querySelectorAll('[data-action-deadline]').forEach(button=>{if(!deadlineState(button.dataset.actionDeadline,now).allowed)button.disabled=true;});
  document.querySelectorAll('[data-deadline]').forEach(node=>{node.textContent=deadlineState(node.dataset.deadline,now).text;});
}
const num=v=>Number(v||0).toLocaleString('zh-CN');
const icon=name=>window.ZSIconSystem?.svg(name)||'';
const TECHNICAL_CODE=/^(?:[A-Z][A-Z0-9_]{2,}|[a-z][a-z0-9]*|[a-z0-9]+(?:[_-][a-z0-9]+)+)$/;
const readableLabel=(value,fallback='待确认')=>{const text=String(value??'').trim();if(!text)return fallback;return LABEL[text]||(TECHNICAL_CODE.test(text)?fallback:text)};
const packageName=packageItem=>{const raw=String(packageItem?.name||'').trim();const fallback=readableLabel(packageItem?.level_code,'充值档位');if(!raw)return fallback;const levelNames={V1:'普通加盟商',V2:'重点加盟商',V3:'核心加盟商'};return raw.replace(/^(V1|V2|V3)\b/i,code=>levelNames[code.toUpperCase()]||fallback)};
const REWARD_REASON={REWARD_DUPLICATE:'该客户已有奖励记录',ZERO_REWARD_POINTS:'本次未产生奖励积分',FRAUD:'核对后不符合奖励条件',SYSTEM_ERROR:'系统核对后调整',ADMIN_ERROR:'平台复核后调整'};
const rewardReason=value=>{const text=String(value||'').trim();if(!text)return'';const [code,...detail]=text.split(':');const reason=REWARD_REASON[code.trim()]||readableLabel(code.trim(),'');const note=detail.join(':').trim();return [reason,note].filter(Boolean).join('：')};
const returnDecisionSummary=x=>{const reason=String(x?.final_decision_reason||'').trim();if(reason)return reason;if(x?.status==='APPROVED')return Number(x.refund_points||0)>0?`审核已通过，已返还 ${num(x.refund_points)} 积分。`:'审核已通过，积分已按规则处理。';if(x?.status==='REJECTED')return'审核未通过，请继续按原客资流程跟进。';if(x?.status==='NEED_MORE_EVIDENCE')return'请按平台说明补充新的沟通截图或电话录音。';if(x?.status==='DRAFT')return'草稿尚未提交，请在截止时间前完成上传并提交';return'等待平台终审';};
const recordCode=(value,prefix='记录')=>{const text=String(value??'').replace(/-/g,'');return text?`${prefix}-${text.slice(-8).toUpperCase()}`:'--'};
const badge=(v,text=readableLabel(v))=>`<span class="wb-status ${['APPROVED','CLAIMED','SETTLED','COMPLETED','READ'].includes(v)?'ok':['REJECTED','CANCELLED','REVERSED'].includes(v)?'bad':'warn'}">${esc(text)}</span>`;
const can=p=>(S.me?.permissions||[]).some(x=>x==='*'||x===p);
const isFranchiseOwner=()=>Boolean((S.me?.roles||[]).includes('FRANCHISE_OWNER'));
const franchiseRole=()=>isFranchiseOwner()?'FRANCHISE_OWNER':(S.me?.roles||[]).includes('FRANCHISE_EMPLOYEE')?'FRANCHISE_EMPLOYEE':'';
const franchiseTabs=()=>FRANCHISE_NAV[franchiseRole()]||[];
const canReadAssignments=()=>can('assignment.own.read')||can('assignment.employee.read');
const canClaimAssignment=()=>can('assignment.own.claim')||can('assignment.employee.claim');
const HOME_ASSIGNMENT_STATUSES=['PENDING_CLAIM','CLAIMED','FOLLOWING','RETURN_PENDING'];
const greetingName=value=>{const name=String(value||'').trim();return name.length>6?`${name.slice(0,6)}…`:name};
function safeDeepLink(raw){const value=String(raw||'').trim();if(!value)return '';try{const url=new URL(value,location.origin);if(url.origin!==location.origin||!url.pathname.startsWith('/h5/'))return '';return `${url.pathname}${url.search}${url.hash}`}catch{return ''}}
function legacyAssignmentLinkToken(hash=location.hash){const match=String(hash||'').match(/^#\/link\/([^/?#]+)$/);return match?match[1]:''}
async function resolveLegacyAssignmentMessageLink(){const token=legacyAssignmentLinkToken();if(!token)return'';const url=new URL(location.href);url.hash='';try{const resolved=await api(`/claims/resolve-link?token=${encodeURIComponent(token)}`);const assignmentId=String(resolved?.assignment_id||'').trim();if(!assignmentId)throw new Error('消息链接未找到对应客资');url.searchParams.set('view','assignments');url.searchParams.set('id',assignmentId);history.replaceState(null,'',url);return''}catch(error){history.replaceState(null,'',url);return error.message||'消息链接已失效，请从接收列表查看客资'}}
const WORKBENCH_REPORT_PERMISSIONS=['assignment.own.read','assignment.employee.read','supplier.lead.manage','supplier.reward.own.read','points.own.read'];
const canAny=permissions=>permissions.some(can);
const canOwnReport=()=>canAny(WORKBENCH_REPORT_PERMISSIONS);
const VIEW_PERMISSION={leads:'supplier.lead.manage',points:'points.own.read',returns:'return.own.manage',rewards:'supplier.reward.own.read',notifications:'notification.own.read'};
const canView=view=>{
  if(view==='home'||view==='profile')return Boolean(franchiseRole());
  if(view==='reports')return Boolean(franchiseRole())&&canOwnReport();
  if(view==='assignments')return canReadAssignments();
  if(view==='followups')return canReadAssignments();
  return Boolean(VIEW_PERMISSION[view]&&can(VIEW_PERMISSION[view]));
};
function defaultWorkbenchView(){
  if(canView('home'))return 'home';
  if(canView('leads'))return 'leads';
  if(canView('points'))return 'points';
  if(canView('notifications'))return 'notifications';
  if(canView('profile'))return 'profile';
  if(canView('returns'))return 'returns';
  return 'home';
}
function redirectWrongWorkbenchRole(){const roles=new Set(S.me?.roles||[]);if(roles.has('FRANCHISE_OWNER')||roles.has('FRANCHISE_EMPLOYEE'))return false;if(roles.has('TELESALES'))location.replace('/h5/call/');else if(roles.has('SUPER_ADMIN')||roles.has('OPERATION'))location.replace('/h5/admin/');else renderLoadError('当前账号没有可用的业务角色，请联系管理员核对');return true}
async function api(path,opt={}){const h={...(opt.headers||{})};if(opt.body&&!(opt.body instanceof FormData))h['Content-Type']='application/json';const r=await fetch(API+path,{...opt,headers:h,credentials:'include'});const serverDate=Date.parse(r.headers.get('Date')||'');if(Number.isFinite(serverDate))S.serverTimeOffset=serverDate-Date.now();let j={};try{j=await r.json()}catch{}if(!r.ok||j.code!=='OK'){const error=new Error(j.message||'请求失败');error.code=j.code;throw error}return j.data}
function toast(msg,err=false){toastBox.textContent=msg;toastBox.className=`workbench-toast show ${err?'error':''}`;clearTimeout(toast.t);toast.t=setTimeout(()=>toastBox.className='workbench-toast',2200)}
let sheetIntent=0;
function beginSheetIntent(){return ++sheetIntent}
function closeSheet(owner=null,intent=null){if((owner&&!owner.isConnected)||(intent!==null&&intent!==sheetIntent))return false;beginSheetIntent();sheet.innerHTML='';return true}
function openSheet(title,html,bind,intent=null){if(intent===null)beginSheetIntent();else if(intent!==sheetIntent)return false;zsSetSafeHtml(sheet, `<div class="wb-overlay"><section class="wb-sheet"><div class="wb-sheet-head"><h2>${esc(title)}</h2><button class="wb-btn" id="sheet-close">关闭</button></div>${html}</section></div>`);document.querySelector('#sheet-close').onclick=()=>closeSheet();bind?.();return true}
function nav(){return franchiseTabs().map(([view,iconName,labelText])=>{const active=S.view===view||(view==='followups'&&S.view==='returns')||(view==='profile'&&S.view==='notifications');return `<button class="wb-nav ${active?'active':''}" data-nav="${view}"><span>${icon(iconName)}</span><span>${labelText}</span></button>`}).join('')}
async function logout(){try{await api('/auth/logout',{method:'POST'});location.replace('/h5/')}catch(error){toast(`退出失败：${error.message}`,true)}}
function shell(body){const tabs=franchiseTabs(),hasMessages=canView('notifications'),badgeCount=Number(S.unreadNotifications||0);zsSetSafeHtml(app, `<div class="workbench-shell"><header class="wb-header"><div class="wb-brand"><img class="wb-mark" src="./logo.png" alt="合家美宅"><div><strong>合家美宅</strong><small>客资管理平台</small></div></div><div class="wb-header-actions">${hasMessages?`<button class="wb-icon-btn wb-message-entry" data-go="notifications" aria-label="消息中心${badgeCount?`，${badgeCount} 条未读`:''}">${icon('bell')}${badgeCount?`<b class="wb-message-badge">${badgeCount>99?'99+':badgeCount}</b>`:''}</button>`:''}</div></header><main class="wb-main">${body}</main><nav class="wb-bottom" style="--wb-tabs:${tabs.length};grid-template-columns:repeat(${tabs.length},minmax(0,1fr))">${nav()}</nav></div>`);document.querySelectorAll('[data-nav]').forEach(b=>b.onclick=()=>go(b.dataset.nav));document.querySelectorAll('[data-go]').forEach(b=>b.onclick=()=>go(b.dataset.go,b.dataset.id||''));document.querySelectorAll('[data-scroll]').forEach(b=>b.onclick=()=>document.getElementById(b.dataset.scroll)?.scrollIntoView({behavior:'smooth',block:'start'}));document.querySelectorAll('[data-logout]').forEach(b=>b.onclick=logout)}
function go(view,id=''){if(!canView(view)){toast('当前账号暂未开通该栏目');view=defaultWorkbenchView();id=''}S.view=view;S.id=id;S.page=1;const u=new URL(location.href);u.searchParams.set('view',view);id?u.searchParams.set('id',id):u.searchParams.delete('id');history.replaceState(null,'',u);render()}
function item(title,status,body,actions='',statusLabel){return `<article class="wb-item"><div class="wb-item-top"><div><h3>${esc(title)}</h3>${body}</div>${badge(status,statusLabel)}</div>${actions?`<div class="wb-actions">${actions}</div>`:''}</article>`}
function metricCard(labelText,value,{view='',id='',scroll='',main=false}={}){const destination=view?`data-go="${esc(view)}"${id?` data-id="${esc(id)}"`:''}`:scroll?`data-scroll="${esc(scroll)}"`:'';return `<button type="button" class="wb-kpi${main?' main':''}" ${destination} aria-label="${esc(labelText)}：${esc(value??0)}，查看详情"><b>${esc(value??0)}</b><span>${esc(labelText)}</span><i aria-hidden="true">${icon('chevron-right')}</i></button>`}
function franchiseHomeGreeting(){const role=isFranchiseOwner()?'加盟商':'加盟商员工';const name=String(S.me?.display_name||'').trim()||role;return `<section class="wb-home-greeting"><div><p>${role}</p><h1>${esc(greetingName(name))}，上午好</h1></div></section>`}
function franchiseHomeHero({labelText,value,description,actionLabel,view}){return `<section class="wb-hero wb-home-priority franchiseHomeHero"><div><p>${esc(labelText)}</p><strong>${esc(value)}</strong><span>${esc(description)}</span></div><button class="wb-btn wb-home-priority-action" data-go="${esc(view)}">${esc(actionLabel)}</button></section>`}
function franchiseHomeMetrics(items){return `<section class="wb-home-metrics franchiseHomeMetrics" aria-label="业务概览">${items.map(({labelText,value,view})=>`<button type="button" class="wb-home-metric" data-go="${esc(view)}"><span>${esc(labelText)}</span><b>${esc(value)}</b></button>`).join('')}</section>`}
function homeAssignmentRow(row){const view=row.status==='PENDING_CLAIM'&&canView('assignments')?'assignments':'followups';const customer=row.customer_name||row.lead?.customer_name||'待处理客户';const place=[row.city||row.lead?.city,row.district||row.lead?.district].filter(Boolean).join(' · ')||'地区待补充';return `<button type="button" class="wb-home-task" data-go="${view}"><span class="wb-home-task-avatar">${esc(String(customer).slice(0,1))}</span><span class="wb-home-task-copy"><b>${esc(customer)}</b><small>${esc(place)} · ${esc(readableLabel(row.status))}</small></span>${badge(row.status)}<i aria-hidden="true">${icon('chevron-right')}</i></button>`}
function homeTaskList(rows,view,title='待处理客资'){if(!rows.length)return'';const tasks=rows.slice(0,3).map(homeAssignmentRow).join('');return `<section class="wb-home-section"><div class="wb-home-section-head"><h2>${esc(title)}</h2><button class="wb-home-link" data-go="${esc(view)}">查看全部</button></div><div class="wb-home-task-list">${tasks}</div></section>`}
function companyTodoList({waitingClaim,following,supplyRework,returnProcessing}){const todos=[['待领取客资',waitingClaim,'有客资等待领取','assignments'],['待补资料',supplyRework,'补充后重新提交','leads'],['待跟进',following,'继续联系客户','followups'],['退回处理中',returnProcessing,'等待处理结果','followups']].filter(([,count])=>Number(count)>0);if(!todos.length)return'';return `<section class="wb-home-section"><div class="wb-home-section-head"><h2>公司待办</h2></div><div class="wb-home-task-list">${todos.map(([name,count,description,view])=>`<button type="button" class="wb-home-task" data-go="${esc(view)}"><span class="wb-home-task-avatar">${icon(view==='assignments'?'hand-claim':view==='leads'?'plus':'clipboard-check')}</span><span class="wb-home-task-copy"><b>${esc(name)}</b><small>${esc(description)}</small></span><strong>${esc(count)} 条</strong><i aria-hidden="true">${icon('chevron-right')}</i></button>`).join('')}</div></section>`}
async function render(){beginSheetIntent();shell('<div class="wb-loading">加载中…</div>');try{if(S.view==='home')await home();else if(S.view==='profile')await profile();else if(S.view==='leads')await leadCenter();else if(S.view==='points')await points();else if(S.view==='assignments'||S.view==='followups')await assignments();else if(S.view==='returns')await returns();else if(S.view==='reports')await businessReport();else if(S.view==='rewards')await rewards();else await notifications()}catch(e){shell(`<div class="wb-error">${esc(e.message)}</div>`);toast(e.message,true)}}
async function home(){
  const companyId=S.me?.company_id;
  const accountRequest=can('points.own.read')&&companyId?api(`/points/accounts/${encodeURIComponent(companyId)}`):Promise.resolve(null);
  const assignmentsRequest=canReadAssignments()
    ?Promise.all(HOME_ASSIGNMENT_STATUSES.map(status=>api(`/v1.2/assignments?status=${status}&page=1&page_size=3`))).then(pages=>pages.flatMap(page=>page.items||[]))
    :Promise.resolve([]);
  const [d,account,assignmentRows]=await Promise.all([api('/v1.2/reports/own'),accountRequest,assignmentsRequest]);
  S.unreadNotifications=Number(d.unread_notifications||0);
  const received=d.received_assignments?.by_status||{};
  const returnsByStatus=d.returns?.by_status||{};
  const waitingClaim=Number(received.PENDING_CLAIM||0);
  const following=Number(received.CLAIMED||0)+Number(received.FOLLOWING||0);
  const returnProcessing=Number(returnsByStatus.VERIFYING||0)+Number(returnsByStatus.REVIEWING||0)+Number(returnsByStatus.NEED_MORE_EVIDENCE||0);
  const supplierLeadTotal=Number(d.supplier_leads?.total||0);
  const supplyRework=Number(d.supplier_leads?.by_status?.DRAFT||0)+Number(d.supplier_leads?.by_status?.INVALID||0);
  const followView='followups';
  const owner=isFranchiseOwner();
  const hero=owner
    ?{labelText:'可用积分',value:`${num(account?.available_for_dispatch??account?.balance)} 分`,description:'可用于领取客资',actionLabel:'查看积分',view:'points'}
    :{labelText:'今日待跟进',value:`${following} 条`,description:'本人待处理客资',actionLabel:'继续跟进',view:followView};
  const consumedPoints=Number(d.points?.consumed_points??d.finance?.consumed_points??0);
  const secondaryMetrics=owner?franchiseHomeMetrics([{labelText:'公司待跟进',value:following,view:followView},{labelText:'消耗积分',value:consumedPoints,view:'points'},{labelText:'退回处理中',value:returnProcessing,view:'returns'}]):franchiseHomeMetrics([{labelText:'待跟进',value:following,view:followView},{labelText:'供资进度',value:supplierLeadTotal,view:'leads'},{labelText:'退回处理中',value:returnProcessing,view:'returns'}]);
  const taskView=owner&&waitingClaim?'assignments':followView;
  const ownerTodos=owner?companyTodoList({waitingClaim,following,supplyRework,returnProcessing}):'';
  shell(`${franchiseHomeGreeting()}${franchiseHomeHero(hero)}${ownerTodos}${secondaryMetrics}${homeTaskList(assignmentRows,taskView,owner?'进行中的客资':'待处理客资')}`);
}
function profileIdentity(role){const name=String(S.me?.display_name||'当前用户').trim()||'当前用户';const initial=name.slice(0,1);return `<section class="wb-profile-summary"><span class="wb-profile-avatar" aria-hidden="true">${esc(initial)}</span><div><p>我的</p><h1>${esc(name)}</h1><span>${esc(S.me?.company_name||'所属加盟商')} · ${esc(role)}</span></div></section>`}
function profileSecurity(){const hasPassword=Boolean(S.me?.has_password);return `<section class="wb-card wb-account-security"><div class="wb-card-head"><h2>账户与安全</h2></div><div class="wb-account-actions"><button class="wb-account-action" id="profile-username" type="button"><i>${icon('user')}</i><span><b>登录账号</b><small>${esc(S.me?.username||'--')}</small></span><em>${icon('chevron-right')}</em></button><button class="wb-account-action" id="profile-password" type="button"><i>${icon('key-round')}</i><span><b>${hasPassword?'登录密码':'备用登录密码'}</b><small>${hasPassword?'修改密码':'公众号登录之外的备用方式'}</small></span><em>${icon('chevron-right')}</em></button></div></section>`}
function profileBusinessEntries(){const entries=[];if(canView('returns'))entries.push(`<button class="wb-account-action" data-go="returns" type="button"><i>${icon('rotate-ccw')}</i><span><b>退回记录</b><small>查看退回原因、证据和审核进度</small></span><em>${icon('chevron-right')}</em></button>`);if(canView('reports'))entries.push(`<button class="wb-account-action" data-go="reports" type="button"><i>${icon('chart-no-axes-combined')}</i><span><b>经营报表</b><small>${isFranchiseOwner()?'查看公司汇总数据':'仅查看本人业务数据'}</small></span><em>${icon('chevron-right')}</em></button>`);return entries.length?`<section class="wb-card wb-account-security"><div class="wb-card-head"><h2>业务记录</h2></div><div class="wb-account-actions">${entries.join('')}</div></section>`:''}
function profileLogout(){return `<section class="wb-card wb-profile-signout"><button class="wb-btn danger" id="wb-profile-logout" data-logout>${icon('log-out')}退出登录</button></section>`}
function bindProfileSecurity(){document.querySelector('#profile-username')?.addEventListener('click',changeProfileUsername);document.querySelector('#profile-password')?.addEventListener('click',changeProfilePassword)}
function changeProfileUsername(){const current=S.me?.username||'';openSheet('修改登录账号',`<form class="wb-form" id="profile-username-form"><div class="wb-field"><label for="profile-current-password">当前密码</label><input class="wb-input" id="profile-current-password" type="password" autocomplete="current-password" minlength="8" maxlength="128" required></div><div class="wb-field"><label for="profile-new-username">新登录账号</label><input class="wb-input" id="profile-new-username" value="${esc(current)}" autocomplete="username" minlength="2" maxlength="64" required></div><button class="wb-btn primary" type="submit" id="profile-username-submit">保存</button></form>`,()=>{const form=document.querySelector('#profile-username-form'),submit=document.querySelector('#profile-username-submit');form.onsubmit=async event=>{event.preventDefault();const current_password=document.querySelector('#profile-current-password').value,username=document.querySelector('#profile-new-username').value.trim();if(username.length<2){toast('登录账号至少 2 个字符',true);return}submit.disabled=true;try{await api('/auth/change-username',{method:'POST',body:JSON.stringify({current_password,username})})}catch(error){if(form.isConnected)submit.disabled=false;toast(error.message,true);return}toast('登录账号已更新');if(!form.isConnected)return;closeSheet(form);try{S.me=await api('/auth/me');await profile()}catch{toast('登录账号修改已成功，请刷新查看',true)}}})}
function changeProfilePassword(){const hasPassword=Boolean(S.me?.has_password);openSheet(hasPassword?'修改登录密码':'设置备用登录密码',`<form class="wb-form" id="profile-password-form">${hasPassword?'<div class="wb-field"><label for="profile-password-current">当前密码</label><input class="wb-input" id="profile-password-current" type="password" autocomplete="current-password" minlength="8" maxlength="128" required></div>':'<div class="wb-notice">此密码仅作为公众号登录之外的备用方式，首次设置不需要当前密码。</div>'}<div class="wb-field"><label for="profile-password-next">新密码</label><input class="wb-input" id="profile-password-next" type="password" autocomplete="new-password" minlength="8" maxlength="128" required></div><div class="wb-field"><label for="profile-password-confirm">确认新密码</label><input class="wb-input" id="profile-password-confirm" type="password" autocomplete="new-password" minlength="8" maxlength="128" required></div><button class="wb-btn primary" type="submit" id="profile-password-submit">保存</button></form>`,()=>{const form=document.querySelector('#profile-password-form'),submit=document.querySelector('#profile-password-submit');form.onsubmit=async event=>{event.preventDefault();const current_password=document.querySelector('#profile-password-current')?.value||null,new_password=document.querySelector('#profile-password-next').value,confirm_password=document.querySelector('#profile-password-confirm').value;if(new_password.length<8){toast('新密码至少 8 位',true);return}if(new_password!==confirm_password){toast('两次输入的新密码不一致',true);return}submit.disabled=true;try{await api('/auth/change-password',{method:'POST',body:JSON.stringify({current_password,new_password})})}catch(error){if(form.isConnected)submit.disabled=false;toast(error.message,true);return}toast(hasPassword?'密码已更新':'备用密码已设置');if(!form.isConnected)return;closeSheet(form);try{S.me=await api('/auth/me');await profile()}catch{toast('登录密码修改已成功，请刷新查看',true)}}})}
async function profile(){
  const identity=profileIdentity(isFranchiseOwner()?'加盟商负责人':'加盟商员工');
  const security=profileSecurity();
  const businessEntries=profileBusinessEntries();
  if(!can('company.profile.manage')){
    shell(identity+businessEntries+security+profileLogout());
    bindProfileSecurity();
    return;
  }
  const areas=await api('/v1.2/company/service-areas');
  const approvedAreas=areas.filter(item=>item.active&&item.review_status==='APPROVED').length;
  const areaCards=approvedAreas?areas.filter(item=>item.active&&item.review_status==='APPROVED').map(item=>'<article class="wb-item"><div class="wb-item-top"><h3>'+esc(item.region_name||'地区待补充')+'</h3>'+`${item.is_primary_city?'<span class="wb-region-tag">主要经营</span>':''}`+'</div></article>').join(''):'<div class="wb-empty">暂未配置经营区域</div>';
  shell(identity+businessEntries+security+`<section class="wb-card" id="service-areas"><div class="wb-card-head"><h2>经营区域</h2><span class="wb-region-count">${approvedAreas} 个</span></div><div class="wb-list">${areaCards}</div></section>`+profileLogout());
  bindProfileSecurity();
}
async function leadCenter(){await leads()}
function ledgerLabel(type){return {RECHARGE:'充值入账',CLAIM:'领取扣分',RETURN:'退回返分',REWARD:'奖励到账',ADJUST:'人工调整',REVERSAL:'冲正调整'}[type]||readableLabel(type,'积分变动')}
function monthDelta(rows){const now=new Date(),start=new Date(now.getFullYear(),now.getMonth(),1);return rows.filter(x=>new Date(x.created_at)>=start).reduce((sum,x)=>sum+Number(x.delta||0),0)}
async function points(){
  const companyId=S.me?.company_id;
  if(!companyId){shell('<div class="wb-error">无法读取当前公司信息</div>');return}
  const [account,ledgers,packages]=await Promise.all([api(`/points/accounts/${encodeURIComponent(companyId)}`),api(`/points/ledgers?company_id=${encodeURIComponent(companyId)}&page=1&page_size=50`),api('/points/packages')]);
  const rows=ledgers.items||[];
  const delta=monthDelta(rows);
  const assignmentMetricTarget=canView('assignments')?{view:'assignments'}:{scroll:'points-ledger'};
  const ledgerList=rows.slice(0,8).map(x=>`<article class="wb-item wb-ledger"><div class="wb-item-top"><div><h3>${esc(ledgerLabel(x.type))}</h3><p>${fmt(x.created_at)} · 余额 ${num(x.balance_after)} 分</p></div><b class="${Number(x.delta||0)>=0?'plus':'minus'}">${Number(x.delta||0)>=0?'+':''}${num(x.delta)} 分</b></div></article>`).join('');
  const packageList=(packages||[]).slice(0,3).map(p=>`<article class="wb-item"><div class="wb-item-top"><div><h3>${esc(packageName(p))}</h3><p>线下实收 ¥${num(Number(p.cash_amount_cents||0)/100)} · 到账 ${num(Number(p.base_points||0)+Number(p.bonus_points||0))} 分</p></div></div></article>`).join('');
  shell(`<section class="wb-hero wb-points-hero"><h1>积分</h1><div class="wb-kpis">${metricCard('当前余额',num(account.balance),{scroll:'points-ledger',main:true})}${metricCard('本月变化',`${delta>=0?'+':''}${num(delta)}`,{scroll:'points-ledger'})}${metricCard('可用于领取',num(account.available_for_dispatch),assignmentMetricTarget)}${metricCard('待领取占用',num(account.pending_claim_points),assignmentMetricTarget)}</div></section><div class="wb-profile-grid"><section class="wb-card" id="points-ledger"><div class="wb-card-head"><div><h2>积分流水</h2></div></div><div class="wb-list">${ledgerList||'<div class="wb-empty">暂无积分流水</div>'}</div></section><section class="wb-card" id="points-packages"><div class="wb-card-head"><h2>线下充值</h2></div><div class="wb-list wb-package-list">${packageList||'<div class="wb-empty">暂无可参考充值档位</div>'}</div>${can('supplier.reward.own.read')?'<div class="wb-actions"><button class="wb-btn" data-go="rewards">查看奖励积分</button></div>':''}</section></div>`);
}
const SUPPLY_SOURCES=[['供应商推荐','加盟商推荐'],['DOUYIN','抖音/信息流'],['WECHAT_VIDEO','视频号'],['XIAOHONGSHU','小红书'],['MANUAL','人工录入']];
const SUPPLY_CATEGORIES=[['OLD_RENOVATION','旧房改造'],['SELF_BUILD','农村自建房'],['INTERIOR','室内装修']];
const SUPPLY_STATUSES=['DRAFT','PENDING_REVIEW','PENDING_TELESALES_VERIFY','PENDING_OPERATION_DISPOSITION','PUBLIC_POOL','READY_DISPATCH','DUPLICATE','INVALID'];
const supplyState={cities:[],districts:[]};

function supplyProgress(lead){
  if(lead.status==='DRAFT')return lead.pending_reason==='PRE_DISPATCH_REWORK_REQUIRED'?lead.review_note||'请根据运营说明补正资料后重新提交。':'资料尚未提交，可以继续补充或删除草稿。';
  if(lead.status==='PENDING_REVIEW')return '平台正在审核资料，结果会在这里更新。';
  if(lead.status==='PENDING_TELESALES_VERIFY')return '已进入电销核实；核实通过后才能进入派送。';
  if(lead.status==='PENDING_OPERATION_DISPOSITION')return '电话核验已完成，运营正在决定后续处理。';
  if(lead.status==='PUBLIC_POOL')return '资料已通过审核，当地暂时没有其他可接收加盟商，平台会在新增覆盖后重新匹配。';
  if(lead.status==='DUPLICATE')return '平台正在核对重复信息，暂时无需再次提交。';
  if(lead.status==='INVALID')return lead.review_note||'请按平台说明修改后重新提交。';
  if(lead.status==='READY_DISPATCH')return '资料已通过审核，等待运营派发。';
  return '';
}

function supplyOptions(options,current,placeholder){
  return `<option value="">${esc(placeholder)}</option>${options.map(([value,label])=>`<option value="${esc(value)}" ${value===current?'selected':''}>${esc(label)}</option>`).join('')}`;
}

function supplyLeadActions(lead){
  if(lead.status==='DRAFT'){
    const editLabel=lead.pending_reason==='PRE_DISPATCH_REWORK_REQUIRED'?'根据运营说明补正':'继续填写';
    return `<button class="wb-btn primary" data-supply-edit="${esc(lead.id)}">${editLabel}</button><button class="wb-btn danger" data-supply-delete="${esc(lead.id)}">删除草稿</button>`;
  }
  if(lead.status==='INVALID'&&lead.review_status==='REJECTED')return `<button class="wb-btn primary" data-supply-revise="${esc(lead.id)}">修改后重新提交</button><button class="wb-btn" data-supply-detail="${esc(lead.id)}">查看说明</button>`;
  return `<button class="wb-btn" data-supply-detail="${esc(lead.id)}">查看进度</button>`;
}

async function loadSupplyCities(){
  if(!supplyState.cities.length){
    const tree=await api('/master-data/region-tree');
    supplyState.cities=(tree.provinces||[]).flatMap(province=>(province.cities||[]).map(city=>({...city,province_name:province.name,option_name:`${province.name} · ${city.name}`})));
  }
  return supplyState.cities;
}

async function loadSupplyDistricts(cityCode){
  const cities=await loadSupplyCities();
  const city=cities.find(item=>item.code===cityCode);
  supplyState.districts=(city?.districts||[]).map(district=>({...district,province_name:city.province_name,city_name:city.name,option_name:`${city.province_name} · ${city.name} · ${district.name}`}));
  return supplyState.districts;
}

async function loadSupplyTownships(districtCode){
  return districtCode
    ?await api(`/master-data/regions?parent_code=${encodeURIComponent(districtCode)}&level=TOWNSHIP`)
    :[];
}

function filterSupplyRegionOptions(select,items,value,emptyLabel){
  const keyword=String(value||'').trim().toLocaleLowerCase('zh-CN');
  const selectedCode=select.value;
  const matchedItems=(items||[]).filter(item=>{
    const label=String(item.option_name||item.name||'').toLocaleLowerCase('zh-CN');
    return !keyword||label.includes(keyword);
  });
  const selectedItem=(items||[]).find(item=>item.code===selectedCode);
  const options=selectedItem&&!matchedItems.some(item=>item.code===selectedCode)?[selectedItem,...matchedItems]:matchedItems;
  zsSetSafeHtml(select,`<option value="">${esc(emptyLabel)}</option>${options.map(item=>`<option value="${esc(item.code)}">${esc(item.option_name||item.name)}</option>`).join('')}`);
  if(selectedItem)select.value=selectedCode;
  const emptyState=document.querySelector(`#${select.id}-empty`);
  if(emptyState){emptyState.textContent=matchedItems.length===0?'未找到匹配地区，请更换关键词。':'';emptyState.hidden=matchedItems.length!==0;}
}

function bindSupplyRegionEmpty(searchInput){
  const emptyState=document.createElement('span');
  emptyState.id=`${searchInput.id.replace('-search','')}-empty`;
  emptyState.className='wb-region-empty';
  emptyState.setAttribute('role','status');
  emptyState.hidden=true;
  searchInput.insertAdjacentElement('afterend',emptyState);
}

function normalizeSupplyPhone(value){
  let digits=String(value||'').replace(/\D/g,'');
  if(digits.startsWith('86')&&digits.length===13)digits=digits.slice(2);
  return digits;
}

const supplyBudgetToWan=amountToWan;
const supplyBudgetFromWan=wanToAmount;

function supplyPayload(){
  const cityCode=document.querySelector('#supply-city')?.value||'';
  const districtCode=document.querySelector('#supply-district')?.value||'';
  const townshipCode=document.querySelector('#supply-township')?.value||'';
  const city=supplyState.cities.find(item=>item.code===cityCode);
  const district=supplyState.districts.find(item=>item.code===districtCode);
  return {
    customer_name:document.querySelector('#supply-name')?.value.trim()||'',
    phone:normalizeSupplyPhone(document.querySelector('#supply-phone')?.value||''),
    city:city?.name||'',
    district:district?.name||'',
    region_code:townshipCode||districtCode||cityCode,
    source_channel:document.querySelector('#supply-source')?.value||'',
    category_code:document.querySelector('#supply-category')?.value||'',
    need_summary:document.querySelector('#supply-need')?.value.trim()||'',
    budget_min:supplyBudgetFromWan(document.querySelector('#supply-budget-min')?.value),
    budget_max:supplyBudgetFromWan(document.querySelector('#supply-budget-max')?.value),
    consent_confirmed:Boolean(document.querySelector('#supply-consent')?.checked),
  };
}

function validateSupplyDraft(payload){
  const errors={};
  if(payload.phone&&!/^1\d{10}$/.test(payload.phone))errors.phone='请填写 11 位手机号';
  if(payload.budget_min!==null&&(Number.isNaN(payload.budget_min)||payload.budget_min<0))errors.budget_min='请输入有效的最低预算';
  if(payload.budget_max!==null&&(Number.isNaN(payload.budget_max)||payload.budget_max<0))errors.budget_max='请输入有效的最高预算';
  if(payload.budget_min!==null&&payload.budget_max!==null&&payload.budget_min>payload.budget_max)errors.budget_max='最高预算不能低于最低预算';
  return errors;
}

function validateSupplySubmission(payload){
  const errors=validateSupplyDraft(payload);
  if(!/^1\d{10}$/.test(payload.phone))errors.phone='请填写 11 位手机号';
  if(!payload.consent_confirmed)errors.consent_confirmed='请确认已获得客户授权';
  return errors;
}

function showSupplyErrors(errors){
  const entries=Object.values(errors||{}).filter(Boolean);
  const summary=document.querySelector('#supply-form-error');
  if(summary){summary.textContent=entries[0]||'';summary.hidden=!entries.length;}
  document.querySelectorAll('[data-supply-field]').forEach(field=>field.removeAttribute('aria-invalid'));
  const fields={customer_name:'#supply-name',phone:'#supply-phone',city:'#supply-city',need_summary:'#supply-need',budget_min:'#supply-budget-min',budget_max:'#supply-budget-max',consent_confirmed:'#supply-consent'};
  Object.keys(errors||{}).forEach(name=>document.querySelector(fields[name])?.setAttribute('aria-invalid','true'));
  const firstField=fields[Object.keys(errors||{})[0]];
  if(entries.length&&firstField)document.querySelector(firstField)?.focus({preventScroll:true});
}

function hasSupplyDraftContent(payload){
  return Boolean(payload.customer_name||payload.phone||payload.city||payload.district||payload.category_code||payload.need_summary||payload.budget_min!==null||payload.budget_max!==null||payload.consent_confirmed);
}

function clearSupplyIntent(){
  S.id='';
  const url=new URL(location.href);
  url.searchParams.delete('id');
  history.replaceState(null,'',url);
}

async function saveSupplyLead(item,submitAfter){
  const form=document.querySelector('#supply-form');
  if(!form||form.dataset.busy==='1')return;
  const payload=supplyPayload();
  const errors=submitAfter?validateSupplySubmission(payload):validateSupplyDraft(payload);
  if(Object.keys(errors).length){showSupplyErrors(errors);return;}
  if(!submitAfter&&!hasSupplyDraftContent(payload)){showSupplyErrors({form:'请至少填写一项内容，再保存草稿。'});return;}
  showSupplyErrors({});
  form.dataset.busy='1';
  form.querySelectorAll('button').forEach(button=>button.disabled=true);
  let saved;
  try{
    saved=item
      ?await api(`/v1.2/supplier/leads/${encodeURIComponent(item.id)}`,{method:'PATCH',body:JSON.stringify(payload)})
      :await api('/v1.2/supplier/leads',{method:'POST',body:JSON.stringify(payload)});
    if(submitAfter)await api(`/v1.2/supplier/leads/${encodeURIComponent(saved.id)}/submit`,{method:'POST'});
  }catch(error){
    toast(error.message||'保存失败，请稍后重试',true);
    if(form.isConnected){
      delete form.dataset.busy;
      form.querySelectorAll('button').forEach(button=>button.disabled=false);
    }
    return;
  }
  toast(submitAfter?'已提交，平台正在审核':'草稿已保存');
  if(form.isConnected){
    closeSheet(form);
    clearSupplyIntent();
    try{await leads()}catch{toast(`${submitAfter?'提交':'保存'}已成功，请刷新查看`,true)}
  }
}

function supplyIdentityView(){
  const companyName=String(S.me?.company_name||'').trim();
  const submitterName=String(S.me?.display_name||'').trim();
  const companyLabel=S.me?.company_id&&companyName?companyName:'未绑定有效加盟商，请联系平台管理员';
  const submitterLabel=submitterName||'姓名未配置，请联系平台管理员';
  const incomplete=!(S.me?.company_id&&companyName&&submitterName);
  return `<section class="wb-supply-identity" aria-readonly="true"><div><small>供资加盟商</small><b>${esc(companyLabel)}</b></div><div><small>实际录入人</small><b>${esc(submitterLabel)}</b></div>${incomplete?'<p>身份信息由当前登录账号确定，异常时请先联系平台管理员。</p>':''}</section>`;
}

async function openSupplyForm(item=null,intent=beginSheetIntent()){
  const cities=await loadSupplyCities();
  const selectedCity=cities.find(row=>row.name===item?.city||row.code===item?.region_code);
  const districts=await loadSupplyDistricts(selectedCity?.code||'');
  const selectedDistrict=districts.find(row=>row.name===item?.district||row.code===item?.region_code);
  const townships=await loadSupplyTownships(selectedDistrict?.code||'');
  const selectedTownship=townships.find(row=>row.code===item?.region_code);
  const title=item?'完善客资资料':'上传客资';
  const note=(item?.review_note?`<div class="wb-notice">平台修改说明：${esc(item.review_note)}</div>`:'')+supplyIdentityView();
  openSheet(title,`${note}<form class="wb-form wb-supply-form" id="supply-form" novalidate><div class="wb-form-error" id="supply-form-error" role="alert" hidden></div><section class="wb-supply-section"><h3>客户信息</h3><div class="wb-row"><div class="wb-field"><label for="supply-name">客户姓名</label><input class="wb-input" id="supply-name" data-supply-field maxlength="64" autocomplete="name" value="${esc(item?.customer_name==='未填写'?'':item?.customer_name||'')}"></div><div class="wb-field"><label for="supply-phone">客户手机号 *</label><input class="wb-input" id="supply-phone" data-supply-field inputmode="tel" maxlength="32" autocomplete="tel" placeholder="请输入 11 位手机号" value="${esc(item?.phone||'')}"></div></div><div class="wb-row"><div class="wb-field"><label for="supply-city">所在地城市</label><input class="wb-input wb-region-search" id="supply-city-search" type="search" inputmode="search" autocomplete="off" placeholder="搜索省份或城市" aria-controls="supply-city"><select class="wb-select" id="supply-city" data-supply-field><option value="">暂不确定，提交后由电销补充</option>${cities.map(row=>`<option value="${esc(row.code)}" ${selectedCity?.code===row.code?'selected':''}>${esc(row.option_name||row.name)}</option>`).join('')}</select></div><div class="wb-field"><label for="supply-district">所在地区县</label><input class="wb-input wb-region-search" id="supply-district-search" type="search" inputmode="search" autocomplete="off" placeholder="搜索区县" aria-controls="supply-district"><select class="wb-select" id="supply-district"><option value="">暂不确定 / 全市范围</option>${districts.map(row=>`<option value="${esc(row.code)}" ${selectedDistrict?.code===row.code?'selected':''}>${esc(row.name)}</option>`).join('')}</select></div></div><div class="wb-field"><label for="supply-township">所在地乡镇/街道</label><select class="wb-select" id="supply-township"><option value="">可选，精确到乡镇/街道</option>${townships.map(row=>`<option value="${esc(row.code)}" ${selectedTownship?.code===row.code?'selected':''}>${esc(row.name)}</option>`).join('')}</select></div></section><section class="wb-supply-section"><h3>客户需求</h3><div class="wb-row"><div class="wb-field"><label for="supply-source">获客来源</label><select class="wb-select" id="supply-source">${supplyOptions(SUPPLY_SOURCES,item?.source_channel||'供应商推荐','请选择获客来源')}</select></div><div class="wb-field"><label for="supply-category">需求类型</label><select class="wb-select" id="supply-category">${supplyOptions(SUPPLY_CATEGORIES,item?.category_code||'','请选择需求类型')}</select></div></div><div class="wb-field"><label for="supply-need">需求说明</label><textarea class="wb-textarea" id="supply-need" data-supply-field maxlength="2000" placeholder="可填写建房或装修地点、计划、时间等关键信息">${esc(item?.need_summary||'')}</textarea></div><div class="wb-row"><div class="wb-field"><label for="supply-budget-min">预算最低（万元）</label><input class="wb-input" id="supply-budget-min" data-supply-field type="number" min="0" step="0.1" inputmode="decimal" value="${esc(supplyBudgetToWan(item?.budget_min))}"></div><div class="wb-field"><label for="supply-budget-max">预算最高（万元）</label><input class="wb-input" id="supply-budget-max" data-supply-field type="number" min="0" step="0.1" inputmode="decimal" value="${esc(supplyBudgetToWan(item?.budget_max))}"></div></div></section><label class="wb-choice wb-supply-consent"><input type="checkbox" id="supply-consent" data-supply-field ${item?.consent_confirmed?'checked':''}><span><b>我确认已获得客户授权 *</b><small>客户知晓其联系方式和需求将用于业务对接。</small></span></label><div class="wb-actions"><button class="wb-btn" type="button" id="supply-save-draft">保存草稿</button><button class="wb-btn primary" type="button" id="supply-submit">提交审核</button></div></form>`,()=>{
    document.querySelector('#supply-form').onsubmit=event=>event.preventDefault();
    const citySelect=document.querySelector('#supply-city');
    const districtSelect=document.querySelector('#supply-district');
    const districtSearch=document.querySelector('#supply-district-search');
    const citySearch=document.querySelector('#supply-city-search');
    const townshipSelect=document.querySelector('#supply-township');
    bindSupplyRegionEmpty(citySearch);
    bindSupplyRegionEmpty(districtSearch);
    filterSupplyRegionOptions(citySelect,supplyState.cities,'','暂不确定，提交后由电销补充');
    filterSupplyRegionOptions(districtSelect,supplyState.districts,'','暂不确定 / 全市范围');
    document.querySelector('#supply-city-search').oninput=event=>filterSupplyRegionOptions(citySelect,supplyState.cities,event.target.value,'暂不确定，提交后由电销补充');
    districtSearch.oninput=event=>filterSupplyRegionOptions(districtSelect,supplyState.districts,event.target.value,'暂不确定 / 全市范围');
    citySelect.onchange=async event=>{
      const districts=await loadSupplyDistricts(event.target.value);
      if(!citySelect.isConnected)return;
      districtSelect.value='';
      townshipSelect.value='';
      zsSetSafeHtml(districtSelect,`<option value="">暂不确定 / 全市范围</option>${districts.map(row=>`<option value="${esc(row.code)}">${esc(row.option_name||row.name)}</option>`).join('')}`);
      districtSearch.value='';
      document.querySelector('#supply-district-empty').hidden=true;
      zsSetSafeHtml(townshipSelect,'<option value="">请先选择区县</option>');
    };
    districtSelect.onchange=async event=>{
      const districtCode=event.target.value;
      const townships=await loadSupplyTownships(districtCode);
      if(!districtSelect.isConnected||districtSelect.value!==districtCode)return;
      zsSetSafeHtml(townshipSelect,`<option value="">可选，精确到乡镇/街道</option>${townships.map(row=>`<option value="${esc(row.code)}">${esc(row.name)}</option>`).join('')}`);
    };
    document.querySelector('#supply-save-draft').onclick=()=>saveSupplyLead(item,false);
    document.querySelector('#supply-submit').onclick=()=>saveSupplyLead(item,true);
  },intent);
}

function launchSupplyForm(item=null){
  openSupplyForm(item).catch(error=>toast(error.message||'暂时无法打开客资表单',true));
}

async function editSupplyLead(id){
  const intent=beginSheetIntent();
  try{await openSupplyForm(await api(`/v1.2/supplier/leads/${encodeURIComponent(id)}`),intent);}catch(error){toast(error.message,true);}
}

async function reviseSupplyLead(id){
  const intent=beginSheetIntent();
  try{const lead=await api(`/v1.2/supplier/leads/${encodeURIComponent(id)}/revise`,{method:'POST'});await openSupplyForm(lead,intent);}catch(error){toast(error.message,true);}
}

function confirmSupplyLeadDeletion(id){
  openSheet('删除这份草稿？','<p class="wb-muted">删除后无法恢复，已提交审核的客资不会受到影响。</p><div class="wb-actions"><button class="wb-btn" id="cancel-supply-delete">保留草稿</button><button class="wb-btn danger" id="confirm-supply-delete">确认删除</button></div>',()=>{
    document.querySelector('#cancel-supply-delete').onclick=()=>closeSheet();
    document.querySelector('#confirm-supply-delete').onclick=async button=>{
      button.currentTarget.disabled=true;
      const owner=button.currentTarget;
      try{await api(`/v1.2/supplier/leads/${encodeURIComponent(id)}`,{method:'DELETE'})}catch(error){if(owner.isConnected)owner.disabled=false;toast(error.message,true);return}
      toast('草稿已删除');
      if(owner.isConnected){closeSheet(owner);try{await leads()}catch{toast('删除已成功，请刷新查看',true)}}
    };
  });
}

async function leads(){
  const status=S.supplyStatus||'';
  const query=new URLSearchParams({page:String(S.page),page_size:'20'});
  if(status)query.set('status',status);
  const [page,capabilities]=await Promise.all([api(`/v1.2/supplier/leads?${query}`),api('/v1.2/company/capabilities')]);
  const supplierCapability=(capabilities||[]).find(item=>item.capability_code==='LEAD_SUPPLIER');
  const canUpload=Boolean(supplierCapability?.active&&supplierCapability?.review_status==='APPROVED');
  const rows=page.items||[];
  const list=rows.map(lead=>item(lead.customer_name==='未填写'?'未填写姓名':lead.customer_name||'未填写姓名',lead.status,`<p>${esc(lead.phone_masked||'手机号待补充')} · ${esc(lead.city||'地区待补充')} ${esc(lead.district||'')}</p>${supplyProgress(lead)?`<p>${esc(supplyProgress(lead))}</p>`:''}`,supplyLeadActions(lead))).join('');
  const capabilityAction=canUpload?'<button class="wb-btn primary" id="supply-create">上传客资</button>':'';
  const statusFilter=`<label class="wb-filter-field">进度<select class="wb-select" id="supply-status"><option value="">全部</option>${SUPPLY_STATUSES.map(value=>`<option value="${value}" ${status===value?'selected':''}>${esc(readableLabel(value))}</option>`).join('')}</select></label>`;
  const totalPages=Math.max(1,Math.ceil(Number(page.total||0)/20));
  const pager=totalPages>1?`<div class="wb-pager"><button class="wb-btn" id="supply-prev" ${S.page<=1?'disabled':''}>上一页</button><span class="wb-muted">第 ${S.page} / ${totalPages} 页</span><button class="wb-btn" id="supply-next" ${S.page>=totalPages?'disabled':''}>下一页</button></div>`:'';
  const empty=canUpload?'<div class="wb-empty">暂无供资记录<br><button class="wb-btn primary" id="supply-empty-create">上传第一条客资</button></div>':'<div class="wb-empty wb-feature-unavailable"><h2>供资暂未开通</h2></div>';
  shell(`<section class="wb-page-head wb-supply-head"><div><h1>供资</h1>${rows.length?`<span>${num(page.total)} 条记录</span>`:''}</div>${capabilityAction}</section>${rows.length||status?`<section class="wb-filter">${statusFilter}</section>`:''}<div class="wb-list">${list||empty}</div>${pager}`);
  document.querySelector('#supply-create')?.addEventListener('click',()=>launchSupplyForm());
  document.querySelector('#supply-empty-create')?.addEventListener('click',()=>launchSupplyForm());
  document.querySelector('#supply-status')?.addEventListener('change',event=>{S.supplyStatus=event.target.value;S.page=1;leads();});
  document.querySelector('#supply-prev')?.addEventListener('click',()=>{S.page-=1;leads();});
  document.querySelector('#supply-next')?.addEventListener('click',()=>{S.page+=1;leads();});
  document.querySelectorAll('[data-supply-detail]').forEach(button=>button.onclick=()=>leadDetail(button.dataset.supplyDetail));
  document.querySelectorAll('[data-supply-edit]').forEach(button=>button.onclick=()=>editSupplyLead(button.dataset.supplyEdit));
  document.querySelectorAll('[data-supply-revise]').forEach(button=>button.onclick=()=>reviseSupplyLead(button.dataset.supplyRevise));
  document.querySelectorAll('[data-supply-delete]').forEach(button=>button.onclick=()=>confirmSupplyLeadDeletion(button.dataset.supplyDelete));
  if(S.id){
    const id=S.id;
    clearSupplyIntent();
    if(id==='supply'){
      if(canUpload)launchSupplyForm();
      else if(isFranchiseOwner())go('profile');
      else toast('供资暂未开通',true);
    }else leadDetail(id);
  }
}

async function leadDetail(id){
  const intent=beginSheetIntent();
  const lead=await api(`/v1.2/supplier/leads/${encodeURIComponent(id)}`);
  const fields=[['客户',lead.customer_name==='未填写'?'未填写':lead.customer_name],['手机号',lead.phone_masked],['所在地',`${lead.city||''} ${lead.district||''}`.trim()],['当前进度',readableLabel(lead.status)],['资料审核',readableLabel(lead.review_status)],['重复情况',lead.duplicate_status?readableLabel(lead.duplicate_status,'平台复核中'):null],['提交时间',fmt(lead.submitted_at)],['最后更新',fmt(lead.updated_at)]].filter(([,value])=>value);
  const actions=lead.status==='DRAFT'?`<button class="wb-btn primary" id="supply-detail-edit">继续填写</button><button class="wb-btn danger" id="supply-detail-delete">删除草稿</button>`:lead.status==='INVALID'&&lead.review_status==='REJECTED'?'<button class="wb-btn primary" id="supply-detail-revise">修改后重新提交</button>':'';
  openSheet('客资进度',`<div class="wb-detail-grid">${fields.map(([name,value])=>`<div class="wb-detail"><small>${esc(name)}</small><b>${esc(value)}</b></div>`).join('')}</div><div class="wb-card"><h3>客户需求</h3><p class="wb-muted">${esc(lead.need_summary||'尚未填写')}</p></div>${lead.review_note?`<div class="wb-notice">平台说明：${esc(lead.review_note)}</div>`:''}<div class="wb-actions">${actions}</div>`,()=>{
    document.querySelector('#supply-detail-edit')?.addEventListener('click',()=>launchSupplyForm(lead));
    document.querySelector('#supply-detail-delete')?.addEventListener('click',()=>confirmSupplyLeadDeletion(lead.id));
    document.querySelector('#supply-detail-revise')?.addEventListener('click',()=>reviseSupplyLead(lead.id));
  },intent);
}
function workbenchPager(pages){
  const totalPages=Math.max(1,...pages.map(page=>Math.ceil(Number(page.total||0)/20)));
  if(totalPages<=1)return '';
  return `<div class="wb-pager"><button class="wb-btn" id="records-prev" ${S.page<=1?'disabled':''}>上一页</button><span class="wb-muted">第 ${S.page} / ${totalPages} 页</span><button class="wb-btn" id="records-next" ${S.page>=totalPages?'disabled':''}>下一页</button></div>`;
}
function bindWorkbenchPager(load){
  let loading=false;
  const move=async delta=>{if(loading)return;loading=true;S.page+=delta;try{await load()}catch(error){S.page-=delta;toast(error.message,true)}finally{loading=false}};
  document.querySelector('#records-prev')?.addEventListener('click',()=>move(-1));
  document.querySelector('#records-next')?.addEventListener('click',()=>move(1));
}
async function assignments(){
  const companyId=S.me?.company_id;
  const followMode=S.view==='followups';
  const canManageInternal=followMode&&isFranchiseOwner()&&can('assignment.own.read')&&Boolean(companyId);
  const statuses=followMode?['CLAIMED','FOLLOWING','RETURN_PENDING','COMPLETED']:['PENDING_CLAIM'];
  const [pages,directory]=await Promise.all([
    Promise.all(statuses.map(status=>api(`/v1.2/assignments?status=${status}&page=${S.page}&page_size=20`))),
    canManageInternal?api(`/companies/${encodeURIComponent(companyId)}/account-directory`):Promise.resolve([]),
  ]);
  const rows=pages.flatMap(page=>page.items||[]);
  const employeeName=userId=>(directory||[]).find(user=>user.id===userId)?.display_name||'';
  const canCollaborate=status=>['CLAIMED','FOLLOWING','RETURN_PENDING'].includes(status);
  const list=rows.map(x=>{
    const currentAssignee=employeeName(x.internal_assignee_user_id);
    const collaboration=canManageInternal&&canCollaborate(x.status)?`<p>内部处理：${esc(currentAssignee||'负责人自己跟进')}</p>`:'';
    const manage=canManageInternal&&canCollaborate(x.status)?`<button class="wb-btn" data-internal-assignment="${x.id}">分配员工</button>`:'';
    const receiveConfirmation=x.receive_confirmation_status==='CONFIRMED'?'已确认':'待确认';
    const currentFollow=x.current_follow_status?readableLabel(x.current_follow_status):'暂无';
    return item(x.customer_name||x.lead?.customer_name||'客户',x.status,`<p>${esc(x.phone||x.phone_masked||'领取后查看')} · ${esc(x.city||x.lead?.city||'')}</p><p>接收确认：${esc(receiveConfirmation)} · 当前跟进：${esc(currentFollow)}</p><p>客资积分 ${x.points_price||0} · ${x.status==='PENDING_CLAIM'?deadlineNotice(x.expires_at,'领取截止'):deadlineNotice(x.appeal_deadline_at,'退回截止')}</p>${collaboration}`,`<button class="wb-btn" data-assignment="${x.id}">详情</button>${x.status==='PENDING_CLAIM'&&canClaimAssignment()?`<button class="wb-btn primary" data-claim="${x.id}" ${deadlineButtonAttributes(x.expires_at)}>领取</button><button class="wb-btn danger" data-refuse="${x.id}" ${deadlineButtonAttributes(x.expires_at)}>拒绝领取</button>`:''}${manage}`);
  }).join('');
  const title=followMode?'跟进':'接收';
  shell(`<section class="wb-page-head"><h1>${title}</h1></section><div class="wb-list">${list||`<div class="wb-empty">暂无${title==='接收'?'待领取':'待跟进'}客资</div>`}</div>${workbenchPager(pages)}`);
  bindWorkbenchPager(assignments);
  document.querySelectorAll('[data-assignment]').forEach(b=>b.onclick=()=>assignmentDetail(b.dataset.assignment));
  document.querySelectorAll('[data-claim]').forEach(b=>b.onclick=()=>claim(b.dataset.claim,b));
  document.querySelectorAll('[data-refuse]').forEach(b=>b.onclick=()=>refuseAssignment(b.dataset.refuse));
  document.querySelectorAll('[data-internal-assignment]').forEach(b=>b.onclick=()=>manageInternalAssignment(b.dataset.internalAssignment));
  if(S.id){const id=S.id;S.id='';assignmentDetail(id)}
}
async function assignmentDetail(id){const intent=beginSheetIntent();const [x,followups]=await Promise.all([api(`/v1.2/assignments/${id}`),api(`/followups/assignments/${id}`)]);const history=(followups||[]).map(row=>`<article class="wb-item"><div class="wb-item-top"><div><h3>${esc(readableLabel(row.status,'状态已更新'))}</h3><p>${esc(row.note||'无备注')}</p><p>记录时间 ${fmt(row.created_at)}${row.next_followup_at?` · 下次跟进 ${fmt(row.next_followup_at)}`:''}</p></div></div></article>`).join('');const currentFollow=x.current_follow_status||followups?.[0]?.status;const receiveConfirmation=x.receive_confirmation_status==='CONFIRMED'?`已确认${x.receive_confirmed_at?` · ${fmt(x.receive_confirmed_at)}`:''}`:'待确认';const correctionBlocked=x.lead_pending_reason==='CORRECTION_REVIEW_REQUIRED';const canFollow=!correctionBlocked&&['CLAIMED','FOLLOWING'].includes(x.status)&&can('followup.own.manage');openSheet('派发单详情',`${correctionBlocked?'<div class="wb-notice">客资信息已更正，当前接收资格需运营处理，处理前暂停确认接收和跟进。</div>':''}<div class="wb-detail-grid">${[['派发编号',recordCode(x.id,'PF')],['客户',x.customer_name],['电话',x.phone||x.phone_masked||'确认接收后查看'],['派发状态',readableLabel(x.status)],['客资状态',readableLabel(x.lead_status)],['接收确认',receiveConfirmation],['当前跟进',currentFollow?readableLabel(currentFollow):'暂无'],['客资积分',x.points_price],['派发时间',fmt(x.assigned_at)],['领取时间',fmt(x.claimed_at)],['领取截止',fmt(x.expires_at)],['退回截止',fmt(x.appeal_deadline_at)],['过期原因',x.release_reason?readableLabel(x.release_reason):'未过期']].map(([a,b])=>`<div class="wb-detail"><small>${a}</small><b>${esc(b||'--')}</b></div>`).join('')}</div><p class="wb-muted">${x.status==='PENDING_CLAIM'?deadlineNotice(x.expires_at,'领取截止'):deadlineNotice(x.appeal_deadline_at,x.status==='RETURN_PENDING'?'首次申诉截止':'领取后 48 小时退回截止')}</p><div class="wb-actions">${!correctionBlocked&&x.status==='PENDING_CLAIM'&&canClaimAssignment()?`<button class="wb-btn primary" id="sheet-claim" ${deadlineButtonAttributes(x.expires_at)}>确认接收</button><button class="wb-btn danger" id="sheet-refuse" ${deadlineButtonAttributes(x.expires_at)}>拒绝领取</button>`:''}${canFollow?`<button class="wb-btn primary" id="sheet-followup">新增跟进</button>`:''}${!correctionBlocked&&['CLAIMED','FOLLOWING','COMPLETED'].includes(x.status)&&can('return.own.manage')?`<button class="wb-btn danger" id="sheet-return" ${deadlineButtonAttributes(x.appeal_deadline_at)}>发起退回</button>`:''}</div><div class="wb-card"><h3>跟进历史</h3><div class="wb-list">${history||'<div class="wb-empty">暂无跟进记录</div>'}</div></div>`,()=>{document.querySelector('#sheet-claim')?.addEventListener('click',event=>claim(id,event.currentTarget));document.querySelector('#sheet-refuse')?.addEventListener('click',()=>refuseAssignment(id));document.querySelector('#sheet-followup')?.addEventListener('click',()=>followupDraft(id,x.appeal_deadline_at));document.querySelector('#sheet-return')?.addEventListener('click',()=>returnDraft(id,'',x.appeal_deadline_at))},intent)}
async function claim(id,owner){const intent=beginSheetIntent();try{await api(`/v1.2/assignments/${id}/claim`,{method:'POST'});toast('已确认接收');if(owner?.isConnected&&intent===sheetIntent){closeSheet(owner,intent);render()}}catch(e){toast(e.message,true)}}
function refuseAssignment(id){openSheet('拒绝领取',`<div class="wb-notice">拒绝后，这条客资会立即回到平台待派发池；该动作与领取后的“发起退回”分开记录和统计。</div><form class="wb-form" id="refuse-assignment-form"><div class="wb-field"><label>拒绝原因</label><textarea class="wb-textarea" name="reason" required minlength="2" maxlength="500" placeholder="请说明当前无法承接的原因"></textarea></div><button class="wb-btn danger" id="refuse-assignment-submit">确认拒绝领取</button></form>`,()=>{const form=document.querySelector('#refuse-assignment-form'),submit=document.querySelector('#refuse-assignment-submit');form.onsubmit=async event=>{event.preventDefault();const reason=String(new FormData(form).get('reason')||'').trim();if(reason.length<2){toast('请至少填写 2 个字的拒绝原因',true);return}submit.disabled=true;try{await api(`/v1.2/assignments/${encodeURIComponent(id)}/refuse`,{method:'POST',body:JSON.stringify({reason})});toast('已拒绝领取，客资已退回平台待派发池');if(form.isConnected){closeSheet(form);await render()}}catch(error){if(form.isConnected)submit.disabled=false;toast(error.message,true)}}})}
async function manageInternalAssignment(assignmentId){
  const intent=beginSheetIntent();
  const companyId=S.me?.company_id;
  if(!isFranchiseOwner()||!companyId){toast('仅加盟商负责人可分配员工',true);return}
  try{
    const [assignment,directory]=await Promise.all([
      api(`/v1.2/assignments/${encodeURIComponent(assignmentId)}`),
      api(`/companies/${encodeURIComponent(companyId)}/account-directory`),
    ]);
    const employees=(directory||[]).filter(user=>user.role_code==='FRANCHISE_EMPLOYEE'&&user.status==='ACTIVE');
    const currentEmployee=employees.find(user=>user.id===assignment.internal_assignee_user_id)?.id||'';
    const options=[`<option value="">负责人自己跟进</option>`,...employees.map(user=>`<option value="${esc(user.id)}" ${user.id===currentEmployee?'selected':''}>${esc(user.display_name||'未命名员工')}</option>`)].join('');
    openSheet('分配员工',`<div class="wb-notice">公司内部直接分配，无需运营审批。选择“负责人自己跟进”可收回该客资，系统会保留交接记录。</div><form class="wb-form" id="internal-assignment-form"><div class="wb-field"><label>处理人员</label><select class="wb-select" name="employee_user_id">${options}</select></div><div class="wb-field"><label>分配或回收原因</label><textarea class="wb-textarea" name="reason" required minlength="2" maxlength="500" placeholder="例如：转交负责该区域的销售跟进"></textarea></div><button class="wb-btn primary" id="internal-assignment-submit">保存分配</button></form>`,()=>{
      const form=document.querySelector('#internal-assignment-form'),submit=document.querySelector('#internal-assignment-submit');
      form.onsubmit=async event=>{
        event.preventDefault();
        const fields=new FormData(form),reason=String(fields.get('reason')||'').trim(),employeeUserId=String(fields.get('employee_user_id')||'').trim();
        if(reason.length<2){toast('请至少填写 2 个字的分配或回收原因',true);return}
        submit.disabled=true;
        try{
          await api(`/v1.2/assignments/${encodeURIComponent(assignmentId)}/internal-assignee`,{method:'POST',body:JSON.stringify({employee_user_id:employeeUserId||null,reason})});
          toast(employeeUserId?'已分配给员工':'已收回到负责人');
          if(form.isConnected){closeSheet(form);render()}
        }catch(error){if(form.isConnected)submit.disabled=false;toast(error.message,true)}
      };
    },intent);
  }catch(error){toast(error.message,true)}
}
function followupDraft(assignmentId,deadline=''){openSheet('新增跟进',`<form class="wb-form" id="followup-form"><div class="wb-field"><label>跟进状态</label><select class="wb-select" name="status"><option value="CONTACTED">已联系</option><option value="INTERESTED">有意向</option><option value="NOT_INTERESTED">无意向</option><option value="DEAL">${esc(readableLabel('DEAL'))}</option><option value="INVALID">无效客资</option><option value="UNCONTACTED">未联系</option></select><small class="wb-muted">无效客资必须进入正式退回申诉，不能只保存为跟进标签。</small></div><div class="wb-field"><label>跟进备注</label><textarea class="wb-textarea" name="note" maxlength="500" placeholder="填写沟通结果或后续安排"></textarea></div><div class="wb-field"><label>下次跟进时间</label><input class="wb-input" type="datetime-local" name="next_followup_at"><small class="wb-muted">选择方便再次联系客户的时间。</small></div><button class="wb-btn primary" id="followup-submit">保存跟进</button></form>`,()=>{const form=document.querySelector('#followup-form'),submitButton=document.querySelector('#followup-submit'),statusField=form.elements.status;const syncSubmitLabel=()=>{submitButton.textContent=statusField.value==='INVALID'?'下一步：发起退回':'保存跟进'};statusField.onchange=syncSubmitLabel;syncSubmitLabel();let submitting=false;form.onsubmit=async e=>{e.preventDefault();if(submitting)return;const fields=Object.fromEntries(new FormData(form));if(fields.status==='INVALID'){closeSheet(form);returnDraft(assignmentId,String(fields.note||'').trim(),deadline);return}submitting=true;submitButton.disabled=true;const nextFollowupAt=String(fields.next_followup_at||'').trim();let nextFollowupAtIso=null;if(nextFollowupAt){const parsed=new Date(nextFollowupAt);if(Number.isNaN(parsed.getTime())){submitting=false;submitButton.disabled=false;toast('下次跟进时间格式不正确',true);return}nextFollowupAtIso=new Date(nextFollowupAt).toISOString()}try{await api(`/followups/assignments/${assignmentId}`,{method:'POST',body:JSON.stringify({status:fields.status,note:String(fields.note||'').trim()||null,next_followup_at:nextFollowupAtIso})})}catch(err){if(form.isConnected){submitting=false;submitButton.disabled=false;if(err.code==='FOLLOWUP_INVALID_REQUIRES_RETURN'){closeSheet(form);returnDraft(assignmentId,String(fields.note||'').trim(),deadline);return}}toast(err.message,true);return}toast('跟进已保存');if(form.isConnected){try{await assignmentDetail(assignmentId)}catch{closeSheet(form);toast('跟进已保存，请刷新查看',true)}}}})}
function returnDraft(assignmentId,initialDescription='',deadline=''){openSheet('发起退回申诉',`<div class="wb-notice">退回申请须在成功领取后连续 48 小时内完成首次提交，周末及节假日照常计时。${deadline?`<br>${deadlineNotice(deadline,'退回截止')}`:''}</div><form class="wb-form" id="return-form"><div class="wb-field"><label>退回原因</label><select class="wb-select" name="reason_code"><option value="EMPTY_NUMBER">空号/停机</option><option value="OUT_OF_SERVICE_REGION">超出服务区域</option><option value="DUPLICATE_TO_RECEIVER">接收方重复客户</option><option value="NON_HOUSING_CONSULTATION">非建房咨询</option></select></div><div class="wb-field"><label>事实说明</label><textarea class="wb-textarea" name="description" required minlength="5" placeholder="请说明联系次数、沟通结果和申请退回的事实依据">${esc(initialDescription)}</textarea></div><button class="wb-btn primary" ${deadline?deadlineButtonAttributes(deadline):''}>下一步：上传证据</button></form>`,()=>{const form=document.querySelector('#return-form');form.onsubmit=async e=>{e.preventDefault();if(deadline&&!deadlineState(deadline).allowed){refreshDeadlineControls();toast('已超过领取后 48 小时首次退回期限',true);return}const f=new FormData(form);try{const x=await api(`/v1.2/returns/assignments/${assignmentId}/draft`,{method:'POST',body:JSON.stringify(Object.fromEntries(f))});if(form.isConnected){closeSheet(form);evidence(x.id,x.evidence_summary||{},x)}}catch(err){toast(err.message,true)}}})}
async function uploadEvidenceBatch(files,uploadFile,onUploaded){
  const succeeded=[];
  const failed=[];
  const results=[];
  let uploaded=0;
  for(const entry of files){
    try{
      await uploadFile(entry.file,entry.type);
      uploaded+=1;
      succeeded.push(entry);
      results.push({...entry,status:'SUCCESS'});
      onUploaded?.(entry.file,entry.type);
    }catch(error){const failedEntry={...entry,error};failed.push(failedEntry);results.push({...failedEntry,status:'FAILED'})}
  }
  return {uploaded,succeeded,failed,results};
}
function syncEvidenceSubmitButton(button,uploadedTypes,uploading,hasSelectedFiles=false){button.disabled=uploading||(uploadedTypes.size===0&&!hasSelectedFiles)}
function renderEvidenceFileResults(root,results){zsSetSafeHtml(root,(results||[]).map(item=>`<div class="wb-notice"><b>${esc(item.file.name)}</b><br>${item.status==='SUCCESS'?'上传成功':`上传失败：${esc(item.error?.message||'请检查文件后重试')}`}</div>`).join(''))}
function isReturnSubmissionConfirmed(request){return ['VERIFYING','REVIEWING'].includes(request?.status)&&Boolean(request.submitted_at&&request.verification_task_id)}
function evidence(returnId,summary={},request={}){
  const supplement=request.status==='NEED_MORE_EVIDENCE';
  const uploadedTypes=new Set();
  if(supplement&&Number(request.supplementary_evidence_count||0)>0)uploadedTypes.add('SUPPLEMENT');
  if(!supplement&&Number(summary.CHAT_SCREENSHOT||0)>0)uploadedTypes.add('CHAT_SCREENSHOT');
  if(!supplement&&Number(summary.CALL_RECORDING||0)>0)uploadedTypes.add('CALL_RECORDING');
  openSheet('提交退回申请',`${supplement?'<div class="wb-notice">请按平台要求新增证据；历史材料或相同内容重复上传不算本轮补证。</div>':`<p class="wb-muted">${deadlineNotice(request.appeal_deadline_at,'首次提交截止')}</p>`}<div class="wb-notice"><b>截图或录音任一类型满足即可。</b><br>选择文件后点击“提交退回申请”，系统会先上传材料，再正式提交；看到提交成功提示后才进入核验。</div><form class="wb-form" id="evidence-form"><div class="wb-field"><label>沟通截图</label><input class="wb-input" type="file" name="chat_screenshots" accept="image/jpeg,image/png,image/webp" multiple><small class="wb-muted">支持 JPG、PNG、WEBP，可选择多张。</small></div><div class="wb-field"><label>电话录音</label><input class="wb-input" type="file" name="call_recording" accept="audio/mpeg,audio/wav,audio/mp4,audio/aac"><small class="wb-muted">支持 MP3、WAV、M4A、AAC，最大 20MB。</small></div></form><p class="wb-muted" id="evidence-progress" role="status">${uploadedTypes.size>0?'已有证据，尚未提交；可直接点击“提交退回申请”。':'申请尚未提交，请至少选择一种证据。'}</p><div class="wb-form" id="evidence-file-results" aria-live="polite"></div><button class="wb-btn primary" id="submit-return" type="submit" form="evidence-form" style="margin-top:12px" ${uploadedTypes.size>0?'':'disabled'} ${!supplement?deadlineButtonAttributes(request.appeal_deadline_at):''}>提交退回申请</button><button class="wb-btn" id="submit-saved-evidence" type="button" hidden>仅提交已上传证据</button>`,()=>{
    const form=document.querySelector('#evidence-form');
    const submitButton=document.querySelector('#submit-return');
    const savedButton=document.querySelector('#submit-saved-evidence');
    const closeButton=document.querySelector('#sheet-close');
    const progress=document.querySelector('#evidence-progress');
    const fileResults=document.querySelector('#evidence-file-results');
    const uploadHistory=[];
    const savedFiles=new Set();
    let processing=false;
    let submissionUncertain=false;
    const selectedFiles=()=>{
      const screenshots=Array.from(form.elements.chat_screenshots.files||[]);
      const recording=form.elements.call_recording.files?.[0];
      return [...screenshots.map(file=>({file,type:'CHAT_SCREENSHOT'})),...(recording?[{file:recording,type:'CALL_RECORDING'}]:[])];
    };
    const syncControls=()=>{
      syncEvidenceSubmitButton(submitButton,uploadedTypes,processing,selectedFiles().length>0);
      savedButton.disabled=processing||uploadedTypes.size===0;
      form.elements.chat_screenshots.disabled=processing;
      form.elements.call_recording.disabled=processing;
      if(closeButton)closeButton.disabled=processing;
      if(!supplement)refreshDeadlineControls();
    };
    const returnApi=async(path,options={})=>{
      const controller=new AbortController();
      const timeout=setTimeout(()=>controller.abort(),120000);
      try{return await api(path,{...options,signal:controller.signal})}
      catch(error){if(controller.signal.aborted)throw new Error('请求超时，请重试或查看退回记录');throw error}
      finally{clearTimeout(timeout)}
    };
    const uploadFile=async(file,type)=>{const body=new FormData();body.append('file',file);body.append('evidence_type',type);await returnApi(`/v1.2/returns/${returnId}/evidence`,{method:'POST',body})};
    const finish=(message='退回申请已提交，等待平台处理')=>{toast(message);closeSheet();go('returns')};
    const recoverSubmission=async()=>{
      const latest=await returnApi(`/v1.2/returns/${returnId}`);
      if(!['VERIFYING','REVIEWING','NEED_MORE_EVIDENCE','APPROVED','REJECTED'].includes(latest.status)||!latest.submitted_at||!latest.verification_task_id)return false;
      // The previous round's task and timestamp do not prove this supplement was submitted.
      if(supplement&&latest.status==='NEED_MORE_EVIDENCE'&&(!request.verification_task_id||latest.verification_task_id===request.verification_task_id))return false;
      finish('申请状态已更新，请查看退回记录');
      return true;
    };
    const submit=async(onlySaved=false)=>{
      if(processing)return;
      const files=onlySaved?[]:selectedFiles().filter(entry=>!savedFiles.has(entry.file));
      if(uploadedTypes.size===0&&files.length===0&&savedFiles.size===0){toast('请至少选择沟通截图或电话录音',true);return}
      processing=true;
      savedButton.hidden=true;
      syncControls();
      let submitting=false;
      try{
        if(submissionUncertain&&await recoverSubmission())return;
        if(!supplement&&!deadlineState(request.appeal_deadline_at).allowed)throw new Error('已超过领取后 48 小时首次退回期限');
        submitButton.textContent='正在上传并提交…';
        progress.textContent='申请尚未提交，正在上传证据，请不要关闭页面…';
        const result=await uploadEvidenceBatch(files,uploadFile,(file,type)=>{
          savedFiles.add(file);
          if(!supplement)uploadedTypes.add(type);
          progress.textContent=`${file.name}上传成功，正在继续处理，申请尚未提交。`;
        });
        uploadHistory.push(...result.results);
        renderEvidenceFileResults(fileResults,uploadHistory);
        if(supplement){
          const latest=await returnApi(`/v1.2/returns/${returnId}`);
          // A previous submit may have committed even when its response was lost.
          if(isReturnSubmissionConfirmed(latest)){finish();return}
          uploadedTypes.clear();
          if(Number(latest.supplementary_evidence_count||0)>0)uploadedTypes.add('SUPPLEMENT');
        }
        if(result.failed.length>0){
          savedButton.hidden=uploadedTypes.size===0;
          const failures=result.failed.map(({file,error})=>`${file.name}：${error.message||'上传失败'}`).join('；');
          throw new Error(`${result.failed.length} 个文件上传失败（${failures}）。可重试失败文件${uploadedTypes.size>0?'，或选择“仅提交已上传证据”':''}`);
        }
        if(uploadedTypes.size===0)throw new Error('本轮尚无新增有效材料，请上传不同内容的证据');
        if(!supplement&&!deadlineState(request.appeal_deadline_at).allowed)throw new Error('已超过领取后 48 小时首次退回期限');
        submitting=true;
        submitButton.textContent='正在提交退回申请…';
        progress.textContent='证据已保存，正在正式提交退回申请…';
        const submitted=await returnApi(`/v1.2/returns/${returnId}/submit`,{method:'POST'});
        if(!isReturnSubmissionConfirmed(submitted))throw new Error('服务器尚未确认申请进入核验，请重试或查看退回记录');
        finish();
      }catch(error){
        if(submitting)submissionUncertain=true;
        progress.textContent=`${submissionUncertain?'未确认提交成功':'申请未提交'}：${error.message}。已上传的材料会保留。`;
        toast(progress.textContent,true);
      }finally{
        processing=false;
        submitButton.textContent='提交退回申请';
        syncControls();
      }
    };
    form.onchange=()=>{
      syncControls();
      if(!processing){
        const count=selectedFiles().length;
        progress.textContent=count?`已选择 ${count} 个文件，申请尚未提交；请点击“提交退回申请”。`:'申请尚未提交；请选择证据，或直接提交已保存的材料。';
      }
    };
    form.onsubmit=event=>{event.preventDefault();return submit()};
    savedButton.onclick=()=>submit(true);
    savedButton.hidden=true;
    syncControls();
  });
}
async function businessReport(){
  const periods=new Set(['day','week','month']);
  const period=periods.has(S.id)?S.id:'month';
  const d=await api(`/v1.2/reports/own?period=${encodeURIComponent(period)}`);
  const received=d.received_assignments||{};
  const receivedByStatus=received.by_status||{};
  const exceptions=d.exception_breakdown||{};
  const consumedPoints=d.points?.consumed_points??d.finance?.consumed_points??0;
  const claimed=d.statistics?.claimed??['CLAIMED','FOLLOWING','RETURN_PENDING','RETURNED','COMPLETED'].reduce((sum,status)=>sum+Number(receivedByStatus[status]||0),0);
  const scopeText=d.scope==='employee'||d.scope?.type==='employee'?'仅本人数据':'公司汇总数据';
  const periodOptions=[['day','近 1 天'],['week','近 7 天'],['month','近 30 天']];
  const metrics=[['供资总数',d.supplier_leads?.total||0],['分配客资',received.total||0],['已领取',claimed],['拒绝领取',exceptions.refused_claim??exceptions.refused_claims??0],['发起退回',exceptions.return_requested??exceptions.return_requests??0],['确认无效',exceptions.confirmed_invalid||0],['消耗积分',consumedPoints]];
  shell(`<section class="wb-page-head"><div><h1>经营报表</h1><span>${esc(scopeText)}</span></div><button class="wb-btn" data-go="profile">返回我的</button></section><section class="wb-filter"><label class="wb-filter-field">统计周期<select class="wb-select" id="business-report-period">${periodOptions.map(([value,text])=>`<option value="${value}" ${period===value?'selected':''}>${text}</option>`).join('')}</select></label></section><section class="wb-card"><div class="wb-card-head"><h2>业务数据</h2></div><div class="wb-detail-grid">${metrics.map(([name,value])=>`<div class="wb-detail"><small>${esc(name)}</small><b>${num(value)}</b></div>`).join('')}</div></section><div class="wb-notice">“拒绝领取”“发起退回”“确认无效”分别统计；积分口径显示领取客资实际消耗的积分。</div>`);
  document.querySelector('#business-report-period')?.addEventListener('change',event=>go('reports',event.target.value));
}
function returnStatusLabel(status){return status==='DRAFT'?'待提交':status==='REJECTED'?'退回未通过':readableLabel(status)}
async function returns(){const d=await api(`/v1.2/returns?page=${S.page}&page_size=20`);const list=(d.items||[]).map(x=>item(`${esc(x.customer_name||'待确认客户')} · ${readableLabel(x.reason_code,'其他原因')}`,x.status,`<p>${esc(x.phone_masked||'手机号待补充')} · ${esc([x.city,x.district].filter(Boolean).join(' / ')||'地区待补充')}</p><p>提交时间 ${x.submitted_at?fmt(x.submitted_at):'尚未提交'}</p><p>派发编号 ${esc(x.assignment_code||recordCode(x.assignment_id,'PF'))}</p>`,`<button class="wb-btn" data-return="${x.id}">查看进度</button>`,returnStatusLabel(x.status))).join('');shell(`<section class="wb-page-head"><h1>退回记录</h1><button class="wb-btn" data-go="profile">返回我的</button></section><div class="wb-list">${list||'<div class="wb-empty">暂无退回记录</div>'}</div>${workbenchPager([d])}`);bindWorkbenchPager(returns);document.querySelectorAll('[data-return]').forEach(b=>b.onclick=()=>returnDetail(b.dataset.return));if(S.id){const id=S.id;S.id='';returnDetail(id)}}
async function returnDetail(id){
  const intent=beginSheetIntent();
  const x=await api(`/v1.2/returns/${id}`),verification=x.verification||{};
  const canSupplement=['DRAFT','NEED_MORE_EVIDENCE'].includes(x.status);
  const continueAction=canSupplement?`<button class="wb-btn primary" data-return-evidence="${esc(x.id)}" ${x.status==='DRAFT'?deadlineButtonAttributes(x.appeal_deadline_at):''}>${x.status==='DRAFT'?'继续提交退回申请':'补充证据并重新提交'}</button>`:'';
  const deadlineHelp=x.status==='DRAFT'?`<p class="wb-muted">${deadlineNotice(x.appeal_deadline_at,'首次提交截止')}</p>`:x.status==='NEED_MORE_EVIDENCE'?'<p class="wb-muted">申请已在期限内提交，当前可按审核要求补证。</p>':'';
  openSheet('退回记录详情',`<div class="wb-detail-grid">${[['客户',x.customer_name],['所在地',[x.city,x.district].filter(Boolean).join(' ')],['退回编号',recordCode(x.id,'TH')],['派发编号',x.assignment_code||recordCode(x.assignment_id,'PF')],['处理状态',returnStatusLabel(x.status)],['提交时间',x.submitted_at?fmt(x.submitted_at):'尚未提交'],['退回原因',readableLabel(x.reason_code,'其他原因')],['电话核验',verification.status?readableLabel(verification.status):'待安排'],['核验结论',verification.conclusion?readableLabel(verification.conclusion):'尚未提交'],['申诉截止',fmt(x.appeal_deadline_at)],['最终结果',returnDecisionSummary(x)]].map(([a,b])=>`<div class="wb-detail"><small>${a}</small><b>${esc(b||'--')}</b></div>`).join('')}</div><div class="wb-card"><h3>申诉说明</h3><p class="wb-muted">${esc(x.description||'暂无说明')}</p></div>${deadlineHelp}${continueAction}`,()=>{
    document.querySelector('[data-return-evidence]')?.addEventListener('click',()=>evidence(x.id,x.evidence_summary||{},x));
  },intent);
}
function rewardExplanation(x){if(x.status==='OBSERVING')return `奖励正在确认中，预计结算时间为 ${fmt(x.reward_due_at)}。`;if(x.status==='FROZEN')return `奖励暂缓结算。${rewardReason(x.exception_reason)||'平台复核完成后会更新进度。'}`;if(x.status==='SETTLED')return `奖励已于 ${fmt(x.settled_at)} 结算到账。`;if(x.status==='WAITING_CLAIM')return '客资已被领取，等待领取人电话确认客资有效。';if(x.status==='CANCELLED')return `本次奖励已取消。${rewardReason(x.exception_reason)}`;if(x.status==='REVERSED')return `本次奖励已调整。${rewardReason(x.exception_reason)}`;return '奖励进度以当前页面显示为准。'}
const REWARD_FILTERS=new Set(['SETTLED','OBSERVING','FROZEN']);
async function rewards(){
  const status=REWARD_FILTERS.has(S.id)?S.id:'';
  const d=await api(`/v1.2/supplier-rewards?page=${S.page}&page_size=20${status?`&status=${encodeURIComponent(status)}`:''}`);
  const sum=d.summary||{};
  const list=(d.items||[]).map(x=>item(`${x.reward_points} 供客积分`,x.status,`<p>当前进度：${esc(readableLabel(x.status))}</p><p>预计结算：${fmt(x.reward_due_at)}</p>`,`<button class="wb-btn" data-reward="${x.id}">查看说明</button>`)).join('');
  const filterNotice=status?`<div class="wb-filter"><span class="wb-status warn">当前筛选：${esc(readableLabel(status))}</span><button class="wb-btn" data-go="rewards">查看全部</button></div>`:'';
  shell(`<section class="wb-hero"><h1>供客积分</h1><div class="wb-kpis">${metricCard('奖励笔数',sum.total_count||0,{view:'rewards'})}${metricCard('已结算积分',sum.settled_points||0,{view:'rewards',id:'SETTLED'})}${metricCard('确认中积分',sum.observing_points||0,{view:'rewards',id:'OBSERVING'})}${metricCard('暂缓积分',sum.frozen_points||0,{view:'rewards',id:'FROZEN'})}</div></section>${filterNotice}<div class="wb-list">${list||'<div class="wb-empty">暂无对应奖励记录。</div>'}</div>`);
  document.querySelectorAll('[data-reward]').forEach(b=>b.onclick=()=>rewardDetail(b.dataset.reward));
  if(S.id&&!status){const id=S.id;S.id='';rewardDetail(id)}
}
async function rewardDetail(id){
  const intent=beginSheetIntent();
  const x=await api(`/v1.2/supplier-rewards/${id}`),rule=x.rule_snapshot||{};
  const amountRule=rule.calculation_mode==='FIXED'?['固定供客积分',`${rule.fixed_points??x.reward_points} 积分/条`]:['原比例规则',`${(Number(rule.ratio_bps??x.reward_ratio_bps??0)/100).toFixed(2).replace(/\.00$/,'')}%`];
  openSheet('供客积分详情',`<div class="wb-detail-grid">${[['当前进度',readableLabel(x.status)],['供客积分',x.reward_points],['对应客资积分',x.claim_points],amountRule,['有效确认',fmt(x.observed_at)],['预计结算',fmt(x.reward_due_at)],['实际到账',fmt(x.settled_at)]].map(([name,value])=>`<div class="wb-detail"><small>${esc(name)}</small><b>${esc(value??'--')}</b></div>`).join('')}</div><div class="wb-card"><h3>结算说明</h3><p class="wb-muted">${esc(rewardExplanation(x))}</p></div><div class="wb-notice">供客积分在客资被领取并电话确认有效后，经过 3 个工作日且无待处理退回时结算给提供方。</div>`,null,intent);
}
async function notifications(){const d=await api(`/notifications?page=${S.page}&page_size=30`);S.unreadNotifications=Number(d.unread_total||0);const list=(d.items||[]).map(x=>`<article class="wb-item wb-notification ${x.read_at?'':'unread'}" data-msg="${x.id}" data-unread="${x.read_at?'false':'true'}" data-link="${esc(x.deep_link||'')}"><div class="wb-item-top"><div><h3>${esc(x.title)}</h3><p>${esc(x.body)}</p><p>${fmt(x.created_at)}</p></div>${badge(x.read_at?'READ':'UNREAD')}</div></article>`).join('');shell(`<section class="wb-page-head"><h1>消息</h1><button class="wb-btn" data-go="profile">返回我的</button></section><div class="wb-list">${list||'<div class="wb-empty">暂无消息</div>'}</div>`);document.querySelector('[data-go="profile"]')?.addEventListener('click',()=>go('profile'));document.querySelectorAll('[data-msg]').forEach(x=>x.onclick=async()=>{const wasUnread=x.dataset.unread==='true';try{await api(`/notifications/${x.dataset.msg}/read`,{method:'POST'});if(wasUnread)S.unreadNotifications=Math.max(0,S.unreadNotifications-1)}catch(error){toast(error.message,true);return}const deepLink=safeDeepLink(x.dataset.link);if(deepLink)location.href=deepLink;else render()})}
function renderLogin(message=''){
  zsSetSafeHtml(app, `<main class="wb-main"><section class="wb-hero"><h1>登录后继续</h1></section><section class="wb-card"><form class="wb-form" id="franchise-login-form">${message?`<div class="wb-notice">${esc(message)}</div>`:''}<div class="wb-field"><label for="franchise-username">登录账号</label><input class="wb-input" id="franchise-username" autocomplete="username" required></div><div class="wb-field"><label for="franchise-password">登录密码</label><input class="wb-input" id="franchise-password" type="password" autocomplete="current-password" required></div><button class="wb-btn primary" id="franchise-login-submit" type="submit">登录工作台</button></form></section></main>`);
  document.querySelector('#franchise-login-form').onsubmit=async event=>{
    event.preventDefault();
    const submit=document.querySelector('#franchise-login-submit');
    submit.disabled=true;
    try{
      await api('/auth/login',{method:'POST',body:JSON.stringify({username:document.querySelector('#franchise-username').value.trim(),password:document.querySelector('#franchise-password').value})});
      location.replace('/h5/');
    }catch(error){submit.disabled=false;toast(error.message,true)}
  };
}
function renderLoadError(message){zsSetSafeHtml(app,`<main class="wb-auth"><section class="wb-login"><h1>页面加载失败</h1><p>${esc(message||'暂时无法加载，请稍后重试')}</p><button class="wb-btn primary" id="workbench-retry" type="button">重新加载</button></section></main>`);document.querySelector('#workbench-retry').onclick=()=>location.reload()}
function renderInvalidLink(message='该页面链接已迁移或不完整，请从工作台首页重新进入。'){
  zsSetSafeHtml(app,`<main class="wb-auth"><section class="wb-login"><h1>链接已失效</h1><p>${esc(message)}</p><button class="wb-btn primary" id="return-workbench-home" type="button">返回首页</button></section></main>`);
  document.querySelector('#return-workbench-home').onclick=()=>go(defaultWorkbenchView());
}
async function boot(){
  try{
    S.me=await api('/auth/me');
    if(redirectWrongWorkbenchRole())return;
    const legacyLinkError=await resolveLegacyAssignmentMessageLink();
    const u=new URL(location.href);
    const fallbackView=defaultWorkbenchView();
    S.view=u.searchParams.get('view')||fallbackView;
    S.id=u.searchParams.get('id')||'';
    S.view=({lead:'leads',assignment:'assignments',return:'returns',reward:'rewards',notification:'notifications'}[S.view]||S.view);
    if(!VIEWS[S.view])return renderInvalidLink();
    if(S.view==='leads'&&S.id==='supply')S.id='';
    if(!canView(S.view))return renderInvalidLink('当前账号未开通该栏目，请联系管理员核对权限。');
    render();
    if(legacyLinkError)toast(legacyLinkError,true);
  }catch(error){
    if(['AUTH_REQUIRED','AUTH_INVALID'].includes(error.code))renderLogin(error.message||'请登录后继续');
    else renderLoadError(error.message);
  }
}
setInterval(refreshDeadlineControls,1000);
boot();
