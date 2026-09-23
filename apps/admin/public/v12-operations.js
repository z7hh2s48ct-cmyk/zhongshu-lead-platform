import { amountToWan, wanToAmount } from '/h5/business-units.js';

const API='/api/v1',app=document.querySelector('#app'),toastEl=document.querySelector('#toast'),modalRoot=document.querySelector('#modal-root');
const beijingToday=()=>new Date(Date.now()+8*60*60*1000).toISOString().slice(0,10);
const S={me:null,view:'overview',id:'',status:'',page:1,processedPage:1,processedCreatedFrom:beijingToday(),processedCreatedTo:beijingToday(),leadSource:'',leadSourceChannel:'',leadKeyword:'',sourceChannelOptions:null,tsKeyword:'',tsPhone:'',tsStatus:'',tsAssignee:'',tsConclusion:'',tsDueFrom:'',tsDueTo:'',tsSource:'',leadCreatedFrom:'',leadCreatedTo:'',leadStatusFilter:'',assignmentStatusFilter:'',leadAssignerId:'',leadSubmitterId:'',leadSupplierCompanyId:'',leadPendingReason:'',leadPhone:'',leadRegion:'',leadReceiverCompanyId:'',leadFilterOptions:null,operationUsers:null,platformLeads:[],supplierLeads:[],supplementHistoryPage:1,publicPoolKeyword:'',publicPoolPhone:'',publicPoolCreatedFrom:'',publicPoolCreatedTo:'',publicPoolSubmitterId:'',publicPoolCustomerSource:'',publicPoolSource:'',publicPoolCompleteness:'',publicPoolDuplicate:'',financeRewardPage:1,financeCompanyKeyword:'',financeCompanyStatus:'',financeCompanyPage:1,financeCompanyId:'',financeLedgerType:'',financePointKind:'',financeDays:30,financeRewardStatus:'',financeSource:'',pointFlowPeriod:'day',pointFlowAnchor:beijingToday(),pointFlowPage:1,platformCities:null,platformDistricts:[],companyKeyword:'',companyLifecycleStatus:'',companyPage:1,telesalesUsers:null,calendarMonth:'',unreadNotifications:0,accountNotifications:[],supplyTerminations:[],supplyTerminationPage:1,returnKeyword:''};
const P={overview:['首页','layout-dashboard',['*','dashboard.operation.read']],leads:['客资','user-check',['*','lead.manual.manage','lead.supplier.review']],supplements:['待补充','file-text',['*','lead.manual.manage']],closed:['已关闭客资','file-text',['*','lead.supplier.review']],publicPool:['公海池','file-text',['*','lead.manual.manage']],telesales:['电销','phone',['*','verification.read']],dispatch:['派发','hand-claim',['*','lead.dispatch']],companies:['加盟商','building',['*','company.profile.review','company.account.manage']],economics:['积分统计','wallet',['*','reward.read']],returns:['异常','rotate-ccw',['*','return.read']],finance:['资金','wallet',['*']],audit:['日志','search',['*','audit.read']],trace:['客资详情','file-text',['*','audit.read'],true],settings:['平台设置','settings',['*'],true],users:['内部账号','users',['*'],true],calendar:['工作日历','calendar',['*'],true],account:['账号中心','user',['*','dashboard.operation.read'],true]};
const ADMIN_VIEW_CONTRACT={SUPER_ADMIN:['overview','leads','supplements','closed','publicPool','companies','finance'],OPERATION:['overview','leads','supplements','closed','publicPool','telesales','dispatch','companies','economics']};
const ROLE_HOME_PRIORITY=['SUPER_ADMIN','OPERATION'];
const ADMIN_ROLE_HOME_CONTENT={
  SUPER_ADMIN:{title:'经营总览',subtitle:'聚焦客资流转、经营异常、加盟商状态、资金风险与完整审计。',cards:['客资总量','异常待办','加盟商账号','资金风险']},
  OPERATION:{title:'今日运营',subtitle:'聚焦待核实、待派发、待电销结论与退回终审。',cards:['待核实','待派发','待电销结论','待终审','加盟商待核验']},
};
const ROLE_IDENTITY_LABEL={SUPER_ADMIN:'系统管理员',OPERATION:'运营人员',TELESALES:'电销人员',FRANCHISE_OWNER:'加盟商',FRANCHISE_EMPLOYEE:'加盟商员工'};
const L={DRAFT:'待完善',PUBLIC_POOL:'待当地加盟商',IMPORTED:'待补信息',IMPORT_ERROR:'导入异常',DUPLICATE_REVIEW:'疑似重复',PENDING:'待审核',PENDING_REVIEW:'待初审',PENDING_TELESALES_VERIFY:'待电销核验',PENDING_OPERATION_DISPOSITION:'待运营处置',PRE_DISPATCH_REWORK_REQUIRED:'待运营补充',READY_DISPATCH:'待派发',PENDING_CLAIM:'待领取',WAITING_CLAIM:'等待48小时结算',CLAIMED:'已领取',SUBMITTED:'已提交',VERIFYING:'核验中',REVIEWING:'待终审',NEED_MORE_EVIDENCE:'待补证',APPROVED:'已通过',REJECTED:'已驳回',OBSERVING:'待自动结算',FROZEN:'已冻结',SETTLED:'已结算',CANCELLED:'已取消',REVERSED:'已撤销',ACTIVE:'已启用',DISABLED:'已停用',ASSIGNED:'待处理',IN_PROGRESS:'核验中',QUALIFIED:'信息合格',INFO_INCOMPLETE:'信息不全',UNVERIFIABLE:'无法核验',INVALID:'信息无效',CLEAR:'无重复',DUPLICATE:'疑似重复',OPERATION_ENTRY:'运营录入',FRANCHISE_SUPPLIED:'加盟商提供',PLATFORM_MANUAL:'平台录入',SUPPLIER_H5:'加盟商提交',FEISHU_IMPORT:'飞书导入',FEISHU_LEGACY:'飞书历史导入',EMPTY_NUMBER:'空号或停机',OUT_OF_SERVICE_REGION:'超出服务区域',DUPLICATE_TO_RECEIVER:'接收方重复客户',NON_HOUSING_CONSULTATION:'非建房装修咨询',CONNECTED:'已接通',NO_ANSWER:'无人接听',OUT_OF_SERVICE:'停机',WRONG_PERSON:'非本人',REFUSED:'拒接或拒访',OTHER:'其他',SUPPORT_RETURN:'支持退回',DOES_NOT_SUPPORT_RETURN:'不支持退回',INCONCLUSIVE:'信息不足',RECHARGE:'充值',ADJUST:'人工调整',REVERSE:'冲正'};
Object.assign(L,{FOLLOWING:'跟进中',RETURN_PENDING:'退回处理中',RETURNED:'已退回',RELEASED:'已释放',EXPIRED:'已过期',COMPLETED:'已完成',CLOSED:'已关闭',UNCONTACTED:'未联系',CONTACTED:'已联系',INTERESTED:'有意向',NOT_INTERESTED:'无意向',DEAL:'电话确认有效',INVALID:'无效'});
Object.assign(L,{NOT_APPLICABLE:'无需供资奖励',CUSTOMER:'客资积分',SUPPLY:'供客积分'});
Object.assign(L,{REQUESTED:'已申请终止',TERMINATION_REQUESTED:'已申请终止',CLEARING:'清算中',PENDING_REVIEW:'待审核',NEED_MORE:'待补充',APPROVED_PENDING_PAYMENT:'待线下付款',PAID_PENDING_WRITE_OFF:'待确认核销',PAYMENT_FAILED:'付款失败',SETTLEMENT_ERROR:'核销异常',TERMINATED:'已终止客资合作'});
const EVIDENCE_LABEL={CHAT_SCREENSHOT:'沟通截图',CALL_RECORDING:'通话录音'};
const AUDIT_ACTION_LABEL={AUTH_LOGIN:'登录账号',AUTH_LOGOUT:'退出账号',AUTH_USERNAME_CHANGE:'修改登录账号',AUTH_USERNAME_CHANGE_FAILED:'修改登录账号失败',FOLLOWUP_CREATE:'记录客户跟进',WECHAT_OAUTH_START_FAILED:'微信授权未完成',COMPANY_CREATE:'创建加盟商主体',COMPANY_SIMPLE_CREATE:'快速创建加盟商主体',COMPANY_UPDATE:'更新加盟商主体',COMPANY_WECHAT_UNBIND:'解绑负责人微信',COMPANY_TEST_MARK:'标记历史测试主体',COMPANY_TEST_DELETE:'历史测试主体清理',COMPANY_ACCOUNT_CREATE:'开通加盟商人员账号',COMPANY_ACCOUNT_ENABLE:'启用加盟商人员账号',COMPANY_ACCOUNT_DISABLE:'停用加盟商人员账号',COMPANY_ACCOUNT_PASSWORD_RESET:'重置加盟商人员账号密码',POINTS_RECHARGE:'加盟商积分充值',V12_COMPANY_CAPABILITY_REQUEST:'提交加盟商能力申请',V12_PLATFORM_LEAD_DRAFT_CREATE:'新建平台客资草稿',V12_PLATFORM_LEAD_DRAFT_UPDATE:'更新平台客资草稿',V12_PLATFORM_LEAD_SUBMIT:'提交平台客资',V12_SUPPLIER_LEAD_DRAFT_CREATE:'新建加盟商客资草稿',V12_SUPPLIER_LEAD_DRAFT_UPDATE:'更新加盟商客资草稿',V12_SUPPLIER_LEAD_SUBMIT:'提交加盟商客资',V12_SUPPLIER_LEAD_REVIEW:'处理加盟商客资',V12_PRE_DISPATCH_VERIFY_ASSIGN:'派发前置电销核验',V12_PRE_DISPATCH_VERIFY_START:'开始前置电销核验',V12_PRE_DISPATCH_DIAL_CLICK:'拨打前置核验电话',V12_PRE_DISPATCH_VERIFY_SUBMIT:'提交前置核验结论',V12_PRE_DISPATCH_DISPOSITION:'运营处置前置核验结论',V12_DEDUP_OVERRIDE:'确认客资不重复',V12_MANUAL_DISPATCH:'人工派发客资',V12_ASSIGNMENT_CLAIM:'领取客资',V12_RETURN_DRAFT_SAVE:'保存退回草稿',V12_RETURN_EVIDENCE_UPLOAD:'上传申诉证据',V12_RETURN_EVIDENCE_READ:'查看申诉证据',V12_RETURN_SUBMIT:'提交退回申诉',V12_RETURN_VERIFY_ASSIGN:'分配电话核验',V12_RETURN_VERIFY_CLAIM:'领取电话核验',V12_RETURN_VERIFY_DIAL:'拨打核验电话',V12_RETURN_VERIFY_SUBMIT:'提交电话核验',V12_RETURN_FINAL_REVIEW:'完成退回终审',V12_SUPPLIER_REWARD_RULE_CREATE:'新建奖励规则',V12_SUPPLIER_REWARD_RULE_PUBLISH:'发布奖励规则',V12_SUPPLIER_REWARD_SETTLE:'结算供客奖励',V12_SUPPLIER_REWARD_SETTLE_DUE:'批量结算到期奖励',V12_SUPPLIER_REWARD_REVERSE:'撤销供客奖励'};
Object.assign(AUDIT_ACTION_LABEL,{POINTS_ADJUST:'人工积分调账',POINTS_REVERSE:'人工积分冲正',POINTS_RECONCILE:'积分账目核对',NOTIFICATION_RETRY:'重新发送消息'});
Object.assign(AUDIT_ACTION_LABEL,{AUTH_BACKUP_PASSWORD_SET:'设置备用登录密码',AUTH_PASSWORD_CHANGE:'修改登录密码',AUTH_PASSWORD_CHANGE_FAILED:'修改密码失败'});
Object.assign(AUDIT_ACTION_LABEL,{INVITE_CREATE:'发起负责人绑定邀请',INVITE_REVOKE:'撤销负责人绑定邀请',V12_COMPANY_CAPABILITY_CONFIGURE:'配置加盟商客资功能'});
Object.assign(AUDIT_ACTION_LABEL,{COMPANY_ENABLE:'启用加盟商主体',COMPANY_DISABLE:'停用加盟商主体',COMPANY_DELETE:'历史测试主体清理'});
Object.assign(AUDIT_ACTION_LABEL,{V12_COMPANY_PROFILE_BULK_APPROVE:'完成加盟商资料整包审核',V12_COMPANY_CAPABILITY_REVIEW:'审核加盟商客资能力',V12_COMPANY_SERVICE_AREA_REVIEW:'审核加盟商服务区域',V12_COMPANY_SERVICE_AREAS_CONFIGURE:'配置加盟商服务区域',V12_PLATFORM_LEAD_FACT_CORRECTION:'更正客资关键信息',V12_LEAD_EXPORT_REQUESTED:'提交客资完整信息导出',V12_LEAD_EXPORT_DOWNLOADED:'下载客资完整信息导出'});
Object.assign(AUDIT_ACTION_LABEL,{LEAD_STAGING_UPDATE:'更新客资待处理信息',LEAD_DISPATCH:'派发客资',ASSIGNMENT_RELEASE:'释放派发单',RETURN_REVIEW:'完成退回审核',LEAD_DUPLICATE_DECISION:'处理重复客资',COMPANY_ACCOUNT_REQUEST_APPROVE:'通过加盟商账号申请',COMPANY_ACCOUNT_REQUEST_REJECT:'驳回加盟商账号申请',INVITE_CREATE:'创建加盟商邀请',INVITE_REVOKE:'撤销加盟商邀请',VERIFICATION_TASK_CREATE:'创建电销核验任务',VERIFICATION_TASK_ASSIGN:'分配电销核验任务',VERIFICATION_TASK_RECLAIM:'收回电销核验任务',V12_PLATFORM_LEAD_CORRECTION_OPEN:'发起客资更正',V12_PLATFORM_LEAD_FACT_CORRECTION:'完成客资更正',V12_PLATFORM_LEAD_CORRECTION_RECHECK:'重新检查更正异常',V12_PLATFORM_LEAD_CORRECTION_REDISPATCH:'解除原派发并重新入池',V12_COMPANY_PROFILE_BULK_APPROVE:'批量通过加盟商资料',V12_COMPANY_CAPABILITY_REVIEW:'审核加盟商能力',V12_COMPANY_SERVICE_AREA_REVIEW:'审核加盟商服务区域',V12_COMPANY_CAPABILITY_CONFIGURE:'配置加盟商能力',V12_COMPANY_SERVICE_AREAS_CONFIGURE:'配置加盟商服务区域'});
Object.assign(AUDIT_ACTION_LABEL,{V12_PUBLIC_POOL_LEAD_CREATE:'公海池新增客户',V12_PUBLIC_POOL_LEAD_UPDATE:'公海池更新客户',V12_PUBLIC_POOL_TRANSFER:'公海池转入派发池',V12_PUBLIC_POOL_TRANSFER_BLOCKED:'公海池转池被校验阻止',V12_PUBLIC_POOL_FEISHU_IMPORT:'飞书客户视图导入公海池'});
Object.assign(AUDIT_ACTION_LABEL,{V12_ASSIGNMENT_AUTO_CONFIRMED:'领取满48小时自动认定有效',V12_SUPPLY_TERMINATION_REQUEST:'申请终止客资合作',V12_SUPPLY_TERMINATION_REFRESH:'复检终止合作清算',V12_SUPPLY_TERMINATION_REVIEW:'审核终止客资合作',V12_SUPPLY_TERMINATION_PAYMENT_RECORDED:'登记终止合作付款',V12_SUPPLY_TERMINATION_CONFIRMED:'确认付款并核销积分',V12_SUPPLY_COOPERATION_REOPEN:'恢复客资合作'});
Object.assign(AUDIT_ACTION_LABEL,{V12_PLATFORM_LEAD_MISDISPATCH_REDISPATCH:'撤回错派并重新入池',V12_TEST_LEAD_PERMANENT_DELETE:'永久删除测试客资'});
Object.assign(AUDIT_ACTION_LABEL,{V12_OPERATION_LEAD_DELETE:'逻辑删除客资',V12_CLOSED_LEAD_REOPEN:'重新启用已关闭客资',V12_PRE_DISPATCH_REWORK_RESTORE:'恢复历史待补充客资',COMPANY_OWNER_CREDENTIALS_PROVISION:'补齐负责人登录账号'});
Object.assign(AUDIT_ACTION_LABEL,{V12_COMPANY_CLAIMED_LEADS_EXPORT:'导出已领取客资'});
Object.assign(AUDIT_ACTION_LABEL,{V12_SUPPLY_WITHDRAWAL_REQUEST:'提交供客积分提现申请',V12_SUPPLY_WITHDRAWAL_CANCEL:'取消提现申请',V12_SUPPLY_WITHDRAWAL_REVIEW:'审核提现申请',V12_SUPPLY_WITHDRAWAL_PAYMENT_RECORDED:'登记提现线下付款',V12_SUPPLY_WITHDRAWAL_PAYMENT_FAILED:'登记提现付款失败',V12_SUPPLY_WITHDRAWAL_CONFIRMED:'确认提现付款并核销',V12_SUPPLY_WITHDRAWAL_PAYMENT_CHANGE:'申请更换提现收款资料',V12_SUPPLY_WITHDRAWAL_PAYMENT_CHANGE_REVIEWED:'审核提现收款资料变更'});
const AUDIT_RESOURCE_LABEL={user:'账号',lead:'客资',assignment:'派发单',calendar_day:'工作日历',company:'加盟商公司',company_capability:'加盟商能力',company_lead_capability:'加盟商客资能力',company_service_area:'服务区域',company_service_area_v12:'服务区域',dictionary:'业务选项',followup:'跟进记录',invite:'加盟邀请',job:'系统任务',lead_price_rule:'客资积分规则',notification:'消息',outbox:'通知任务',points_account:'积分账户',points_ledger:'积分记录',points_package:'充值档位',rbac:'账号权限',return_evidence:'申诉证据',return_request:'退回申诉',supplier_lead_reward:'供客奖励',supplier_reward:'供客奖励',supplier_reward_batch:'奖励批次',supplier_reward_rule:'供客奖励规则',sync_batch:'客资导入批次',system_config:'规则配置',verification_task:'电话核验任务',verification_template:'电话核验内容',wechat_bind:'微信绑定'};
const EXCLUSION_REASON_LABEL={COMPANY_INACTIVE:'加盟商当前未启用',RECEIVER_CAPABILITY_REQUIRED:'尚未开通接收客资能力',SELF_SUPPLY_FORBIDDEN:'不能接收自己提交的客资',SERVICE_REGION_MISMATCH:'服务区域不匹配',DUPLICATE_TO_RECEIVER:'接收方已有相同客户',RETURNED_RECEIVER_EXCLUDED:'该公司曾领取后退回，默认不再次派发',POINTS_INSUFFICIENT:'可用积分不足'};
const NOTIFICATION_EVENT_LABEL={ASSIGNMENT_DISPATCHED:'客资派发提醒',INVITE_CREATED:'账号开通提醒',POINTS_RECHARGED:'积分到账提醒',V12_COMPANY_PROFILE_APPROVED:'加盟商审核结果',V12_SUPPLIER_LEAD_SUBMITTED:'客资提交提醒',V12_SUPPLIER_LEAD_REJECTED:'客资补正提醒'};
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const TECHNICAL_CODE=/^(?:[A-Z][A-Z0-9_]{2,}|[a-z][a-z0-9]*|[a-z0-9]+(?:[_-][a-z0-9]+)+)$/;
const readableLabel=(value,fallback='待确认')=>{const text=String(value??'').trim();if(!text)return fallback;return L[text]||(TECHNICAL_CODE.test(text)?fallback:text)};
const recordCode=(value,prefix='记录')=>{const text=String(value??'').replace(/-/g,'');return text?`${prefix}-${text.slice(-8).toUpperCase()}`:'--'};
const fmt=v=>v?new Date(v).toLocaleString('zh-CN'):'--',can=p=>(S.me?.permissions||[]).some(x=>x==='*'||x===p),label=v=>readableLabel(v);
const returnStatusLabel=status=>status==='DRAFT'?'待提交':label(status);
const verificationTaskLabel=task=>task?.status==='PENDING'&&!task?.assignee_user_id?'待分配':label(task?.status);
Object.assign(AUDIT_ACTION_LABEL,{V12_LEAD_POINTS_SETTINGS_UPDATE:'修改客资固定积分',V12_RETURN_DRAFT_EXPIRED:'退回草稿超时关闭'});
Object.assign(AUDIT_RESOURCE_LABEL,{lead_points_settings:'客资固定积分',supply_termination:'终止客资合作'});
const auditAction=v=>AUDIT_ACTION_LABEL[v]||readableLabel(v,'其他业务操作'),auditResource=v=>AUDIT_RESOURCE_LABEL[v]||readableLabel(v,'业务记录');
const notificationEventLabel=v=>NOTIFICATION_EVENT_LABEL[v]||'业务消息',notificationStatusLabel=v=>({FAILED:'发送未成功',DEAD:'发送已停止',MANUAL_ACTION_REQUIRED:'需要人工处理'}[v]||'等待处理');
const candidateReasons=reasons=>(reasons||[]).map(reason=>EXCLUSION_REASON_LABEL[reason]||readableLabel(reason,'其他条件暂不符合')).join('、')||'符合条件';
const icon=name=>window.ZSIconSystem?.svg?.(name)||'';
const badge=(v,text=label(v))=>`<span class="ops-status ${['APPROVED','SETTLED','CLAIMED'].includes(v)?'ok':['REJECTED','CANCELLED','REVERSED'].includes(v)?'bad':v==='REVIEWING'?'info':'warn'}">${esc(text)}</span>`;
const verificationTaskBadge=task=>`<span class="ops-status warn">${esc(verificationTaskLabel(task))}</span>`;
const qs=o=>{const p=new URLSearchParams;Object.entries(o).forEach(([k,v])=>v!==''&&v!=null&&p.set(k,v));return p.toString()?`?${p}`:''};
async function api(path,opt={}){const h={...(opt.headers||{})};if(opt.body&&!(opt.body instanceof FormData))h['Content-Type']='application/json';const r=await fetch(API+path,{...opt,headers:h,credentials:'include'});let j={};try{j=await r.json()}catch{}if(!r.ok||j.code!=='OK'){const error=new Error(j.message||'请求失败');error.code=j.code;error.status=r.status;throw error}return j.data}
function toast(m,e=false){toastEl.textContent=m;toastEl.className=`ops-toast show ${e?'error':''}`;clearTimeout(toast.t);toast.t=setTimeout(()=>toastEl.className='ops-toast',2400)}
let modalIntent=0;
let lockedModal=null;
function invalidateModalIntent(){return ++modalIntent}
function beginModalRequest(){const intent=invalidateModalIntent();return ()=>intent===modalIntent}
function modalOwnerIsCurrent(owner){return Boolean(owner?.isConnected)}
function lockModal(owner,cancel){const close=document.querySelector('#modal-close');lockedModal={owner,cancel,close};if(close)close.disabled=true;if(cancel)cancel.disabled=true}
function unlockModal(owner){if(lockedModal?.owner!==owner)return;for(const button of [lockedModal.close,lockedModal.cancel])if(button?.isConnected)button.disabled=false;lockedModal=null}
function closeModal(){if(modalOwnerIsCurrent(lockedModal?.owner))return false;invalidateModalIntent();modalRoot.innerHTML='';document.body.classList.remove('ops-modal-open');return true}
function closeModalFor(owner){return Boolean(owner?.isConnected)&&closeModal()}
function modal(title,body,bind){if(modalOwnerIsCurrent(lockedModal?.owner))return false;invalidateModalIntent();document.body.classList.add('ops-modal-open');zsSetSafeHtml(modalRoot, `<div class="ops-overlay"><section class="ops-modal"><div class="ops-modal-head"><h2>${esc(title)}</h2><button class="ops-btn" id="modal-close">关闭</button></div>${body}</section></div>`);document.querySelector('#modal-close').onclick=closeModal;bind?.();return true}
async function refreshAfterSuccess(refresh,message){try{await refresh()}catch(error){toast(`${message}，但页面刷新失败：${error.message}`,true)}}
function actionForm(options,onSubmit){
  const {title,message='',labelText='处理说明',value='',required=false,minLength=0,inputType='textarea',submitLabel='确认提交',danger=false,validate,lockOnSubmit=false}=options;
  const control=inputType==='number'
    ?`<input class="ops-input" id="action-value" type="number" value="${esc(value)}" inputmode="decimal">`
    :inputType==='text'
      ?`<input class="ops-input" id="action-value" type="text" value="${esc(value)}">`
    :`<textarea class="ops-textarea" id="action-value" placeholder="请填写${esc(labelText)}">${esc(value)}</textarea>`;
  modal(title,`<form class="ops-form" id="action-form">${message?`<div class="ops-notice">${esc(message)}</div>`:''}<div class="ops-field"><label for="action-value">${esc(labelText)}${required?' *':''}</label>${control}<small class="ops-muted" id="action-hint">${required?`至少填写 ${minLength||1} 个字符`:'可选填写，便于后续追溯'}</small></div><div class="ops-actions"><button type="button" class="ops-btn" id="action-cancel">取消</button><button class="ops-btn ${danger?'danger':'primary'}" id="action-submit">${esc(submitLabel)}</button></div></form>`,()=>{
    const form=document.querySelector('#action-form'),input=document.querySelector('#action-value'),submit=document.querySelector('#action-submit');
    document.querySelector('#action-cancel').onclick=closeModal;
    form.onsubmit=async event=>{
      event.preventDefault();
      const raw=input.value.trim();
      const validationMessage=(required&&raw.length<Math.max(1,minLength))?`请至少填写 ${Math.max(1,minLength)} 个字符`:validate?.(raw);
      if(validationMessage){toast(validationMessage,true);input.focus();return}
      submit.disabled=true;
      if(lockOnSubmit)lockModal(form,document.querySelector('#action-cancel'));
      try{if(await onSubmit(raw,()=>form.isConnected,()=>unlockModal(form))!==false){unlockModal(form);closeModalFor(form)}}catch(error){unlockModal(form);if(form.isConnected)submit.disabled=false;toast(error.message,true)}
    };
    input.focus();
  });
}
function allowed(meta){return meta[2].some(can)}
function primaryRole(){const roles=new Set(S.me?.roles||[]);return ROLE_HOME_PRIORITY.find(role=>roles.has(role))||''}
function roleAllowsView(view){
  const role=primaryRole();
  if(view==='account'||view==='trace')return Boolean(role);
  if(['settings','users','calendar'].includes(view))return role==='SUPER_ADMIN';
  if(['returns','audit'].includes(view))return role==='SUPER_ADMIN'||role==='OPERATION';
  return Boolean(ADMIN_VIEW_CONTRACT[role]?.includes(view));
}
function canOpenView(view){return Boolean(P[view]&&allowed(P[view])&&roleAllowsView(view))}
function nav(){return (ADMIN_VIEW_CONTRACT[primaryRole()]||[]).filter(canOpenView).map(k=>{const m=P[k];return `<button class="${S.view===k?'active':''}" data-view="${k}"><span>${icon(m[1])}</span><span>${m[0]}</span></button>`}).join('')}
function enhanceResponsiveTables(root=document){
  root.querySelectorAll('.ops-table').forEach(tableElement=>{
    const labels=Array.from(tableElement.querySelectorAll('thead th')).map(cell=>cell.textContent.trim());
    tableElement.querySelectorAll('tbody tr').forEach(row=>{
      Array.from(row.cells).forEach((cell,index)=>{
        if(cell.colSpan<=1)cell.dataset.label=labels[index]||'';
      });
    });
  });
}
function shell(body){
  const accountName=S.me?.username||'当前账号';
  const identity=ROLE_IDENTITY_LABEL[primaryRole()]||'平台人员';
  const unread=Number(S.unreadNotifications||0);
  const accountButton=`<button class="ops-account-card" data-account-center type="button" aria-label="账号与消息${unread?`，${unread} 条未读`:''}"><i>${icon('user')}${unread?`<b class="ops-message-badge">${unread}</b>`:''}</i><span><b>${esc(accountName)}</b><small>${esc(identity)}</small></span></button>`;
  zsSetSafeHtml(app, `<div class="ops-shell"><aside class="ops-side"><div class="ops-brand"><img class="ops-logo" src="./logo.png" alt="合家美宅"><div><strong>合家美宅</strong><small>客资管理平台</small></div></div><div class="ops-menu-label">业务工作台</div><nav class="ops-menu">${nav()}</nav><div class="ops-account-zone">${accountButton}</div></aside><section class="ops-main"><header class="ops-mobile-head"><div><strong>合家美宅</strong><small>${esc(identity)}</small></div><div class="ops-mobile-account">${accountButton}</div></header><main class="ops-content">${body}</main></section></div>`);
  enhanceResponsiveTables(app);
  document.querySelectorAll('[data-view]').forEach(button=>button.onclick=()=>go(button.dataset.view));
  document.querySelectorAll('[data-account-center]').forEach(button=>button.addEventListener('click',()=>go('account')));
}
function firstAllowedView(){return (ADMIN_VIEW_CONTRACT[primaryRole()]||[]).find(canOpenView)||''}
function syncRouteFromUrl({canonicalize=false}={}){
  const url=new URL(location.href);
  const requestedView=url.searchParams.get('view')||'overview';
  const nextView=canOpenView(requestedView)?requestedView:firstAllowedView();
  if(!nextView)return false;
  const leadViews=new Set(['leads','supplements','closed']);
  if(S.view!==nextView&&(leadViews.has(S.view)||leadViews.has(nextView)))resetLeadReportFilters(nextView);
  S.view=nextView;
  S.id=url.searchParams.get('id')||'';
  S.status=url.searchParams.get('status')||'';
  if(nextView!=='supplements')S.leadSource=url.searchParams.get('source')||'';
  S.page=1;
  if(canonicalize&&requestedView!==nextView){
    url.searchParams.set('view',nextView);
    url.searchParams.delete('id');
    history.replaceState(null,'',url);
  }
  return true;
}
function go(view,id=''){
  if(!canOpenView(view)||(S.view===view&&S.id===id&&!S.status))return;
  if(modalOwnerIsCurrent(lockedModal?.owner)){toast('操作处理中，请等待结果',true);return}
  invalidateModalIntent();
  const url=new URL(location.href);
  url.searchParams.set('view',view);
  id?url.searchParams.set('id',id):url.searchParams.delete('id');
  url.searchParams.delete('status');
  history.pushState(null,'',url);
  syncRouteFromUrl();
  render();
}
function table(head,rows){return `<div class="ops-table-wrap"><table class="ops-table"><thead><tr>${head.map(x=>`<th>${x}</th>`).join('')}</tr></thead><tbody>${rows.join('')||`<tr><td colspan="${head.length}" class="ops-empty">暂无数据</td></tr>`}</tbody></table></div>`}
const rowSequence=(pageData,index)=>((Number(pageData?.page||S.page||1)-1)*Number(pageData?.page_size||20))+index+1;
function pager(d){const pages=Math.max(1,Math.ceil((d.total||0)/(d.page_size||20)));return `<div class="ops-pager"><button class="ops-btn" id="prev" ${S.page<=1?'disabled':''}>上一页</button><span>${S.page}/${pages}，共 ${d.total||0} 条</span><button class="ops-btn" id="next" ${S.page>=pages?'disabled':''}>下一页</button></div>`}
function bindPager(d,fn){const pages=Math.max(1,Math.ceil((d.total||0)/(d.page_size||20)));document.querySelector('#prev')?.addEventListener('click',()=>{S.page--;fn()});document.querySelector('#next')?.addEventListener('click',()=>{S.page=Math.min(pages,S.page+1);fn()})}
function fileSize(bytes){const value=Number(bytes||0);if(value<1024)return `${value} B`;if(value<1024*1024)return `${(value/1024).toFixed(1)} KB`;return `${(value/1024/1024).toFixed(1)} MB`}
function remainingAppealTime(seconds){const total=Math.max(0,Number(seconds||0)),hours=Math.floor(total/3600),minutes=Math.floor(total%3600/60);return `${hours?`${hours}小时`:''}${minutes||!hours?`${minutes}分钟`:''}`}
function returnAppealTiming(item){if(item?.appeal_paused_at)return `48小时计时已暂停，剩余 ${remainingAppealTime(item.appeal_remaining_seconds)}`;if(item?.appeal_resumed_at)return `已恢复计时，截止 ${fmt(item.appeal_deadline_at)}，剩余 ${remainingAppealTime((new Date(item.appeal_deadline_at).getTime()-Date.now())/1000)}`;return item?.appeal_deadline_at?`截止 ${fmt(item.appeal_deadline_at)}`:'尚未开始48小时计时'}
function evidenceList(items){
  return (items||[]).map(item=>{
    const url=item.access_token?`${API}/v1.2/return-evidences/${encodeURIComponent(item.id)}/download?token=${encodeURIComponent(item.access_token)}`:'';
    const isImage=item.type==='CHAT_SCREENSHOT'||String(item.mime_type||'').startsWith('image/');
    const isAudio=item.type==='CALL_RECORDING'||String(item.mime_type||'').startsWith('audio/');
    const preview=!url?'':isImage?`<a class="ops-evidence-preview" target="_blank" rel="noopener" href="${esc(url)}"><img data-evidence-media src="${esc(url)}" alt="${esc(item.original_name||'沟通截图')}" loading="lazy"></a><a class="ops-btn" target="_blank" rel="noopener" href="${esc(url)}">在线查看截图</a>`:isAudio?`<audio data-evidence-media controls preload="metadata" src="${esc(url)}">当前浏览器不支持在线播放录音。</audio><a class="ops-btn" target="_blank" rel="noopener" href="${esc(url)}">在线播放录音</a>`:`<a class="ops-btn" target="_blank" rel="noopener" href="${esc(url)}">查看文件</a>`;
    return `<article class="ops-detail ops-evidence-card"><small>${esc(EVIDENCE_LABEL[item.type]||'申诉证据')}</small><b>${esc(item.original_name||'未命名文件')}</b><p>${esc(fileSize(item.file_size))} · ${fmt(item.created_at)} · 上传人 ${esc(item.uploaded_by_name||'未记录')}</p>${preview}<p class="ops-evidence-error" hidden>该证据暂时无法预览，可稍后重试或使用查看文件。</p></article>`;
  }).join('')||'<div class="ops-empty">暂无证据</div>';
}
function bindEvidencePreviewErrors(root=document){root.querySelectorAll('[data-evidence-media]').forEach(media=>media.addEventListener('error',()=>{const message=media.closest('.ops-evidence-card')?.querySelector('.ops-evidence-error');if(message)message.hidden=false}))}
function telesalesName(userId){if(!userId)return '未分配';const user=(S.telesalesUsers||[]).find(item=>item.id===userId);return user?.display_name||user?.username||'已分配'}
async function loadTelesalesUsers(){if(!S.telesalesUsers)S.telesalesUsers=await api('/admin-meta/telesales-users');return S.telesalesUsers}
function normalizedMobile(value){const digits=String(value||'').replace(/\D/g,'');return digits.startsWith('86')&&digits.length===13?digits.slice(2):digits}
function isValidMobile(value){return /^1\d{10}$/.test(normalizedMobile(value))}
async function render(){
  invalidateModalIntent();
  shell('<div class="ops-loading">加载中…</div>');
  const views={overview,leads:review,supplements,closed:closedLeads,publicPool,telesales,dispatch,companies,economics,returns,finance,audit,trace:fullTrace,settings,users:internalUsers,calendar,account};
  try{await (views[S.view]||overview)()}catch(error){shell(`<div class="ops-error">${esc(error.message)}</div>`);toast(error.message,true)}
}
const totalOf=values=>Object.values(values||{}).reduce((sum,value)=>sum+Number(value||0),0);
const countStatus=(items,statuses)=>items.filter(item=>statuses.includes(item.status)).length;
function roleMetricCards(cards){return `<div class="ops-grid ops-role-metrics">${cards.map(([name,value,iconName,href])=>{const content=`<i>${icon(iconName)}</i><small>${esc(name)}</small><b>${esc(value??0)}</b>`;return href?`<a class="ops-kpi" href="${href}">${content}</a>`:`<div class="ops-kpi">${content}</div>`}).join('')}</div>`}
function barChart(title,description,items){const max=Math.max(1,...items.map(([,value])=>Number(value||0)));return `<section class="ops-card ops-chart-card"><div class="ops-card-head"><div><h2>${esc(title)}</h2><p>${esc(description)}</p></div></div><div class="ops-bar-chart">${items.map(([name,value,view])=>`<button class="ops-bar-row" data-overview-view="${esc(view)}" type="button"><span>${esc(name)}</span><i><b style="width:${Math.max(3,Math.round(Number(value||0)/max*100))}%"></b></i><strong>${Number(value||0)}</strong></button>`).join('')}</div></section>`}
function roleHome(content,cards,body=''){shell(`<section class="ops-role-hero"><div><span>今日工作面</span><h2>${esc(content.title)}</h2><p>${esc(content.subtitle)}</p></div><div class="ops-role-mark">${icon('layout-dashboard')}</div></section>${roleMetricCards(cards)}${body}`)}
function decisionBars(title,description,items){return barChart(title,description,items.map(item=>[item.label||item.date,item.value??item.new_leads??0,item.view||'leads']))}
function distributionCard(title,items){return `<section class="ops-card ops-chart-card"><div class="ops-card-head"><div><h2>${esc(title)}</h2><p>按客资数量与有效完成率查看表现。</p></div></div><div class="ops-distribution-list">${(items||[]).map(item=>`<div><b>${esc(item.label||'未分类')}</b><span>${Number(item.leads||0)} 条 · 有效 ${Number(item.effective_rate||0)}%</span></div>`).join('')||'<p class="ops-muted">暂无可比较的数据</p>'}</div></section>`}
async function overview(){
  const role=primaryRole();
  if(role==='SUPER_ADMIN'){
    const dashboard=await api('/v1.2/reports/management-dashboard?days=30');
    const kpis=dashboard.kpis||{},pendingReward=kpis.pending_reward_settlement||{};
    const cards=[
      ['新增客资',kpis.new_leads||0,'user-check','?view=leads'],
      ['待核实',kpis.pending_verification||0,'phone','?view=telesales'],
      ['待派送',kpis.ready_dispatch||0,'hand-claim','?view=dispatch'],
      ['已领取',kpis.claimed||0,'hand-claim','?view=dispatch'],
      ['有效完成率',`${Number(kpis.effective_completion_rate||0)}%`,'chart-no-axes-combined','?view=leads'],
      ['异常退回数',kpis.returned_exceptions||0,'rotate-ccw','?view=returns'],
      ['待结算奖励',`${Number(pendingReward.points||0)} 分`,'wallet','?view=finance'],
    ];
    const trendItems=(dashboard.trend||[]).slice(-7).map(item=>({label:item.date?.slice(5)||'--',value:item.new_leads||0,view:'leads'}));
    const funnel=(dashboard.funnel||[]).map(item=>`<div class="ops-funnel-step"><small>${esc(item.label)}</small><b>${Number(item.value||0)}</b></div>`).join('');
    const exceptionRows=(dashboard.exceptions||[]).filter(item=>Number(item.count)>0).map(item=>`<button class="ops-action-row" data-overview-view="${esc(item.view)}"><span>${esc(item.label)}</span><b>${Number(item.count)} 条</b>${icon('chevron-right')}</button>`).join('')||'<div class="ops-empty">当前没有需要处理的异常待办</div>';
    const body=`${decisionBars('客资新增趋势','最近 7 天新增量；有效率在下方来源/地区/加盟商表现中展示。',trendItems)}<section class="ops-card ops-chart-card"><div class="ops-card-head"><div><h2>流转漏斗</h2><p>录入 &rarr; 核实 &rarr; 派送 &rarr; 领取 &rarr; 确认完成</p></div></div><div class="ops-funnel">${funnel}</div></section><div class="ops-dashboard-columns">${distributionCard('来源表现',dashboard.source_distribution)}${distributionCard('地区表现',dashboard.region_distribution)}${distributionCard('加盟商供资表现',dashboard.provider_distribution)}</div><section class="ops-card"><div class="ops-card-head"><div><h2>需要处理的异常</h2><p>只保留需要明确下一步的队列，不展示泛化治理提示。</p></div></div><div class="ops-action-list">${exceptionRows}</div></section>`;
    roleHome({title:'经营总览',subtitle:'用关键数据判断客资质量、流转效率与待处理风险。'},cards,body);
    document.querySelectorAll('[data-overview-view]').forEach(button=>button.onclick=()=>go(button.dataset.overviewView));
    return;
  }
  const [report,processed]=await Promise.all([
    api('/v1.2/reports/overview'),
    api(`/v1.2/operations/my-processed${qs({created_from:leadDateBoundary(S.processedCreatedFrom),created_to:processedDateEnd(S.processedCreatedTo),page:S.processedPage,page_size:8})}`),
  ]);
  const management=report.management||{};
  const verification=management.verification||{};
  const returnVerification=management.return_verification||{};
  const exceptions=management.exceptions||{};
  const cards=[
    ['待核实',(report.leads.by_status?.PENDING_REVIEW||0)+(report.leads.by_status?.PENDING_TELESALES_VERIFY||0),'user-check','?view=telesales'],
    ['电销处理中',(verification.pending||0)+(verification.in_progress||0),'phone','?view=telesales'],
    ['待运营处置',verification.awaiting_operation||0,'clipboard-check','?view=telesales'],
    ['退回核验中',(returnVerification.pending||0)+(returnVerification.in_progress||0),'rotate-ccw','?view=returns'],
    ['待派发',report.leads.by_status?.READY_DISPATCH||0,'hand-claim','?view=dispatch'],
    ['待退回终审',exceptions.return_final_review||0,'rotate-ccw','?view=returns'],
  ];
  const operationRows=[
    ['加盟商客资待核实',(report.leads.by_status?.PENDING_REVIEW||0)+(report.leads.by_status?.PENDING_TELESALES_VERIFY||0),'分配电销确认客户意向、资料与服务区域','telesales'],
    ['电销正在核验',(verification.pending||0)+(verification.in_progress||0),'电销只核实事实；运营可改派超时或异常任务','telesales'],
    ['电销结论待处置',verification.awaiting_operation||0,'电销提交事实结论后，由运营决定进入派发池、补充或关闭','telesales'],
    ['退回事实核验',(returnVerification.pending||0)+(returnVerification.in_progress||0),'退回核验独立于派发前电销任务，进入退回页分配或改派','returns'],
    ['待人工派发',report.leads.by_status?.READY_DISPATCH||0,'优先选择覆盖客资所在地且符合接收条件的加盟商','dispatch'],
    ['退回终审',exceptions.return_final_review||0,'核验结论只作为事实依据，最终退款与后续动作由运营决定','returns'],
    ['加盟商资料审核',exceptions.company_review||0,'能力与服务区域可一键通过；加盟商内部员工分配不在运营视图展示','companies'],
  ];
  const processedRows=(processed.items||[]).map(item=>`<tr><td>${fmt(item.created_at)}</td><td><b>${esc(auditAction(item.action))}</b></td><td>${esc(auditResource(item.resource_type))} · ${esc(recordCode(item.resource_id,'业务'))}</td><td>${esc(auditResult(item).text)}</td><td><button class="ops-btn" data-my-processed-detail="${esc(item.id)}">查看明细</button></td></tr>`);
  const processedTable=processedRows.length?table(['处理时间','业务动作','相关记录','结果','操作'],processedRows):'<div class="ops-empty">当前日期范围内暂无已处理记录</div>';
  const processedPages=Math.max(1,Math.ceil(Number(processed.total||0)/Number(processed.page_size||8)));
  const processedPager=`<div class="ops-pager"><button class="ops-btn" id="processed-prev" ${S.processedPage<=1?'disabled':''}>上一页</button><span>${S.processedPage}/${processedPages}，共 ${Number(processed.total||0)} 条</span><button class="ops-btn" id="processed-next" ${S.processedPage>=processedPages?'disabled':''}>下一页</button></div>`;
  const processedFilter=`<form class="ops-filter-row" id="processed-filter-form"><label>处理日期从 <input class="ops-input" id="processed-created-from" type="date" value="${esc(S.processedCreatedFrom)}" required></label><label>到 <input class="ops-input" id="processed-created-to" type="date" value="${esc(S.processedCreatedTo)}" required></label><button class="ops-btn primary" type="submit">查询</button><button class="ops-btn" id="processed-filter-today" type="button">回到今天</button></form>`;
  const body=`<section class="ops-card"><div class="ops-card-head"><div><h2>我的待处理</h2><p>按下一步责任人排列。电销不具备自主领取或决定后续处置的入口；加盟商内部员工分配仅由公司负责人处理。</p></div></div>${table(['待办事项','数量','下一步','操作'],operationRows.map(([name,count,description,view])=>`<tr><td><b>${esc(name)}</b></td><td>${esc(count)}</td><td>${esc(description)}</td><td><button class="ops-btn" data-overview-view="${view}">立即处理</button></td></tr>`))}</section><section class="ops-card"><div class="ops-card-head"><div><h2>我已处理</h2><p>仅统计当前运营账号在所选日期范围内完成的业务处置，与待处理队列分开。</p></div></div>${processedFilter}${processedTable}${processedPager}</section>`;
  roleHome(ADMIN_ROLE_HOME_CONTENT[role],cards,body);
  document.querySelectorAll('[data-overview-view]').forEach(button=>button.onclick=()=>go(button.dataset.overviewView));
  document.querySelectorAll('[data-my-processed-detail]').forEach(button=>button.onclick=()=>{
    const item=(processed.items||[]).find(candidate=>candidate.id===button.dataset.myProcessedDetail);
    if(item)auditDetail(item);
  });
  document.querySelector('#processed-filter-form').onsubmit=event=>{event.preventDefault();const createdFrom=document.querySelector('#processed-created-from').value,createdTo=document.querySelector('#processed-created-to').value;if(createdFrom>createdTo){toast('处理开始日期不能晚于结束日期',true);return}S.processedCreatedFrom=createdFrom;S.processedCreatedTo=createdTo;S.processedPage=1;overview()};
  document.querySelector('#processed-filter-today').onclick=()=>{const today=beijingToday();S.processedCreatedFrom=today;S.processedCreatedTo=today;S.processedPage=1;overview()};
  document.querySelector('#processed-prev')?.addEventListener('click',()=>{S.processedPage=Math.max(1,S.processedPage-1);overview()});
  document.querySelector('#processed-next')?.addEventListener('click',()=>{S.processedPage=Math.min(processedPages,S.processedPage+1);overview()});
}
const leadDateBoundary=(value,end=false)=>{if(!value)return undefined;const start=Date.parse(`${value}T00:00:00.000+08:00`);return new Date(start+(end?86400000:0)).toISOString()};
const processedDateEnd=value=>leadDateBoundary(value,true);
function resetLeadReportFilters(view=S.view){
  for(const key of ['leadCreatedFrom','leadCreatedTo','leadSource','leadSourceChannel','leadKeyword','leadSupplierCompanyId','leadSubmitterId','leadPhone','leadRegion','leadReceiverCompanyId','leadStatusFilter','assignmentStatusFilter','leadAssignerId'])S[key]='';
  S.leadPendingReason=view==='supplements'?'PRE_DISPATCH_REWORK_REQUIRED':'';
  S.leadStatusFilter=view==='closed'?'CLOSED':'';
  S.page=1;
}
function leadReportFilters(){return {created_from:leadDateBoundary(S.leadCreatedFrom),created_to:leadDateBoundary(S.leadCreatedTo,true),source_kind:S.leadSource||undefined,source_channel:S.leadSourceChannel||undefined,keyword:S.leadKeyword.trim()||undefined,supplier_company_id:S.leadSupplierCompanyId||undefined,submitter_user_id:S.leadSubmitterId||undefined,pending_reason:S.leadPendingReason||undefined,phone:S.leadPhone.trim()||undefined,region:S.leadRegion.trim()||undefined,receiver_company_id:S.leadReceiverCompanyId||undefined,lead_status:S.leadStatusFilter||undefined,assignment_status:S.assignmentStatusFilter||undefined,assigned_by_user_id:S.leadAssignerId||undefined}}
function publicPoolFilters(){return {created_from:leadDateBoundary(S.publicPoolCreatedFrom),created_to:leadDateBoundary(S.publicPoolCreatedTo,true),submitter_user_id:S.publicPoolSubmitterId||undefined,keyword:S.publicPoolKeyword||undefined,phone:S.publicPoolPhone?.trim()||undefined,customer_source:S.publicPoolCustomerSource||undefined,source_kind:S.publicPoolSource||undefined,completeness:S.publicPoolCompleteness||undefined,duplicate_status:S.publicPoolDuplicate||undefined}}
const leadFilterOptionName=(items,id)=>{const item=(items||[]).find(option=>option.id===id);return item?.name||item?.display_name||item?.username||id};
const exportFilterSummary=filters=>{if(filters.scope==='PUBLIC_POOL'){return [['创建开始',filters.created_from],['创建结束',filters.created_to],['录入人员',leadFilterOptionName(S.leadFilterOptions?.submitters,filters.submitter_user_id)],['关键词',filters.keyword],['完整手机号',filters.phone||filters.phone_hash?'已指定':''],['客户来源',filters.customer_source],['录入方式',filters.source_kind],['完整性',filters.completeness],['查重状态',filters.duplicate_status]].filter(([,value])=>value).map(([name,value])=>`${name}：${label(value)}`).join('；')||'全部公海池'}return [['创建开始',filters.created_from],['创建结束',filters.created_to],['来源',filters.source_kind],['来源渠道',filters.source_channel],['关键词',filters.keyword],['供资加盟商',leadFilterOptionName(S.leadFilterOptions?.supplier_companies,filters.supplier_company_id)],['录入人员',leadFilterOptionName(S.leadFilterOptions?.submitters,filters.submitter_user_id)],['待办事项',filters.pending_reason],['完整手机号',filters.phone||filters.phone_hash?'已指定':''],['地区',filters.region],['接收加盟商',leadFilterOptionName(S.leadFilterOptions?.receiver_companies,filters.receiver_company_id)],['客资状态',filters.lead_status],['派发状态',filters.assignment_status],['派发运营人员',leadFilterOptionName(S.leadFilterOptions?.assigners,filters.assigned_by_user_id)]].filter(([,value])=>value).map(([name,value])=>`${name}：${label(value)}`).join('；')||'全部客资'};
async function loadSourceChannelOptions(){
  try{
    const d=await api('/v1.2/source-channel/options');
    return (d.items||[]).filter(item=>item.enabled!==false).map(item=>[item.code,item.label]);
  }catch{
    return [['MANUAL','人工录入'],['DOUYIN','抖音/信息流'],['WECHAT_VIDEO','视频号'],['XIAOHONGSHU','小红书'],['LIVE','直播'],['AD','广告'],['OTHER','其他']];
  }
}
function sourceChannelName(code){
  if(!code)return '';
  const hit=(S.sourceChannelOptions||[]).find(item=>item[0]===code);
  return hit?hit[1]:code;
}
function publicPoolValidationText(item){const errors=Object.values(item.public_pool_validation_errors||{});if(errors.length)return errors.join('；');if(item.status==='DUPLICATE')return '手机号查重结论待处理';return '资料可提交复核'}
function publicPoolTelesalesBlockReason(item){
  if(!['PLATFORM_MANUAL','FEISHU_IMPORT'].includes(item.source_kind))return '加盟商提供的客资不走公海池直接电销入口';
  if(item.status!=='DRAFT')return '当前状态不可直接分配电销';
  if(item.public_pool_validation_errors?.phone)return item.public_pool_validation_errors.phone;
  if(item.duplicate_status&&item.duplicate_status!=='CLEAR')return '手机号查重结论尚未处理';
  return '';
}
function openPublicPoolImport(){
  modal('从飞书客户视图导入',`<form class="ops-form" id="public-pool-import-form"><div class="ops-notice">系统只读取已配置的“客户视图”。同一飞书记录不会重复建档，手机号会先标准化并检查系统内重复客资。</div><div class="ops-field"><label for="public-pool-import-target">整批目标</label><select class="ops-input" id="public-pool-import-target"><option value="PUBLIC_POOL">全部先保存到公海池</option><option value="DISPATCH_POOL">资料完整的进入派发池，其余留在公海池</option></select></div><div class="ops-actions"><button class="ops-btn" id="public-pool-import-cancel" type="button">取消</button><button class="ops-btn primary" id="public-pool-import-submit" type="submit">开始导入</button></div></form>`,()=>{
    document.querySelector('#public-pool-import-cancel').onclick=closeModal;
    const form=document.querySelector('#public-pool-import-form');
    form.onsubmit=async event=>{event.preventDefault();const button=document.querySelector('#public-pool-import-submit');button.disabled=true;try{const result=await api('/v1.2/public-pool/feishu/import',{method:'POST',body:JSON.stringify({target_pool:document.querySelector('#public-pool-import-target').value})});closeModalFor(form);toast(`导入完成：新增 ${result.created_count} 条，派发池 ${result.dispatch_pool_count} 条，公海池 ${result.public_pool_count} 条，跳过重复行 ${result.skipped_count} 条`);await refreshAfterSuccess(publicPool,'导入已完成')}catch(error){if(form.isConnected)button.disabled=false;toast(error.message,true)}};
  });
}
async function transferPublicPoolLead(item){
  const isCurrent=beginModalRequest();
  try{
    const result=await api(`/v1.2/public-pool/leads/${encodeURIComponent(item.id)}/transfer-to-dispatch`,{method:'POST'});
    if(result.transferred){toast('客户已进入派发池');await refreshAfterSuccess(publicPool,'客户已进入派发池');return}
    const errors=Object.values(result.validation_errors||{});
    const supplier=item.customer_source==='FRANCHISE_SUPPLIED';
    if(isCurrent())modal('暂不能进入派发池',`<div class="ops-notice">${errors.map(esc).join('；')||'请先处理手机号查重结论'}</div><div class="ops-actions"><button class="ops-btn primary" id="public-pool-edit-now">${supplier?'知道了':'立即补充资料'}</button></div>`,()=>document.querySelector('#public-pool-edit-now').onclick=()=>{closeModal();if(!supplier)openPlatformLeadForm(item)});
  }catch(error){toast(error.message,true)}
}
function overridePublicPoolDuplicate(item){
  actionForm({title:'确认非重复并转入派发池',message:'仅在人工核实确认为不同客户时使用；系统会记录操作人和核实说明。',labelText:'核实说明 *',submitLabel:'确认非重复并转入派发池',validate:raw=>raw.trim().length>=5?'':'核实说明至少填写 5 个字符'},async reason=>{
    await api(`/v1.2/admin/leads/${encodeURIComponent(item.id)}/dedup-override`,{method:'POST',body:JSON.stringify({reason:reason.trim()})});
    toast('已确认非重复，客户进入派发池');
    await publicPool();
  });
}
const canDeleteOperationLead=()=>can('lead.own.delete')&&primaryRole()==='OPERATION';
async function deleteOperationLead(lead,refresh){
  const isCurrent=beginModalRequest();
  try{
    const preview=await api(`/v1.2/operation/leads/${encodeURIComponent(lead.id)}/deletion-preview`);
    if(!isCurrent())return;
    if(!preview.deletable){modal('当前不能删除',`<div class="ops-notice">${(preview.blocker_messages||[]).map(esc).join('；')||'该客资当前不能删除'}</div>`,()=>{});return}
    const impact=preview.impact||{};
    actionForm({title:'删除客资',message:`将从业务列表隐藏“${lead.customer_name||'未填写客户'}”。派发 ${Number(impact.assignment_history||0)} 条、进行中核验 ${Number(impact.active_verification_tasks||0)} 条、进行中退回 ${Number(impact.active_return_requests||0)} 条及全部积分奖励历史仍保留并可审计。`,labelText:'删除原因',required:true,minLength:2,submitLabel:'确认删除',danger:true},async reason=>{await api(`/v1.2/operation/leads/${encodeURIComponent(lead.id)}`,{method:'DELETE',body:JSON.stringify({reason})});toast('客资已删除');await refreshAfterSuccess(refresh,'客资已删除')});
  }catch(error){toast(error.message,true)}
}
async function publicPool(){
  const [data,exportTasks,filterOptions]=await Promise.all([api(`/v1.2/public-pool/leads${qs({page:S.page,page_size:20,...publicPoolFilters()})}`),can('lead.phone.export')?api('/v1.2/reports/leads/exports?page=1&page_size=100'):Promise.resolve({items:[]}),S.leadFilterOptions?Promise.resolve(S.leadFilterOptions):api('/v1.2/reports/leads/filter-options')]);
  S.leadFilterOptions=filterOptions;
  const submitterOptions=filterOptions.submitters||[];
  const rows=(data.items||[]).map((item,index)=>{
    const actions=[`<button class="ops-btn" data-public-pool-detail="${esc(item.id)}">详情</button>`];
    if(item.status==='DRAFT'){
      actions.push(`<button class="ops-btn" data-public-pool-edit="${esc(item.id)}">编辑</button>`);
      actions.push(`<button class="ops-btn primary" data-public-pool-transfer="${esc(item.id)}">转入派发池</button>`);
      if(!publicPoolTelesalesBlockReason(item))actions.push(`<button class="ops-btn" data-public-pool-telesales="${esc(item.id)}">分配电销核验</button>`);
    }
    if(item.status==='PUBLIC_POOL')actions.push(`<button class="ops-btn primary" data-public-pool-transfer="${esc(item.id)}">重新匹配并转入派发池</button>`);
    if(item.status==='DUPLICATE'&&can('lead.dedup.override'))actions.push(`<button class="ops-btn gold" data-public-pool-override="${esc(item.id)}">确认非重复并转入派发池</button>`);
    if(canDeleteOperationLead())actions.push(`<button class="ops-btn danger" data-public-pool-delete="${esc(item.id)}">删除</button>`);
    return `<tr><td>${rowSequence(data,index)}</td><td><b>${esc(item.customer_name||'未填写')}</b><br><small>${esc(item.phone||item.phone_masked||'--')}</small></td><td>${badge(item.customer_source)}</td><td>${badge(item.source_kind)}</td><td>${esc(item.city||'待补充')} ${esc(item.district||'')}</td><td>${esc(item.source_display||label(item.source_channel)||'未填写')}</td><td>${esc(item.supplier_company_name||'--')}</td><td>${esc(publicPoolValidationText(item))}</td><td>${badge(item.duplicate_status||'CLEAR')}</td><td>${esc(item.submitter_name||'后台人员')}<br><small>${fmt(item.created_at)}</small></td><td>${actions.join(' ')}</td></tr>`;
  });
  const inlineForm=`<section class="ops-card" id="public-pool-inline-card" hidden><div class="ops-card-head"><div><h2>直接添加一行</h2><p>先保存基本联系方式，其他资料可以随后继续补充。</p></div></div><form class="ops-filter" id="public-pool-inline-form"><input class="ops-input" id="public-pool-inline-name" maxlength="64" placeholder="客户姓名"><input class="ops-input" id="public-pool-inline-phone" maxlength="32" inputmode="tel" placeholder="联系电话"><input class="ops-input" id="public-pool-inline-source" maxlength="128" placeholder="具体来源"><button class="ops-btn primary" type="submit">保存到公海池</button><button class="ops-btn" id="public-pool-inline-cancel" type="button">取消</button></form></section>`;
  const publicPoolExportRows=(exportTasks.items||[]).filter(task=>task.filters?.scope==='PUBLIC_POOL').map(task=>{const expired=task.expires_at&&new Date(task.expires_at)<=new Date();const action=task.status==='COMPLETED'&&!expired?`<a class="ops-btn" href="${API}/v1.2/reports/leads/exports/${encodeURIComponent(task.id)}/download" download>下载 Excel</a>`:expired?'文件已过期':task.status==='FAILED'?esc(task.error_message||'导出失败'):'后台处理中';return `<tr><td>${fmt(task.created_at)}</td><td>${esc(exportFilterSummary(task.filters||{}))}</td><td>${badge(task.status)}</td><td>${Number(task.row_count||0)} 条</td><td>${action}</td></tr>`});
  const publicPoolExportPanel=can('lead.phone.export')?`<section class="ops-card"><div class="ops-card-head"><div><h2>公海池导出任务</h2><p>完整手机号只进入授权导出文件，所有筛选条件、请求人和下载动作均保留审计。</p></div><button class="ops-btn" id="public-pool-export-refresh">刷新状态</button></div>${table(['提交时间','筛选条件','状态','客资数','文件'],publicPoolExportRows)}</section>`:'';
  shell(`<section class="ops-card"><div class="ops-card-head"><div><h2>公海池</h2><p>运营及管理人员共享维护；资料待补、查重待处理或当地暂无接收加盟商的客户先留在这里，转入派发池时系统会重新校验。</p></div><div class="ops-actions"><button class="ops-btn primary" id="public-pool-new">新增客户</button><button class="ops-btn" id="public-pool-inline">直接添加一行</button><button class="ops-btn gold" id="public-pool-feishu">从飞书客户视图导入</button>${can('lead.phone.export')?'<button class="ops-btn" id="public-pool-export-filtered">导出当前筛选</button><button class="ops-btn" id="public-pool-export-all">导出全部公海池</button>':''}</div></div><form class="ops-filter" id="public-pool-filter"><label>创建日期从 <input class="ops-input" id="public-pool-created-from" type="date" value="${esc(S.publicPoolCreatedFrom)}"></label><label>到 <input class="ops-input" id="public-pool-created-to" type="date" value="${esc(S.publicPoolCreatedTo)}"></label><select class="ops-input" id="public-pool-submitter"><option value="">全部录入人员</option>${submitterOptions.map(user=>`<option value="${esc(user.id)}" ${S.publicPoolSubmitterId===user.id?'selected':''}>${esc(user.name||'未命名人员')}</option>`).join('')}</select><input class="ops-input" id="public-pool-phone" inputmode="tel" autocomplete="off" maxlength="32" value="${esc(S.publicPoolPhone)}" placeholder="完整手机号"><input class="ops-input" id="public-pool-keyword" value="${esc(S.publicPoolKeyword)}" placeholder="客户、地区或具体来源"><select class="ops-input" id="public-pool-customer-source"><option value="">全部客户来源</option><option value="OPERATION_ENTRY" ${S.publicPoolCustomerSource==='OPERATION_ENTRY'?'selected':''}>运营录入</option><option value="FRANCHISE_SUPPLIED" ${S.publicPoolCustomerSource==='FRANCHISE_SUPPLIED'?'selected':''}>加盟商提供</option></select><select class="ops-input" id="public-pool-source"><option value="">全部录入方式</option><option value="PLATFORM_MANUAL" ${S.publicPoolSource==='PLATFORM_MANUAL'?'selected':''}>后台录入</option><option value="FEISHU_IMPORT" ${S.publicPoolSource==='FEISHU_IMPORT'?'selected':''}>飞书导入</option><option value="SUPPLIER_H5" ${S.publicPoolSource==='SUPPLIER_H5'?'selected':''}>加盟商提交</option></select><select class="ops-input" id="public-pool-completeness"><option value="">全部完整性</option><option value="COMPLETE" ${S.publicPoolCompleteness==='COMPLETE'?'selected':''}>资料可提交</option><option value="INCOMPLETE" ${S.publicPoolCompleteness==='INCOMPLETE'?'selected':''}>资料待补充</option></select><select class="ops-input" id="public-pool-duplicate"><option value="">全部查重状态</option><option value="CLEAR" ${S.publicPoolDuplicate==='CLEAR'?'selected':''}>未发现重复</option><option value="HARD_DUPLICATE" ${S.publicPoolDuplicate==='HARD_DUPLICATE'?'selected':''}>短期重复</option><option value="HISTORICAL_SUSPECT" ${S.publicPoolDuplicate==='HISTORICAL_SUSPECT'?'selected':''}>历史疑似</option></select><button class="ops-btn primary" type="submit">查询</button><button class="ops-btn" id="public-pool-filter-reset" type="button">重置</button></form></section>${inlineForm}<section class="ops-card"><div class="ops-card-head"><div><h2>待处理客户</h2><p>加盟商看不到本页和其中的客户信息。</p></div></div>${table(['序号','客户','客户来源','录入方式','所在地','渠道来源','提供加盟商','完整性 / 地区覆盖','手机号查重','创建人 / 时间','操作'],rows)}${pager(data)}</section>${publicPoolExportPanel}`);
  bindPager(data,publicPool);
  const byId=Object.fromEntries((data.items||[]).map(item=>[item.id,item]));
  document.querySelector('#public-pool-new').onclick=()=>openPlatformLeadForm(null);
  document.querySelector('#public-pool-inline').onclick=()=>{document.querySelector('#public-pool-inline-card').hidden=false;document.querySelector('#public-pool-inline-name').focus()};
  document.querySelector('#public-pool-inline-cancel').onclick=()=>{document.querySelector('#public-pool-inline-card').hidden=true};
  document.querySelector('#public-pool-feishu').onclick=openPublicPoolImport;
  const requestPublicPoolExport=async(button,filters)=>{button.disabled=true;const idempotency_key=button.dataset.idempotencyKey||`public-pool-export-${crypto.randomUUID()}`;button.dataset.idempotencyKey=idempotency_key;try{await api('/v1.2/reports/leads/exports',{method:'POST',body:JSON.stringify({scope:'PUBLIC_POOL',...filters,idempotency_key})});delete button.dataset.idempotencyKey;toast('公海池导出任务已提交，完成后可在本页下载');await refreshAfterSuccess(publicPool,'公海池导出任务已提交，完成后可在本页下载')}catch(error){button.disabled=false;toast(error.message,true)}};
  document.querySelector('#public-pool-export-filtered')?.addEventListener('click',event=>requestPublicPoolExport(event.currentTarget,publicPoolFilters()));
  document.querySelector('#public-pool-export-all')?.addEventListener('click',event=>requestPublicPoolExport(event.currentTarget,{}));
  document.querySelector('#public-pool-export-refresh')?.addEventListener('click',publicPool);
  document.querySelector('#public-pool-filter').onsubmit=async event=>{event.preventDefault();const createdFrom=document.querySelector('#public-pool-created-from').value,createdTo=document.querySelector('#public-pool-created-to').value;if(createdFrom&&createdTo&&createdFrom>createdTo){toast('创建开始日期不能晚于结束日期',true);return}S.publicPoolCreatedFrom=createdFrom;S.publicPoolCreatedTo=createdTo;S.publicPoolSubmitterId=document.querySelector('#public-pool-submitter').value;S.publicPoolKeyword=document.querySelector('#public-pool-keyword').value.trim();S.publicPoolPhone=document.querySelector('#public-pool-phone').value.trim();S.publicPoolCustomerSource=document.querySelector('#public-pool-customer-source').value;S.publicPoolSource=document.querySelector('#public-pool-source').value;S.publicPoolCompleteness=document.querySelector('#public-pool-completeness').value;S.publicPoolDuplicate=document.querySelector('#public-pool-duplicate').value;S.page=1;try{await publicPool()}catch(error){toast(error.message,true)}};
  document.querySelector('#public-pool-filter-reset').onclick=()=>{S.publicPoolCreatedFrom='';S.publicPoolCreatedTo='';S.publicPoolSubmitterId='';S.publicPoolKeyword='';S.publicPoolPhone='';S.publicPoolCustomerSource='';S.publicPoolSource='';S.publicPoolCompleteness='';S.publicPoolDuplicate='';S.page=1;publicPool()};
  document.querySelector('#public-pool-inline-form').onsubmit=async event=>{event.preventDefault();const source=document.querySelector('#public-pool-inline-source').value.trim();try{await api('/v1.2/public-pool/leads',{method:'POST',body:JSON.stringify({customer_name:document.querySelector('#public-pool-inline-name').value.trim()||null,phone:document.querySelector('#public-pool-inline-phone').value.trim()||null,source_channel:source?'OTHER':null,source_detail:source||null,consent_confirmed:false})});toast('客户已保存到公海池');await refreshAfterSuccess(publicPool,'客户已保存到公海池')}catch(error){toast(error.message,true)}};
  document.querySelectorAll('[data-public-pool-detail]').forEach(button=>button.onclick=()=>adminLeadDetail(button.dataset.publicPoolDetail));
  document.querySelectorAll('[data-public-pool-edit]').forEach(button=>button.onclick=()=>openPlatformLeadForm(byId[button.dataset.publicPoolEdit]));
  document.querySelectorAll('[data-public-pool-transfer]').forEach(button=>button.onclick=()=>transferPublicPoolLead(byId[button.dataset.publicPoolTransfer]));
  document.querySelectorAll('[data-public-pool-telesales]').forEach(button=>button.onclick=()=>assignPublicPoolPreDispatch(byId[button.dataset.publicPoolTelesales]));
  document.querySelectorAll('[data-public-pool-override]').forEach(button=>button.onclick=()=>overridePublicPoolDuplicate(byId[button.dataset.publicPoolOverride]));
  document.querySelectorAll('[data-public-pool-delete]').forEach(button=>button.onclick=()=>deleteOperationLead(byId[button.dataset.publicPoolDelete],publicPool));
}
function filterLeadSubmitters(items,companyId,keyword=''){
  const normalized=String(keyword||'').trim().toLocaleLowerCase('zh-CN');
  return (items||[]).filter(user=>(!companyId||user.company_id===companyId)&&(!normalized||String(user.name||'').toLocaleLowerCase('zh-CN').includes(normalized)));
}
function leadSubmitterOptions(items,selectedId){return `<option value="">全部录入人员</option>${items.map(user=>`<option value="${esc(user.id)}" ${selectedId===user.id?'selected':''}>${esc(user.name||'未命名人员')}${user.status==='ACTIVE'?'':'（已停用）'}</option>`).join('')}`}
function supplements(){return review()}
function closedLeads(){return review()}
async function loadSupplementHistory(enabled){
  if(!enabled)return {items:[],total:0,page:1,page_size:20};
  try{return await api(`/v1.2/admin/leads/pre-dispatch-rework-history${qs({page:S.supplementHistoryPage,page_size:20})}`)}
  catch(error){return {items:[],total:0,page:S.supplementHistoryPage,page_size:20,load_error:error.message||'历史记录加载失败'}}
}
async function review(){
  if(!S.sourceChannelOptions){try{S.sourceChannelOptions=await loadSourceChannelOptions()}catch{S.sourceChannelOptions=[]}}
  const supplementQueue=S.view==='supplements';
  const closedQueue=S.view==='closed';
  if(supplementQueue)S.leadPendingReason='PRE_DISPATCH_REWORK_REQUIRED';
  if(closedQueue)S.leadStatusFilter='CLOSED';
  const readOnly=primaryRole()==='SUPER_ADMIN',canPlatform=can('lead.manual.manage'),canSupplier=can('lead.supplier.review');
  const filters=leadReportFilters();
  const [reportData,operationUsers,filterOptions,exportTasks,supplementHistory]=await Promise.all([
    api('/v1.2/reports/leads/search',{method:'POST',body:JSON.stringify({...filters,page:S.page,page_size:20})}),
    S.operationUsers?Promise.resolve(S.operationUsers):api('/admin-meta/operation-users'),
    S.leadFilterOptions?Promise.resolve(S.leadFilterOptions):api('/v1.2/reports/leads/filter-options'),
    can('lead.phone.export')?api('/v1.2/reports/leads/exports?page=1&page_size=100'):Promise.resolve({items:[]}),
    loadSupplementHistory(supplementQueue),
  ]);
  S.operationUsers=operationUsers;
  S.leadFilterOptions=filterOptions;
  const leads=reportData.items||[];
  S.platformLeads=leads.filter(item=>item.source_kind==='PLATFORM_MANUAL');
  S.supplierLeads=leads.filter(item=>item.source_kind==='SUPPLIER_H5');
  const rows=leads.map((lead,index)=>{
    const platform=lead.source_kind==='PLATFORM_MANUAL',pending=['PENDING_REVIEW','PENDING_TELESALES_VERIFY'].includes(lead.lead_status);
    const actions=[`<button class="ops-btn" data-lead-detail="${esc(lead.id)}" data-lead-source="${esc(lead.source_kind)}">详情</button>`];
    if(isSuperAdmin()&&lead.is_test&&!lead.current_assignment_id)actions.push(`<button class="ops-btn danger" data-lead-test-delete="${esc(lead.id)}">删除测试客资</button>`);
    if(!readOnly&&canDeleteOperationLead())actions.push(`<button class="ops-btn danger" data-operation-lead-delete="${esc(lead.id)}">删除客资</button>`);
    if(!readOnly&&closedQueue&&lead.lead_status==='CLOSED'&&!lead.can_requeue_pre_dispatch)actions.push(`<button class="ops-btn primary" data-lead-reopen="${esc(lead.id)}">重新启用并选择接收方</button>`);
    if(!readOnly&&canPlatform&&lead.can_requeue_pre_dispatch)actions.push(`<button class="ops-btn primary" data-pre-dispatch-requeue="${esc(lead.id)}">重新分配电销核验</button>`);
    if(!readOnly&&platform&&lead.lead_status==='DRAFT'){
      actions.push(`<button class="ops-btn" data-platform-edit="${esc(lead.id)}">编辑</button>`);
      actions.push(`<button class="ops-btn primary" data-platform-submit="${esc(lead.id)}">资料完整，进入派发池</button>`);
      actions.push(`<button class="ops-btn" data-platform-pre-dispatch="${esc(lead.id)}">分配电销核验</button>`);
    }
    if(!readOnly&&['PLATFORM_MANUAL','FEISHU_IMPORT'].includes(lead.source_kind)&&lead.pending_reason==='PRE_DISPATCH_REWORK_REQUIRED')actions.push(`<button class="ops-btn primary" data-operation-supplement="${esc(lead.id)}">补充并继续处理</button>`);
    if(!readOnly&&canPlatform)actions.push(`<button class="ops-btn" data-platform-correction="${esc(lead.id)}" data-lead-correction="${esc(lead.id)}" data-lead-correction-source="${esc(lead.source_kind)}">更正关键信息</button>`);
    if(!readOnly&&canPlatform&&lead.pending_reason==='CORRECTION_REVIEW_REQUIRED'){
      actions.push(`<button class="ops-btn primary" data-lead-correction-recheck="${esc(lead.id)}" data-snapshot-version="${Number(lead.snapshot_version||1)}">重新检查接收资格</button>`);
      const canRelease=['PENDING_CLAIM','CLAIMED','FOLLOWING'].includes(lead.assignment_status)&&!(lead.correction_issues||[]).some(issue=>String(issue).startsWith('DEDUP_'));
      if(canRelease)actions.push(`<button class="ops-btn danger" data-lead-correction-release="${esc(lead.id)}" data-snapshot-version="${Number(lead.snapshot_version||1)}">解除原派发并重新入池</button>`);
    }
    const canWithdrawMisdispatch=['PENDING_CLAIM','CLAIMED','FOLLOWING'].includes(lead.assignment_status)&&lead.pending_reason!=='CORRECTION_REVIEW_REQUIRED';
    if(!readOnly&&canPlatform&&canWithdrawMisdispatch)actions.push(`<button class="ops-btn danger" data-lead-misdispatch-release="${esc(lead.id)}" data-snapshot-version="${Number(lead.snapshot_version||1)}">撤回错派并重新入池</button>`);
    if(!readOnly&&!platform&&pending){const telesalesAssigned=lead.lead_status==='PENDING_TELESALES_VERIFY'&&lead.latest_pre_dispatch_assignee_user_id;actions.push(`<button class="ops-btn primary" data-supplier-pre-dispatch="${esc(lead.id)}">${telesalesAssigned?'改派':'分配电销核实'}</button>`);}
    const followup=lead.latest_followup;
    const handlerKind=lead.franchise_handler_kind==='FRANCHISE_EMPLOYEE'?'加盟商员工':lead.franchise_handler_name?'加盟商公司':'--';
    const reward=lead.supplier_reward_status?`${lead.supplier_reward_points??0} 分<br><small>${esc(label(lead.supplier_reward_status))}${lead.supplier_reward_settled_at?` · ${fmt(lead.supplier_reward_settled_at)}`:''}</small>`:'--';
    return `<tr><td>${lead.sequence??rowSequence(reportData,index)}</td><td><b>${esc(lead.customer_name||'未填写')}</b>${lead.is_test?'<br><small>测试客资</small>':''}<br>${esc(lead.phone||lead.phone_masked||'--')}</td><td>${badge(lead.source_kind)}${lead.source_channel?`<br><small>${esc(sourceChannelName(lead.source_channel))}</small>`:''}</td><td>${esc(lead.supplier_company_name||'平台客资')}<br><small>${esc(lead.submitter_name||'未记录录入人')}</small></td><td>${esc(lead.city||'--')} ${esc(lead.district||'')}</td><td>${esc(lead.receiver_company_name||'未分配')}</td><td>${esc(lead.franchise_handler_name||'未分配')}<br><small>${handlerKind}</small></td><td>${badge(lead.lead_status)}${lead.lead_status==='PENDING_TELESALES_VERIFY'&&lead.latest_pre_dispatch_assignee_name?`<br><small>电销 ${esc(lead.latest_pre_dispatch_assignee_name)} · ${fmt(lead.latest_pre_dispatch_assigned_at)}</small>`:''}</td><td>${lead.assignment_status?badge(lead.assignment_status):'--'}</td><td>${reward}</td><td>${esc(lead.assigned_by_name||'未派发')}</td><td>${followup?`${badge(followup.status)}<br><small>${esc(followup.note||'无备注')} · ${fmt(followup.created_at)}</small>`:'--'}</td><td>${fmt(lead.created_at)}</td><td>${actions.join(' ')}</td></tr>`;
  });
  const sourceOptions=[['','全部来源'],['PLATFORM_MANUAL','平台录入'],['FEISHU_IMPORT','飞书导入'],['SUPPLIER_H5','加盟商提交'],['FEISHU_LEGACY','飞书历史导入']].filter(([value])=>!value||(value==='SUPPLIER_H5'?canSupplier:canPlatform));
  const supplierOptions=filterOptions.supplier_companies||[];
  const submitterOptions=filterOptions.submitters||[];
  const visibleSubmitters=filterLeadSubmitters(submitterOptions,S.leadSupplierCompanyId);
  const receiverOptions=filterOptions.receiver_companies||[];
  const assignerOptions=filterOptions.assigners||operationUsers;
  const leadStatusOptions=['DRAFT','PUBLIC_POOL','PENDING_REVIEW','PENDING_TELESALES_VERIFY','PENDING_OPERATION_DISPOSITION','READY_DISPATCH','DISPATCHED','CLAIMED','FOLLOWING','RETURN_PENDING','RETURNED','COMPLETED','INVALID','DUPLICATE','CLOSED'];
  const assignmentStatusOptions=['PENDING_CLAIM','CLAIMED','FOLLOWING','RETURN_PENDING','RETURNED','RELEASED','EXPIRED','COMPLETED'];
  const queueTitle=supplementQueue?'待运营补充':closedQueue?'已关闭客资':readOnly?'客资总览':'客资录入与流转';
  const queueDescription=supplementQueue?'集中处理当前待补充客资，并在下方找回所有曾被标记为待运营补充的历史记录。保存后重新校验，完整时进入派发池，仍有缺项时继续保留。':closedQueue?'统一查看已关闭客资。运营填写重新启用原因后，系统保留旧派发和积分历史，再进入接收加盟商选择；新派发单领取后重新计算 48 小时。':'统一展示平台客资与加盟商客资队列；日期始终按客资创建时间筛选，客资状态与派发状态分开。“派发运营人员”是实际执行派发的后台人员。加盟商客资在当地暂无其他合格接收方时先进入公海池；缺县客资可直派（系统软提醒、留缺县派发标记），也可由运营主动分配电销核实补县。';
  const pendingControl=supplementQueue?'<div class="ops-notice">已固定筛选：当前待运营补充</div>':closedQueue?'<div class="ops-notice">已固定筛选：已关闭客资</div>':`<select class="ops-input" id="lead-pending-reason-filter"><option value="">全部待办事项</option><option value="PRE_DISPATCH_REWORK_REQUIRED" ${S.leadPendingReason==='PRE_DISPATCH_REWORK_REQUIRED'?'selected':''}>待运营补充</option></select>`;
  const filterPanel=`<section class="ops-card"><div class="ops-card-head"><div><h2>${queueTitle}</h2><p>${queueDescription}</p></div><div class="ops-actions">${canPlatform&&!readOnly?'<button class="ops-btn primary" id="new-platform-lead">新建平台客资</button>':''}</div></div><form class="ops-filter" id="lead-report-filter"><label>创建日期从 <input class="ops-input" id="lead-created-from" type="date" value="${esc(S.leadCreatedFrom)}"></label><label>到 <input class="ops-input" id="lead-created-to" type="date" value="${esc(S.leadCreatedTo)}"></label><select class="ops-input" id="lead-source-filter">${sourceOptions.map(([value,text])=>`<option value="${value}" ${S.leadSource===value?'selected':''}>${text}</option>`).join('')}</select><input class="ops-input" id="lead-keyword-filter" type="search" autocomplete="off" placeholder="搜索客户/地区/具体来源" value="${esc(S.leadKeyword||'')}"><select class="ops-input" id="lead-source-channel-filter"><option value="">全部来源渠道</option>${(S.sourceChannelOptions||[]).map(([value,text])=>`<option value="${esc(value)}" ${S.leadSourceChannel===value?'selected':''}>${esc(text)}</option>`).join('')}</select><select class="ops-input" id="lead-supplier-company-filter"><option value="">全部供资加盟商</option>${supplierOptions.map(company=>`<option value="${esc(company.id)}" ${S.leadSupplierCompanyId===company.id?'selected':''}>${esc(company.name||'未命名加盟商')}${company.status==='ACTIVE'?'':'（已停用）'}</option>`).join('')}</select><input class="ops-input" id="lead-submitter-search" type="search" autocomplete="off" placeholder="搜索录入人员"><select class="ops-input" id="lead-submitter-filter">${leadSubmitterOptions(visibleSubmitters,S.leadSubmitterId)}</select>${pendingControl}<input class="ops-input" id="lead-phone-filter" inputmode="tel" maxlength="32" autocomplete="off" value="${esc(S.leadPhone)}" placeholder="完整手机号"><input class="ops-input" id="lead-region-filter" maxlength="64" value="${esc(S.leadRegion)}" placeholder="省/市/区县名称或地区编码（如阜阳/阜阳市）"><select class="ops-input" id="lead-receiver-filter"><option value="">全部接收加盟商</option>${receiverOptions.map(company=>`<option value="${esc(company.id)}" ${S.leadReceiverCompanyId===company.id?'selected':''}>${esc(company.name||'未命名加盟商')}${company.status==='ACTIVE'?'':'（已停用）'}</option>`).join('')}</select><select class="ops-input" id="lead-status-filter"><option value="">全部客资状态</option>${leadStatusOptions.map(value=>`<option value="${value}" ${S.leadStatusFilter===value?'selected':''}>${esc(label(value))}</option>`).join('')}</select><select class="ops-input" id="assignment-status-filter"><option value="">全部派发状态</option>${assignmentStatusOptions.map(value=>`<option value="${value}" ${S.assignmentStatusFilter===value?'selected':''}>${esc(label(value))}</option>`).join('')}</select><select class="ops-input" id="lead-assigner-filter"><option value="">全部派发运营人员</option>${assignerOptions.map(user=>`<option value="${esc(user.id)}" ${S.leadAssignerId===user.id?'selected':''}>${esc(user.name||user.display_name||user.username||'未命名人员')}${user.status==='ACTIVE'?'':'（已停用）'}</option>`).join('')}</select><button class="ops-btn primary" type="submit">查询</button><button class="ops-btn" type="button" id="lead-filter-reset">重置</button>${can('lead.phone.export')?'<button class="ops-btn" type="button" id="lead-export-request">导出客资完整信息</button>':''}</form></section>`;
  const queueRows=rows.length?rows:[`<tr><td colspan="14" class="ops-empty">未找到符合当前筛选条件的客资</td></tr>`];
  const queue=`<section class="ops-card"><div class="ops-card-head"><div><h2>客资记录</h2><p>列表显示提供加盟商、实际录入人、当前接收方、供资奖励和加盟商跟进人；完整派发与人员变更记录请在详情中查看。</p></div></div>${table(['序号','客户','来源','提供加盟商 / 录入人','所在地','当前接收方','加盟商跟进人','客资状态','派发状态','供资奖励','派发运营人员','最新加盟商跟进','创建时间','操作'],queueRows)}${pager(reportData)}</section>`;
  const historyItems=supplementHistory.items||[];
  const historyRows=historyItems.map((item,index)=>{const state=item.rework_pending?'当前待补充':item.status==='DRAFT'?'可恢复':'已进入后续流程';const action=!readOnly&&!item.rework_pending&&item.status==='DRAFT'?`<button class="ops-btn primary" data-rework-restore="${esc(item.id)}">恢复到待补充</button>`:'--';return `<tr><td>${rowSequence(supplementHistory,index)}</td><td><b>${esc(item.customer_name||'未填写')}</b><br><small>${esc(recordCode(item.id,'KZ'))}</small></td><td>${badge(item.status)}</td><td>${esc(state)}</td><td>${action}</td></tr>`});
  const historyPages=Math.max(1,Math.ceil(Number(supplementHistory.total||0)/Number(supplementHistory.page_size||20)));
  const historyPager=Number(supplementHistory.total||0)>20?`<div class="ops-pager"><button class="ops-btn" id="supplement-history-prev" ${S.supplementHistoryPage<=1?'disabled':''}>上一页</button><span>${S.supplementHistoryPage}/${historyPages}，共 ${Number(supplementHistory.total||0)} 条</span><button class="ops-btn" id="supplement-history-next" ${S.supplementHistoryPage>=historyPages?'disabled':''}>下一页</button></div>`:'';
  const historyPanel=supplementQueue?`<section class="ops-card"><div class="ops-card-head"><div><h2>历史待补充记录</h2><p>依据历次运营处置审计找回，不受当前待办标记变化影响；已进入后续流程的记录保留查阅，不会回退。</p></div></div>${supplementHistory.load_error?`<div class="ops-error">历史记录加载失败：${esc(supplementHistory.load_error)}。当前待补充队列仍可继续处理。</div>`:`${table(['序号','客户','当前状态','待补充状态','操作'],historyRows)}${historyPager}`}</section>`:'';
  const exportRows=(exportTasks.items||[]).filter(task=>task.filters?.scope!=='PUBLIC_POOL').map(task=>{const expired=task.expires_at&&new Date(task.expires_at)<=new Date();const action=task.status==='COMPLETED'&&!expired?`<a class="ops-btn" href="${API}/v1.2/reports/leads/exports/${encodeURIComponent(task.id)}/download" download>下载 Excel</a>`:expired?'文件已过期':task.status==='FAILED'?esc(task.error_message||'导出失败'):'后台处理中';return `<tr><td>${fmt(task.created_at)}</td><td>${esc(exportFilterSummary(task.filters||{}))}</td><td>${badge(task.status)}</td><td>${Number(task.row_count||0)} 条</td><td>${action}</td></tr>`});
  const exportPanel=can('lead.phone.export')?`<section class="ops-card"><div class="ops-card-head"><div><h2>后台导出任务</h2><p>完整手机号仅进入授权的后台导出文件；系统记录导出人和全部筛选条件。</p></div><button class="ops-btn" id="lead-export-refresh">刷新状态</button></div>${table(['提交时间','筛选条件','状态','客资数','文件'],exportRows)}</section>`:'';
  shell(filterPanel+queue+historyPanel+exportPanel);
  bindPager(reportData,review);
  document.querySelector('#supplement-history-prev')?.addEventListener('click',()=>{S.supplementHistoryPage=Math.max(1,S.supplementHistoryPage-1);review()});
  document.querySelector('#supplement-history-next')?.addEventListener('click',()=>{S.supplementHistoryPage=Math.min(historyPages,S.supplementHistoryPage+1);review()});
  document.querySelector('#lead-report-filter').onsubmit=async event=>{event.preventDefault();S.leadCreatedFrom=document.querySelector('#lead-created-from').value;S.leadCreatedTo=document.querySelector('#lead-created-to').value;S.leadSource=document.querySelector('#lead-source-filter').value;S.leadSourceChannel=document.querySelector('#lead-source-channel-filter')?.value||'';S.leadKeyword=document.querySelector('#lead-keyword-filter')?.value.trim()||'';S.leadSupplierCompanyId=document.querySelector('#lead-supplier-company-filter').value;S.leadSubmitterId=document.querySelector('#lead-submitter-filter').value;S.leadPendingReason=supplementQueue?'PRE_DISPATCH_REWORK_REQUIRED':closedQueue?'':document.querySelector('#lead-pending-reason-filter').value;S.leadPhone=document.querySelector('#lead-phone-filter').value.trim();S.leadRegion=document.querySelector('#lead-region-filter').value.trim();S.leadReceiverCompanyId=document.querySelector('#lead-receiver-filter').value;S.leadStatusFilter=closedQueue?'CLOSED':document.querySelector('#lead-status-filter').value;S.assignmentStatusFilter=document.querySelector('#assignment-status-filter').value;S.leadAssignerId=document.querySelector('#lead-assigner-filter').value;S.page=1;try{await review()}catch(error){toast(`地区筛选失败：${error.message}`,true)}};
  document.querySelector('#lead-filter-reset').onclick=()=>{resetLeadReportFilters();review()};
  const supplierFilter=document.querySelector('#lead-supplier-company-filter'),submitterSearch=document.querySelector('#lead-submitter-search'),submitterFilter=document.querySelector('#lead-submitter-filter');
  const syncSubmitterFilter=()=>{const matching=filterLeadSubmitters(submitterOptions,supplierFilter.value,submitterSearch.value);const selected=matching.some(user=>user.id===submitterFilter.value)?submitterFilter.value:'';zsSetSafeHtml(submitterFilter,leadSubmitterOptions(matching,selected));submitterFilter.value=selected};
  supplierFilter.onchange=()=>{submitterSearch.value='';syncSubmitterFilter()};
  submitterSearch.oninput=syncSubmitterFilter;
  let exportIdempotencyKey=null;
  document.querySelector('#lead-export-request')?.addEventListener('click',async()=>{const button=document.querySelector('#lead-export-request');button.disabled=true;exportIdempotencyKey=exportIdempotencyKey||`lead-export-${crypto.randomUUID()}`;try{await api('/v1.2/reports/leads/exports',{method:'POST',body:JSON.stringify({...leadReportFilters(),idempotency_key:exportIdempotencyKey})});exportIdempotencyKey=null;toast('后台导出任务已提交，完成后可在下方下载');await refreshAfterSuccess(review,'后台导出任务已提交，完成后可在下方下载')}catch(error){button.disabled=false;toast(error.message,true)}});
  document.querySelector('#lead-export-refresh')?.addEventListener('click',review);
  document.querySelector('#new-platform-lead')?.addEventListener('click',()=>openPlatformLeadForm(null));
  document.querySelectorAll('[data-lead-detail]').forEach(button=>button.onclick=()=>openLeadDetailForSource(button.dataset.leadDetail,button.dataset.leadSource));
  document.querySelectorAll('[data-platform-edit]').forEach(button=>button.onclick=async()=>{const isCurrent=beginModalRequest();try{const lead=await api(`/v1.2/platform/leads/${encodeURIComponent(button.dataset.platformEdit)}`);if(isCurrent())await openPlatformLeadForm(lead)}catch(error){toast(error.message,true)}});
  document.querySelectorAll('[data-operation-supplement]').forEach(button=>button.onclick=()=>supplementOperationLead(leads.find(lead=>lead.id===button.dataset.operationSupplement)));
  document.querySelectorAll('[data-lead-correction]').forEach(button=>button.onclick=async()=>{const isCurrent=beginModalRequest();try{const lead=await api(`/v1.2/admin/leads/${encodeURIComponent(button.dataset.leadCorrection)}`);if(isCurrent())await openPlatformLeadForm(lead,true)}catch(error){toast(error.message,true)}});
  document.querySelectorAll('[data-lead-correction-recheck]').forEach(button=>button.onclick=()=>recheckPlatformLeadCorrection(button.dataset.leadCorrectionRecheck,Number(button.dataset.snapshotVersion)));
  document.querySelectorAll('[data-lead-correction-release]').forEach(button=>button.onclick=()=>releasePlatformLeadCorrection(button.dataset.leadCorrectionRelease,Number(button.dataset.snapshotVersion)));
  document.querySelectorAll('[data-lead-misdispatch-release]').forEach(button=>button.onclick=()=>releaseMisdispatchedLead(button.dataset.leadMisdispatchRelease,Number(button.dataset.snapshotVersion)));
  document.querySelectorAll('[data-lead-test-delete]').forEach(button=>button.onclick=()=>deleteTestLead(leads.find(lead=>lead.id===button.dataset.leadTestDelete)));
  document.querySelectorAll('[data-operation-lead-delete]').forEach(button=>button.onclick=()=>deleteOperationLead(leads.find(lead=>lead.id===button.dataset.operationLeadDelete),review));
  document.querySelectorAll('[data-lead-reopen]').forEach(button=>button.onclick=()=>reopenClosedLead(leads.find(lead=>lead.id===button.dataset.leadReopen)));
  document.querySelectorAll('[data-pre-dispatch-requeue]').forEach(button=>button.onclick=()=>requeueLeadPreDispatch(leads.find(lead=>lead.id===button.dataset.preDispatchRequeue)));
  document.querySelectorAll('[data-rework-restore]').forEach(button=>button.onclick=()=>restoreHistoricalRework(historyItems.find(item=>item.id===button.dataset.reworkRestore)));
  document.querySelectorAll('[data-platform-submit]').forEach(button=>button.onclick=()=>submitPlatformLead(button.dataset.platformSubmit));
  document.querySelectorAll('[data-platform-pre-dispatch]').forEach(button=>button.onclick=()=>assignPlatformPreDispatch(button.dataset.platformPreDispatch));
  document.querySelectorAll('[data-supplier-pre-dispatch]').forEach(button=>button.onclick=()=>assignPreDispatch(button.dataset.supplierPreDispatch));
  if(S.id){const id=S.id;S.id='';await openLeadDetail(id)}
}
function reopenClosedLead(lead){
  if(!lead)return;
  actionForm({title:'重新启用已关闭客资',message:'旧派发、积分、奖励和退回记录会完整保留。重新启用后立即进入接收加盟商选择，新派发领取后重新计算 48 小时。',labelText:'重新启用原因',required:true,minLength:2,submitLabel:'重新启用并选择接收方'},async reason=>{
    await api(`/v1.2/admin/leads/${encodeURIComponent(lead.id)}/reopen`,{method:'POST',body:JSON.stringify({reason})});
    toast('客资已重新启用，请选择接收加盟商');
    go('dispatch',lead.id);
  });
}
function restoreHistoricalRework(item){
  if(!item)return;
  actionForm({title:'恢复历史待补充客资',message:'恢复后该客资会重新出现在当前待运营补充队列，原处置和核验记录保持不变。',labelText:'恢复原因',required:true,minLength:2,submitLabel:'确认恢复'},async reason=>{
    await api(`/v1.2/admin/leads/${encodeURIComponent(item.id)}/pre-dispatch-rework-restore`,{method:'POST',body:JSON.stringify({reason})});
    toast('已恢复到待运营补充');
    await supplements();
  });
}
function requeueLeadPreDispatch(lead){
  if(!lead)return;
  const contactResult=label(lead.latest_pre_dispatch_contact_result);
  actionForm({title:'重新分配电销核验',message:`上一轮联系结果为“${contactResult}”。重新入库后先创建新的电销核验轮次，通过运营处置后才能再次派发给加盟商。`,labelText:'重新核验原因',required:true,minLength:2,submitLabel:'重新进入电销核验'},async reason=>{
    await api(`/v1.2/admin/leads/${encodeURIComponent(lead.id)}/pre-dispatch-verification/requeue`,{method:'POST',body:JSON.stringify({reason})});
    toast('已重新进入电销核验队列，等待分配人员');
    await refreshAfterSuccess(review,'已重新进入电销核验队列');
  });
}
async function supplementOperationLead(lead){
  if(!lead)return;
  const isCurrent=beginModalRequest();
  try{
    const history=await api(`/v1.2/pre-dispatch-verifications/tasks${qs({lead_id:lead.id,submitted_history:true,page:1,page_size:1})}`);
    const task=history.items?.[0];
    const detail=task?await api(`/v1.2/pre-dispatch-verifications/tasks/${encodeURIComponent(task.id)}`):null;
    const refresh=S.view==='supplements'?supplements:review;
    if(isCurrent())await openPlatformLeadForm(lead,true,{verificationInfo:detail?.verification_info||null,refresh,forcePublicPool:['PLATFORM_MANUAL','FEISHU_IMPORT'].includes(lead.source_kind)});
  }catch(error){toast(error.message,true)}
}
function leadDetailBody(x){
  const history=x.assignment_history||[];
  const followups=x.followup_history||[];
  const historySection=`<section class="ops-card"><h3>派发历史</h3>${history.length?table(['接收方','派发运营人员','派发时间','状态'],history.map(item=>`<tr><td>${esc(item.receiver_company_name||'--')}</td><td>${esc(item.assigned_by_name||'--')}</td><td>${fmt(item.assigned_at)}</td><td>${badge(item.status)}</td></tr>`)):'<p class="ops-muted">暂无派发记录</p>'}</section>`;
  const followupSection=`<section class="ops-card"><h3>加盟商跟进记录</h3>${followups.length?table(['跟进人','状态','跟进内容','下次跟进','跟进时间'],followups.map(item=>`<tr><td>${esc(item.created_by_name||'--')}</td><td>${badge(item.status)}</td><td>${esc(item.note||'--')}</td><td>${fmt(item.next_followup_at)}</td><td>${fmt(item.created_at)}</td></tr>`)):'<p class="ops-muted">暂无加盟商跟进记录</p>'}</section>`;
  return `<div class="ops-detail-grid">${[['客资编号',recordCode(x.id,'KZ')],['客户来源',label(x.customer_source)],['录入方式',label(x.source_kind)],['渠道来源',x.source_display?label(x.source_display):null],['提供加盟商',x.supplier_company_name],['提交人',x.submitter_name],['客户',x.customer_name],['手机号',x.phone||x.phone_masked],['当前接收方',x.current_receiver_company_name],['派发运营人员',x.assigned_by_name],['处理状态',label(x.status)],['核验结果',label(x.review_status)],['重复检查',label(x.duplicate_status)],['所在地',`${x.city||''} ${x.district||''}`]].map(([a,b])=>`<div class="ops-detail"><small>${a}</small><b>${esc(b||'--')}</b></div>`).join('')}</div><section class="ops-card"><h3>客户需求</h3><p class="ops-muted">${esc(x.need_summary||'暂无说明')}</p></section>${historySection}${followupSection}<button class="ops-btn" id="trace">查看处理详情</button>`
}
function showLeadDetail(title,x){modal(title,leadDetailBody(x),()=>document.querySelector('#trace').onclick=()=>{closeModal();go('trace',x.id)})}
async function reviewDetail(id){const isCurrent=beginModalRequest(),x=await api(`/v1.2/admin/supplier-leads/${encodeURIComponent(id)}`);if(isCurrent())showLeadDetail('加盟商客资详情',x)}
async function platformDetail(id){const isCurrent=beginModalRequest(),x=await api(`/v1.2/platform/leads/${encodeURIComponent(id)}`);if(isCurrent())showLeadDetail('平台客资详情',x)}
async function adminLeadDetail(id){const isCurrent=beginModalRequest(),x=await api(`/v1.2/admin/leads/${encodeURIComponent(id)}`);if(isCurrent())showLeadDetail('客资详情',x)}
async function openLeadDetailForSource(id,sourceKind){
  if(sourceKind==='PLATFORM_MANUAL'){await platformDetail(id);return}
  if(sourceKind==='SUPPLIER_H5'){await reviewDetail(id);return}
  try{await adminLeadDetail(id)}catch(error){if(error.status===404){toast('客资不存在或已被清理',true);return}throw error}
}
async function openLeadDetail(id){await openLeadDetailForSource(id,S.leadSource)}
async function platformCities(){if(!S.platformCities){const tree=await api('/master-data/region-tree');S.platformCities=(tree.provinces||[]).flatMap(province=>(province.cities||[]).map(city=>({...city,province_code:province.code,province_name:province.name,option_name:`${province.name} · ${city.name}`})));}return S.platformCities}
async function platformDistricts(cityCode){const cities=await platformCities();S.platformDistricts=cities.find(city=>city.code===cityCode)?.districts||[];return S.platformDistricts}
async function platformTownships(districtCode){return districtCode?api(`/master-data/regions?parent_code=${encodeURIComponent(districtCode)}&level=TOWNSHIP`):[]}
async function applyPlatformRegionSearchResult(region){
  const path=region?.path||[];
  const city=path.find(item=>item.level==='CITY');
  const district=path.find(item=>item.level==='DISTRICT');
  const township=path.find(item=>item.level==='TOWNSHIP');
  if(!city)return;
  const citySelect=document.querySelector('#platform-lead-city'),districtSelect=document.querySelector('#platform-lead-district'),townshipSelect=document.querySelector('#platform-lead-township');
  citySelect.value=city.code;
  const districts=await platformDistricts(city.code);
  replacePlatformSelectOptions(districtSelect,districts,district?.code||'','','全市范围');
  const townships=district?await platformTownships(district.code):[];
  replacePlatformSelectOptions(townshipSelect,townships,township?.code||'','','可选，精确到乡镇/街道');
}
const platformOptionName=item=>item.option_name||item.label||item.name;
function platformSelectOptions(items,currentCode,currentName,emptyLabel){const options=[...items];if(currentName&&!options.some(item=>item.name===currentName))options.unshift({code:currentCode||'',name:currentName});return `<option value="">${emptyLabel}</option>${options.map(item=>`<option value="${esc(item.code)}" ${item.code===currentCode||item.name===currentName?'selected':''}>${esc(platformOptionName(item))}</option>`).join('')}`}
function replacePlatformSelectOptions(select,items,currentCode,currentName,emptyLabel){const entries=[...items];if(currentName&&!entries.some(item=>item.name===currentName))entries.unshift({code:currentCode||'',name:currentName});const option=(code,name,selected=false)=>{const node=document.createElement('option');node.value=code;node.textContent=name;node.selected=selected;return node};const options=[option('',emptyLabel),...entries.map(item=>option(item.code,platformOptionName(item),item.code===currentCode||item.name===currentName))];select.replaceChildren(...options)}
const budgetToWan=amountToWan;
function budgetFromWan(selector){return wanToAmount(document.querySelector(selector).value)}
function preDispatchVerificationInfo(info){
  if(!info)return '';
  const facts=[['核验人员',info.submitted_by_name],['提交时间',fmt(info.submitted_at)],['联系结果',label(info.contact_result)],['事实结论',label(info.conclusion)]];
  return `<section class="ops-card"><h3>电销核验信息</h3><div class="ops-detail-grid">${facts.map(([name,value])=>`<div class="ops-detail"><small>${name}</small><b>${esc(value||'--')}</b></div>`).join('')}</div><h3>核验备注</h3><p class="ops-muted">${esc(info.note||'暂无核验备注')}</p></section>`;
}
async function openPlatformLeadForm(item,correction=false,options={}){
  const isCurrent=beginModalRequest();
  const newLead=!item&&!correction;
  const directTelesalesIdempotencyKey=newLead?`pre-dispatch-${crypto.randomUUID()}`:null;
  const cities=await platformCities();
  let telesalesUsers=[],telesalesLoadError='';
  if(newLead){try{telesalesUsers=await loadTelesalesUsers()}catch(error){telesalesLoadError=error.message||'电销人员加载失败'}}
  const currentCity=cities.find(city=>city.name===item?.city)||null;
  const districts=await platformDistricts(currentCity?.code||'');
  const currentDistrict=districts.find(district=>district.name===item?.district)||null;
  const townships=await platformTownships(currentDistrict?.code||'');
  if(!isCurrent())return;
  const currentTownship=townships.find(township=>township.code===item?.region_code)||null;
  const sourceOptions=S.sourceChannelOptions?.length?S.sourceChannelOptions:[['MANUAL','人工录入'],['DOUYIN','抖音/信息流'],['WECHAT_VIDEO','视频号'],['XIAOHONGSHU','小红书'],['LIVE','直播'],['AD','广告'],['OTHER','其他']];
  const selectedSource=item?.source_channel||'OTHER';
  if(!sourceOptions.some(([code])=>code===selectedSource))sourceOptions.unshift([selectedSource,selectedSource]);
  const categoryOptions=[['OLD_RENOVATION','旧房改造'],['SELF_BUILD','农村自建房'],['INTERIOR','室内装修']];
  const hasDispatchHistory=Boolean(item?.current_assignment_id||(item?.assignment_history||[]).length);
  const correctionReasonLabel=hasDispatchHistory?'更正原因 *':'更正原因（未派发可选）';
  const lockAttrs=correction?'readonly':'',lockSelectAttrs=correction?'disabled':'';
  const quickDispatchAction=newLead?'<button class="ops-btn" type="button" id="platform-lead-quick-dispatch">创建并直接派发加盟商</button>':'';
  const testLeadField=newLead?'<label class="ops-field"><input id="platform-lead-is-test" type="checkbox"> 测试客资（仅超级管理员可清理）</label>':'';
  const newLeadRoute=newLead?`<div class="ops-field"><label for="platform-lead-next-action">保存后处理</label><select class="ops-input" id="platform-lead-next-action"><option value="DRAFT" selected>仅保存草稿</option><option value="TELESALES" ${telesalesUsers.length?'':'disabled'}>保存并派发电销核验</option></select></div><div id="platform-lead-telesales-fields" hidden><div class="ops-notice">只要求有效手机号；其他资料无论是否完整，都可以先交给电销核验。电销提交事实后由运营继续处理。</div>${telesalesLoadError?`<div class="ops-error">${esc(telesalesLoadError)}</div>`:''}<div class="ops-field"><label for="platform-lead-telesales-assignee">电销人员 *</label><select class="ops-input" id="platform-lead-telesales-assignee">${telesalesUsers.map(user=>`<option value="${esc(user.id)}">${esc(user.display_name||user.username)}</option>`).join('')}</select></div><div class="ops-field"><label for="platform-lead-telesales-reason">核验重点 *</label><textarea class="ops-textarea" id="platform-lead-telesales-reason" placeholder="例如：确认具体需求、客户授权和所在区域"></textarea></div></div>`:'';
  const formTitle=correction?'更正客资':item?.source_kind==='FEISHU_IMPORT'?'编辑公海池客户':item?'编辑平台客资':'新建平台客资';
  modal(formTitle,`${correction?preDispatchVerificationInfo(options.verificationInfo):''}<form class="ops-form" id="platform-lead-form"><div class="ops-notice">${correction?'本次更正可修改：客户姓名、客户需求、所在地址、客户信息授权；其余资料只读。更正会保留前后值和原因；已派发客资不会改写历史派发。':'默认只保存草稿，不选择加盟商；所在地可搜索或按层级选择。'}</div><div class="ops-field"><label>客户姓名</label><input class="ops-input" id="platform-lead-name" value="${esc(item?.customer_name==='未填写'?'':item?.customer_name||'')}"></div><div class="ops-field"><label>联系电话</label><input class="ops-input" id="platform-lead-phone" inputmode="tel" ${lockAttrs} value="${esc(item?.phone||'')}"></div>${newLeadRoute}<div class="ops-field"><label for="platform-lead-region-search">搜索标准地区</label><input class="ops-input" id="platform-lead-region-search" placeholder="输入省、市、区县或乡镇/街道"><select class="ops-input" id="platform-lead-region-search-results" hidden><option value="">请选择搜索结果</option></select></div><div class="ops-field"><label>所在地城市</label><select class="ops-input" id="platform-lead-city">${platformSelectOptions(cities,currentCity?.code||'',item?.city||'','请选择全国城市')}</select></div><div class="ops-field"><label>所在地区县</label><select class="ops-input" id="platform-lead-district">${platformSelectOptions(districts,currentDistrict?.code||'',item?.district||'','全市范围')}</select></div><div class="ops-field"><label>所在地乡镇/街道</label><select class="ops-input" id="platform-lead-township">${platformSelectOptions(townships,currentTownship?.code||'',currentTownship?.name||'','可选，精确到乡镇/街道')}</select></div><div class="ops-field"><label>来源渠道</label><select class="ops-input" id="platform-lead-source" ${lockSelectAttrs}>${sourceOptions.map(([code,name])=>`<option value="${code}" ${selectedSource===code?'selected':''}>${name}</option>`).join('')}</select></div><div class="ops-field" id="platform-lead-source-detail-field" ${selectedSource==='OTHER'?'':'hidden'}><label for="platform-lead-source-detail">具体来源 *</label><input class="ops-input" id="platform-lead-source-detail" maxlength="128" ${lockAttrs} value="${esc(item?.source_detail||'')}" placeholder="例如：老客户转介绍"></div><div class="ops-field"><label>咨询类别</label><select class="ops-input" id="platform-lead-category" ${lockSelectAttrs}>${categoryOptions.map(([code,name])=>`<option value="${code}" ${item?.category_code===code?'selected':''}>${name}</option>`).join('')}</select></div><div class="ops-field"><label>预算下限（万元）</label><input class="ops-input" id="platform-lead-budget-min" type="number" min="0" step="0.1" inputmode="decimal" ${lockAttrs} value="${esc(budgetToWan(item?.budget_min))}"></div><div class="ops-field"><label>预算上限（万元）</label><input class="ops-input" id="platform-lead-budget-max" type="number" min="0" step="0.1" inputmode="decimal" ${lockAttrs} value="${esc(budgetToWan(item?.budget_max))}"></div><div class="ops-field"><label>客户需求</label><textarea class="ops-textarea" id="platform-lead-need">${esc(item?.need_summary||'')}</textarea></div>${testLeadField}<label class="ops-field"><input id="platform-lead-consent" type="checkbox" ${item?.consent_confirmed?'checked':''}> 已获得客户信息授权</label>${correction?`<div class="ops-field"><label for="platform-lead-correction-reason">${correctionReasonLabel}</label><textarea class="ops-textarea" id="platform-lead-correction-reason" maxlength="1000" placeholder="说明信息来源和更正依据"></textarea></div>`:''}<div class="ops-actions"><button class="ops-btn" type="button" id="platform-lead-cancel">取消</button><button class="ops-btn primary" type="submit" id="platform-lead-submit">${correction?'保存更正':'保存草稿'}</button>${quickDispatchAction}</div></form>`,()=>{
    const form=document.querySelector('#platform-lead-form');
    form.dataset.regionChanged='false';
    const regionSearch=document.querySelector('#platform-lead-region-search'),regionResults=document.querySelector('#platform-lead-region-search-results'),sourceSelect=document.querySelector('#platform-lead-source'),sourceDetail=document.querySelector('#platform-lead-source-detail'),sourceDetailField=document.querySelector('#platform-lead-source-detail-field'),nextAction=document.querySelector('#platform-lead-next-action'),telesalesFields=document.querySelector('#platform-lead-telesales-fields'),submit=document.querySelector('#platform-lead-submit');
    let searchResults=[],searchTimer=null;
    document.querySelector('#platform-lead-cancel').onclick=closeModal;
    document.querySelector('#platform-lead-city').onchange=async event=>{form.dataset.regionChanged='true';const next=await platformDistricts(event.target.value);replacePlatformSelectOptions(document.querySelector('#platform-lead-district'),next,'','','全市范围');replacePlatformSelectOptions(document.querySelector('#platform-lead-township'),[],'','','请先选择区县')};
    document.querySelector('#platform-lead-district').onchange=async event=>{form.dataset.regionChanged='true';const next=await platformTownships(event.target.value);replacePlatformSelectOptions(document.querySelector('#platform-lead-township'),next,'','','可选，精确到乡镇/街道')};
    document.querySelector('#platform-lead-township').onchange=()=>{form.dataset.regionChanged='true'};
    const syncSourceDetail=()=>{const other=sourceSelect.value==='OTHER';sourceDetailField.hidden=!other;sourceDetail.required=other;if(!other)sourceDetail.value=''};
    sourceSelect.onchange=syncSourceDetail;syncSourceDetail();
    const syncNextAction=()=>{if(!nextAction)return;const telesales=nextAction.value==='TELESALES';telesalesFields.hidden=!telesales;submit.textContent=telesales?'保存并派发电销核验':'保存草稿'};
    if(nextAction){nextAction.onchange=syncNextAction;syncNextAction()}
    regionSearch.oninput=()=>{clearTimeout(searchTimer);const keyword=regionSearch.value.trim();if(!keyword){searchResults=[];regionResults.hidden=true;return}searchTimer=setTimeout(async()=>{try{searchResults=await api(`/master-data/regions/search?keyword=${encodeURIComponent(keyword)}&limit=30`);replacePlatformSelectOptions(regionResults,searchResults.map(region=>({...region,option_name:region.path_label})), '', '', '请选择搜索结果');regionResults.hidden=!searchResults.length}catch(error){toast(error.message,true)}},250)};
    regionResults.onchange=async()=>{const selected=searchResults.find(region=>region.code===regionResults.value);if(selected){form.dataset.regionChanged='true';await applyPlatformRegionSearchResult(selected)}};
    form.onsubmit=async event=>{event.preventDefault();if(sourceSelect.value==='OTHER'&&!sourceDetail.value.trim()){toast('来源选择其他时，请填写具体来源',true);sourceDetail.focus();return}if(nextAction?.value==='TELESALES'){if(!isValidMobile(document.querySelector('#platform-lead-phone').value)){toast('手机号必填且必须为 11 位有效号码',true);document.querySelector('#platform-lead-phone').focus();return}if(!document.querySelector('#platform-lead-telesales-assignee').value){toast('请选择电销人员',true);return}if(document.querySelector('#platform-lead-telesales-reason').value.trim().length<2){toast('请至少填写 2 个字的核验重点',true);return}submit.disabled=true;try{await saveAndAssignNewLeadToTelesales(directTelesalesIdempotencyKey)}finally{submit.disabled=false}return}submit.disabled=true;try{await savePlatformLead(item,correction,options.refresh,options.forcePublicPool)}finally{submit.disabled=false}};
    document.querySelector('#platform-lead-quick-dispatch')?.addEventListener('click',async()=>{if(sourceSelect.value==='OTHER'&&!sourceDetail.value.trim()){toast('来源选择其他时，请填写具体来源',true);sourceDetail.focus();return}const payload=await readPlatformLeadPayload();if(payload)await openQuickDispatchCandidates(payload)});
  });
}
async function readPlatformLeadPayload(item=null){
  const citySelect=document.querySelector('#platform-lead-city');
  const districtSelect=document.querySelector('#platform-lead-district');
  const townshipSelect=document.querySelector('#platform-lead-township');
  const cities=await platformCities();
  const city=cities.find(item=>item.code===citySelect.value);
  const districts=await platformDistricts(citySelect.value);
  const district=districts.find(item=>item.code===districtSelect.value);
  const townships=await platformTownships(districtSelect.value);
  const township=townships.find(item=>item.code===townshipSelect.value);
  const budget_min=budgetFromWan('#platform-lead-budget-min'),budget_max=budgetFromWan('#platform-lead-budget-max');
  if(Number.isNaN(budget_min)||Number.isNaN(budget_max)||budget_min!=null&&budget_min<0||budget_max!=null&&budget_max<0){toast('请填写有效的预算金额（万元）',true);return}
  if(budget_min!=null&&budget_max!=null&&budget_min>budget_max){toast('预算上限不能低于预算下限',true);return}
  const preserveOriginalRegion=Boolean(item)&&document.querySelector('#platform-lead-form')?.dataset.regionChanged!=='true';
  const selectedRegion={city:city?.name||null,district:district?.name||null,region_code:township?.code||district?.code||city?.code||null,province:city?.province_name||null};
  if(preserveOriginalRegion)Object.assign(selectedRegion,{city:item.city||null,district:item.district||null,region_code:item.region_code||null,province:item.province||null});
  const payload={customer_name:document.querySelector('#platform-lead-name').value.trim()||null,phone:document.querySelector('#platform-lead-phone').value.trim()||null,...selectedRegion,source_channel:document.querySelector('#platform-lead-source').value,source_detail:document.querySelector('#platform-lead-source-detail').value.trim()||null,category_code:document.querySelector('#platform-lead-category').value,need_summary:document.querySelector('#platform-lead-need').value.trim()||null,budget_min,budget_max,consent_confirmed:document.querySelector('#platform-lead-consent').checked};
  const testToggle=document.querySelector('#platform-lead-is-test');
  if(testToggle)Object.assign(payload,{is_test:testToggle.checked});
  return payload;
}
async function savePlatformLead(item,correction=false,refresh=null,forcePublicPool=false){
  const form=document.querySelector('#platform-lead-form');
  const id=item?.id||null;
  const payload=await readPlatformLeadPayload(item);
  if(!payload)return;
  if(correction&&id){Object.assign(payload,{phone:item?.phone||null,source_channel:item?.source_channel||null,source_detail:item?.source_detail||null,category_code:item?.category_code||null,budget_min:item?.budget_min??null,budget_max:item?.budget_max??null})}
  try{
    let transferResult=null;
    if(correction&&id&&!forcePublicPool){
      const reason=document.querySelector('#platform-lead-correction-reason').value.trim();
      if((item.current_assignment_id||(item.assignment_history||[]).length)&&!reason){toast('已派发客资必须填写更正原因',true);return}
      await api(`/v1.2/platform/leads/${encodeURIComponent(id)}/correction`,{method:'PATCH',body:JSON.stringify({...payload,reason,expected_snapshot_version:item.snapshot_version})});
    }else{
      const publicPoolPath=id?`/v1.2/public-pool/leads/${encodeURIComponent(id)}`:'/v1.2/public-pool/leads';
      const path=S.view==='publicPool'?publicPoolPath:forcePublicPool?publicPoolPath:id?`/v1.2/platform/leads/${encodeURIComponent(id)}`:'/v1.2/platform/leads';
      await api(path,{method:id?'PATCH':'POST',body:JSON.stringify(payload)});
      if(forcePublicPool&&id)transferResult=await api(`/v1.2/public-pool/leads/${encodeURIComponent(id)}/transfer-to-dispatch`,{method:'POST'});
    }
    if(transferResult&&!transferResult.transferred){toast(`资料仍需补充：${Object.values(transferResult.validation_errors||{}).join('；')||'请处理手机号查重结论'}`,true);return}
    if(transferResult?.transferred){closeModalFor(form);toast('补充完成，客资已进入派发池');await refreshAfterSuccess(refresh||review,'客资已进入派发池');return}
    const message=correction?'客资更正已保存，异常会标记待运营处理':S.view==='publicPool'?'客户已保存到公海池':'平台客资草稿已保存';closeModalFor(form);toast(message);const refreshView=refresh||(S.view==='publicPool'?publicPool:review);await refreshAfterSuccess(refreshView,message)
  }catch(error){toast(error.message,true)}
}
async function saveAndAssignNewLeadToTelesales(idempotency_key){
  const form=document.querySelector('#platform-lead-form');
  const payload=await readPlatformLeadPayload();
  if(!payload)return;
  const assignee_user_id=document.querySelector('#platform-lead-telesales-assignee').value;
  const reason=document.querySelector('#platform-lead-telesales-reason').value.trim();
  const refresh=S.view==='publicPool'?publicPool:review;
  try{
    await api('/v1.2/platform/leads/pre-dispatch-verification',{method:'POST',body:JSON.stringify({...payload,assignee_user_id,reason,idempotency_key})});
    closeModalFor(form);
    toast('客资已保存并派发电销核验');
    await refreshAfterSuccess(refresh,'客资已保存并派发电销核验');
  }catch(error){
    toast(error.message,true);
  }
}
async function openQuickDispatchCandidates(payload){
  let keyword='',requestSequence=0,searchTimer;
  const quickDispatchKeys=new Map();
  const candidateCard=item=>`<article class="ops-candidate-card eligible"><div class="ops-candidate-head"><div><h3>${esc(item.company_name)}</h3><p>当前符合接收资格与服务区域</p></div>${badge('APPROVED')}</div><div class="ops-candidate-facts"><span><small>所需积分</small><b>${esc(item.points_price)}</b></span></div><div class="ops-actions"><button class="ops-btn primary" data-quick-dispatch-company="${esc(item.company_id)}" data-company-name="${esc(item.company_name)}">选择接收人并派发</button></div></article>`;
  modal('创建并直接派发',`<div class="ops-notice">系统会先验证客资和加盟商接收资格；选定后一次完成创建、进入派发池和派发。</div><div class="ops-filter"><input class="ops-input" id="quick-dispatch-search" placeholder="搜索可接收的加盟商" autocomplete="off"></div><div id="quick-dispatch-results"><div class="ops-loading">正在校验客资并匹配加盟商…</div></div><div class="ops-actions"><button class="ops-btn" id="quick-dispatch-cancel">取消</button></div>`,()=>{
    const results=document.querySelector('#quick-dispatch-results');
    const bindActions=()=>document.querySelectorAll('[data-quick-dispatch-company]').forEach(button=>button.onclick=()=>{const companyId=button.dataset.quickDispatchCompany;if(!quickDispatchKeys.has(companyId))quickDispatchKeys.set(companyId,`quick-${crypto.randomUUID()}`);chooseDispatchEmployee(companyId,button.dataset.companyName||'',{onSubmit:async(employeeUserId,note)=>{await api('/v1.2/platform/leads/quick-dispatch',{method:'POST',body:JSON.stringify({...payload,company_id:companyId,employee_user_id:employeeUserId||null,idempotency_key:quickDispatchKeys.get(companyId),note:note||'后台快捷派发'})});toast(employeeUserId?'客资已创建并直派给所选员工':'客资已创建并直派给加盟商负责人');await review()}})});
    const load=async()=>{const requestId=++requestSequence;zsSetSafeHtml(results,'<div class="ops-loading">正在校验客资并匹配加盟商…</div>');try{const data=await api(`/v1.2/platform/leads/quick-dispatch/candidates${qs({keyword,page:1,page_size:50})}`,{method:'POST',body:JSON.stringify(payload)});if(requestId!==requestSequence)return;const items=data.candidates||[];zsSetSafeHtml(results,items.length?`<div class="ops-candidate-grid">${items.map(candidateCard).join('')}</div>`:'<div class="ops-empty">当前没有符合条件的接收加盟商</div>');bindActions()}catch(error){if(requestId===requestSequence)zsSetSafeHtml(results,`<div class="ops-error">${esc(error.message||'当前客资无法快捷派发')}</div>`)}};
    document.querySelector('#quick-dispatch-cancel').onclick=closeModal;
    document.querySelector('#quick-dispatch-search').oninput=event=>{keyword=event.target.value.trim();clearTimeout(searchTimer);searchTimer=setTimeout(load,250)};
    load();
  });
}
async function submitPlatformLead(id){try{await api(`/v1.2/platform/leads/${encodeURIComponent(id)}/submit`,{method:'POST'});toast('资料完整，已进入待派发池');await refreshAfterSuccess(review,'资料完整，已进入待派发池')}catch(error){toast(error.message,true)}}
async function recheckPlatformLeadCorrection(id,snapshotVersion){const reason=window.prompt('请填写重新检查原因（至少5个字符）','接收方资格已调整，申请重新检查');if(!reason)return;if(reason.trim().length<5){toast('重新检查原因至少5个字符',true);return}try{const result=await api(`/v1.2/platform/leads/${encodeURIComponent(id)}/correction/recheck`,{method:'POST',body:JSON.stringify({reason:reason.trim(),expected_snapshot_version:snapshotVersion})});toast(result.pending_reason?'重新检查完成，仍有异常需处理':'接收资格重新检查通过');await review()}catch(error){toast(error.message,true)}}
async function releasePlatformLeadCorrection(id,snapshotVersion){const reason=window.prompt('请填写解除原派发的原因（至少5个字符）','事实更正后原接收方不再符合资格，解除后重新派发');if(!reason)return;if(reason.trim().length<5){toast('解除原派发原因至少5个字符',true);return}try{await api(`/v1.2/platform/leads/${encodeURIComponent(id)}/correction/release-for-redispatch`,{method:'POST',body:JSON.stringify({reason:reason.trim(),expected_snapshot_version:snapshotVersion})});toast('原派发已解除，客资已重新进入待派发池');await refreshAfterSuccess(review,'原派发已解除，客资已重新进入待派发池')}catch(error){toast(error.message,true)}}
async function releaseMisdispatchedLead(id,snapshotVersion){const reason=window.prompt('请填写错派撤回原因（至少5个字符）','运营误派加盟商，撤回后重新派发');if(!reason)return;if(reason.trim().length<5){toast('错派撤回原因至少5个字符',true);return}try{await api(`/v1.2/platform/leads/${encodeURIComponent(id)}/misdispatch/release-for-redispatch`,{method:'POST',body:JSON.stringify({reason:reason.trim(),expected_snapshot_version:snapshotVersion})});toast('错派已撤回，客资已重新进入待派发池');await refreshAfterSuccess(review,'错派已撤回，客资已重新进入待派发池')}catch(error){toast(error.message,true)}}
async function deleteTestLead(lead){
  if(!lead)return;
  const isCurrent=beginModalRequest();
  try{
    const preview=await api(`/v1.2/platform/leads/${encodeURIComponent(lead.id)}/test-record/impact`);
    if(!isCurrent())return;
    const impact=preview.impact||{};
    const impactRows=[['派发历史',impact.assignment_history],['导入问题',impact.import_issues],['重复关系',impact.duplicate_relations],['核验任务',impact.verification_tasks],['核验提交',impact.verification_submissions],['查重记录',Number(impact.dedup_events||0)+Number(impact.dedup_overrides||0)]];
    modal('永久删除测试客资',`<form class="ops-form" id="test-lead-delete-form"><div class="ops-notice">此操作不可恢复。系统将删除这条测试客资及下列关联记录；只允许从未进入派发流程的数据执行。</div><div class="ops-detail-grid">${impactRows.map(([name,count])=>`<div class="ops-detail"><small>${name}</small><b>${Number(count||0)} 条</b></div>`).join('')}</div><div class="ops-field"><label>客资编号</label><div class="ops-notice"><b>${esc(recordCode(lead.id,'KZ'))}</b><br>${esc(lead.id)}</div></div><div class="ops-field"><label for="test-lead-confirm-id">输入完整客资 ID *</label><input class="ops-input" id="test-lead-confirm-id" autocomplete="off" placeholder="${esc(lead.id)}"></div><div class="ops-field"><label for="test-lead-confirm-name">输入客户完整名称 *</label><input class="ops-input" id="test-lead-confirm-name" autocomplete="off" placeholder="${esc(lead.customer_name)}"></div><div class="ops-field"><label for="test-lead-delete-reason">删除原因 *</label><textarea class="ops-textarea" id="test-lead-delete-reason" minlength="5" maxlength="1000" placeholder="例如：清理本轮功能验收测试数据"></textarea></div>${preview.deletable?'':`<div class="ops-notice">当前记录不可永久删除：${esc((preview.blockers||[]).join('、')||'不符合删除条件')}</div>`}<div class="ops-actions"><button class="ops-btn" id="test-lead-delete-cancel" type="button">取消</button><button class="ops-btn danger" type="submit" ${preview.deletable?'':'disabled'}>确认永久删除</button></div></form>`,()=>{
      const form=document.querySelector('#test-lead-delete-form'),submit=form.querySelector('button[type="submit"]');
      document.querySelector('#test-lead-delete-cancel').onclick=closeModal;
      form.onsubmit=async event=>{
        event.preventDefault();
        const confirmed_lead_id=document.querySelector('#test-lead-confirm-id').value.trim(),confirmed_customer_name=document.querySelector('#test-lead-confirm-name').value.trim(),reason=document.querySelector('#test-lead-delete-reason').value.trim();
        if(confirmed_lead_id!==lead.id){toast('完整客资 ID 不匹配',true);return}
        if(confirmed_customer_name!==lead.customer_name){toast('客户完整名称不匹配',true);return}
        if(reason.length<5){toast('删除原因至少填写 5 个字符',true);return}
        submit.disabled=true;
        try{await api(`/v1.2/platform/leads/${encodeURIComponent(lead.id)}/test-record`,{method:'DELETE',body:JSON.stringify({confirmed_lead_id,confirmed_customer_name,reason})});closeModalFor(form);toast('测试客资已永久删除');await refreshAfterSuccess(review,'测试客资已永久删除')}catch(error){if(form.isConnected)submit.disabled=false;toast(error.message,true)}
      };
    });
  }catch(error){toast(error.message,true)}
}
async function assignPlatformPreDispatch(leadId){
  const lead=S.platformLeads.find(item=>item.id===leadId);
  if(lead)await assignLeadPreDispatch(lead,review);
}
async function assignPublicPoolPreDispatch(lead){
  const blocked=publicPoolTelesalesBlockReason(lead);
  if(blocked){toast(blocked,true);return}
  await assignLeadPreDispatch(lead,publicPool);
}
async function assignLeadPreDispatch(lead,refresh){
  if(!lead?.phone_masked){toast('手机号必填且必须为 11 位有效号码',true);openPlatformLeadForm(lead);return}
  const isCurrent=beginModalRequest();
  try{
    const users=await loadTelesalesUsers();
    if(!isCurrent())return;
    const options=users.map(user=>`<option value="${esc(user.id)}">${esc(user.display_name||user.username)}</option>`).join('');
    modal('分配电销核验',users.length?`<form class="ops-form" id="platform-pre-dispatch-form"><div class="ops-notice">只要手机号有效即可分配；其他资料由电销核验后交回运营继续处理，不会直接进入加盟商派发。</div><div class="ops-field"><label>电销人员 *</label><select class="ops-input" id="platform-pre-assignee">${options}</select></div><div class="ops-field"><label>核验重点 *</label><textarea class="ops-textarea" id="platform-pre-reason" placeholder="例如：补充联系方式、客户授权和具体需求"></textarea></div><div class="ops-actions"><button class="ops-btn" type="button" id="platform-pre-cancel">取消</button><button class="ops-btn primary" id="platform-pre-submit" type="submit">确认派发</button></div></form>`:'<div class="ops-empty">暂无可分配的电销人员</div>',()=>{
      const form=document.querySelector('#platform-pre-dispatch-form');
      if(!form)return;
      document.querySelector('#platform-pre-cancel').onclick=closeModal;
      form.onsubmit=async event=>{
        event.preventDefault();
        const reason=document.querySelector('#platform-pre-reason').value.trim(),submit=document.querySelector('#platform-pre-submit');
        if(reason.length<2){toast('请至少填写 2 个字的核验重点',true);return}
        submit.disabled=true;
        try{
          await api(`/v1.2/admin/leads/${encodeURIComponent(lead.id)}/pre-dispatch-verification`,{method:'POST',body:JSON.stringify({assignee_user_id:document.querySelector('#platform-pre-assignee').value,reason})});
          closeModalFor(form);
          toast('已分配电销核验');
          await refreshAfterSuccess(refresh,'电销核验已分配');
        }catch(error){submit.disabled=false;toast(error.message,true)}
      };
    });
  }catch(error){toast(error.message,true)}
}
async function assignPreDispatch(leadId,refreshView=review){
  const isCurrent=beginModalRequest();
  try{
    const users=await loadTelesalesUsers();
    if(!isCurrent())return;
    const options=users.map(user=>`<option value="${esc(user.id)}">${esc(user.display_name||user.username)}${user.username?` · ${esc(user.username)}`:''}</option>`).join('');
    modal('派发前置电销核验',users.length?`<form class="ops-form" id="pre-dispatch-form"><div class="ops-notice">电销只能处理运营派发的任务；提交结论后由运营决定后续处置。</div><div class="ops-field"><label for="pre-dispatch-assignee">电销人员 *</label><select class="ops-input" id="pre-dispatch-assignee">${options}</select></div><div class="ops-field"><label for="pre-dispatch-reason">派发原因 *</label><textarea class="ops-textarea" id="pre-dispatch-reason" placeholder="例如：客户意向、区域或联系方式需要电话核实"></textarea></div><div class="ops-actions"><button class="ops-btn" type="button" id="pre-dispatch-cancel">取消</button><button class="ops-btn primary" id="pre-dispatch-submit">确认派发</button></div></form>`:'<div class="ops-empty">暂无可分配的电销人员</div>',()=>{
      const form=document.querySelector('#pre-dispatch-form');
      if(!form)return;
      document.querySelector('#pre-dispatch-cancel').onclick=closeModal;
      form.onsubmit=async event=>{
        event.preventDefault();
        const reason=document.querySelector('#pre-dispatch-reason').value.trim();
        const submit=document.querySelector('#pre-dispatch-submit');
        if(reason.length<2){toast('请至少填写 2 个字的派发原因',true);return}
        submit.disabled=true;
        try{
          await api(`/v1.2/admin/leads/${encodeURIComponent(leadId)}/pre-dispatch-verification`,{method:'POST',body:JSON.stringify({assignee_user_id:document.querySelector('#pre-dispatch-assignee').value,reason})});
          toast('前置电销核验已派发');
          closeModalFor(form);
          await refreshAfterSuccess(refreshView,'前置电销核验已派发');
        }catch(error){submit.disabled=false;toast(error.message,true)}
      };
    });
  }catch(error){toast(error.message,true)}
}
async function telesales(){
  const data=await api(`/v1.2/pre-dispatch-verifications/tasks${qs({
    page:S.page,
    page_size:20,
    status:S.tsStatus||undefined,
    keyword:S.tsKeyword||undefined,
    phone:S.tsPhone||undefined,
    assignee_user_id:S.tsAssignee||undefined,
    conclusion:S.tsConclusion||undefined,
    source_kind:S.tsSource||undefined,
    due_from:S.tsDueFrom?`${S.tsDueFrom}T00:00:00+08:00`:undefined,
    due_to:S.tsDueTo?`${S.tsDueTo}T23:59:59+08:00`:undefined,
  })}`);
  await loadTelesalesUsers();
  const telesalesOptions=(S.telesalesUsers||[]).map(user=>`<option value="${esc(user.id)}" ${S.tsAssignee===user.id?'selected':''}>${esc(user.display_name||user.username)}</option>`).join('');
  const tsSourceOptions=[['','全部来源'],['PLATFORM_MANUAL','平台录入'],['FEISHU_IMPORT','飞书导入'],['SUPPLIER_H5','加盟商提交'],['FEISHU_LEGACY','飞书历史导入']];
  const tsStatusOptions=[['','进行中（全部）'],['PENDING','待分配'],['IN_PROGRESS','核验中'],['SUBMITTED','已提交'],['RELEASED','已释放']];
  const tsConclusionOptions=[['','全部结论'],['QUALIFIED','信息合格'],['INFO_INCOMPLETE','信息不全'],['UNVERIFIABLE','无法核验'],['INVALID','无效客资'],['DUPLICATE','疑似重复']];
  const phoneField=can('lead.phone.export')?`<input class="ops-input" id="ts-filter-phone" type="search" autocomplete="off" placeholder="完整手机号精确搜索" value="${esc(S.tsPhone||'')}">`:'';
  const filterBar=`<form class="ops-filter" id="ts-filter-form"><input class="ops-input" id="ts-filter-keyword" type="search" autocomplete="off" placeholder="客户姓名搜索" value="${esc(S.tsKeyword||'')}">${phoneField}<select class="ops-input" id="ts-filter-status">${tsStatusOptions.map(([value,text])=>`<option value="${value}" ${S.tsStatus===value?'selected':''}>${text}</option>`).join('')}</select><select class="ops-input" id="ts-filter-assignee"><option value="">全部电销人员</option>${telesalesOptions}</select><select class="ops-input" id="ts-filter-conclusion">${tsConclusionOptions.map(([value,text])=>`<option value="${value}" ${S.tsConclusion===value?'selected':''}>${text}</option>`).join('')}</select><select class="ops-input" id="ts-filter-source">${tsSourceOptions.map(([value,text])=>`<option value="${esc(value)}" ${S.tsSource===value?'selected':''}>${esc(text)}</option>`).join('')}</select><label>参考时间从 <input class="ops-input" id="ts-filter-due-from" type="date" value="${esc(S.tsDueFrom||'')}"></label><label>到 <input class="ops-input" id="ts-filter-due-to" type="date" value="${esc(S.tsDueTo||'')}"></label><button class="ops-btn primary" type="submit">查询</button><button class="ops-btn" type="button" id="ts-filter-reset">重置</button></form>`;
  const rows=(data.items||[]).map((task,index)=>{
    const lead=task.lead||{};
    const nextStep=task.status==='SUBMITTED'?'运营处置电销结论':'电销继续完成事实核验';
    const action=task.status==='SUBMITTED'
      ?`<button class="ops-btn primary" data-pre-disposition-task="${esc(task.id)}">运营处置</button>`
      :`<button class="ops-btn" data-pre-assign="${esc(task.lead_id)}">${task.assignee_user_id?'改派':'派发'}</button>`;
    return `<tr><td>${rowSequence(data,index)}</td><td><b>${esc(lead.customer_name||'待核验客户')}</b><br><small>${esc(lead.phone||lead.phone_masked||'--')}</small></td><td>${badge(lead.source_kind)}</td><td>${verificationTaskBadge(task)}</td><td>${esc(telesalesName(task.assignee_user_id))}</td><td>${esc(label(task.conclusion))}</td><td>${esc(nextStep)}</td><td>${fmt(task.due_at)}</td><td>${action}</td></tr>`;
  });
  shell(`<section class="ops-card"><div class="ops-card-head"><div><h2>前置电销核验</h2><p>运营在此分配或改派；未完成任务持续保留在待处理队列，参考时间不限制电销继续处理。平台来源补充资料由运营处理，加盟商来源才可退回加盟商补正。</p></div></div>${filterBar}${table(['序号','客户','来源','任务状态','电销人员','事实结论','下一步','核验参考时间','操作'],rows)}${pager(data)}</section>`);
  bindPager(data,telesales);
  document.querySelector('#ts-filter-form').onsubmit=event=>{
    event.preventDefault();
    S.tsKeyword=document.querySelector('#ts-filter-keyword').value.trim();
    S.tsPhone=document.querySelector('#ts-filter-phone')?.value.trim()||'';
    S.tsStatus=document.querySelector('#ts-filter-status').value;
    S.tsAssignee=document.querySelector('#ts-filter-assignee').value;
    S.tsConclusion=document.querySelector('#ts-filter-conclusion').value;
    S.tsSource=document.querySelector('#ts-filter-source').value;
    S.tsDueFrom=document.querySelector('#ts-filter-due-from').value;
    S.tsDueTo=document.querySelector('#ts-filter-due-to').value;
    S.page=1;
    telesales();
  };
  document.querySelector('#ts-filter-reset').onclick=()=>{
    S.tsKeyword='';S.tsPhone='';S.tsStatus='';S.tsAssignee='';S.tsConclusion='';S.tsSource='';S.tsDueFrom='';S.tsDueTo='';
    S.page=1;
    telesales();
  };
  document.querySelectorAll('[data-pre-assign]').forEach(button=>button.onclick=()=>assignPreDispatch(button.dataset.preAssign,telesales));
  document.querySelectorAll('[data-pre-disposition-task]').forEach(button=>button.onclick=()=>openPreDispatchTask(button.dataset.preDispositionTask));
  if(S.id){const taskId=S.id;S.id='';await openPreDispatchTask(taskId)}
}
async function openPreDispatchTask(taskId){
  const isCurrent=beginModalRequest();
  try{
    const task=await api(`/v1.2/pre-dispatch-verifications/tasks/${encodeURIComponent(taskId)}`);
    if(!isCurrent())return;
    if(task.status!=='SUBMITTED'){toast('该电销核验任务当前无需运营处置',true);return}
    disposePreDispatch(task);
  }catch(error){toast(error.message,true)}
}
function canApprovePreDispatch(task){return !task.requires_qualified_verification||(task.conclusion||task.verification_info?.conclusion)==='QUALIFIED'}
function disposePreDispatch(task){
  const leadId=task.lead_id,sourceKind=task.lead?.source_kind||'';
  const operationOwned=['PLATFORM_MANUAL','FEISHU_IMPORT'].includes(sourceKind);
  const contactResult=task.contact_result||task.verification_info?.contact_result;
  const noAnswer=['NO_ANSWER','NOT_ANSWERED','MISSED_CALL','UNREACHABLE','REFUSED','REJECTED_CALL','CALL_REJECTED','无人接听','未接','未接听','拒接','拒绝接听'].includes(contactResult);
  const approveOption=canApprovePreDispatch(task)?'<option value="APPROVE_POOL">确认合格，进入派发池</option>':'';
  const requeueOption=noAnswer||task.conclusion==='UNVERIFIABLE'||task.verification_info?.conclusion==='UNVERIFIABLE'?'<option value="REQUEUE_VERIFICATION">未接或拒接，重新分配电销核验</option>':'';
  modal('运营处置电销结论',`${preDispatchVerificationInfo(task.verification_info)}<form class="ops-form" id="pre-disposition-form"><div class="ops-notice">电销只提供核验事实；${operationOwned?'运营来源由运营补充资料，不会退回加盟商。':'加盟商来源核实无效时会退回加盟商，并记录原因。'}</div><div class="ops-field"><label for="pre-disposition-decision">后续处理 *</label><select class="ops-input" id="pre-disposition-decision">${approveOption}${requeueOption}<option value="RETURN_REWORK">${operationOwned?'资料待补，运营补充资料后再处理':'资料待补，退回加盟商补正'}</option><option value="DUPLICATE">标记为重复客资</option><option value="CLOSE">${operationOwned?'关闭该客资':'确认无效，退回加盟商'}</option></select></div><div class="ops-field"><label for="pre-disposition-note">运营处理说明 *</label><textarea class="ops-textarea" id="pre-disposition-note" placeholder="写明结合电销结论作出的处理判断"></textarea></div><div class="ops-actions"><button class="ops-btn" type="button" id="pre-disposition-correct-lead">依据核验备注修改客户信息</button><button class="ops-btn" type="button" id="pre-disposition-cancel">取消</button><button class="ops-btn primary" id="pre-disposition-submit">确认处置</button></div></form>`,()=>{
    const form=document.querySelector('#pre-disposition-form');
    document.querySelector('#pre-disposition-cancel').onclick=closeModal;
    document.querySelector('#pre-disposition-correct-lead').onclick=async()=>{closeModalFor(form);await correctLeadFromVerificationTask(task.id)};
    form.onsubmit=async event=>{
      event.preventDefault();
      const note=document.querySelector('#pre-disposition-note').value.trim();
      const submit=document.querySelector('#pre-disposition-submit');
      if(note.length<2){toast('请至少填写 2 个字的运营处理说明',true);return}
      submit.disabled=true;
      try{
        const decision=document.querySelector('#pre-disposition-decision').value;
        if(decision==='REQUEUE_VERIFICATION')await api(`/v1.2/admin/leads/${encodeURIComponent(leadId)}/pre-dispatch-verification/requeue`,{method:'POST',body:JSON.stringify({reason:note})});
        else await api(`/v1.2/admin/leads/${encodeURIComponent(leadId)}/pre-dispatch-disposition`,{method:'POST',body:JSON.stringify({decision,note})});
        toast(decision==='REQUEUE_VERIFICATION'?'已重新进入电销核验队列':'运营处置已提交');
        closeModalFor(form);
        await refreshAfterSuccess(telesales,'电销核验结论已处置');
      }catch(error){submit.disabled=false;toast(error.message,true)}
    };
  });
}
function companyQueuePager(data,prefix,currentPage){
  const pages=Math.max(1,Math.ceil((data.total||0)/(data.page_size||20)));
  return `<div class="ops-pager"><button class="ops-btn" id="${prefix}-prev" ${currentPage<=1?'disabled':''}>上一页</button><span>${currentPage}/${pages}，共 ${data.total||0} 条</span><button class="ops-btn" id="${prefix}-next" ${currentPage>=pages?'disabled':''}>下一页</button></div>`;
}
function bindCompanyQueuePager(data,prefix,pageKey){
  const pages=Math.max(1,Math.ceil((data.total||0)/(data.page_size||20)));
  document.querySelector(`#${prefix}-prev`).onclick=()=>{S[pageKey]=Math.max(1,S[pageKey]-1);companies()};
  document.querySelector(`#${prefix}-next`).onclick=()=>{S[pageKey]=Math.min(pages,S[pageKey]+1);companies()};
}
const INTERNAL_ROLE_OPTIONS=[['SUPER_ADMIN','超级管理员'],['OPERATION','运营管理员'],['TELESALES','电销人员']];
const INTERNAL_ROLE_LABEL=Object.fromEntries(INTERNAL_ROLE_OPTIONS);
const calendarMonthValue=moment=>{const parts=Object.fromEntries(new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit'}).formatToParts(moment).filter(part=>part.type!=='literal').map(part=>[part.type,part.value]));return `${parts.year}-${parts.month}`};
const calendarRange=value=>{const [year,month]=value.split('-').map(Number),daysInMonth=new Date(Date.UTC(year,month,0)).getUTCDate();return {year,month,daysInMonth,start:`${value}-01`,end:`${value}-${String(daysInMonth).padStart(2,'0')}`}};
const shiftCalendarMonth=(value,offset)=>{const [year,month]=value.split('-').map(Number),absolute=year*12+month-1+offset;return `${Math.floor(absolute/12)}-${String(absolute%12+1).padStart(2,'0')}`};
const calendarUpdatedBy=item=>item.updated_by_name||(item.updated_by?recordCode(item.updated_by,'账号'):'系统默认规则');
const calendarUpdatedAt=item=>item.updated_at?new Date(item.updated_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'--';
const calendarDayBadge=item=>`<span class="ops-status ${item.is_workday?'ok':''}">${item.is_workday?'工作日':'休息日'}</span>`;
function calendarDayModal(dayValue,item={},lockedDate=false){
  const day=dayValue||`${S.calendarMonth}-01`,isWorkday=typeof item.is_workday==='boolean'?item.is_workday:true;
  modal(lockedDate?'编辑工作日历日期':'单日设定',`<form class="ops-form" id="calendar-form"><div class="ops-field"><label for="calendar-day">日期 *</label><input class="ops-input" id="calendar-day" type="date" value="${esc(day)}" ${lockedDate?'readonly':''}></div><div class="ops-field"><label for="calendar-is-workday">当天安排 *</label><select class="ops-input" id="calendar-is-workday"><option value="true" ${isWorkday?'selected':''}>工作日</option><option value="false" ${isWorkday?'':'selected'}>休息日</option></select></div><div class="ops-field"><label for="calendar-holiday-name">节日或说明</label><input class="ops-input" id="calendar-holiday-name" maxlength="128" value="${esc(item.holiday_name||'')}" placeholder="例如：国庆节或调休工作日"></div><div class="ops-notice">只影响保存后新领取或历史缺失字段补算；已固化的历史截止时间不回算。</div><div class="ops-actions"><button type="button" class="ops-btn" id="calendar-cancel">取消</button><button class="ops-btn primary" id="calendar-submit">保存</button></div></form>`,()=>{
    const form=document.querySelector('#calendar-form'),submit=document.querySelector('#calendar-submit');
    document.querySelector('#calendar-cancel').onclick=closeModal;
    form.onsubmit=async event=>{event.preventDefault();const selectedDay=document.querySelector('#calendar-day').value.trim();if(!selectedDay){toast('请选择日期',true);return}submit.disabled=true;try{const result=await api(`/admin/v1.2/calendar-days/${encodeURIComponent(selectedDay)}`,{method:'PUT',body:JSON.stringify({is_workday:document.querySelector('#calendar-is-workday').value==='true',holiday_name:document.querySelector('#calendar-holiday-name').value.trim()||null})});const message=result.changed?'工作日历已保存':'配置无变化，未重复写入审计';toast(message);closeModalFor(form);await refreshAfterSuccess(calendar,message)}catch(error){if(form.isConnected)submit.disabled=false;toast(error.message,true)}};
  });
}
function calendarDayDetail(item){
  modal('工作日历详情',`<div class="ops-detail-grid">${[['日期',item.day],['当天安排',item.is_workday?'工作日':'休息日'],['设置方式',item.is_override?'单日调整':'系统默认'],['节日或说明',item.holiday_name||'--'],['变更人',calendarUpdatedBy(item)],['变更时间（中国时区）',calendarUpdatedAt(item)]].map(([name,value])=>`<div class="ops-detail"><small>${esc(name)}</small><b>${esc(value)}</b></div>`).join('')}</div><div class="ops-notice">只影响保存后新领取或历史缺失字段补算；已固化的历史截止时间不回算。</div><div class="ops-actions"><button class="ops-btn primary" id="calendar-edit">编辑当天</button></div>`,()=>document.querySelector('#calendar-edit').onclick=()=>calendarDayModal(item.day,item,true));
}
async function calendar(){
  S.calendarMonth=S.calendarMonth||calendarMonthValue(new Date());
  const range=calendarRange(S.calendarMonth),items=await api(`/admin/v1.2/calendar-days?start=${range.start}&end=${range.end}`);
  if(items.length!==range.daysInMonth)throw new Error('工作日历返回的有效日期不完整');
  const overrides=items.filter(item=>item.is_override),leadingDays=(new Date(Date.UTC(range.year,range.month-1,1)).getUTCDay()+6)%7;
  const cells=[...Array(leadingDays)].map(()=>'<div class="ops-calendar-blank" aria-hidden="true"></div>');
  cells.push(...items.map(item=>`<button class="ops-calendar-day ${item.is_workday?'':'rest'}" type="button" data-calendar-day="${esc(item.day)}"><b>${Number(item.day.slice(-2))}</b><span>${esc(item.is_workday?'工作日':'休息日')}</span><small>${esc(item.holiday_name||(item.is_override?'单日调整':'默认规则'))}</small></button>`));
  const rows=overrides.map(item=>`<tr><td><b>${esc(item.day)}</b></td><td>${calendarDayBadge(item)}</td><td>${esc(item.holiday_name||'--')}</td><td>${esc(calendarUpdatedBy(item))}<br><small>${esc(calendarUpdatedAt(item))}</small></td><td><button class="ops-btn" data-calendar-day="${esc(item.day)}">查看</button></td></tr>`);
  shell(`<div class="ops-page-actions"><button class="ops-btn" data-view="settings">返回设置</button><div class="ops-calendar-controls"><button class="ops-btn" id="calendar-prev">上月</button><input class="ops-input" id="calendar-month" type="month" value="${esc(S.calendarMonth)}"><button class="ops-btn" id="calendar-next">下月</button><button class="ops-btn primary" id="calendar-new">单日设定</button></div></div><section class="ops-card"><div class="ops-card-head"><div><h2>${esc(S.calendarMonth)} 工作日历</h2><p>默认按周一到周五计算工作日，法定节假日和调休只维护单日例外。</p></div></div><div class="ops-notice">只影响保存后新领取或历史缺失字段补算；已固化的历史截止时间不回算。</div><div class="ops-calendar-week">${['一','二','三','四','五','六','日'].map(day=>`<span>${day}</span>`).join('')}</div><div class="ops-calendar-grid">${cells.join('')}</div></section><section class="ops-card"><div class="ops-card-head"><div><h2>单日例外</h2><p>本月共 ${overrides.length} 项显式调整。</p></div></div>${table(['日期','状态','节日或说明','变更人/时间','操作'],rows)}</section>`);
  const byDay=Object.fromEntries(items.map(item=>[item.day,item]));
  document.querySelector('#calendar-month').onchange=event=>{S.calendarMonth=event.target.value;calendar()};
  document.querySelector('#calendar-prev').onclick=()=>{S.calendarMonth=shiftCalendarMonth(S.calendarMonth,-1);calendar()};
  document.querySelector('#calendar-next').onclick=()=>{S.calendarMonth=shiftCalendarMonth(S.calendarMonth,1);calendar()};
  document.querySelector('#calendar-new').onclick=()=>calendarDayModal(range.start,byDay[range.start]);
  document.querySelectorAll('[data-calendar-day]').forEach(button=>button.onclick=()=>calendarDayDetail(byDay[button.dataset.calendarDay]));
}
const internalUserRoles=user=>(user.roles||user.role_codes||[]).filter(role=>INTERNAL_ROLE_LABEL[role]);
const internalRoleOptions=(name,current='OPERATION')=>INTERNAL_ROLE_OPTIONS.map(([role,labelText])=>`<label class="ops-choice"><input type="radio" name="${esc(name)}" value="${role}" ${role===current?'checked':''}>${labelText}</label>`).join('');
const selectedInternalRole=name=>document.querySelector(`input[name="${name}"]:checked`)?.value||'';
async function runInternalUserAction(button,action,success){
  const original=button.textContent;button.disabled=true;button.textContent='处理中';
  try{await action();toast(success);closeModalFor(button);await refreshAfterSuccess(internalUsers,success)}catch(error){toast(error.message,true)}finally{if(button.isConnected){button.disabled=false;button.textContent=original}}
}
function showInternalUserCredentials(user,onClose){
  const password=user.initial_password||'',username=user.username||'';
  if(!password){modal('内部账号已创建',`<div class="ops-notice">账号已创建，但初始密码未返回。请立即在账号列表中重置密码后再交付账号本人。</div><div class="ops-detail"><small>登录账号</small><b>${esc(username)}</b></div>`,()=>{});return false}
  const credentials=`登录账号：${username}\n初始密码：${password}`;
  modal('请安全交付初始密码',`<div class="ops-notice">初始密码仅在当前窗口展示一次，请立即复制并通过安全渠道交付账号本人。</div><div class="ops-detail-grid"><div class="ops-detail"><small>登录账号</small><b>${esc(username)}</b></div><div class="ops-detail"><small>初始密码</small><b id="internal-user-password">${esc(password)}</b></div></div><div class="ops-actions"><button class="ops-btn primary" id="copy-internal-user-password">复制初始密码</button><button class="ops-btn" id="copy-internal-user-credentials">复制账号与密码</button><button class="ops-btn" id="internal-user-credentials-close">已保存</button></div>`,()=>{
    const copy=value=>navigator.clipboard.writeText(value).then(()=>toast('已复制')).catch(()=>toast('浏览器不支持自动复制，请手动复制',true));
    document.querySelector('#copy-internal-user-password').onclick=()=>copy(password);
    document.querySelector('#copy-internal-user-credentials').onclick=()=>copy(credentials);
    document.querySelector('#internal-user-credentials-close').onclick=()=>{closeModal();onClose?.()};
  });
  return true;
}
function internalUserModal(){
  modal('新建内部账号',`<form class="ops-form" id="internal-user-form"><div class="ops-field"><label for="internal-user-name">姓名 *</label><input class="ops-input" id="internal-user-name" maxlength="64" autocomplete="name"></div><div class="ops-field"><label for="internal-user-username">登录账号 *</label><input class="ops-input" id="internal-user-username" maxlength="64" autocomplete="username"></div><div class="ops-notice">无需填写密码，系统会自动生成可复制的 8 位以上初始密码。</div><label class="ops-check"><input type="checkbox" id="internal-user-is-test"> 测试账号</label><small class="ops-muted">仅用于联测；停用且无业务数据时才允许删除。</small><div class="ops-field"><label>角色 *</label><div class="ops-choice-list">${internalRoleOptions('internal-role')}</div><small class="ops-muted">单选，仅限平台内部角色。</small></div><div class="ops-actions"><button type="button" class="ops-btn" id="internal-user-cancel">取消</button><button class="ops-btn primary" id="internal-user-submit">创建</button></div></form>`,()=>{
    const form=document.querySelector('#internal-user-form'),submit=document.querySelector('#internal-user-submit');
    document.querySelector('#internal-user-cancel').onclick=closeModal;
    form.onsubmit=async event=>{event.preventDefault();const display_name=document.querySelector('#internal-user-name').value.trim(),username=document.querySelector('#internal-user-username').value.trim(),role=selectedInternalRole('internal-role'),is_test=document.querySelector('#internal-user-is-test').checked;if(!display_name){toast('请输入姓名',true);return}if(username.length<2){toast('登录账号至少输入 2 个字符',true);return}if(!role){toast('请选择一个角色',true);return}submit.disabled=true;lockModal(form,document.querySelector('#internal-user-cancel'));try{const created=await api('/users',{method:'POST',body:JSON.stringify({display_name,username,role_codes:[role],is_test})});unlockModal(form);const passwordReady=showInternalUserCredentials(created);toast(passwordReady?'账号已创建，请复制初始密码':'账号已创建，但需要立即重置密码',!passwordReady);try{await internalUsers()}catch(refreshError){toast(`账号已创建，但账号列表刷新失败：${refreshError.message}`,true)}}catch(error){unlockModal(form);submit.disabled=false;document.querySelector('#internal-user-cancel').disabled=false;document.querySelector('#modal-close').disabled=false;toast(error.message,true)}};
  });
}
function internalRoleModal(user){
  const current=internalUserRoles(user)[0]||'OPERATION';
  modal('编辑内部账号角色',`<form class="ops-form" id="internal-role-form"><div class="ops-notice">${esc(user.display_name)} · ${esc(user.username||'--')}。一个账号只能有一个角色，保存后该账号现有会话立即失效。</div><div class="ops-field"><label>角色 *</label><div class="ops-choice-list">${internalRoleOptions('internal-role-edit',current)}</div></div><div class="ops-actions"><button type="button" class="ops-btn" id="internal-role-cancel">取消</button><button class="ops-btn primary" id="internal-role-submit">保存角色</button></div></form>`,()=>{
    const form=document.querySelector('#internal-role-form'),submit=document.querySelector('#internal-role-submit');document.querySelector('#internal-role-cancel').onclick=closeModal;
    form.onsubmit=event=>{event.preventDefault();const role=selectedInternalRole('internal-role-edit');runInternalUserAction(submit,()=>api(`/users/${encodeURIComponent(user.id)}/roles`,{method:'PUT',body:JSON.stringify({role_codes:[role]})}),'角色已更新')};
  });
}
function internalDisplayNameModal(user){
  modal('修改人员姓名',`<form class="ops-form" id="internal-name-form"><div class="ops-notice">仅修改后台显示姓名，登录账号、角色和账号状态保持不变。</div><div class="ops-field"><label for="internal-display-name">姓名 *</label><input class="ops-input" id="internal-display-name" maxlength="64" value="${esc(user.display_name||'')}"></div><div class="ops-actions"><button type="button" class="ops-btn" id="internal-name-cancel">取消</button><button class="ops-btn primary" id="internal-name-submit">保存姓名</button></div></form>`,()=>{
    const form=document.querySelector('#internal-name-form'),submit=document.querySelector('#internal-name-submit'),input=document.querySelector('#internal-display-name');
    document.querySelector('#internal-name-cancel').onclick=closeModal;
    form.onsubmit=event=>{event.preventDefault();const display_name=input.value.trim();if(!display_name){toast('请输入姓名',true);input.focus();return}runInternalUserAction(submit,()=>api(`/users/${encodeURIComponent(user.id)}`,{method:'PATCH',body:JSON.stringify({display_name})}),'人员姓名已更新')};
    input.focus();input.select();
  });
}
function resetInternalUserPassword(user){
  actionForm({title:'重置内部账号密码',message:'保存后该账号现有会话立即失效。',labelText:'新密码',required:true,minLength:8,submitLabel:'重置密码',danger:true,validate:value=>value.length>128?'密码需为 8-128 位':null},async new_password=>{if(new_password.length<8)throw new Error('密码需为 8-128 位');await api(`/users/${encodeURIComponent(user.id)}/reset-password`,{method:'POST',body:JSON.stringify({new_password})});toast('密码已重置');await refreshAfterSuccess(internalUsers,'密码已重置')});
}
function internalUserLifecycleConfirmation(user,{title,message,submitLabel},onSubmit){
  modal(title,`<form class="ops-form" id="internal-user-lifecycle-form"><div class="ops-notice">${esc(message)}。只允许删除已停用、无业务数据的测试账号。</div><div class="ops-field"><label for="internal-user-confirm-username">输入完整登录账号 *</label><input class="ops-input" id="internal-user-confirm-username" autocomplete="off" placeholder="${esc(user.username||'')}"></div><div class="ops-field"><label for="internal-user-lifecycle-reason">操作原因 *</label><textarea class="ops-textarea" id="internal-user-lifecycle-reason" maxlength="500"></textarea></div><label class="ops-check"><input type="checkbox" id="internal-user-second-confirm"> 我已二次确认操作对象和数据范围</label><div class="ops-actions"><button type="button" class="ops-btn" id="internal-user-lifecycle-cancel">取消</button><button class="ops-btn danger" id="internal-user-lifecycle-submit">${esc(submitLabel)}</button></div></form>`,()=>{
    const form=document.querySelector('#internal-user-lifecycle-form'),submit=document.querySelector('#internal-user-lifecycle-submit'),usernameInput=document.querySelector('#internal-user-confirm-username'),reasonInput=document.querySelector('#internal-user-lifecycle-reason');
    document.querySelector('#internal-user-lifecycle-cancel').onclick=closeModal;
    form.onsubmit=async event=>{event.preventDefault();const confirm_username=usernameInput.value.trim(),reason=reasonInput.value.trim();if(confirm_username!==user.username){toast('登录账号不一致，不能执行',true);usernameInput.focus();return}if(reason.length<2){toast('请填写至少 2 个字的操作原因',true);reasonInput.focus();return}if(!document.querySelector('#internal-user-second-confirm').checked){toast('请完成二次确认',true);return}submit.disabled=true;try{await onSubmit({confirm_username,reason});closeModalFor(form);await refreshAfterSuccess(internalUsers,'账号操作已完成')}catch(error){if(form.isConnected)submit.disabled=false;toast(error.message,true)}};
  });
}
function markInternalUserAsTest(user){
  internalUserLifecycleConfirmation(user,{title:'标记为测试账号',message:'系统会先检查该账号是否已停用且没有业务记录',submitLabel:'确认标记'},async payload=>{await api(`/users/${encodeURIComponent(user.id)}/mark-test`,{method:'POST',body:JSON.stringify(payload)});toast('已标记为测试账号')});
}
function deleteInternalTestUser(user){
  internalUserLifecycleConfirmation(user,{title:'删除内部测试账号',message:'删除后账号本身不可恢复，审计记录仍保留',submitLabel:'确认删除'},async payload=>{await api(`/users/${encodeURIComponent(user.id)}`,{method:'DELETE',body:JSON.stringify(payload)});toast('测试账号已删除')});
}
async function internalUsers(){
  const data=await api('/users'),users=data.filter(user=>!user.company_id&&internalUserRoles(user).length);
  const rows=users.map(user=>{const active=user.status==='ACTIVE',cleanup=!active?(user.is_test?` <button class="ops-btn danger" data-internal-delete="${esc(user.id)}">删除测试数据</button>`:` <button class="ops-btn" data-internal-mark-test="${esc(user.id)}">标记测试账号</button>`):'';return `<tr><td><b>${esc(user.display_name)}</b></td><td>${esc(user.username||'--')}${user.is_test?'<br><small>测试账号</small>':''}</td><td>${esc(INTERNAL_ROLE_LABEL[internalUserRoles(user)[0]]||'--')}</td><td>${badge(user.status)}</td><td><button class="ops-btn" data-internal-name="${esc(user.id)}">修改姓名</button> <button class="ops-btn" data-internal-role="${esc(user.id)}">编辑角色</button> <button class="ops-btn" data-internal-reset="${esc(user.id)}">重置密码</button> <button class="ops-btn ${active?'danger':'primary'}" data-internal-status="${esc(user.id)}:${active?'disable':'enable'}">${active?'停用':'启用'}</button>${cleanup}</td></tr>`});
  shell(`<div class="ops-page-actions"><button class="ops-btn" data-view="settings">返回设置</button><button class="ops-btn primary" id="new-internal-user">新建内部账号</button></div><section class="ops-card"><div class="ops-card-head"><div><h2>内部账号</h2><p>停用用于业务隔离；只有已停用、已标记且无业务数据的测试账号才能删除。</p></div></div>${table(['姓名','登录账号','角色','状态','操作'],rows)}</section>`);
  const byId=Object.fromEntries(users.map(user=>[user.id,user]));
  document.querySelector('#new-internal-user').onclick=internalUserModal;
  document.querySelectorAll('[data-internal-name]').forEach(button=>button.onclick=()=>internalDisplayNameModal(byId[button.dataset.internalName]));
  document.querySelectorAll('[data-internal-role]').forEach(button=>button.onclick=()=>internalRoleModal(byId[button.dataset.internalRole]));
  document.querySelectorAll('[data-internal-reset]').forEach(button=>button.onclick=()=>resetInternalUserPassword(byId[button.dataset.internalReset]));
  document.querySelectorAll('[data-internal-mark-test]').forEach(button=>button.onclick=()=>markInternalUserAsTest(byId[button.dataset.internalMarkTest]));
  document.querySelectorAll('[data-internal-delete]').forEach(button=>button.onclick=()=>deleteInternalTestUser(byId[button.dataset.internalDelete]));
  document.querySelectorAll('[data-internal-status]').forEach(button=>button.onclick=()=>{const [userId,action]=button.dataset.internalStatus.split(':');const user=byId[userId],enabling=action==='enable';actionForm({title:enabling?'启用内部账号':'停用内部账号',message:enabling?'启用后该账号可重新登录。':'停用后该账号的全部会话会立即失效。',submitLabel:enabling?'确认启用':'确认停用',danger:!enabling},async()=>{await api(`/users/${encodeURIComponent(user.id)}/${enabling?'enable':'disable'}`,{method:'POST'});toast(enabling?'账号已启用':'账号已停用');await internalUsers()})});
}
function platformSettingsContent(){return `<div class="ops-settings-grid"><button class="ops-setting-card" data-view="users"><i>${icon('users')}</i><b>内部账号</b><span>开通、角色调整、启停与测试数据清理</span></button><button class="ops-setting-card" data-view="calendar"><i>${icon('calendar')}</i><b>工作日历</b><span>维护法定节假日与调休单日例外</span></button><button class="ops-setting-card" data-view="companies"><i>${icon('building')}</i><b>加盟商治理</b><span>维护主体、服务区域与客资功能</span></button></div>`}
async function settings(){
  shell(`<section class="ops-card"><div class="ops-card-head"><div><h2>系统设置</h2><p>内部账号、工作日历和加盟商治理均在账号入口集中维护，不展示底层参数或旧版入口。</p></div></div>${platformSettingsContent()}</section>`);
}
async function account(){
  const accountName=S.me?.username||'当前账号';
  const identity=ROLE_IDENTITY_LABEL[primaryRole()]||'平台人员';
  const tool=(view,iconName,title,description)=>`<button class="ops-account-tool" data-account-tool="${esc(view)}" type="button"><i>${icon(iconName)}</i><span><b>${esc(title)}</b><small>${esc(description)}</small></span><em>${icon('chevron-right')}</em></button>`;
  const tools=[];
  const messages=(S.accountNotifications||[]).slice(0,3).map(item=>`<button class="ops-account-tool" data-account-message="${esc(item.id)}" type="button"><i>${icon('bell')}</i><span><b>${esc(item.title||'系统消息')}</b><small>${esc(item.body||'请查看相关业务')}</small></span><em>${icon('chevron-right')}</em></button>`).join('');
  const markAll=S.unreadNotifications?`<button class="ops-btn" id="account-messages-read-all" type="button">全部标为已读（${Number(S.unreadNotifications)}）</button>`:'';
  if(canOpenView('returns'))tools.push(tool('returns','rotate-ccw','异常处理','处理退回申诉与电话核验任务'));
  if(canOpenView('audit'))tools.push(tool('audit','search','操作日志','查看业务处理记录与通知异常'));
  const hasPassword=Boolean(S.me?.has_password);
  shell(`<section class="ops-account-page"><section class="ops-card ops-account-summary"><div class="ops-account-avatar">${icon('user')}</div><div><h2>${esc(accountName)}</h2><p>${esc(identity)} · ${esc(S.me?.company_name||'合家美宅平台')}</p></div></section><section class="ops-card"><div class="ops-card-head"><div><h3>安全与登录</h3><p>只保留账号维护所需的操作。</p></div></div><div class="ops-account-security-list"><button class="ops-security-action" id="account-username" type="button"><i>${icon('user')}</i><span><b>修改登录账号</b><small>修改后使用新账号登录</small></span><em>${icon('chevron-right')}</em></button><button class="ops-security-action" id="account-password" type="button"><i>${icon('key-round')}</i><span><b>${hasPassword?'修改登录密码':'设置备用登录密码'}</b><small>${hasPassword?'保存后其他设备自动退出':'公众号登录之外的备用方式'}</small></span><em>${icon('chevron-right')}</em></button></div><button class="ops-btn danger ops-account-logout" id="account-logout" type="button">${icon('log-out')}退出当前账号</button></section>${isSuperAdmin()?`<section class="ops-card"><div class="ops-card-head"><div><h3>平台设置</h3><p>设置直接在此进入，不再跳转到另一套设置页面。</p></div></div>${platformSettingsContent()}</section>`:''}${messages?`<section class="ops-card ops-account-tools"><div class="ops-card-head"><div><h3>消息提醒</h3><p>仅显示需要当前账号关注的最新消息。</p></div>${markAll}</div><div class="ops-account-tool-grid">${messages}</div></section>`:''}${tools.length?`<section class="ops-card ops-account-tools"><div class="ops-card-head"><div><h3>业务记录</h3><p>异常和日志集中在账号入口，避免干扰日常业务导航。</p></div></div><div class="ops-account-tool-grid">${tools.join('')}</div></section>`:''}</section>`);
  document.querySelector('#account-username').onclick=changeOwnUsername;
  document.querySelector('#account-password').onclick=changeOwnPassword;
  document.querySelector('#account-logout').onclick=async()=>{try{await api('/auth/logout',{method:'POST'});location.replace('/admin/')}catch(error){toast(`退出失败：${error.message}`,true)}};
  document.querySelectorAll('[data-account-tool]').forEach(button=>button.onclick=()=>go(button.dataset.accountTool));
  document.querySelector('#account-messages-read-all')?.addEventListener('click',async event=>{const button=event.currentTarget;button.disabled=true;try{await api('/notifications/read-all',{method:'POST'});S.unreadNotifications=0;S.accountNotifications=[];toast('全部消息已标为已读');account()}catch(error){button.disabled=false;toast(error.message,true)}});
  document.querySelectorAll('[data-account-message]').forEach(button=>button.onclick=async()=>{const item=(S.accountNotifications||[]).find(message=>message.id===button.dataset.accountMessage);if(!item)return;const wasUnread=!item.read_at;try{await api(`/notifications/${encodeURIComponent(item.id)}/read`,{method:'POST'})}catch(error){toast(`消息状态更新失败：${error.message}`,true);return}if(wasUnread)S.unreadNotifications=Math.max(0,S.unreadNotifications-1);const deepLink=String(item.deep_link||'');if(deepLink.startsWith('/admin/'))location.href=deepLink;else render()});
}
function changeOwnUsername(){
  const current=S.me?.username||'';
  modal('修改登录账号',`<form class="ops-form" id="own-username-form"><div class="ops-notice">修改后立即使用新登录账号；当前设备保持登录，原登录账号将不能再用于登录。</div><div class="ops-field"><label for="username-current-password">当前密码 *</label><input class="ops-input" id="username-current-password" type="password" autocomplete="current-password" minlength="8" maxlength="128" required></div><div class="ops-field"><label for="new-username">新登录账号 *</label><input class="ops-input" id="new-username" value="${esc(current)}" autocomplete="username" minlength="2" maxlength="64" required></div><div class="ops-actions"><button class="ops-btn" type="button" id="own-username-cancel">取消</button><button class="ops-btn primary" id="own-username-submit">保存登录账号</button></div></form>`,()=>{
    const form=document.querySelector('#own-username-form'),submit=document.querySelector('#own-username-submit');
    document.querySelector('#own-username-cancel').onclick=closeModal;
    form.onsubmit=async event=>{event.preventDefault();const current_password=document.querySelector('#username-current-password').value,username=document.querySelector('#new-username').value.trim();if(username.length<2){toast('登录账号至少 2 个字符',true);return}submit.disabled=true;try{await api('/auth/change-username',{method:'POST',body:JSON.stringify({current_password,username})})}catch(error){if(form.isConnected)submit.disabled=false;toast(error.message,true);return}const message='登录账号已更新';toast(message);if(closeModalFor(form))await refreshAfterSuccess(async()=>{S.me=await api('/auth/me');await account()},message)};
  });
}
function changeOwnPassword(){
  const hasPassword=Boolean(S.me?.has_password);
  modal(hasPassword?'修改登录密码':'设置备用登录密码',`<form class="ops-form" id="own-password-form"><div class="ops-notice">${hasPassword?'新密码只要求 8 至 128 位，不要求字符组合。保存后，本设备会保持登录，其他设备会自动退出。':'此密码作为公众号登录之外的备用方式，首次设置不需要当前密码。'}</div>${hasPassword?'<div class="ops-field"><label for="current-password">当前密码 *</label><input class="ops-input" id="current-password" type="password" autocomplete="current-password" minlength="8" maxlength="128" required></div>':''}<div class="ops-field"><label for="new-password">新密码 *</label><input class="ops-input" id="new-password" type="password" autocomplete="new-password" minlength="8" maxlength="128" required></div><div class="ops-field"><label for="confirm-password">确认新密码 *</label><input class="ops-input" id="confirm-password" type="password" autocomplete="new-password" minlength="8" maxlength="128" required></div><div class="ops-actions"><button class="ops-btn" type="button" id="own-password-cancel">取消</button><button class="ops-btn primary" id="own-password-submit">保存新密码</button></div></form>`,()=>{
    const form=document.querySelector('#own-password-form'),submit=document.querySelector('#own-password-submit');
    document.querySelector('#own-password-cancel').onclick=closeModal;
    form.onsubmit=async event=>{event.preventDefault();const current_password=document.querySelector('#current-password')?.value||null,new_password=document.querySelector('#new-password').value,confirm_password=document.querySelector('#confirm-password').value;if(new_password.length<8){toast('新密码至少 8 位',true);return}if(new_password!==confirm_password){toast('两次输入的新密码不一致',true);return}submit.disabled=true;try{await api('/auth/change-password',{method:'POST',body:JSON.stringify({current_password,new_password})})}catch(error){if(form.isConnected)submit.disabled=false;toast(error.message,true);return}const message=hasPassword?'密码已更新，其他设备已退出':'备用密码已设置';toast(message);if(closeModalFor(form))await refreshAfterSuccess(async()=>{S.me=await api('/auth/me');await account()},message)};
  });
}
async function companies(){
  const companyPage=await api(`/companies${qs({keyword:S.companyKeyword,status:S.companyLifecycleStatus,page:S.companyPage,page_size:20})}`);
  const companyBusinessSummary=company=>{const exceptions=company.exception_breakdown||{},received=company.received||{},statusButtons=Object.entries(received.by_status||{}).map(([status,count])=>`<button class="ops-company-link" data-company-received-status="${esc(company.id)}:${esc(status)}" type="button">${esc(label(status))} ${Number(count||0)}</button>`).join(' · ');return `<small><button class="ops-company-link" data-company-provided-total="${esc(company.id)}" type="button">供资 ${Number(company.provided?.total||0)}</button> · <button class="ops-company-link" data-company-received-total="${esc(company.id)}" type="button">接收 ${Number(received.total||0)}</button></small>${statusButtons?`<br><small>${statusButtons}</small>`:''}<br><small>拒绝领取 ${Number(exceptions.refused_claim||0)} · 发起退回 ${Number(exceptions.return_requested||0)} · 确认无效 ${Number(exceptions.confirmed_invalid||0)}</small>`};
  const companyRows=(companyPage.items||[]).map(company=>{const active=company.status==='ACTIVE',lifecycle=active?'disable':'enable',lifecycleLabel=active?'停用':'启用';return `<tr><td><button class="ops-company-link" data-company-detail="${esc(company.id)}" type="button">${esc(company.name)}</button><br><small>${esc(company.code)}${company.is_test?' · 测试主体':''}</small></td><td>${badge(company.status)}</td><td><b>${Number(company.points_balance??0).toLocaleString('zh-CN')} 分</b></td><td>${companyBusinessSummary(company)}</td><td>${esc(company.owner_name||'--')}</td><td><button class="ops-btn ${active?'danger':'primary'}" data-company-lifecycle="${esc(company.id)}:${lifecycle}">${lifecycleLabel}</button> <button class="ops-btn primary" data-company-accounts="${esc(company.id)}" data-company-name="${esc(company.name)}">账号与人员</button></td></tr>`});
  shell(`<section class="ops-card"><div class="ops-card-head"><div><h2>加盟商</h2><p>列表直接显示当前积分余额；点击加盟商名称可查看供资、接收、退回与服务区域。</p></div><button class="ops-btn primary" id="new-franchise-company" type="button">新建加盟商</button></div><form class="ops-filter-row" id="company-filter-form"><input class="ops-input" id="company-keyword" value="${esc(S.companyKeyword)}" placeholder="搜索公司名称或编号"><select class="ops-input" id="company-lifecycle-status"><option value="" ${S.companyLifecycleStatus===''?'selected':''}>全部状态</option><option value="ACTIVE" ${S.companyLifecycleStatus==='ACTIVE'?'selected':''}>正常</option><option value="PENDING" ${S.companyLifecycleStatus==='PENDING'?'selected':''}>待审核</option><option value="DISABLED" ${S.companyLifecycleStatus==='DISABLED'?'selected':''}>已停用</option></select><button class="ops-btn primary" type="submit">查询</button><button class="ops-btn" id="company-filter-reset" type="button">重置</button></form>${table(['加盟商','公司状态','当前积分','业务数据','负责人','操作'],companyRows)}${companyQueuePager(companyPage,'company-list',S.companyPage)}</section>`);
  document.querySelector('#company-filter-form').onsubmit=event=>{event.preventDefault();S.companyKeyword=document.querySelector('#company-keyword').value.trim();S.companyLifecycleStatus=document.querySelector('#company-lifecycle-status').value;S.companyPage=1;companies()};
  document.querySelector('#company-filter-reset').onclick=()=>{S.companyKeyword='';S.companyLifecycleStatus='';S.companyPage=1;companies()};
  document.querySelector('#new-franchise-company').onclick=openNewFranchiseCompany;
  bindCompanyQueuePager(companyPage,'company-list','companyPage');
  const companiesById=Object.fromEntries((companyPage.items||[]).map(company=>[company.id,company]));
  document.querySelectorAll('[data-company-detail]').forEach(button=>button.onclick=()=>companyDetail(companiesById[button.dataset.companyDetail]));
  document.querySelectorAll('[data-company-provided-total]').forEach(button=>button.onclick=()=>openCompanyProvidedLeads(button.dataset.companyProvidedTotal));
  document.querySelectorAll('[data-company-received-total]').forEach(button=>button.onclick=()=>companyAssignmentHistory(button.dataset.companyReceivedTotal,companiesById[button.dataset.companyReceivedTotal]?.name||'',null,1));
  document.querySelectorAll('[data-company-received-status]').forEach(button=>button.onclick=()=>{const [companyId,assignmentStatus]=button.dataset.companyReceivedStatus.split(':');companyAssignmentHistory(companyId,companiesById[companyId]?.name||'',assignmentStatus,1)});
  document.querySelectorAll('[data-company-accounts]').forEach(button=>button.onclick=()=>companyAccounts(button.dataset.companyAccounts,button.dataset.companyName));
  document.querySelectorAll('[data-company-lifecycle]').forEach(button=>button.onclick=()=>{const [companyId,action]=button.dataset.companyLifecycle.split(':');changeCompanyLifecycle(companiesById[companyId],action)});
}
function openCompanyProvidedLeads(companyId){S.leadSource='SUPPLIER_H5';S.leadSupplierCompanyId=companyId;S.leadSubmitterId='';S.leadPendingReason='';S.page=1;go('leads')}
async function companyAssignmentHistory(companyId,companyName,assignmentStatus=null,pageNo=1){
  const isCurrent=beginModalRequest();
  try{
    const result=await api(`/v1.2/companies/${encodeURIComponent(companyId)}/assignments${qs({assignment_status:assignmentStatus,page:pageNo,page_size:20})}`);
    if(!isCurrent())return;
    const rows=(result.items||[]).map((item,index)=>`<tr><td>${rowSequence(result,index)}</td><td>${esc(item.customer_name||'--')}<br><small>${esc(item.phone||item.phone_masked||'--')}</small></td><td>${badge(item.status)}</td><td>${esc(label(item.lead_status))}</td><td>${esc(item.current_follow_status?label(item.current_follow_status):'暂无')}</td><td>${fmt(item.assigned_at)}</td><td><button class="ops-btn" data-company-assignment-detail="${esc(item.lead_id)}" type="button">查看详情</button></td></tr>`);
    const totalPages=Math.max(1,Math.ceil(Number(result.total||0)/Number(result.page_size||20)));
    modal(`${companyName||result.company_name||'加盟商'} · ${assignmentStatus?label(assignmentStatus):'全部接收客资'}`,`${assignmentStatus?`<div class="ops-notice">已自动筛选：${esc(label(assignmentStatus))}</div>`:''}${table(['序号','客户','派发状态','客资状态','当前跟进','派发时间','操作'],rows)}<div class="ops-pager"><button class="ops-btn" id="company-assignment-prev" ${pageNo<=1?'disabled':''}>上一页</button><span>第 ${pageNo} / ${totalPages} 页，共 ${Number(result.total||0)} 条</span><button class="ops-btn" id="company-assignment-next" ${pageNo>=totalPages?'disabled':''}>下一页</button></div>`,()=>{
      document.querySelectorAll('[data-company-assignment-detail]').forEach(button=>button.onclick=()=>openLeadDetailForSource(button.dataset.companyAssignmentDetail,null));
      document.querySelector('#company-assignment-prev')?.addEventListener('click',()=>companyAssignmentHistory(companyId,companyName,assignmentStatus,pageNo-1));
      document.querySelector('#company-assignment-next')?.addEventListener('click',()=>companyAssignmentHistory(companyId,companyName,assignmentStatus,pageNo+1));
    });
  }catch(error){toast(error.message,true)}
}
function changeCompanyLifecycle(company,action){
  if(!company)return;
  const enabling=action==='enable';
  actionForm({title:enabling?'启用加盟商':'停用加盟商',message:enabling?'启用后，该加盟商及其人员可重新登录和接收业务。':'停用只负责业务隔离，账号、微信绑定和历史业务数据会保留。',labelText:'操作原因',required:true,minLength:2,submitLabel:enabling?'确认启用':'确认停用',danger:!enabling},async reason=>{
    await api(`/companies/${encodeURIComponent(company.id)}`,{method:'PATCH',body:JSON.stringify({status:enabling?'ACTIVE':'DISABLED',reason})});
    toast(enabling?'加盟商已启用':'加盟商已停用');
    await companies();
  });
}
const COMPANY_CAPABILITY_LABEL={LEAD_SUPPLIER:'提供客资',LEAD_RECEIVER:'接收客资'};
const INVITE_STATUS_LABEL={ACTIVE:'等待绑定',USED:'已完成绑定',REVOKED:'已撤销',EXPIRED:'已过期'};
function companyCapabilitySetting(capabilities,code,compact=false){
  const item=(capabilities||[]).find(capability=>capability.capability_code===code);
  const active=Boolean(item?.active&&item?.review_status==='APPROVED');
  return `<article class="ops-company-capability"><div><small>${esc(COMPANY_CAPABILITY_LABEL[code])}</small><b>${active?'已开通':'未开通'}</b>${compact?'':`<p>${active?'加盟商当前可以使用此功能。':'加盟商当前无法使用此功能。'}</p>`}</div><button class="ops-btn ${active?'danger':'primary'}" data-company-capability="${esc(code)}" data-capability-active="${active?'true':'false'}" type="button">${active?'停用':'开通'}</button></article>`;
}
async function companyDetail(company){
  if(!company)return;
  const isCurrent=beginModalRequest();
  const [profile,inviteResult]=await Promise.all([
    api(`/v1.2/admin/companies/${encodeURIComponent(company.id)}/profile`),
    api(`/auth/companies/${encodeURIComponent(company.id)}/invites`),
  ]);
  if(!isCurrent())return;
  const detail=profile.company;
  const companyRecord={...company,...detail};
  const areas=(profile.service_areas||[]).filter(item=>item.active&&item.review_status==='APPROVED');
  const activeInvite=(inviteResult.items||[]).find(item=>item.status==='ACTIVE');
  const displayedAreas=areas.slice(0,6);
  const areaCards=displayedAreas.length?displayedAreas.map(item=>`<span class="ops-area-chip">${esc(item.region_name||recordCode(item.region_code,'区域'))}</span>`).join(''):'<span class="ops-muted">尚未配置服务区域</span>';
  const remainingAreaCount=Math.max(0,areas.length-displayedAreas.length);
  const allAreaNames=areas.map(item=>item.region_name||recordCode(item.region_code,'区域')).join('、');
  const inviteRows=(inviteResult.items||[]).slice(0,4).map(item=>`<tr><td>${badge(item.status)}<br><small>${fmt(item.created_at)}</small></td><td>${esc(item.invitee_name||detail.owner_name||'未记录')}</td><td>${fmt(item.expires_at)}</td><td>${item.status==='ACTIVE'?`<button class="ops-btn danger" data-invite-revoke="${esc(item.id)}">撤销</button>`:esc(item.used_by_name||'--')}</td></tr>`);
  const inviteNotice=detail.wechat_bound?'负责人已完成微信绑定。':activeInvite?`邀请已发起，有效至 ${fmt(activeInvite.expires_at)}。`:'尚未发起负责人绑定。';
  const areaDetail=remainingAreaCount?`<details class="ops-company-inline-details"><summary>另有 ${remainingAreaCount} 个区域</summary><p>${esc(allAreaNames)}</p></details>`:'';
  const inviteHistory=inviteRows.length?`<details class="ops-company-invite-history"><summary>最近 ${inviteRows.length} 条绑定记录（最多显示 4 条）</summary>${table(['状态 / 发起时间','受邀人','有效期','操作'],inviteRows)}</details>`:'';
  const exceptions=companyRecord.exception_breakdown||{};
  const businessFacts=`<div class="ops-company-facts"><div><small>供资总数</small><button class="ops-company-link" data-company-provided-total="${esc(detail.id)}" type="button">${Number(companyRecord.provided?.total||0)} 条，查看明细</button></div><div><small>接收总数</small><b>${Number(companyRecord.received?.total||0)}</b></div><div><small>拒绝领取</small><b>${Number(exceptions.refused_claim||0)}</b></div><div><small>发起退回 / 确认无效</small><b>${Number(exceptions.return_requested||0)} / ${Number(exceptions.confirmed_invalid||0)}</b></div></div>`;
  const lifecycleActions=`${detail.status==='ACTIVE'?`<button class="ops-btn danger" data-company-disable="${esc(detail.id)}">停用主体</button>`:''}${detail.status==='DISABLED'?`<button class="ops-btn primary" data-company-enable="${esc(detail.id)}">重新启用</button>`:''}${detail.status==='DISABLED'&&detail.wechat_bound?`<button class="ops-btn danger" data-company-wechat-unbind="${esc(detail.id)}">解绑负责人微信</button>`:''}${detail.status==='DISABLED'&&!detail.is_test&&isSuperAdmin()?`<button class="ops-btn danger" data-company-mark-test="${esc(detail.id)}">标记历史测试数据</button>`:''}${detail.status==='DISABLED'&&detail.is_test&&isSuperAdmin()?`<button class="ops-btn danger" data-company-test-delete="${esc(detail.id)}">删除测试主体</button>`:''}`;
  modal(`${detail.name} · 加盟商信息`,`<section class="ops-company-detail ops-company-detail-compact"><div class="ops-company-facts"><div><small>主体状态</small><b>${esc(label(detail.status))}</b></div><div><small>负责人</small><b>${esc(detail.owner_name||'未填写')}</b></div><div><small>联系电话</small><b>${esc(detail.contact_phone_masked||'未填写')}</b></div><div><small>负责人微信</small><b>${detail.wechat_bound?'已绑定':'待绑定'}</b></div></div>${businessFacts}<div class="ops-company-summary-grid"><section class="ops-company-section"><div class="ops-company-section-head"><div><h3>服务区域</h3><p>已开通 ${areas.length} 个城市/区县/乡镇。</p></div><button class="ops-btn" data-company-service-areas="${esc(detail.id)}">编辑区域</button></div><div class="ops-area-chips">${areaCards}</div>${areaDetail}</section><section class="ops-company-section"><div class="ops-company-section-head"><div><h3>客资功能</h3><p>平台统一配置。</p></div></div><div class="ops-company-capabilities compact">${['LEAD_RECEIVER','LEAD_SUPPLIER'].map(code=>companyCapabilitySetting(profile.capabilities,code,true)).join('')}</div></section></div><section class="ops-company-binding-compact"><div><small>负责人绑定</small><b>${detail.wechat_bound?'已完成绑定':'等待负责人绑定'}</b><p>${esc(inviteNotice)}</p></div><div class="ops-actions">${!detail.wechat_bound&&detail.status==='ACTIVE'?`<button class="ops-btn primary" data-invite-create="${esc(detail.id)}">发起绑定</button>`:''}<button class="ops-btn" data-company-detail-accounts="${esc(detail.id)}">账号与人员</button><button class="ops-btn" data-company-detail-edit="${esc(detail.id)}">编辑资料</button></div></section>${lifecycleActions?`<section class="ops-company-binding-compact"><div><small>主体治理</small><b>停用只负责业务隔离</b><p>测试主体可永久清理其客资、派发、积分和业务历史，不因已产生业务或已派发而阻止删除。</p></div><div class="ops-actions">${lifecycleActions}</div></section>`:''}${inviteHistory}</section>`,()=>{
    document.querySelectorAll('[data-company-capability]').forEach(button=>button.onclick=()=>configureCompanyCapability(detail.id,button.dataset.companyCapability,button.dataset.capabilityActive!=='true',companyRecord));
    document.querySelector('[data-company-provided-total]')?.addEventListener('click',()=>openCompanyProvidedLeads(detail.id));
    document.querySelector('[data-invite-create]')?.addEventListener('click',()=>createCompanyInvite(detail));
    document.querySelectorAll('[data-invite-revoke]').forEach(button=>button.onclick=()=>revokeCompanyInvite(button.dataset.inviteRevoke,companyRecord));
    document.querySelector('[data-company-detail-edit]')?.addEventListener('click',()=>editCompany(companyRecord));
    document.querySelector('[data-company-service-areas]')?.addEventListener('click',()=>editCompanyServiceAreas(companyRecord,profile));
    document.querySelector('[data-company-detail-accounts]')?.addEventListener('click',()=>companyAccounts(detail.id,detail.name));
    document.querySelector('[data-company-disable]')?.addEventListener('click',()=>disableCompany(detail));
    document.querySelector('[data-company-enable]')?.addEventListener('click',()=>enableCompany(detail));
    document.querySelector('[data-company-wechat-unbind]')?.addEventListener('click',()=>unbindCompanyOwnerWechat(detail));
    document.querySelector('[data-company-mark-test]')?.addEventListener('click',()=>markCompanyAsTest(detail));
    document.querySelector('[data-company-test-delete]')?.addEventListener('click',()=>deleteTestCompany(detail));
  });
}
function disableCompany(company){
  actionForm({title:'停用加盟商主体',message:'停用只负责业务隔离：该加盟商无法继续登录和接收业务，但账号、微信绑定、客资、积分与审计历史都会保留。',labelText:'停用原因',required:true,minLength:2,submitLabel:'确认停用',danger:true},async reason=>{
    await api(`/companies/${encodeURIComponent(company.id)}`,{method:'PATCH',body:JSON.stringify({status:'DISABLED',reason})});
    toast('加盟商已停用');
    await companies();
  });
}
function enableCompany(company){
  actionForm({title:'重新启用加盟商',message:'启用后该加盟商可恢复登录和业务处理。如果负责人微信已解绑，启用后再发起新的绑定邀请。',labelText:'启用原因',required:true,minLength:2,submitLabel:'确认启用'},async reason=>{
    await api(`/companies/${encodeURIComponent(company.id)}`,{method:'PATCH',body:JSON.stringify({status:'ACTIVE',reason})});
    toast('加盟商已重新启用');
    await companies();
  });
}
function companyLifecycleConfirmation({title,message,company,submitLabel,preview=null,confirmPhrase=null},onSubmit){
  const counts=preview?.counts||{},cross=preview?.cross_company_impact||{};
  const previewHtml=preview?`<div class="ops-notice">影响预览：客资 ${Number(counts.leads||0)} 条、派发 ${Number(counts.assignments||0)} 条、退回 ${Number(counts.returns||0)} 条、积分流水 ${Number(counts.points_ledgers||0)} 条、证据文件 ${Number(counts.evidence_files||0)} 份；将影响其他公司 ${Number(cross.companies||0)} 家、其他积分账户 ${Number(cross.points_accounts||0)} 个。</div>`:'';
  const phraseHtml=confirmPhrase?`<div class="ops-field"><label for="company-lifecycle-phrase">输入固定确认短语“${esc(confirmPhrase)}” *</label><input class="ops-input" id="company-lifecycle-phrase" autocomplete="off"></div>`:'';
  modal(title,`<form class="ops-form" id="company-lifecycle-form"><div class="ops-notice">${esc(message)}</div>${previewHtml}<div class="ops-field"><label for="company-lifecycle-name">输入加盟商完整名称 *</label><input class="ops-input" id="company-lifecycle-name" autocomplete="off" placeholder="${esc(company.name)}"></div>${phraseHtml}<div class="ops-field"><label for="company-lifecycle-reason">操作原因 *</label><textarea class="ops-textarea" id="company-lifecycle-reason" maxlength="500"></textarea></div><div class="ops-actions"><button type="button" class="ops-btn" id="company-lifecycle-cancel">取消</button><button class="ops-btn danger" id="company-lifecycle-submit">${esc(submitLabel)}</button></div></form>`,()=>{
    const form=document.querySelector('#company-lifecycle-form'),nameInput=document.querySelector('#company-lifecycle-name'),phraseInput=document.querySelector('#company-lifecycle-phrase'),reasonInput=document.querySelector('#company-lifecycle-reason'),submit=document.querySelector('#company-lifecycle-submit');
    document.querySelector('#company-lifecycle-cancel').onclick=closeModal;
    form.onsubmit=async event=>{event.preventDefault();const confirm_name=nameInput.value.trim(),confirm_phrase=phraseInput?.value.trim()||'',reason=reasonInput.value.trim();if(confirm_name!==company.name){toast('名称不一致，不能执行',true);nameInput.focus();return}if(confirmPhrase&&confirm_phrase!==confirmPhrase){toast('固定确认短语不一致，请重新输入',true);phraseInput.focus();return}if(reason.length<2){toast('请填写至少 2 个字的操作原因',true);reasonInput.focus();return}submit.disabled=true;try{await onSubmit({confirm_name,reason,...(confirmPhrase?{confirm_phrase,scope_token:preview.scope_token}:{})});closeModal()}catch(error){submit.disabled=false;toast(error.message,true)}};
    nameInput.focus();
  });
}
function unbindCompanyOwnerWechat(company){
  companyLifecycleConfirmation({title:'解绑负责人微信',message:'解绑后旧负责人微信登录会立即失效。请先确保主体已停用；之后重新启用，再发起新的负责人绑定。业务历史不会删除。',company,submitLabel:'确认解绑'},async body=>{
    await api(`/companies/${encodeURIComponent(company.id)}/wechat-binding/unbind`,{method:'POST',body:JSON.stringify(body)});
    toast('负责人微信已解绑，请重新发起绑定');
    await companies();
  });
}
async function markCompanyAsTest(company){
  const isCurrent=beginModalRequest();
  try{
    const preview=await api(`/companies/${encodeURIComponent(company.id)}/purge-preview`);
    if(!isCurrent())return;
    companyLifecycleConfirmation({title:'标记历史测试数据',message:'标记后可删除该主体及其相关测试业务，不因已产生客资或已完成派发而阻止。',company,submitLabel:'确认标记为测试',preview,confirmPhrase:'永久删除测试数据'},async body=>{
      await api(`/companies/${encodeURIComponent(company.id)}/mark-test`,{method:'POST',body:JSON.stringify(body)});
      toast('已标记为测试主体，可继续执行删除');
      await companies();
    });
  }catch(error){toast(error.message,true)}
}
function deleteTestCompany(company){
  companyLifecycleConfirmation({title:'删除测试数据',message:'该操作会永久删除测试主体及其客资、派发、退回、跟进、奖励、业务消息、积分账户、充值与积分流水；已派给其他加盟商的测试客资也会一并清理。原成员账号会停用并解除公司与微信关联，审计记录会保留。',company,submitLabel:'确认永久删除'},async body=>{
    await api(`/companies/${encodeURIComponent(company.id)}`,{method:'DELETE',body:JSON.stringify(body)});
    toast('测试加盟商已删除');
    await companies();
  });
}
function configureCompanyCapability(companyId,capabilityCode,active,company){
  const name=COMPANY_CAPABILITY_LABEL[capabilityCode]||'客资功能';
  actionForm({title:active?`开通${name}`:`停用${name}`,message:active?'开通后加盟商可以立即使用该功能。':'停用后加盟商无法继续使用该功能，不影响已留存业务记录。',labelText:'配置说明',value:active?'平台开通':'平台停用',submitLabel:active?'确认开通':'确认停用',danger:!active},async(note,isCurrent)=>{
    await api(`/v1.2/admin/companies/${encodeURIComponent(companyId)}/capabilities/${encodeURIComponent(capabilityCode)}`,{method:'PUT',body:JSON.stringify({active,note:note||null})});
    toast(`${name}已${active?'开通':'停用'}`);
    if(isCurrent())await refreshAfterSuccess(()=>companyDetail(company),`${name}已${active?'开通':'停用'}`);
  });
}
function copyText(text,success){
  if(!navigator.clipboard){toast('浏览器不支持自动复制，请手动复制',true);return}
  navigator.clipboard.writeText(text).then(()=>toast(success)).catch(()=>toast('浏览器不支持自动复制，请手动复制',true));
}
function createCompanyInvite(company){
  actionForm({title:'发起负责人绑定',message:'系统会生成一次性邀请链接。请通过微信或其他已确认渠道发送给负责人；平台不会在未绑定前自动发送消息。',labelText:'邀请有效期（小时）',value:'72',inputType:'number',submitLabel:'生成邀请链接',lockOnSubmit:true,validate:value=>{const hours=Number(value);return Number.isInteger(hours)&&hours>=1&&hours<=720?'':'请输入 1 到 720 小时'}} ,async(raw,_isCurrent,unlock)=>{
    const invitation=await api(`/auth/companies/${encodeURIComponent(company.id)}/invites`,{method:'POST',body:JSON.stringify({expires_hours:Number(raw)})});
    unlock();
    showCompanyInvite(invitation,company);
    return false;
  });
}
function showCompanyInvite(invitation,company){
  modal('邀请链接已生成',`<div class="ops-notice">链接仅在本窗口展示一次。请复制后通过已确认的渠道发送给负责人；负责人打开后确认微信授权即可完成绑定。</div><div class="ops-detail-grid"><div class="ops-detail"><small>加盟商</small><b>${esc(invitation.company_name)}</b></div><div class="ops-detail"><small>负责人</small><b>${esc(invitation.owner_name||'待确认')}</b></div><div class="ops-detail"><small>有效期至</small><b>${fmt(invitation.expires_at)}</b></div></div><div class="ops-field"><label for="company-invite-link">邀请链接</label><textarea class="ops-textarea ops-copy-text" id="company-invite-link" readonly>${esc(invitation.url)}</textarea></div><div class="ops-field"><label for="company-invite-copy">邀请内容</label><textarea class="ops-textarea ops-copy-text" id="company-invite-copy" readonly>${esc(invitation.copy_text)}</textarea></div><div class="ops-actions"><button class="ops-btn" id="copy-company-invite-link">复制链接</button><button class="ops-btn primary" id="copy-company-invite-text">复制邀请内容</button><button class="ops-btn" id="company-invite-close">完成</button></div>`,()=>{
    document.querySelector('#copy-company-invite-link').onclick=()=>copyText(invitation.url,'邀请链接已复制');
    document.querySelector('#copy-company-invite-text').onclick=()=>copyText(invitation.copy_text,'邀请内容已复制');
    document.querySelector('#company-invite-close').onclick=()=>{closeModal();companyDetail(company)};
  });
}
function revokeCompanyInvite(inviteId,company){
  actionForm({title:'撤销负责人邀请',message:'撤销后该邀请链接将立即失效，需重新发起绑定时可生成新链接。',labelText:'撤销说明',value:'负责人信息变更',submitLabel:'确认撤销',danger:true},async(_note,isCurrent)=>{
    await api(`/auth/invites/${encodeURIComponent(inviteId)}/revoke`,{method:'POST'});
    toast('负责人邀请已撤销');
    if(isCurrent())await refreshAfterSuccess(()=>companyDetail(company),'负责人邀请已撤销');
  });
}
function serviceRegionBuilderMarkup(prefix,provinces){return `<div class="ops-region-builder" id="${prefix}-region-builder"><div class="ops-field"><label for="${prefix}-region-search">搜索服务区域</label><input class="ops-input" id="${prefix}-region-search" maxlength="64" placeholder="输入城市、区县或乡镇/街道"><div class="ops-actions ops-region-actions" id="${prefix}-region-search-results"></div><small class="ops-muted">点击搜索结果即可直接加入已选区域。</small></div><div class="ops-row"><div class="ops-field"><label for="${prefix}-province">省份</label><select class="ops-input" id="${prefix}-province"><option value="">请选择省份</option>${provinces.map(item=>`<option value="${esc(item.code)}">${esc(item.name)}</option>`).join('')}</select></div><div class="ops-field"><label for="${prefix}-city">城市</label><select class="ops-input" id="${prefix}-city" disabled><option value="">请先选省份</option></select></div></div><div class="ops-row"><div class="ops-field"><label for="${prefix}-district">区/县</label><select class="ops-input" id="${prefix}-district" disabled><option value="">可选</option></select></div><div class="ops-field"><label for="${prefix}-township">乡镇/街道</label><select class="ops-input" id="${prefix}-township" disabled><option value="">可选</option></select></div></div><div class="ops-actions ops-region-actions"><button class="ops-btn primary" id="${prefix}-select-national-cities" type="button">全选全国城市</button><button class="ops-btn" id="${prefix}-clear-regions" type="button">清空已选区域</button><button class="ops-btn" id="${prefix}-select-province-cities" type="button">全选当前省城市</button><button class="ops-btn" id="${prefix}-select-city-districts" type="button">全选当前市区县</button><button class="ops-btn" id="${prefix}-add-city" type="button">添加整市</button><button class="ops-btn" id="${prefix}-add-district" type="button">添加区县</button><button class="ops-btn" id="${prefix}-add-township" type="button">添加乡镇</button></div><div class="ops-field"><label>已选服务区域</label><div class="ops-area-chips" id="${prefix}-selected-regions"></div></div><div class="ops-field"><label for="${prefix}-primary-city">主要城市 *</label><select class="ops-input" id="${prefix}-primary-city"></select><small class="ops-muted">可选择城市或区县（含县级市）作为主要城市；主要城市仍用于主体归属。已选区域可逐个移除或清空。</small></div></div>`}
function bindServiceRegionBuilder(prefix,cities,initialAreas=[]){
  const selected=new Map(initialAreas.filter(item=>item.active!==false&&item.review_status!=='REJECTED').map(item=>[item.region_code,{code:item.region_code,label:item.region_name||recordCode(item.region_code,'区域'),level:item.region_level||'DISTRICT'}]));
  const provinces=[...new Map(cities.map(city=>[city.province_code,{code:city.province_code,name:city.province_name}])).values()];
  const regionSearch=document.querySelector(`#${prefix}-region-search`),searchResultsRoot=document.querySelector(`#${prefix}-region-search-results`),province=document.querySelector(`#${prefix}-province`),city=document.querySelector(`#${prefix}-city`),district=document.querySelector(`#${prefix}-district`),township=document.querySelector(`#${prefix}-township`),selectedRoot=document.querySelector(`#${prefix}-selected-regions`),primary=document.querySelector(`#${prefix}-primary-city`);
  const currentCity=()=>cities.find(item=>item.code===city.value);
  const currentDistrict=()=>currentCity()?.districts?.find(item=>item.code===district.value);
  const cityForRegion=item=>{if(!item?.code)return null;if(item.level==='CITY')return cities.find(cityItem=>cityItem.code===item.code)||null;return cities.find(cityItem=>(cityItem.districts||[]).some(districtItem=>districtItem.code===item.code||(item.level==='TOWNSHIP'&&item.code.startsWith(districtItem.code))))||null};
  const initialPrimaryArea=initialAreas.find(item=>item.is_primary_city&&item.active!==false);
  const initialPrimaryItem=selected.get(initialPrimaryArea?.region_code);
  let primaryCityCode=initialPrimaryItem&&['CITY','DISTRICT'].includes(initialPrimaryItem.level)?initialPrimaryItem.code:cityForRegion(initialPrimaryItem)?.code||'';
  const add=item=>{if(!item?.code)return;selected.set(item.code,item);renderSelected()};
  const addMany=items=>{items.forEach(item=>selected.set(item.code,item));renderSelected()};
  const renderSelected=()=>{
    const primaryEntries=[...new Map(Array.from(selected.values()).filter(item=>item.level==='CITY'||item.level==='DISTRICT').map(item=>[item.code,{code:item.code,label:item.label,level:item.level}])).values()];
    if(!primaryEntries.some(item=>item.code===primaryCityCode))primaryCityCode=primaryEntries[0]?.code||'';
    const nationwide=cities.length>0&&cities.every(item=>selected.has(item.code));
    const summary=nationwide?`已覆盖全国 ${cities.length} 个城市`:`已选择 ${selected.size} 个服务区域`;
    const chips=Array.from(selected.values()).map(item=>`<button class="ops-area-chip" data-remove-region="${esc(item.code)}" type="button">${esc(item.label)}（移除）</button>`).join('');
    zsSetSafeHtml(selectedRoot,selected.size?`<details ${selected.size<=20?'open':''}><summary>${esc(summary)} · 查看与移除</summary><div class="ops-area-chips">${chips}</div></details>`:'<span class="ops-muted">尚未选择服务区域</span>');
    replacePlatformSelectOptions(primary,primaryEntries,primaryCityCode,'','请先添加服务区域');
    primary.value=primaryCityCode;
    selectedRoot.querySelectorAll('[data-remove-region]').forEach(button=>button.onclick=()=>{selected.delete(button.dataset.removeRegion);renderSelected()});
  };
  let searchTimer=null,searchResults=[],searchRequestSequence=0;
  regionSearch.oninput=()=>{clearTimeout(searchTimer);const requestSequence=++searchRequestSequence,keyword=regionSearch.value.trim();if(!keyword){searchResults=[];zsSetSafeHtml(searchResultsRoot,'');return}searchTimer=setTimeout(async()=>{try{const items=await api(`/master-data/regions/search?keyword=${encodeURIComponent(keyword)}&limit=30`);if(requestSequence!==searchRequestSequence)return;searchResults=items.filter(item=>['CITY','DISTRICT','TOWNSHIP'].includes(item.level));zsSetSafeHtml(searchResultsRoot,searchResults.length?searchResults.map(item=>`<button class="ops-btn" type="button" data-add-region="${esc(item.code)}">${esc(item.path_label||item.name)}</button>`).join(''):'<span class="ops-muted">未找到可添加的服务区域</span>');searchResultsRoot.querySelectorAll('[data-add-region]').forEach(button=>button.onclick=()=>{const item=searchResults.find(candidate=>candidate.code===button.dataset.addRegion);if(item)add({code:item.code,label:item.path_label||item.name,level:item.level})})}catch(error){if(requestSequence!==searchRequestSequence)return;zsSetSafeHtml(searchResultsRoot,'');toast(error.message,true)}},250)};
  province.onchange=()=>{const options=cities.filter(item=>item.province_code===province.value);replacePlatformSelectOptions(city,options,'','','请选择城市');city.disabled=!province.value;replacePlatformSelectOptions(district,[],'','','可选');district.disabled=true;replacePlatformSelectOptions(township,[],'','','可选');township.disabled=true};
  city.onchange=()=>{const options=currentCity()?.districts||[];replacePlatformSelectOptions(district,options,'','','可选区/县');district.disabled=!city.value;replacePlatformSelectOptions(township,[],'','','可选乡镇/街道');township.disabled=true};
  district.onchange=async()=>{replacePlatformSelectOptions(township,[],'','','加载中…');township.disabled=true;if(!district.value)return;try{const items=await api(`/master-data/regions?parent_code=${encodeURIComponent(district.value)}&level=TOWNSHIP`);replacePlatformSelectOptions(township,items,'','','可选乡镇/街道');township.disabled=!items.length}catch(error){replacePlatformSelectOptions(township,[],'','','乡镇数据加载失败');toast(error.message,true)}};
  document.querySelector(`#${prefix}-select-national-cities`).onclick=()=>{if(!cities.length){toast('暂无可选择的城市，请刷新后重试',true);return}addMany(cities.map(item=>({code:item.code,label:item.option_name,level:'CITY'})))};
  document.querySelector(`#${prefix}-clear-regions`).onclick=()=>{selected.clear();renderSelected()};
  document.querySelector(`#${prefix}-select-province-cities`).onclick=()=>{const items=cities.filter(item=>item.province_code===province.value);if(!items.length){toast('请先选择省份',true);return}addMany(items.map(item=>({code:item.code,label:item.option_name,level:'CITY'})))};
  document.querySelector(`#${prefix}-select-city-districts`).onclick=()=>{const cityItem=currentCity(),items=currentCity()?.districts||[];if(!cityItem){toast('请先选择城市',true);return}addMany(items.map(item=>({code:item.code,label:`${cityItem.option_name} · ${item.name}`,level:'DISTRICT'})))};
  document.querySelector(`#${prefix}-add-city`).onclick=()=>{const item=currentCity();if(!item){toast('请先选择城市',true);return}add({code:item.code,label:item.option_name,level:'CITY'})};
  document.querySelector(`#${prefix}-add-district`).onclick=()=>{const item=currentDistrict(),parent=currentCity();if(!item||!parent){toast('请先选择区/县',true);return}add({code:item.code,label:`${parent.option_name} · ${item.name}`,level:'DISTRICT'})};
  document.querySelector(`#${prefix}-add-township`).onclick=()=>{const option=township.selectedOptions[0],parent=currentCity(),districtItem=currentDistrict();if(!option?.value||!parent||!districtItem){toast('请先选择乡镇/街道',true);return}add({code:option.value,label:`${parent.option_name} · ${districtItem.name} · ${option.textContent}`,level:'TOWNSHIP'})};
  primary.onchange=()=>{primaryCityCode=primary.value};
  renderSelected();
  return {regionCodes:()=>Array.from(selected.keys()),primaryCityCode:()=>primaryCityCode};
}
async function openNewFranchiseCompany(){
  const isCurrent=beginModalRequest();
  const cities=await platformCities();
  if(!isCurrent())return;
  const provinces=[...new Map(cities.map(city=>[city.province_code,{code:city.province_code,name:city.province_name}])).values()];
  modal('新建加盟商主体',`<form class="ops-form" id="new-franchise-form"><div class="ops-notice">可跨省市重复添加整市、区县和乡镇/街道。创建完成后立即开通接单资格，并同步到加盟商 H5。</div><div class="ops-field"><label for="new-franchise-name">加盟商名称 *</label><input class="ops-input" id="new-franchise-name" maxlength="128" placeholder="例如：北京合家美宅"></div><div class="ops-field"><label for="new-franchise-owner">负责人姓名</label><input class="ops-input" id="new-franchise-owner" maxlength="64" placeholder="例如：北京负责人"></div><div class="ops-field"><label for="new-franchise-phone">联系电话</label><input class="ops-input" id="new-franchise-phone" inputmode="tel" maxlength="32"></div><label class="ops-check"><input type="checkbox" id="new-franchise-is-test"> 测试主体</label><div class="ops-field"><label>服务范围 *</label>${serviceRegionBuilderMarkup('new-franchise',provinces)}</div><div class="ops-field"><label for="new-franchise-notes">备注</label><textarea class="ops-textarea" id="new-franchise-notes" maxlength="500" placeholder="可记录签约或交接说明"></textarea></div><div class="ops-actions"><button class="ops-btn" type="button" id="new-franchise-cancel">取消</button><button class="ops-btn primary" type="submit">创建并开通</button></div></form>`,()=>{
    const form=document.querySelector('#new-franchise-form'),submit=document.querySelector('#new-franchise-form button[type="submit"]');
    document.querySelector('#new-franchise-cancel').onclick=closeModal;
    const regionBuilder=bindServiceRegionBuilder('new-franchise',cities);
    form.onsubmit=async event=>{event.preventDefault();const name=document.querySelector('#new-franchise-name').value.trim(),primary_city_code=regionBuilder.primaryCityCode(),region_codes=regionBuilder.regionCodes();if(name.length<2||!primary_city_code||!region_codes.length){toast('请填写加盟商名称并至少添加一个主要城市',true);return}submit.disabled=true;try{const company=await api('/companies/simple',{method:'POST',body:JSON.stringify({name,owner_name:document.querySelector('#new-franchise-owner').value.trim()||null,contact_phone:document.querySelector('#new-franchise-phone').value.trim()||null,primary_city_code,district_codes:[],region_codes,serve_all_districts:false,is_test:document.querySelector('#new-franchise-is-test').checked,notes:document.querySelector('#new-franchise-notes').value.trim()||null})});const isCurrent=closeModalFor(form)?beginModalRequest():()=>false;const message=`${company.name} 已创建，所选区域与接收客资已开通；请发起负责人绑定`;toast(message);await refreshAfterSuccess(companies,message);if(isCurrent())await refreshAfterSuccess(()=>companyDetail(company),message)}catch(error){if(form.isConnected)submit.disabled=false;toast(error.message,true)}};
  });
}
async function editCompanyServiceAreas(company,profile){
  const isCurrent=beginModalRequest();
  const cities=await platformCities();
  if(!isCurrent())return;
  const provinces=[...new Map(cities.map(city=>[city.province_code,{code:city.province_code,name:city.province_name}])).values()];
  const activeAreas=(profile.service_areas||[]).filter(item=>item.active&&item.review_status==='APPROVED');
  modal(`编辑${company.name}服务区域`,`<form class="ops-form" id="company-service-area-form"><div class="ops-notice">可跨省市添加整市、区县或乡镇/街道。保存后立即用于客资候选匹配，历史流转记录不会删除。</div>${serviceRegionBuilderMarkup('company-service-area',provinces)}<div class="ops-actions"><button class="ops-btn" id="company-service-area-cancel" type="button">取消</button><button class="ops-btn primary" id="company-service-area-submit">保存服务区域</button></div></form>`,()=>{
    const form=document.querySelector('#company-service-area-form'),submit=document.querySelector('#company-service-area-submit'),builder=bindServiceRegionBuilder('company-service-area',cities,activeAreas);
    document.querySelector('#company-service-area-cancel').onclick=()=>companyDetail(company);
    form.onsubmit=async event=>{event.preventDefault();const region_codes=builder.regionCodes(),primary_city_code=builder.primaryCityCode();if(!region_codes.length||!primary_city_code){toast('至少保留一个主要城市',true);return}submit.disabled=true;try{await api(`/v1.2/admin/companies/${encodeURIComponent(company.id)}/service-areas`,{method:'PUT',body:JSON.stringify({region_codes,primary_city_code})});toast('服务区域已更新');if(closeModalFor(form))await refreshAfterSuccess(()=>companyDetail(company),'服务区域已更新')}catch(error){submit.disabled=false;toast(error.message,true)}};
  });
}
function editCompany(company){
  if(!company)return;
  modal(`编辑${company.name}资料`,`<form class="ops-form" id="company-edit-form"><div class="ops-notice">此处只修改基本资料。主体启用与停用请使用列表或详情页的专用操作，确保理由和审计记录完整。联系电话留空不会覆盖原信息。</div><div class="ops-field"><label for="company-edit-name">公司名称 *</label><input class="ops-input" id="company-edit-name" maxlength="128" value="${esc(company.name||'')}"></div><div class="ops-field"><label for="company-edit-owner">负责人</label><input class="ops-input" id="company-edit-owner" maxlength="64" value="${esc(company.owner_name||'')}"></div><div class="ops-field"><label for="company-edit-phone">联系电话</label><input class="ops-input" id="company-edit-phone" inputmode="tel" maxlength="32" placeholder="当前：${esc(company.contact_phone_masked||'未填写')}；留空不修改"></div><div class="ops-field"><label for="company-edit-level">合作等级</label><input class="ops-input" id="company-edit-level" maxlength="32" value="${esc(company.level_code||'V1')}"></div><div class="ops-field"><label for="company-edit-notes">备注</label><textarea class="ops-textarea" id="company-edit-notes" maxlength="500">${esc(company.notes||'')}</textarea></div><div class="ops-actions"><button class="ops-btn" type="button" id="company-edit-cancel">取消</button><button class="ops-btn primary" id="company-edit-submit">保存资料</button></div></form>`,()=>{
    const form=document.querySelector('#company-edit-form'),submit=document.querySelector('#company-edit-submit');
    document.querySelector('#company-edit-cancel').onclick=closeModal;
    form.onsubmit=async event=>{event.preventDefault();const name=document.querySelector('#company-edit-name').value.trim(),phone=document.querySelector('#company-edit-phone').value.trim();if(name.length<2){toast('公司名称至少 2 个字符',true);return}submit.disabled=true;try{const body={name,owner_name:document.querySelector('#company-edit-owner').value.trim()||null,level_code:document.querySelector('#company-edit-level').value.trim()||'V1',notes:document.querySelector('#company-edit-notes').value.trim()||null};if(phone)body.contact_phone=phone;await api(`/companies/${encodeURIComponent(company.id)}`,{method:'PATCH',body:JSON.stringify(body)});toast('加盟商资料已保存');closeModalFor(form);await refreshAfterSuccess(companies,'加盟商资料已保存')}catch(error){if(form.isConnected)submit.disabled=false;toast(error.message,true)}};
  });
}
const COMPANY_ACCOUNT_ROLE_LABEL={FRANCHISE_OWNER:'加盟商负责人',FRANCHISE_EMPLOYEE:'加盟商员工'};
const isSuperAdmin=()=>primaryRole()==='SUPER_ADMIN';
async function companyAccounts(companyId,companyName){
  const isCurrent=beginModalRequest();
  try{
    const accounts=await api(`/companies/${encodeURIComponent(companyId)}/accounts`);
    if(!isCurrent())return;
    const rows=accounts.map(account=>{
      const action=account.status==='ACTIVE'?'DISABLED':'ACTIVE';
      const actionLabel=action==='ACTIVE'?'启用':'停用';
      const credentialAction=account.role_code==='FRANCHISE_OWNER'&&!account.credentials_ready?` <button class="ops-btn primary" data-company-owner-provision="${esc(companyId)}:${esc(account.id)}">补齐登录账号</button>`:'';
      return `<tr><td><b>${esc(account.display_name)}</b><br><small>${esc(account.username||'尚未设置登录账号')}</small></td><td>${esc(COMPANY_ACCOUNT_ROLE_LABEL[account.role_code]||account.role_code||'--')}</td><td>${badge(account.status)}</td><td>${account.wechat_bound?'已绑定':'未绑定'}</td><td><button class="ops-btn" data-company-account-status="${esc(companyId)}:${esc(account.id)}:${action}">${actionLabel}</button> <button class="ops-btn" data-company-account-reset="${esc(companyId)}:${esc(account.id)}" ${account.credentials_ready?'':'disabled'}>重置密码</button>${credentialAction}</td></tr>`;
    });
    modal(`${companyName||'加盟商'} · 账号与人员`,`${isSuperAdmin()?'<div class="ops-notice">超级管理员的开通、停用和重置操作必须填写理由，并写入审计。</div>':'<div class="ops-notice">运营可开通、停用、重置账号，并为历史微信负责人补齐登录账号。更换负责人时先在主体详情停用并解绑原负责人微信，再更新负责人资料、重新启用并发出新邀请，全部步骤留存审计。加盟商内部客资分配不在此页面展示。</div>'}<div class="ops-actions"><button class="ops-btn primary" id="company-account-create">开通账号</button></div>${table(['姓名 / 登录账号','角色','状态','微信','操作'],rows)}`,()=>{
      document.querySelector('#company-account-create').onclick=()=>createCompanyAccount(companyId,companyName);
      document.querySelectorAll('[data-company-account-status]').forEach(button=>button.onclick=()=>{
        const [targetCompanyId,userId,status]=button.dataset.companyAccountStatus.split(':');
        changeCompanyAccountStatus(targetCompanyId,userId,status,companyName);
      });
      document.querySelectorAll('[data-company-account-reset]').forEach(button=>button.onclick=()=>{
        const [targetCompanyId,userId]=button.dataset.companyAccountReset.split(':');
        resetCompanyAccountPassword(targetCompanyId,userId,companyName);
      });
      document.querySelectorAll('[data-company-owner-provision]').forEach(button=>button.onclick=()=>{
        const [targetCompanyId,userId]=button.dataset.companyOwnerProvision.split(':');
        provisionCompanyOwnerCredentials(targetCompanyId,userId,companyName);
      });
    });
  }catch(error){toast(error.message,true)}
}
function createCompanyAccount(companyId,companyName){
  modal(`开通${companyName||'加盟商'}账号`,`<form class="ops-form" id="company-account-form"><div class="ops-notice">只需填写姓名和登录账号，系统会生成 8 位以上初始密码并在成功后仅展示一次。</div><div class="ops-field"><label for="company-account-name">姓名 *</label><input class="ops-input" id="company-account-name" maxlength="64"></div><div class="ops-field"><label for="company-account-username">登录账号 *</label><input class="ops-input" id="company-account-username" maxlength="64" autocomplete="off"></div><div class="ops-field"><label for="company-account-role">角色 *</label><select class="ops-input" id="company-account-role"><option value="FRANCHISE_OWNER">加盟商负责人</option><option value="FRANCHISE_EMPLOYEE">加盟商员工</option></select></div><div class="ops-field"><label for="company-account-reason">操作理由${isSuperAdmin()?' *':''}</label><textarea class="ops-textarea" id="company-account-reason" placeholder="${isSuperAdmin()?'请说明超级管理员代为开通的原因':'可选填写，便于后续追溯'}"></textarea></div><div class="ops-actions"><button class="ops-btn" type="button" id="company-account-cancel">取消</button><button class="ops-btn primary" id="company-account-submit">确认开通</button></div></form>`,()=>{
    const form=document.querySelector('#company-account-form');
    document.querySelector('#company-account-cancel').onclick=()=>companyAccounts(companyId,companyName);
    form.onsubmit=async event=>{
      event.preventDefault();
      const display_name=document.querySelector('#company-account-name').value.trim();
      const username=document.querySelector('#company-account-username').value.trim();
      const reason=document.querySelector('#company-account-reason').value.trim();
      const submit=document.querySelector('#company-account-submit');
      if(!display_name||!username){toast('请填写姓名和登录账号',true);return}
      if(isSuperAdmin()&&reason.length<2){toast('超级管理员操作必须填写至少 2 个字的理由',true);return}
      submit.disabled=true;
      const cancel=document.querySelector('#company-account-cancel');
      lockModal(form,cancel);
      try{
        const account=await api(`/companies/${encodeURIComponent(companyId)}/accounts`,{method:'POST',body:JSON.stringify({display_name,username,role_code:document.querySelector('#company-account-role').value,reason:reason||null})});
        unlockModal(form);
        showInitialPassword(account.initial_password,()=>companyAccounts(companyId,companyName));
      }catch(error){unlockModal(form);submit.disabled=false;cancel.disabled=false;document.querySelector('#modal-close').disabled=false;toast(error.message,true)}
    };
  });
}
function changeCompanyAccountStatus(companyId,userId,status,companyName){
  const enabling=status==='ACTIVE';
  actionForm({title:enabling?'启用加盟商账号':'停用加盟商账号',message:enabling?'启用后该账号可重新登录。':'停用会使该账号的现有会话失效。',labelText:'操作理由',required:isSuperAdmin(),minLength:2,submitLabel:enabling?'确认启用':'确认停用',danger:!enabling},async(reason,isCurrent)=>{
    await api(`/companies/${encodeURIComponent(companyId)}/accounts/${encodeURIComponent(userId)}/${enabling?'enable':'disable'}`,{method:'POST',body:JSON.stringify({reason:reason||null})});
    toast(enabling?'账号已启用':'账号已停用');
    if(isCurrent())await refreshAfterSuccess(()=>companyAccounts(companyId,companyName),enabling?'账号已启用':'账号已停用');
  });
}
function resetCompanyAccountPassword(companyId,userId,companyName){
  actionForm({title:'重置加盟商账号密码',message:'系统会生成新的初始密码并仅展示一次；旧会话将立即失效。',labelText:'重置理由',required:isSuperAdmin(),minLength:2,submitLabel:'确认重置',danger:true,lockOnSubmit:true},async(reason,_isCurrent,unlock)=>{
    const account=await api(`/companies/${encodeURIComponent(companyId)}/accounts/${encodeURIComponent(userId)}/reset-password`,{method:'POST',body:JSON.stringify({reason:reason||null})});
    unlock();
    showInitialPassword(account.initial_password,()=>companyAccounts(companyId,companyName));
    return false;
  });
}
function provisionCompanyOwnerCredentials(companyId,userId,companyName){
  modal('补齐负责人登录账号',`<form class="ops-form" id="company-owner-provision-form"><div class="ops-notice">适用于历史上只绑定微信、尚无登录账号的当前负责人。登录账号留空时优先使用加盟商联系电话；系统生成的初始密码只展示一次。</div><div class="ops-field"><label for="company-owner-username">登录账号</label><input class="ops-input" id="company-owner-username" maxlength="64" autocomplete="off" placeholder="留空使用加盟商联系电话"></div><div class="ops-field"><label for="company-owner-provision-reason">操作理由${isSuperAdmin()?' *':''}</label><textarea class="ops-textarea" id="company-owner-provision-reason" placeholder="${isSuperAdmin()?'请说明补齐原因':'可选填写，便于后续追溯'}"></textarea></div><div class="ops-actions"><button class="ops-btn" type="button" id="company-owner-provision-cancel">取消</button><button class="ops-btn primary" id="company-owner-provision-submit">生成登录账号与初始密码</button></div></form>`,()=>{
    const form=document.querySelector('#company-owner-provision-form'),submit=document.querySelector('#company-owner-provision-submit');
    document.querySelector('#company-owner-provision-cancel').onclick=()=>companyAccounts(companyId,companyName);
    form.onsubmit=async event=>{
      event.preventDefault();
      const username=document.querySelector('#company-owner-username').value.trim(),reason=document.querySelector('#company-owner-provision-reason').value.trim();
      if(username&&username.length<2){toast('登录账号至少 2 个字符',true);return}
      if(isSuperAdmin()&&reason.length<2){toast('超级管理员操作必须填写至少 2 个字的理由',true);return}
      submit.disabled=true;
      try{
        const account=await api(`/companies/${encodeURIComponent(companyId)}/accounts/${encodeURIComponent(userId)}/provision-owner-credentials`,{method:'POST',body:JSON.stringify({username:username||null,reason:reason||null})});
        showInitialPassword(account.initial_password,()=>companyAccounts(companyId,companyName));
      }catch(error){submit.disabled=false;toast(error.message,true)}
    };
  });
}
function showInitialPassword(password,onClose){
  if(!password){toast('操作已完成，但未返回初始密码',true);onClose?.();return}
  modal('请安全交付初始密码',`<div class="ops-notice">初始密码仅在当前窗口展示一次，请立即复制并通过安全渠道交付账号本人。</div><div class="ops-detail"><small>初始密码</small><b id="initial-password">${esc(password)}</b></div><div class="ops-actions"><button class="ops-btn primary" id="copy-initial-password">复制密码</button><button class="ops-btn" id="initial-password-close">已保存</button></div>`,()=>{
    document.querySelector('#copy-initial-password').onclick=async()=>{
      try{await navigator.clipboard.writeText(password);toast('初始密码已复制')}catch{toast('浏览器不支持自动复制，请手动复制',true)}
    };
    document.querySelector('#initial-password-close').onclick=()=>{closeModal();onClose?.()};
  });
}
async function dispatch(){
  const d=await api(`/v1.2/dispatch-pool${qs({page:S.page,page_size:20})}`);
  const canCorrect=can('lead.manual.manage')&&primaryRole()!=='SUPER_ADMIN';
  const byId=Object.fromEntries((d.items||[]).map(x=>[x.id,x]));
  const rows=(d.items||[]).map((x,index)=>{
    const actions=[`<button class="ops-btn primary" data-candidate="${esc(x.id)}">选择接收公司</button>`];
    if(canCorrect)actions.push(`<button class="ops-btn" data-dispatch-correction="${esc(x.id)}">修改信息</button>`);
    const verificationFlag=x.has_verification_info?`<button class="ops-btn" data-verification-info="${esc(x.id)}">有核验信息</button>`:'';
    return `<tr><td>${rowSequence(d,index)}</td><td><b>${esc(x.customer_name)}</b><br>${esc(x.phone||x.phone_masked||'--')}</td><td>${esc(x.city||'--')} ${esc(x.district||'')}</td><td>${esc(label(x.source_kind))}</td><td>${esc(x.supplier_company_name||'平台客资')}<br><small>${esc(x.submitter_name||'未知录入人')}</small></td><td>${esc(x.need_summary||'--')}</td><td><div class="ops-actions">${actions.join(' ')}${verificationFlag}</div></td></tr>`;
  });
  shell(`<section class="ops-card"><div class="ops-card-head"><div><h2>待人工派发池</h2><p>运营可核对完整手机号、客资提供方和录入人员；有在职员工时选择具体员工；无在职员工时自动派发给负责人。点击“有核验信息”可查看电销核验内容并据此修改客户资料。</p></div></div>${table(['序号','客户','所在地','客资来源','提供方 / 录入人','客户需求','操作'],rows)}${pager(d)}</section>`);
  bindPager(d,dispatch);
  document.querySelectorAll('[data-candidate]').forEach(button=>button.onclick=()=>candidates(button.dataset.candidate));
  document.querySelectorAll('[data-dispatch-correction]').forEach(button=>button.onclick=()=>openDispatchCorrection(byId[button.dataset.dispatchCorrection]));
  document.querySelectorAll('[data-verification-info]').forEach(button=>button.onclick=()=>openDispatchCorrection(byId[button.dataset.verificationInfo]));
  if(S.id){const id=S.id;S.id='';candidates(id)}
}
async function openDispatchCorrection(item){
  if(!item)return;
  const isCurrent=beginModalRequest();
  try{
    const [lead,task]=await Promise.all([
      api(`/v1.2/admin/leads/${encodeURIComponent(item.id)}`),
      item.pre_dispatch_task_id?api(`/v1.2/pre-dispatch-verifications/tasks/${encodeURIComponent(item.pre_dispatch_task_id)}`):Promise.resolve(null),
    ]);
    if(isCurrent())await openPlatformLeadForm(lead,true,{verificationInfo:task?.verification_info||null,refresh:dispatch});
  }catch(error){toast(error.message,true)}
}
async function correctLeadFromVerificationTask(taskId){
  const isCurrent=beginModalRequest();
  try{
    const task=await api(`/v1.2/pre-dispatch-verifications/tasks/${encodeURIComponent(taskId)}`);
    if(!isCurrent())return;
    const lead=await api(`/v1.2/admin/leads/${encodeURIComponent(task.lead_id)}`);
    if(isCurrent())await openPlatformLeadForm(lead,true,{verificationInfo:task.verification_info||null,refresh:telesales});
  }catch(error){toast(error.message,true)}
}
async function candidates(leadId){
  const candidateCard=x=>{const returnedReceiver=(x.exclusion_reasons||[]).includes('RETURNED_RECEIVER_EXCLUDED');const onlyReturnedReceiver=returnedReceiver&&(x.exclusion_reasons||[]).length===1;const action=x.eligible?`<button class="ops-btn primary" data-dispatch="${esc(x.company_id)}">派发</button>`:onlyReturnedReceiver?`<button class="ops-btn" data-dispatch-override="${esc(x.company_id)}">例外派发</button>`:'<span class="ops-muted">不可派发</span>';return `<article class="ops-candidate-card ${x.eligible?'eligible':'blocked'}"><div class="ops-candidate-head"><div><h3>${esc(x.company_name)}</h3><p>${x.region_match?'与客户所在地匹配':'其他服务区域'}</p></div>${x.eligible?badge('APPROVED'):badge('REJECTED')}</div><div class="ops-candidate-facts"><span><small>所需积分</small><b>${esc(x.points_price)}</b></span><span><small>可用积分</small><b>${esc(x.points_available??'按权限隐藏')}</b></span></div><p class="ops-candidate-reason">${esc(candidateReasons(x.exclusion_reasons))}</p><div class="ops-actions">${action}</div></article>`};
  let keyword='',page=0,items=[],hasMore=false,requestSequence=0,searchTimer;
  let districtWarning='';
  try{
    const leadDetail=await api(`/v1.2/admin/leads/${encodeURIComponent(leadId)}`);
    if(leadDetail?.missing_district_region)districtWarning='<div class="ops-notice">注意：该客资缺少有效区县（仅到市级），可能影响加盟商接单匹配；继续派发即视为运营确认承担后续风险。</div>';
  }catch{}
  modal('选择接收公司',`${districtWarning}<div class="ops-notice">按所在地优先展示可承接的加盟商；也可以搜索其他加盟商。曾领取后退回的原公司默认不可再次派发，确需例外派发时必须填写运营判断原因并保留审计。</div><div class="ops-filter"><input class="ops-input" id="candidate-search" placeholder="搜索其他加盟商" autocomplete="off"></div><div id="candidate-results"><div class="ops-loading">正在匹配可承接的加盟商…</div></div>`,()=>{
    const result=document.querySelector('#candidate-results');
    const bindActions=()=>{document.querySelectorAll('[data-dispatch]').forEach(b=>b.onclick=()=>dispatchOne(leadId,b.dataset.dispatch,false,items.find(item=>item.company_id===b.dataset.dispatch)?.company_name));document.querySelectorAll('[data-dispatch-override]').forEach(b=>b.onclick=()=>dispatchOne(leadId,b.dataset.dispatchOverride,true,items.find(item=>item.company_id===b.dataset.dispatchOverride)?.company_name))};
    const draw=()=>{const cards=items.map(candidateCard).join('');const body=cards?`<div class="ops-candidate-grid">${cards}</div>`:'<div class="ops-empty">没有匹配的候选公司</div>';const more=hasMore?'<div class="ops-actions"><button class="ops-btn" id="candidate-load-more">加载更多</button></div>':'';zsSetSafeHtml(result,body+more);bindActions();document.querySelector('#candidate-load-more')?.addEventListener('click',()=>loadCandidates(false))};
    const loadCandidates=async reset=>{
      const requestedPage=reset?1:page+1;
      const requestId=++requestSequence;
      if(reset)zsSetSafeHtml(result,'<div class="ops-loading">正在匹配可承接的加盟商…</div>');
      try{
        const data=await api(`/v1.2/dispatch-pool/${encodeURIComponent(leadId)}/candidates${qs({page:requestedPage,page_size:20,keyword})}`);
        if(requestId!==requestSequence)return;
        items=reset?[...(data.candidates||[])]:[...items,...(data.candidates||[])];
        page=Number(data.page||requestedPage);
        hasMore=Boolean(data.has_more);
        draw();
      }catch(error){
        if(requestId!==requestSequence)return;
        zsSetSafeHtml(result,`<div class="ops-error">${esc(error.message||'暂时无法获取可派发的加盟商')}</div>`);
      }
    };
    document.querySelector('#candidate-search').oninput=event=>{keyword=event.target.value.trim();clearTimeout(searchTimer);searchTimer=setTimeout(()=>loadCandidates(true),250)};
    loadCandidates(true);
  });
}
async function chooseDispatchEmployee(companyId,companyName,{returnReceiverOverride=false,onSubmit}){
  const isCurrent=beginModalRequest();
  try{
    const directory=await api(`/companies/${encodeURIComponent(companyId)}/account-directory`);
    if(!isCurrent())return;
    const employees=(directory||[]).filter(user=>user.role_code==='FRANCHISE_EMPLOYEE'&&user.status==='ACTIVE');
    const owner=(directory||[]).find(user=>user.role_code==='FRANCHISE_OWNER'&&user.status==='ACTIVE');
    const ownerFallback=!employees.length;
    if(ownerFallback&&!owner){toast('该加盟商缺少有效负责人，暂时无法派发',true);return}
    const options=employees.map(user=>`<option value="${esc(user.id)}">${esc(user.display_name||user.username||'未命名员工')}</option>`).join('');
    const receiverField=ownerFallback?`<div class="ops-field"><label>接收负责人</label><div class="ops-notice">${esc(owner.display_name||owner.username||'加盟商负责人')}</div></div>`:`<div class="ops-field"><label for="direct-dispatch-employee">接收员工 *</label><select class="ops-input" id="direct-dispatch-employee">${options}</select></div>`;
    const receiverNotice=ownerFallback?`该加盟商暂无在职员工，客资将直接派发给负责人。`:`客资将直接派发给 ${esc(companyName||'所选加盟商')} 的指定员工，负责人同时接收通知并查看进度。`;
    modal(returnReceiverOverride?'确认例外直派':ownerFallback?'确认派发负责人':'选择接收员工',`<form class="ops-form" id="direct-dispatch-form"><div class="ops-notice">${receiverNotice}</div>${receiverField}<div class="ops-field"><label for="direct-dispatch-note">${returnReceiverOverride?'例外派发原因 *':'派发备注'}</label><textarea class="ops-textarea" id="direct-dispatch-note" ${returnReceiverOverride?'required minlength="2"':''}></textarea></div><div class="ops-actions"><button class="ops-btn" type="button" id="direct-dispatch-cancel">取消</button><button class="ops-btn primary" id="direct-dispatch-submit">确认直派</button></div></form>`,()=>{
      const form=document.querySelector('#direct-dispatch-form'),submit=document.querySelector('#direct-dispatch-submit');
      document.querySelector('#direct-dispatch-cancel').onclick=closeModal;
      form.onsubmit=async event=>{event.preventDefault();const employeeUserId=ownerFallback?null:document.querySelector('#direct-dispatch-employee').value,note=document.querySelector('#direct-dispatch-note').value.trim();if(!ownerFallback&&!employeeUserId){toast('请选择接收员工',true);return}if(returnReceiverOverride&&note.length<2){toast('请至少填写 2 个字的例外派发原因',true);return}submit.disabled=true;try{await onSubmit(employeeUserId,note);closeModalFor(form)}catch(error){if(form.isConnected)submit.disabled=false;toast(error.message,true)}};
    });
  }catch(error){toast(error.message,true)}
}
function dispatchOne(leadId,companyId,returnReceiverOverride=false,companyName=''){chooseDispatchEmployee(companyId,companyName,{returnReceiverOverride,onSubmit:async(employeeUserId,note)=>{await api(`/v1.2/dispatch-pool/${encodeURIComponent(leadId)}/dispatch`,{method:'POST',body:JSON.stringify({company_id:companyId,employee_user_id:employeeUserId||null,idempotency_key:`dispatch-${crypto.randomUUID()}`,note:returnReceiverOverride?null:note||null,return_receiver_override:returnReceiverOverride,return_receiver_override_reason:returnReceiverOverride?note:null})});toast(employeeUserId?'客资已直派给所选员工':'客资已直派给加盟商负责人');await refreshAfterSuccess(dispatch,'客资已派发')}})}
async function returns(){const [d,t]=await Promise.all([api(`/v1.2/returns${qs({status:S.status,keyword:S.returnKeyword,page:S.page,page_size:20})}`),can('verification.read')?api('/v1.2/return-verifications/tasks?page=1&page_size=100'):Promise.resolve({items:[]})]);if(can('verification.read'))await loadTelesalesUsers();const rows=(d.items||[]).map((x,index)=>`<tr><td>${rowSequence(d,index)}</td><td>${esc(recordCode(x.id,'TH'))}<br><small>派发编号 ${esc(x.assignment_code||recordCode(x.assignment_id,'PF'))}</small></td><td><b>${esc(x.customer_name||'待确认客户')}</b><br><small>${esc(x.phone||x.phone_masked||'--')} · ${esc([x.city,x.district].filter(Boolean).join(' / ')||'地区待补充')}</small></td><td>${esc(label(x.reason_code))}</td><td>${esc(x.verification?.assignee_user_id?telesalesName(x.verification.assignee_user_id):'未分配')}</td><td>${badge(x.status,returnStatusLabel(x.status))}</td><td>${x.submitted_at?fmt(x.submitted_at):'尚未提交'}</td><td>${esc(returnAppealTiming(x))}</td><td><button class="ops-btn" data-return="${x.id}">${x.submitted_at?'查看与审核':'查看草稿（只读）'}</button></td></tr>`);const tasks=(t.items||[]).map((x,index)=>{const r=x.return_request||{},lead=x.lead||{},nextStep=x.status==='SUBMITTED'?'等待运营终审':'电销继续完成退回事实核验';return `<tr><td>${rowSequence(t,index)}</td><td><b>${esc(lead.customer_name||'待核验客户')}</b><br><small>${esc(lead.phone||lead.phone_masked||'--')}</small></td><td>${verificationTaskBadge(x)}</td><td>${esc(telesalesName(x.assignee_user_id))}</td><td>${esc(label(r.reason_code))}</td><td>${esc(nextStep)}</td><td>${fmt(x.due_at)}</td><td><button class="ops-btn" data-task="${x.id}">查看</button> <button class="ops-btn" data-assign="${x.id}">${x.assignee_user_id?'重新分配':'分配人员'}</button></td></tr>`});const filterNotice=S.status?`<div class="ops-notice">当前筛选：${esc(returnStatusLabel(S.status))} <button class="ops-btn" id="returns-clear">查看全部</button></div>`:'';shell(`${filterNotice}<section class="ops-card"><h2>${S.status==='DRAFT'?'未提交草稿（只读）':'正式退回记录'}</h2><p>正式提交后暂停原48小时有效确认计时；驳回后按剩余时间继续。未提交草稿由发起人继续完善。</p>${S.status==='DRAFT'?'':'<button class="ops-btn" id="returns-drafts">查看未提交草稿</button>'}<form class="ops-filter-row" id="return-filter-form"><input class="ops-input" id="return-keyword" value="${esc(S.returnKeyword)}" placeholder="搜索退回编号、客户姓名或手机号"><button class="ops-btn primary" type="submit">查询</button><button class="ops-btn" id="return-filter-reset" type="button">重置</button></form>${table(['序号','退回编号','客户','退回原因','核验电销','处理状态','申诉时间','48小时计时','操作'],rows)}${pager(d)}</section>${can('verification.read')?`<section class="ops-card"><h2>电话核验任务</h2><p>任务未完成时持续保留在待处理队列，参考时间不限制继续核验；运营仍可随时改派。</p>${table(['序号','客户','状态','核验人员','退回原因','下一步','核验参考时间','操作'],tasks)}</section>`:''}`);bindPager(d,returns);document.querySelector('#return-filter-form').onsubmit=event=>{event.preventDefault();S.returnKeyword=document.querySelector('#return-keyword').value.trim();S.page=1;returns()};document.querySelector('#return-filter-reset').onclick=()=>{S.returnKeyword='';S.page=1;returns()};document.querySelector('#returns-drafts')?.addEventListener('click',()=>{S.status='DRAFT';S.page=1;returns()});document.querySelector('#returns-clear')?.addEventListener('click',()=>go('returns'));document.querySelectorAll('[data-return]').forEach(b=>b.onclick=()=>returnDetail(b.dataset.return));document.querySelectorAll('[data-task]').forEach(b=>b.onclick=()=>taskDetail(b.dataset.task));document.querySelectorAll('[data-assign]').forEach(b=>b.onclick=()=>assignTask(b.dataset.assign));if(S.id){const id=S.id;S.id='';returnDetail(id)}}
function canCorrectReturnRegion(returnRequest,verification){return can('return.review')&&returnRequest.reason_code==='OUT_OF_SERVICE_REGION'&&returnRequest.status==='REVIEWING'&&verification.conclusion==='SUPPORT_RETURN'}
function returnFundImpact(returnRequest){if(!returnRequest.submitted_at)return '尚未正式提交，不产生退款或结算冻结';if(returnRequest.assignment_release_reason==='V12_RETURN_REGION_CORRECTED')return `已返还 ${Number(returnRequest.refund_points||0)} 积分，区域已更正并重新入池`;if(returnRequest.status==='APPROVED')return `已返还 ${Number(returnRequest.refund_points||0)} 积分，客资已关闭`;return '终审支持退回后按原领取流水返还积分'}
async function returnDetail(id){
  const isCurrent=beginModalRequest();
  const x=await api(`/v1.2/returns/${encodeURIComponent(id)}`);
  if(can('verification.read'))await loadTelesalesUsers();
  if(!isCurrent())return;
  const verification=x.verification||{},reward=x.reward||{};
  const canFinalReview=can('return.review')&&x.status==='REVIEWING'&&verification.conclusion;
  const canChooseVerificationPath=can('return.review')&&['SUBMITTED','VERIFYING'].includes(x.status)&&!verification.conclusion;
  const regionCorrectionRelevant=can('return.review')&&x.reason_code==='OUT_OF_SERVICE_REGION'&&['SUBMITTED','VERIFYING','REVIEWING'].includes(x.status);
  const canCorrectRegion=canCorrectReturnRegion(x,verification);
  const fundImpact=returnFundImpact(x);
  const finalActions=verification.conclusion==='SUPPORT_RETURN'?'<button class="ops-btn primary" data-final="APPROVE">确认无效并返分</button>':verification.conclusion==='DOES_NOT_SUPPORT_RETURN'?'<button class="ops-btn primary" data-final="REJECT">确认可用，回原领取人</button>':'<button class="ops-btn" data-final="NEED_MORE">要求补充证据</button>';
  const pathActions=canChooseVerificationPath?`<div class="ops-actions"><button class="ops-btn danger" data-return-direct-invalid>运营直接判定无效</button><button class="ops-btn primary" data-return-assign-task>${verification.assignee_user_id?'重新分配电销核验':'分配给电销核验'}</button></div>`:'';
  const regionAction=canCorrectRegion?'<div class="ops-actions"><button class="ops-btn primary" data-return-correct-region>修改实际区域、返分并重新派发</button></div>':regionCorrectionRelevant?'<div class="ops-notice">请先完成退回核验；核验结论为“支持退回”后才可修改区域、返分并重新入池。</div>':'';
  modal('退回申诉详情',`<div class="ops-detail-grid">${[['退回编号',recordCode(x.id,'TH')],['派发编号',recordCode(x.assignment_id,'PF')],['客户',x.customer_name],['完整电话',x.phone||x.phone_masked],['处理状态',returnStatusLabel(x.status)],['申诉时间',x.submitted_at?fmt(x.submitted_at):'尚未提交'],['退回原因',label(x.reason_code)],['申诉人',x.submitted_by_name],['核验人员',telesalesName(verification.assignee_user_id)],['核验状态',label(verification.status)],['联系结果',label(verification.contact_result)],['核验结论',label(verification.conclusion)],['48小时计时',returnAppealTiming(x)],['资金影响',fundImpact],['供资奖励',reward.status?label(reward.status):'无关联奖励'],['终审说明',x.final_decision_reason]].map(([a,b])=>`<div class="ops-detail"><small>${a}</small><b>${esc(b||'--')}</b></div>`).join('')}</div><section class="ops-card"><h3>申诉说明</h3><p>${esc(x.description||'暂无说明')}</p></section><section class="ops-card"><h3>核验备注</h3><p>${esc(verification.note||'暂无核验备注')}</p></section><section class="ops-card"><h3>申诉证据</h3><div class="ops-detail-grid">${evidenceList(x.evidences)}</div></section><section class="ops-card"><h3>电销核验证据</h3><div class="ops-detail-grid">${evidenceList(verification.evidences)}</div></section>${regionAction}${pathActions}${canFinalReview?`<div class="ops-actions">${finalActions}</div>`:x.status==='REVIEWING'?'<div class="ops-notice">等待电销提交事实核验结论后，才能进行运营终审。</div>':''}`,()=>{
    bindEvidencePreviewErrors(modalRoot);
    document.querySelectorAll('[data-final]').forEach(button=>button.onclick=()=>finalReview(id,button.dataset.final));
    document.querySelector('[data-return-assign-task]')?.addEventListener('click',()=>assignTask(verification.task_id));
    document.querySelector('[data-return-direct-invalid]')?.addEventListener('click',()=>directInvalidReturn(id));
    document.querySelector('[data-return-correct-region]')?.addEventListener('click',()=>correctReturnRegionAndRedispatch(x));
  });
}
async function correctReturnRegionAndRedispatch(returnRequest){
  const isCurrent=beginModalRequest();
  try{
    const cities=await platformCities();
    if(!isCurrent())return;
    const provinces=Array.from(new Map(cities.map(city=>[city.province_code,city.province_name])).entries()).map(([code,name])=>({code,name}));
    modal('修改实际区域并重新派发',`<form class="ops-form" id="return-region-form"><div class="ops-notice">确认后按原领取流水返还积分，关闭原派发轮次；修改后的区域有可承接的加盟商时，客资回到派发池，无可承接时进入公海池。历史派发和积分记录继续保留。</div><div class="ops-field"><label for="return-region-search">搜索区县</label><input class="ops-input" id="return-region-search" maxlength="64" placeholder="输入城市或区县名称快速定位"><select class="ops-input" id="return-region-search-results" hidden><option value="">请选择搜索结果</option></select></div><div class="ops-field"><label for="return-region-province">省份 *</label><select class="ops-input" id="return-region-province"><option value="">请选择省份</option>${provinces.map(item=>`<option value="${esc(item.code)}">${esc(item.name)}</option>`).join('')}</select></div><div class="ops-field"><label for="return-region-city">城市 *</label><select class="ops-input" id="return-region-city" disabled><option value="">请先选择省份</option></select></div><div class="ops-field"><label for="return-region-district">区/县 *</label><select class="ops-input" id="return-region-district" disabled><option value="">请先选择城市</option></select></div><div class="ops-field"><label for="return-region-reason">修改与重新派发原因 *</label><textarea class="ops-textarea" id="return-region-reason" minlength="2" maxlength="500" placeholder="写明核实到的实际区域与处理依据"></textarea></div><div class="ops-actions"><button class="ops-btn" type="button" id="return-region-cancel">取消</button><button class="ops-btn primary" id="return-region-submit">确认返分并重新入池</button></div></form>`,()=>{
      const form=document.querySelector('#return-region-form'),province=document.querySelector('#return-region-province'),city=document.querySelector('#return-region-city'),district=document.querySelector('#return-region-district'),submit=document.querySelector('#return-region-submit');
      document.querySelector('#return-region-cancel').onclick=closeModal;
      let regionSearchResults=[],regionSearchTimer=null,regionSearchSequence=0;
      const regionSearchInput=document.querySelector('#return-region-search'),regionSearchSelect=document.querySelector('#return-region-search-results');
      regionSearchInput.oninput=()=>{clearTimeout(regionSearchTimer);const requestSequence=++regionSearchSequence,keyword=regionSearchInput.value.trim();if(!keyword){regionSearchResults=[];regionSearchSelect.hidden=true;return}regionSearchTimer=setTimeout(async()=>{try{const items=await api(`/master-data/regions/search?keyword=${encodeURIComponent(keyword)}&limit=30`);if(requestSequence!==regionSearchSequence)return;regionSearchResults=items.filter(item=>item.level==='DISTRICT');replacePlatformSelectOptions(regionSearchSelect,regionSearchResults.map(region=>({...region,option_name:region.path_label})),'','','请选择搜索结果');regionSearchSelect.hidden=!regionSearchResults.length}catch(error){if(requestSequence===regionSearchSequence)toast(error.message,true)}},250)};
      regionSearchSelect.onchange=async()=>{const selected=regionSearchResults.find(region=>region.code===regionSearchSelect.value);const path=selected?.path||[];const p=path.find(item=>item.level==='PROVINCE'),c=path.find(item=>item.level==='CITY'),d=path.find(item=>item.level==='DISTRICT');if(!p||!c||!d)return;province.value=p.code;replacePlatformSelectOptions(city,cities.filter(item=>item.province_code===p.code),'','','请选择城市');city.disabled=false;city.value=c.code;try{const items=await platformDistricts(c.code);replacePlatformSelectOptions(district,items,d.code,'','请选择区/县');district.disabled=!items.length}catch(error){toast(error.message,true)}};
      province.onchange=()=>{replacePlatformSelectOptions(city,cities.filter(item=>item.province_code===province.value),'','','请选择城市');city.disabled=!province.value;replacePlatformSelectOptions(district,[],'','','请先选择城市');district.disabled=true};
      city.onchange=async()=>{replacePlatformSelectOptions(district,[],'','','加载中…');district.disabled=true;if(!city.value)return;try{const items=await platformDistricts(city.value);replacePlatformSelectOptions(district,items,'','','请选择区/县');district.disabled=!items.length}catch(error){replacePlatformSelectOptions(district,[],'','','区县加载失败');toast(error.message,true)}};
      form.onsubmit=async event=>{event.preventDefault();const reason=document.querySelector('#return-region-reason').value.trim();if(!province.value||!city.value||!district.value){toast('请选择完整的省、市、区县',true);return}if(reason.length<2){toast('请至少填写 2 个字的处理原因',true);return}submit.disabled=true;try{const result=await api(`/v1.2/returns/${encodeURIComponent(returnRequest.id)}/correct-region-and-redispatch`,{method:'POST',body:JSON.stringify({province_code:province.value,city_code:city.value,district_code:district.value,reason})});const poolTargetMessage=result.pool_target==='PUBLIC_POOL'?'暂无可承接的加盟商，客资已转入公海池':'客资已重新进入派发池';toast(`区域已修改，已返还 ${Number(result.refund_points||0)} 积分，${poolTargetMessage}`);closeModalFor(form);await refreshAfterSuccess(returns,'区域已修改并按承接结果入池')}catch(error){if(form.isConnected)submit.disabled=false;toast(error.message,true)}};
    });
  }catch(error){toast(error.message,true)}
}
function finalReview(id,decision){const actionLabel={APPROVE:'确认无效并返分',REJECT:'确认可用，回原领取人',NEED_MORE:'要求补充证据'}[decision]||'提交终审';actionForm({title:actionLabel,message:'终审严格以电销核实结论为准：确认可用时保留原领取人；确认无效时关闭客资并返分。',labelText:'终审说明',required:true,minLength:2,submitLabel:`确认${actionLabel}`,danger:decision==='APPROVE'},async note=>{await api(`/v1.2/returns/${encodeURIComponent(id)}/final-review`,{method:'POST',body:JSON.stringify({decision,note})});toast('终审完成');await refreshAfterSuccess(returns,'终审完成')})}
function directInvalidReturn(id){actionForm({title:'运营直接判定无效',message:'此操作将跳过电销核验，确认客资无效并按原领取流水返还积分，同时处理关联供资奖励。',labelText:'判定依据',required:true,minLength:2,submitLabel:'确认无效并返分',danger:true},async note=>{await api(`/v1.2/returns/${encodeURIComponent(id)}/direct-invalid`,{method:'POST',body:JSON.stringify({note})});toast('已直接判定无效并完成关联处理');await refreshAfterSuccess(returns,'已直接判定无效并完成关联处理')})}
async function taskDetail(id){
  const isCurrent=beginModalRequest();
  const x=await api(`/v1.2/return-verifications/tasks/${encodeURIComponent(id)}`);
  await loadTelesalesUsers();
  if(!isCurrent())return;
  const r=x.return_request||{},lead=x.lead||{},info=x.verification_info||{};
  const returnEvidences=r.evidences||x.return_evidences||[];
  const evidenceTotal=Object.values(r.evidence_summary||{}).reduce((sum,count)=>sum+Number(count||0),0)||returnEvidences.length;
  const finalAction=x.conclusion==='SUPPORT_RETURN'?'<button class="ops-btn primary" data-detail-final="APPROVE">确认无效并返分</button>':x.conclusion==='DOES_NOT_SUPPORT_RETURN'?'<button class="ops-btn primary" data-detail-final="REJECT">确认可用，回原领取人</button>':x.conclusion==='INCONCLUSIVE'?'<button class="ops-btn" data-detail-final="NEED_MORE">要求补充证据</button>':'';
  const directInvalid=!x.submitted_at&&['SUBMITTED','VERIFYING'].includes(r.status)?'<button class="ops-btn danger" data-detail-invalid>运营直接判定无效</button>':'';
  const actions=`<div class="ops-actions">${directInvalid}<button class="ops-btn" data-detail-assign="${esc(id)}">${x.assignee_user_id?'重新分配核验人员':'分配核验人员'}</button>${r.status==='REVIEWING'?finalAction:''}</div>`;
  modal('电话核验详情',`<div class="ops-detail-grid">${[['客户',lead.customer_name],['联系电话',lead.phone||lead.phone_masked],['所在地',`${lead.city||''} ${lead.district||''}`],['任务状态',verificationTaskLabel(x)],['核验人员',telesalesName(x.assignee_user_id)],['退回原因',label(r.reason_code)],['证据数量',`${evidenceTotal} 份`],['核验参考时间',fmt(x.due_at)],['48小时计时',returnAppealTiming(r)],['联系结果',label(x.contact_result)],['核验结论',label(x.conclusion)],['核验提交人',info.submitted_by_name],['核验提交时间',fmt(info.submitted_at)]].map(([a,b])=>`<div class="ops-detail"><small>${a}</small><b>${esc(b||'--')}</b></div>`).join('')}</div><section class="ops-card"><h3>申诉说明</h3><p>${esc(r.description||'暂无说明')}</p></section><section class="ops-card"><h3>加盟商提交的申诉证据</h3><div class="ops-detail-grid">${evidenceList(returnEvidences)}</div></section><section class="ops-card"><h3>核验备注</h3><p>${esc(info.note||'暂无核验备注')}</p></section><section class="ops-card"><h3>电销核验证据</h3><div class="ops-detail-grid">${evidenceList(info.evidences)}</div></section>${actions}`,()=>{
    bindEvidencePreviewErrors(modalRoot);
    document.querySelector('[data-detail-assign]')?.addEventListener('click',()=>assignTask(id));
    document.querySelector('[data-detail-invalid]')?.addEventListener('click',()=>directInvalidReturn(r.id));
    document.querySelector('[data-detail-final]')?.addEventListener('click',()=>finalReview(r.id,document.querySelector('[data-detail-final]').dataset.detailFinal));
  });
}
async function assignTask(id){
  const isCurrent=beginModalRequest();
  try{
    const users=await loadTelesalesUsers();
    if(!isCurrent())return;
    const options=users.map(user=>`<option value="${esc(user.id)}">${esc(user.display_name||user.username)}${user.username?` · ${esc(user.username)}`:''}</option>`).join('');
    modal('派发退回电话核验',users.length?`<form class="ops-form" id="return-assignment-form"><div class="ops-notice">电销只核实事实，不决定是否退回加盟商。改派会记录原责任人、当前责任人与派发理由。</div><div class="ops-field"><label for="telesales-assignee">电销人员 *</label><select class="ops-input" id="telesales-assignee">${options}</select></div><div class="ops-field"><label for="return-assignment-reason">派发或改派原因 *</label><textarea class="ops-textarea" id="return-assignment-reason" placeholder="例如：原人员请假，交由另一位电销继续核验"></textarea></div><div class="ops-actions"><button class="ops-btn" type="button" id="return-assignment-cancel">取消</button><button class="ops-btn primary" id="confirm-assignment">确认分配</button></div></form>`:'<div class="ops-empty">暂无可分配的电销人员</div>',()=>{
      const form=document.querySelector('#return-assignment-form');
      if(!form)return;
      document.querySelector('#return-assignment-cancel').onclick=closeModal;
      form.onsubmit=async event=>{
        event.preventDefault();
        const reason=document.querySelector('#return-assignment-reason').value.trim();
        const submit=document.querySelector('#confirm-assignment');
        if(reason.length<2){toast('请至少填写 2 个字的派发或改派原因',true);return}
        submit.disabled=true;
        try{
          await api(`/v1.2/return-verifications/tasks/${encodeURIComponent(id)}/assign`,{method:'POST',body:JSON.stringify({assignee_user_id:document.querySelector('#telesales-assignee').value,reason})});
          toast('电话核验任务已分配');
          closeModalFor(form);
          await refreshAfterSuccess(returns,'电话核验任务已分配');
        }catch(error){submit.disabled=false;toast(error.message,true)}
      };
    });
  }catch(error){toast(error.message,true)}
}
function financeRechargeSection(dashboard,companies,packages){
  const summary=dashboard.recharge_summary||{},recharge=dashboard.recharge||{};
  const metric=(title,value,detail)=>`<div class="ops-finance-metric"><small>${esc(title)}</small><b>${Number(value||0).toLocaleString('zh-CN')} 分</b><span>${esc(detail)}</span></div>`;
  const rows=(recharge.recent_records||[]).map(item=>{
    const original=Number(item.original_points??item.points??0).toLocaleString('zh-CN');
    const net=Number(item.points||0).toLocaleString('zh-CN');
    const points=item.reversed?`0（原 +${original}）`:`+${net}`;
    return `<tr><td>${fmt(item.created_at)}</td><td><b>${esc(item.company_name||'加盟商')}</b></td><td>${points}</td><td>${item.reversed?badge('REVERSED'):'有效'}</td><td>${Number(item.balance_after||0).toLocaleString('zh-CN')} 分</td><td>${esc(item.external_reference||'--')}</td></tr>`;
  });
  const canRecharge=companies.length&&packages.length;
  return `<section class="ops-card ops-recharge-hero"><div class="ops-recharge-hero-copy"><span>${icon('wallet')} 加盟商积分充值</span><h2>充值与账户余额</h2><p>先选择加盟商并核实线下收款，再为其积分账户入账；净充值已扣除后续冲正，每笔变动都保留凭据与审计记录。</p></div><button class="ops-btn primary ops-recharge-start" data-recharge-start ${canRecharge?'':'disabled'}>${icon('plus')}发起充值</button></section><section class="ops-finance-metrics">${metric(`近 ${S.financeDays} 天净充值`,summary.period_recharged_points,`${Number(summary.period_recharge_count||0)} 笔有效充值 · ${Number(summary.period_reversal_count||0)} 笔已冲正`)}${metric('累计净充值',summary.total_recharged_points,`${Number(summary.total_recharge_count||0)} 笔有效充值 · ${Number(summary.total_reversal_count||0)} 笔已冲正`)}${metric('当前剩余积分',summary.remaining_points,'加盟商账户可用余额汇总')}</section><section class="ops-card ops-recharge-records"><div class="ops-card-head"><div><h2>充值记录</h2><p>已冲正的充值以净额 0 展示；完整流水与冲正记录可在下方展开查看。</p></div></div>${table(['充值时间','加盟商','净到账积分','状态','充值后余额','外部凭据'],rows)}</section>`;
}
function financeRewardSection(dashboard){
  const summary=dashboard.summary||{},metric=(title,item)=>`<div class="ops-detail"><small>${esc(title)}</small><b>${Number(item?.points||0)} 分</b><span>${Number(item?.count||0)} 笔</span></div>`;
  const trend=(dashboard.trend||[]).slice(-7).map(item=>({label:item.date?.slice(5)||'--',value:Number(item.pending_points||0)+Number(item.settled_points||0),view:'finance'}));
  const ranking=(dashboard.source_ranking||[]).map(item=>`<div><b>${esc(item.label||'加盟商')}</b><span>${Number(item.points||0)} 分 · 已结算 ${Number(item.settled_points||0)} 分</span></div>`).join('')||'<p class="ops-muted">暂无奖励数据</p>';
  return `<section class="ops-card ops-finance-dashboard"><div class="ops-card-head"><div><h2>客资资金</h2><p>先看结算与争议，再按需进入积分账户和流水明细。</p></div></div><div class="ops-filter-row"><select class="ops-input" id="finance-dashboard-days"><option value="7" ${S.financeDays===7?'selected':''}>近 7 天</option><option value="30" ${S.financeDays===30?'selected':''}>近 30 天</option><option value="90" ${S.financeDays===90?'selected':''}>近 90 天</option></select><select class="ops-input" id="finance-dashboard-status"><option value="" ${S.financeRewardStatus===''?'selected':''}>全部状态</option><option value="OBSERVING" ${S.financeRewardStatus==='OBSERVING'?'selected':''}>待结算</option><option value="SETTLED" ${S.financeRewardStatus==='SETTLED'?'selected':''}>已结算</option><option value="FROZEN" ${S.financeRewardStatus==='FROZEN'?'selected':''}>退回争议</option><option value="CANCELLED" ${S.financeRewardStatus==='CANCELLED'?'selected':''}>已作废</option></select><select class="ops-input" id="finance-dashboard-source"><option value="" ${S.financeSource===''?'selected':''}>全部来源</option><option value="PLATFORM_MANUAL" ${S.financeSource==='PLATFORM_MANUAL'?'selected':''}>平台录入</option><option value="SUPPLIER_H5" ${S.financeSource==='SUPPLIER_H5'?'selected':''}>加盟商提供</option></select></div><div class="ops-detail-grid">${metric('奖励待结算',summary.pending_settlement)}${metric('已结算',summary.settled)}${metric('退回争议',summary.disputed)}${metric('作废积分',summary.voided)}</div></section>${decisionBars('奖励趋势','展示待结算与已结算奖励的总量变化。',trend)}<section class="ops-card ops-chart-card"><div class="ops-card-head"><div><h2>来源 / 加盟商排行</h2><p>按奖励积分观察供资质量与结算表现。</p></div></div><div class="ops-distribution-list">${ranking}</div></section>`
}
function pointFlowSection(data){
  const summary=data.summary||{};
  const inputType=S.pointFlowPeriod==='month'?'month':'date';
  const inputValue=S.pointFlowPeriod==='month'?S.pointFlowAnchor.slice(0,7):S.pointFlowAnchor;
  const rows=(data.items||[]).map((item,index)=>{const refund=Number(item.receiver_points_refunded||0),reversed=Number(item.supplier_reward_reversed_points||0);return `<tr><td>${item.sequence??rowSequence(data,index)}</td><td><b>${esc(item.customer_name||'未填写')}</b><br><small>${esc(item.lead_code||recordCode(item.lead_id,'KZ'))}</small></td><td>${esc(item.receiver_company_name||'未记录')}</td><td>${Number(item.receiver_points_consumed||0)} 分${refund?`<br><small>已退 ${refund} 分 · ${fmt(item.receiver_refunded_at)}</small>`:''}</td><td>${esc(item.supplier_company_name||'平台')}<br><small>${Number(item.supplier_reward_points||0)} 分 · ${esc(label(item.settlement_status))}${reversed?`<br>已冲回 ${reversed} 分 · ${fmt(item.supplier_reward_reversed_at)}`:''}</small></td><td>${Number(item.platform_net_points||0)} 分</td><td>${fmt(item.transaction_confirmed_at)}</td><td>${fmt(item.reward_settled_at)}</td><td><button class="ops-btn" data-point-flow-lead="${esc(item.lead_id)}">查看客资</button></td></tr>`});
  const pages=Math.max(1,Math.ceil(Number(data.total||0)/Number(data.page_size||20)));
  const pager=data.total?`<div class="ops-pager"><button class="ops-btn" id="point-flow-prev" ${S.pointFlowPage<=1?'disabled':''}>上一页</button><span>${S.pointFlowPage}/${pages}，共 ${Number(data.total||0)} 条</span><button class="ops-btn" id="point-flow-next" ${S.pointFlowPage>=pages?'disabled':''}>下一页</button></div>`:'';
  return `<section class="ops-card"><div class="ops-card-head"><div><h2>客资积分经营统计</h2><p>按自然日或自然月查看新增客资，以及每条已确认交易的接收方消耗、供资奖励、后续退回冲回和平台最终净积分。</p></div></div><form class="ops-filter-row" id="point-flow-filter"><select class="ops-input" id="point-flow-period"><option value="day" ${S.pointFlowPeriod==='day'?'selected':''}>按日</option><option value="month" ${S.pointFlowPeriod==='month'?'selected':''}>按月</option></select><input class="ops-input" id="point-flow-anchor" type="${inputType}" value="${esc(inputValue)}" required><button class="ops-btn primary" type="submit">查询</button></form><div class="ops-detail-grid">${[['新增客资',summary.new_lead_count||0],['交易确认客资',summary.transaction_confirmed_count||0],['接收方消耗',`${Number(summary.receiver_points_consumed||0)} 分`],['接收方退款',`${Number(summary.receiver_points_refunded||0)} 分`],['供资奖励',`${Number(summary.supplier_reward_points||0)} 分`],['奖励冲回',`${Number(summary.supplier_reward_reversed_points||0)} 分`],['平台最终净积分',`${Number(summary.platform_net_points||0)} 分`]].map(([name,value])=>`<div class="ops-detail"><small>${esc(name)}</small><b>${esc(value)}</b></div>`).join('')}</div>${table(['序号','客资','接收加盟商','消耗 / 退款','提供加盟商 / 奖励 / 冲回','平台净积分','成交确认时间','奖励实际到账','操作'],rows)}${pager}</section>`;
}
function bindPointFlowControls(refresh){
  document.querySelector('#point-flow-period').onchange=event=>{S.pointFlowPeriod=event.target.value;S.pointFlowPage=1;refresh()};
  document.querySelector('#point-flow-filter').onsubmit=event=>{event.preventDefault();const value=document.querySelector('#point-flow-anchor').value;if(!value)return;S.pointFlowAnchor=S.pointFlowPeriod==='month'?`${value}-01`:value;S.pointFlowPage=1;refresh()};
  document.querySelector('#point-flow-prev')?.addEventListener('click',()=>{S.pointFlowPage=Math.max(1,S.pointFlowPage-1);refresh()});
  document.querySelector('#point-flow-next')?.addEventListener('click',()=>{S.pointFlowPage+=1;refresh()});
  document.querySelectorAll('[data-point-flow-lead]').forEach(button=>button.onclick=()=>go('trace',button.dataset.pointFlowLead));
}
function leadPointsSettingsSection(settings){
  const status=settings.configured?'固定积分已启用':'首次使用请填写两个积分并保存；保存前沿用现有积分规则。';
  return `<section class="ops-card"><div class="ops-card-head"><div><h2>客资固定积分</h2><p>分别设置加盟商领取客资扣除的积分，以及向平台提供客资获得的供客积分。</p></div></div><form class="ops-form" id="lead-points-form"><div class="ops-row"><div class="ops-field"><label for="operation-claim-points">客资领取积分（积分/条）</label><input class="ops-input" id="operation-claim-points" type="number" min="1" max="2147483647" step="1" required value="${esc(settings.operation_claim_points??'')}"><small class="ops-muted">适用于所有来源的客资，领取时按后台最新设置生效；已派发但未领取的客资也立即生效，历史已扣费记录保持原金额。</small></div><div class="ops-field"><label for="supplier-provision-points">加盟商供客积分（积分/条）</label><input class="ops-input" id="supplier-provision-points" type="number" min="0" max="2147483647" step="1" required value="${esc(settings.supplier_provision_points??'')}"><small class="ops-muted">供客奖励独立计算：人工电话确认有效时及时结算；否则领取满48小时无正式退回申请时自动结算。已入账奖励在退回通过时自动冲回，驳回则保持入账。设为 0 时不发放。</small></div></div><p class="ops-muted" id="lead-points-message" role="status">${esc(status)}</p><button class="ops-btn primary" id="lead-points-save" type="submit">保存固定积分</button></form></section>`;
}
function bindLeadPointsSettings(settings){
  const form=document.querySelector('#lead-points-form'),submit=document.querySelector('#lead-points-save');
  let version=settings.version;
  form.onsubmit=async event=>{
    event.preventDefault();
    if(submit.disabled)return;
    const operationInput=document.querySelector('#operation-claim-points').value.trim(),supplierInput=document.querySelector('#supplier-provision-points').value.trim();
    const operation_claim_points=Number(operationInput),supplier_provision_points=Number(supplierInput);
    if(!operationInput||!supplierInput||!Number.isSafeInteger(operation_claim_points)||operation_claim_points<1||operation_claim_points>2147483647||!Number.isSafeInteger(supplier_provision_points)||supplier_provision_points<0||supplier_provision_points>2147483647){toast('领取积分须为正整数，供客积分须为非负整数，且不能超过 2147483647',true);return}
    submit.disabled=true;
    try{
      const saved=await api('/v1.2/admin/lead-points-settings',{method:'PUT',body:JSON.stringify({operation_claim_points,supplier_provision_points,expected_version:version})});
      version=saved.version;
      document.querySelector('#lead-points-message').textContent='固定积分已保存；未领取客资立即按最新积分执行，历史已扣费及已生成的供客积分记录保持原金额。';
      toast('固定积分已保存');
    }catch(error){toast(error.message,true)}finally{submit.disabled=false}
  };
}
const SUPPLY_TERMINATION_BLOCKER_LABEL={POINTS_SPLIT_RECONCILIATION:'历史积分分账',POINTS_RECONCILIATION:'积分账目核对',SUPPLIED_LEADS_UNFINISHED:'供资客资未完成',RETURNS_UNFINISHED:'退回申请未完成',REWARDS_UNFINISHED:'供客奖励未完成'};
function supplyTerminationBlockers(items){return (items||[]).map(item=>`<li><b>${esc(item.label||SUPPLY_TERMINATION_BLOCKER_LABEL[item.code]||'待处理事项')}</b><span>${esc(item.message||item.description||'完成相关业务后重新复检')}</span>${item.record_ids?.length?`<small>相关记录：${esc(item.record_ids.map(id=>recordCode(id,'JL')).join('、'))}</small>`:''}</li>`).join('')||'<li><b>无阻断项</b><span>可以继续审核或付款流程</span></li>'}
function supplyTerminationActions(item){const review=can('supply.termination.review'),pay=can('supply.termination.pay'),reopen=can('supply.termination.reopen');return `<button class="ops-btn" data-termination-detail="${esc(item.id)}">详情</button>${review&&['REQUESTED','NEED_MORE'].includes(item.status)?` <button class="ops-btn" data-termination-refresh="${esc(item.id)}">清算复检</button>`:''}${review&&['REQUESTED','NEED_MORE'].includes(item.status)?` <button class="ops-btn primary" data-termination-review="${esc(item.id)}">审核</button>`:''}${pay&&['APPROVED_PENDING_PAYMENT','PAYMENT_FAILED'].includes(item.status)?` <button class="ops-btn gold" data-termination-payment="${esc(item.id)}">${item.status==='PAYMENT_FAILED'?'重新登记线下付款':'登记线下付款'}</button>`:''}${pay&&item.status==='PAID_PENDING_WRITE_OFF'?` <button class="ops-btn" data-termination-payment-correction="${esc(item.id)}">更正付款登记</button> <button class="ops-btn danger" data-termination-confirm="${esc(item.id)}">确认付款并核销积分</button>`:''}${pay&&['APPROVED_PENDING_PAYMENT','PAID_PENDING_WRITE_OFF'].includes(item.status)?` <button class="ops-btn danger" data-termination-payment-fail="${esc(item.id)}">登记付款失败</button>`:''}${reopen&&item.status==='TERMINATED'?` <button class="ops-btn" data-termination-reopen="${esc(item.company_id)}">恢复客资合作</button>`:''}`}
function supplyTerminationRate(configs){return (configs||[]).find(item=>item.key==='cashout_rate'&&item.status==='PUBLISHED')||null}
function supplyTerminationRateSection(configs,editable=false){const config=supplyTerminationRate(configs),rate=config?.value?.cash_cents_per_point;return `<section class="ops-card"><div class="ops-card-head"><div><h2>终止合作积分兑换比例</h2><p>结算审核通过时按当时已发布比例生成不可变快照。</p></div>${rate?badge('APPROVED','已配置'):badge('PENDING','未配置')}</div>${editable?`<form class="ops-form" id="termination-rate-form"><div class="ops-field"><label for="termination-rate-value">积分兑换比例（分/积分）</label><input class="ops-input" id="termination-rate-value" type="number" min="1" step="1" value="${esc(rate??'')}" required><small class="ops-muted">例如填写 100，表示每 1 积分按 1.00 元结算。</small></div><button class="ops-btn primary" id="termination-rate-submit">发布兑换比例</button></form>`:`<div class="ops-detail-grid"><div class="ops-detail"><small>当前积分兑换比例（分/积分）</small><b>${rate??'尚未配置'}</b></div></div>`}</section>`}
function bindSupplyTerminationRate(){const form=document.querySelector('#termination-rate-form');if(!form)return;const submit=document.querySelector('#termination-rate-submit');form.onsubmit=async event=>{event.preventDefault();const cash_cents_per_point=Number(document.querySelector('#termination-rate-value').value);if(!Number.isSafeInteger(cash_cents_per_point)||cash_cents_per_point<=0){toast('兑换比例必须是正整数分/积分',true);return}submit.disabled=true;try{await api('/system-configs',{method:'POST',body:JSON.stringify({domain:'supply_termination',key:'cashout_rate',value:{cash_cents_per_point},publish_immediately:true})});toast('终止合作积分兑换比例已发布');await finance()}catch(error){if(form.isConnected)submit.disabled=false;toast(error.message,true)}}}
function supplyTerminationSection(pageData){const rows=(pageData.items||[]).map((item,index)=>`<tr><td>${(Number(pageData.page||1)-1)*Number(pageData.page_size||20)+index+1}</td><td><b>${esc(item.company_name||recordCode(item.company_id,'加盟商'))}</b><br><small>${fmt(item.created_at)}</small></td><td>${badge(item.status)}</td><td>${Number(item.customer_points_snapshot||0)} / ${Number(item.supply_points_snapshot||0)}</td><td>${Number(item.general_points_snapshot||0)} 分<br><small>¥${(Number(item.cash_amount_cents_snapshot||0)/100).toFixed(2)}</small></td><td>${(item.blockers||[]).length} 项</td><td>${supplyTerminationActions(item)}</td></tr>`);return `<section class="ops-card" id="supply-termination-settlements"><div class="ops-card-head"><div><h2>终止客资合作结算</h2><p>两类积分只在终止合作时合并计算；平台线下付款后分别核销本次冻结快照积分，快照后新增客资积分保留。公司与登录账号继续保留。</p></div></div>${table(['序号','加盟商','状态','客资 / 供客积分','终止合作通用积分 / 金额','阻断项','操作'],rows)}${Number(pageData.total||0)>Number(pageData.page_size||20)?`<div class="ops-pager"><button class="ops-btn" id="termination-prev" ${S.supplyTerminationPage<=1?'disabled':''}>上一页</button><span>第 ${S.supplyTerminationPage} 页，共 ${Number(pageData.total||0)} 条</span><button class="ops-btn" id="termination-next" ${S.supplyTerminationPage>=Math.ceil(Number(pageData.total||0)/Number(pageData.page_size||20))?'disabled':''}>下一页</button></div>`:''}<div class="ops-notice">付款确认和积分核销使用同一幂等业务指令，不编辑历史流水；历史终止与付款记录会保留，恢复合作也不会删除记录。</div></section>`}
function supplyTerminationDetail(id){const item=S.supplyTerminations.find(candidate=>candidate.id===id);if(!item){toast('未找到终止合作记录',true);return}const payeeAccount=can('supply.termination.pay')?(item.payee_account||item.payee_account_masked):item.payee_account_masked;modal('终止客资合作详情',`<div class="ops-detail-grid">${[['加盟商',item.company_name||recordCode(item.company_id,'加盟商')],['状态',label(item.status)],['终止原因',item.reason],['客资积分',item.customer_points_snapshot],['供客积分',item.supply_points_snapshot],['终止合作通用积分',item.general_points_snapshot],['兑换比例',`1 积分 = ¥${(Number(item.cash_cents_per_point_snapshot||0)/100).toFixed(2)}`],['系统应付金额',`¥${(Number(item.cash_amount_cents_snapshot||0)/100).toFixed(2)}`],['实际付款金额',item.payment_amount_cents==null?'--':`¥${(Number(item.payment_amount_cents)/100).toFixed(2)}`],['收款人',item.payee_name],['收款账号',payeeAccount],['付款方式',item.payment_method],['付款流水号',item.payment_external_reference],['付款凭证地址',item.payment_proof_url],['付款时间',fmt(item.paid_at)],['终止完成时间',fmt(item.terminated_at)]].map(([name,value])=>`<div class="ops-detail"><small>${esc(name)}</small><b>${esc(value??'--')}</b></div>`).join('')}</div><section class="ops-card"><h3>清算阻断项</h3><ul class="ops-termination-blockers">${supplyTerminationBlockers(item.blockers)}</ul></section>`)}
function refreshSupplyTerminationView(){return S.view==='economics'?economics():finance()}
async function refreshSupplyTermination(id){await api(`/v1.2/supply-terminations/${encodeURIComponent(id)}/refresh`,{method:'POST'});toast('清算复检已完成');await refreshSupplyTerminationView()}
function reviewSupplyTermination(id){modal('审核终止客资合作',`<form class="ops-form" id="termination-review-form"><div class="ops-field"><label for="termination-review-decision">审核结论</label><select class="ops-input" id="termination-review-decision"><option value="APPROVE">审核通过</option><option value="NEED_MORE">要求补充</option><option value="REJECT">驳回申请</option></select></div><div class="ops-field"><label for="termination-review-note">审核说明</label><textarea class="ops-textarea" id="termination-review-note" minlength="3" required></textarea></div><button class="ops-btn primary" id="termination-review-submit">提交审核</button></form>`,()=>{const form=document.querySelector('#termination-review-form'),submit=document.querySelector('#termination-review-submit');form.onsubmit=async event=>{event.preventDefault();submit.disabled=true;try{await api(`/v1.2/supply-terminations/${encodeURIComponent(id)}/review`,{method:'POST',body:JSON.stringify({decision:document.querySelector('#termination-review-decision').value,note:document.querySelector('#termination-review-note').value.trim()})});closeModalFor(form);toast('审核结果已保存');await refreshSupplyTerminationView()}catch(error){if(form.isConnected)submit.disabled=false;toast(error.message,true)}}})}
function datetimeLocalValue(value){const date=value?new Date(value):null;if(!date||!Number.isFinite(date.getTime()))return'';return new Date(date.getTime()-date.getTimezoneOffset()*60000).toISOString().slice(0,16)}
function recordSupplyTerminationPayment(id,correction=false){const item=S.supplyTerminations.find(candidate=>candidate.id===id),amountCents=Number(item?.payment_amount_cents??item?.cash_amount_cents_snapshot??0);modal(correction?'更正付款登记':'登记线下付款',`<form class="ops-form" id="termination-payment-form"><div class="ops-notice">${correction?'仅在确认核销前更正付款流水、金额、时间或凭证信息。':'请核对系统应付金额，完成线下付款后登记实际付款金额。此步骤不会立即核销积分。'}</div><div class="ops-detail-grid"><div class="ops-detail"><small>系统应付金额</small><b>¥${(Number(item?.cash_amount_cents_snapshot||0)/100).toFixed(2)}</b></div></div><div class="ops-field"><label for="termination-payment-amount">实际付款金额（元）</label><input class="ops-input" id="termination-payment-amount" type="number" min="0" step="0.01" value="${(amountCents/100).toFixed(2)}" required></div><div class="ops-field"><label for="termination-payment-reference">付款流水号</label><input class="ops-input" id="termination-payment-reference" value="${esc(correction?item?.payment_external_reference||'':'')}" required></div><div class="ops-field"><label for="termination-paid-at">付款时间</label><input class="ops-input" id="termination-paid-at" type="datetime-local" value="${esc(correction?datetimeLocalValue(item?.paid_at):'')}" required></div><div class="ops-field"><label for="termination-payment-proof">付款凭证地址（可选）</label><input class="ops-input" id="termination-payment-proof" type="url" value="${esc(correction?item?.payment_proof_url||'':'')}" placeholder="已上传凭证的 HTTPS 地址"></div><div class="ops-field"><label for="termination-payment-note">付款凭证说明</label><textarea class="ops-textarea" id="termination-payment-note" minlength="3" required>${esc(correction?item?.payment_note||'':'')}</textarea></div><button class="ops-btn primary" id="termination-payment-submit">${correction?'保存付款更正':'确认登记'}</button></form>`,()=>{const form=document.querySelector('#termination-payment-form'),submit=document.querySelector('#termination-payment-submit');form.onsubmit=async event=>{event.preventDefault();const rawAmount=document.querySelector('#termination-payment-amount').value.trim();if(!/^\d+(?:\.\d{1,2})?$/.test(rawAmount)){toast('实际付款金额最多保留两位小数',true);return}const payment_amount_cents=Math.round(Number(rawAmount)*100);if(!Number.isSafeInteger(payment_amount_cents)||payment_amount_cents<0){toast('请输入有效的实际付款金额',true);return}submit.disabled=true;const proof_url=document.querySelector('#termination-payment-proof').value.trim()||null;try{await api(`/v1.2/supply-terminations/${encodeURIComponent(id)}/payment`,{method:correction?'PATCH':'POST',body:JSON.stringify({external_reference:document.querySelector('#termination-payment-reference').value.trim(),payment_amount_cents,paid_at:new Date(document.querySelector('#termination-paid-at').value).toISOString(),note:document.querySelector('#termination-payment-note').value.trim(),proof_url})});closeModalFor(form);toast(correction?'付款登记已更正':'线下付款已登记');await refreshSupplyTerminationView()}catch(error){if(form.isConnected)submit.disabled=false;toast(error.message,true)}}})}
function failSupplyTerminationPayment(id){actionForm({title:'登记付款失败',message:'登记后会清除当前付款信息并保留积分冻结，终止申请继续等待重新付款；不会生成核销流水。',labelText:'失败原因',required:true,minLength:2,submitLabel:'确认付款失败',danger:true},async note=>{await api(`/v1.2/supply-terminations/${encodeURIComponent(id)}/payment/fail`,{method:'POST',body:JSON.stringify({note})});toast('付款失败已登记，请重新付款后录入');await refreshSupplyTerminationView()})}
function confirmSupplyTermination(id){modal('确认付款并核销积分',`<form class="ops-form" id="termination-confirm-form"><div class="ops-notice">请再次确认线下款项已实际付出。确认后系统会分别生成本次冻结快照积分的负向核销流水；快照后新增客资积分保留。</div><label class="ops-check"><input type="checkbox" id="termination-payment-confirmed" required> 我已核实线下款项实际付出</label><div class="ops-actions"><button class="ops-btn" id="termination-confirm-cancel" type="button">取消</button><button class="ops-btn danger" id="termination-confirm-submit">确认付款并核销积分</button></div></form>`,()=>{const form=document.querySelector('#termination-confirm-form'),submit=document.querySelector('#termination-confirm-submit');document.querySelector('#termination-confirm-cancel').onclick=closeModal;form.onsubmit=async event=>{event.preventDefault();if(!document.querySelector('#termination-payment-confirmed').checked){toast('请先确认线下款项已实际付出',true);return}submit.disabled=true;lockModal(form,document.querySelector('#termination-confirm-cancel'));try{await api(`/v1.2/supply-terminations/${encodeURIComponent(id)}/confirm`,{method:'POST',body:JSON.stringify({confirmed:true,idempotency_key:`supply-termination-${id}`})});unlockModal(form);closeModalFor(form);toast('付款已确认，积分已核销');await refreshSupplyTerminationView()}catch(error){unlockModal(form);if(form.isConnected)submit.disabled=false;toast(error.message,true)}}})}
function reopenSupplyCooperation(companyId){actionForm({title:'恢复客资合作',message:'恢复后可重新提供新客资；历史终止、付款和核销记录继续保留。',labelText:'恢复说明',required:true,minLength:3,submitLabel:'恢复客资合作'},async note=>{await api(`/v1.2/companies/${encodeURIComponent(companyId)}/supply-cooperation/reopen`,{method:'POST',body:JSON.stringify({note})});toast('客资合作已恢复');await refreshSupplyTerminationView()})}
function bindSupplyTerminationActions(){document.querySelector('#termination-prev')?.addEventListener('click',()=>{S.supplyTerminationPage=Math.max(1,S.supplyTerminationPage-1);refreshSupplyTerminationView()});document.querySelector('#termination-next')?.addEventListener('click',()=>{S.supplyTerminationPage+=1;refreshSupplyTerminationView()});document.querySelectorAll('[data-termination-detail]').forEach(button=>button.onclick=()=>supplyTerminationDetail(button.dataset.terminationDetail));document.querySelectorAll('[data-termination-refresh]').forEach(button=>button.onclick=()=>refreshSupplyTermination(button.dataset.terminationRefresh));document.querySelectorAll('[data-termination-review]').forEach(button=>button.onclick=()=>reviewSupplyTermination(button.dataset.terminationReview));document.querySelectorAll('[data-termination-payment]').forEach(button=>button.onclick=()=>recordSupplyTerminationPayment(button.dataset.terminationPayment));document.querySelectorAll('[data-termination-payment-correction]').forEach(button=>button.onclick=()=>recordSupplyTerminationPayment(button.dataset.terminationPaymentCorrection,true));document.querySelectorAll('[data-termination-payment-fail]').forEach(button=>button.onclick=()=>failSupplyTerminationPayment(button.dataset.terminationPaymentFail));document.querySelectorAll('[data-termination-confirm]').forEach(button=>button.onclick=()=>confirmSupplyTermination(button.dataset.terminationConfirm));document.querySelectorAll('[data-termination-reopen]').forEach(button=>button.onclick=()=>reopenSupplyCooperation(button.dataset.terminationReopen))}
async function economics(){
  const [pointFlows,terminationPage,terminationRateConfigs]=await Promise.all([api(`/v1.2/reports/point-flows${qs({period:S.pointFlowPeriod,anchor:S.pointFlowAnchor,page:S.pointFlowPage,page_size:20})}`),api(`/v1.2/supply-terminations?page=${S.supplyTerminationPage}&page_size=20`),api('/system-configs?domain=supply_termination&status=PUBLISHED')]);
  S.pointFlows=pointFlows;
  S.supplyTerminations=terminationPage.items||[];
  shell(`${supplyTerminationRateSection(terminationRateConfigs)}${supplyTerminationSection(terminationPage)}${pointFlowSection(pointFlows)}`);
  bindSupplyTerminationActions();
  bindPointFlowControls(economics);
}
async function finance(){
  const [financeDashboard,pointFlows,companyPage,activePackages,allPackages,priceRules,ledgerPage,rewardPage,currentRewardRule,cities,leadPointsSettings,terminationPage,terminationRateConfigs,withdrawalPage,withdrawalPolicy]=await Promise.all([
    api(`/v1.2/reports/finance-dashboard${qs({days:S.financeDays,status:S.financeRewardStatus||undefined,source:S.financeSource||undefined})}`),
    api(`/v1.2/reports/point-flows${qs({period:S.pointFlowPeriod,anchor:S.pointFlowAnchor,page:S.pointFlowPage,page_size:20})}`),
    api(`/companies${qs({keyword:S.financeCompanyKeyword,status:S.financeCompanyStatus,page:S.financeCompanyPage,page_size:20})}`),
    api('/points/packages?active_only=true'),
    api('/points/packages?active_only=false'),
    api('/points/price-rules'),
    api(`/points/ledgers${qs({company_id:S.financeCompanyId||undefined,ledger_type:S.financeLedgerType||undefined,point_kind:S.financePointKind||undefined,page:S.page,page_size:20})}`),
    api(`/v1.2/supplier-rewards${qs({supplier_company_id:S.financeCompanyId||undefined,page:S.financeRewardPage,page_size:20})}`),
    api('/v1.2/admin/supplier-reward-rules/current'),
    platformCities(),
    api('/v1.2/admin/lead-points-settings'),
    api(`/v1.2/supply-terminations?page=${S.supplyTerminationPage}&page_size=20`),
    api('/system-configs?domain=supply_termination&status=PUBLISHED'),
    api(`/v1.2/supply-withdrawals${qs({page:S.financeWithdrawalPage||1,page_size:20})}`),
    can('*')?api('/v1.2/supply-withdrawals/policy'):Promise.resolve(null),
  ]);
  S.pointFlows=pointFlows;
  S.supplyTerminations=terminationPage.items||[];
  const companies=companyPage.items||[];
  const companyNames=new Map(companies.map(company=>[company.id,company.name]));
  const cityNames=new Map((cities||[]).map(city=>[city.code,city.name]));
  const financeCompanyPages=Math.max(1,Math.ceil((companyPage.total||0)/(companyPage.page_size||20)));
  const companyRows=companies.map(company=>`<tr><td><b>${esc(company.name)}</b><br><small>${esc(company.code)}</small></td><td>${badge(company.status)}</td><td><b>客资 ${Number(company.customer_points_balance??company.points_balance??0).toLocaleString('zh-CN')} 分</b><br><small>供客 ${Number(company.supply_points_balance??0).toLocaleString('zh-CN')} 分</small></td><td>${company.id===S.financeCompanyId?'<span class="ops-status ok">正在查看</span>':'<button class="ops-btn" data-finance-company="'+esc(company.id)+'">查看流水</button>'} <button class="ops-btn" data-reconcile-company="${esc(company.id)}">对账</button> <button class="ops-btn" data-adjust-company="${esc(company.id)}">调账</button> <button class="ops-btn primary" data-recharge-company="${esc(company.id)}">充值</button></td></tr>`);
  const packageRows=(allPackages||[]).map(item=>`<tr><td>${esc(item.name)}<br><small>${esc(item.code)} · V${esc(item.version)}</small></td><td>${Number(item.cash_amount_cents||0)/100} 元</td><td>${esc(item.base_points)}</td><td>${esc(item.bonus_points)}</td><td>${esc(item.total_points)}</td><td>${badge(item.status)}</td></tr>`);
  const priceRows=(priceRules||[]).map(item=>`<tr><td>${esc(item.region_code?cityNames.get(item.region_code)||item.region_code:'全部地区')}</td><td>${esc(item.category_code||'全部类目')}</td><td>${esc(item.brand_code||'全部品牌')}</td><td>${esc(item.level_code||'全部等级')}</td><td>${esc(item.points_cost)}</td><td>${badge(item.status)}</td></tr>`);
  const ledgerRows=(ledgerPage.items||[]).map(ledger=>{const ledgerType=ledger.ledger_type||ledger.type;const reversible=['RECHARGE','ADJUST'].includes(ledgerType);return `<tr><td>${fmt(ledger.created_at)}</td><td>${esc(companyNames.get(ledger.company_id)||recordCode(ledger.company_id,'加盟商'))}</td><td>${esc(label(ledger.point_kind||'CUSTOMER'))}</td><td>${esc(label(ledgerType))}</td><td>${esc(ledger.delta>0?`+${ledger.delta}`:ledger.delta)}</td><td>${esc(ledger.balance_after)}</td><td>${esc(ledger.external_reference||'--')}</td><td>${reversible?`<button class="ops-btn danger" data-ledger-reverse="${esc(ledger.id)}">冲正</button>`:'业务流程处理'}</td></tr>`});
  shell(`${leadPointsSettingsSection(leadPointsSettings)}${supplyTerminationRateSection(terminationRateConfigs,true)}${supplyTerminationSection(terminationPage)}${financeWithdrawalSection(withdrawalPage,withdrawalPolicy)}${pointFlowSection(pointFlows)}${financeRechargeSection(financeDashboard,companies,activePackages)}<section class="ops-card ops-finance-accounts"><div class="ops-card-head"><div><h2>加盟商账户与余额</h2><p>选择一个加盟商即可充值、核对账目或查看其完整积分流水。</p></div></div><form class="ops-filter" id="finance-company-filter"><input class="ops-input" id="finance-company-keyword" value="${esc(S.financeCompanyKeyword)}" placeholder="搜索公司名称或编号"><select class="ops-input" id="finance-company-status"><option value="" ${S.financeCompanyStatus===''?'selected':''}>全部状态</option><option value="ACTIVE" ${S.financeCompanyStatus==='ACTIVE'?'selected':''}>正常</option><option value="PENDING" ${S.financeCompanyStatus==='PENDING'?'selected':''}>待审核</option><option value="DISABLED" ${S.financeCompanyStatus==='DISABLED'?'selected':''}>已停用</option></select><button class="ops-btn primary" type="submit">查询</button><button class="ops-btn" type="button" id="finance-company-reset">重置</button>${S.financeCompanyId?'<button class="ops-btn" type="button" id="finance-company-clear">查看全部账户</button>':''}</form>${table(['加盟商','状态','客资积分 / 供客积分','操作'],companyRows)}<div class="ops-pager"><button class="ops-btn" id="finance-company-prev" ${S.financeCompanyPage<=1?'disabled':''}>上一页</button><span>${S.financeCompanyPage}/${financeCompanyPages}，共 ${companyPage.total||0} 家</span><button class="ops-btn" id="finance-company-next" ${S.financeCompanyPage>=financeCompanyPages?'disabled':''}>下一页</button></div></section><details class="ops-finance-drilldown"><summary>查看奖励结算、档位与完整流水</summary><div class="ops-finance-detail-body">${financeRewardSection(financeDashboard)}<section class="ops-card"><div class="ops-card-head"><h2>充值档位</h2><button class="ops-btn primary" id="new-package">新增充值档位</button></div>${table(['档位','线下实收','基础积分','赠送积分','到账积分','状态'],packageRows)}</section><section class="ops-card"><div class="ops-card-head"><div><h2>通用领取价格规则</h2><p>上方未启用统一客资领取积分时，此处规则才用于计算领取价格。</p></div><button class="ops-btn primary" id="new-price-rule">新增价格规则</button></div>${table(['适用地区','业务类目','品牌','加盟商等级','领取积分','状态'],priceRows)}</section><section class="ops-card"><div class="ops-card-head"><div><h2>完整积分流水</h2><p>只允许冲正人工充值和人工调账；领取、退回与奖励必须通过相应业务流程处理。</p></div><select class="ops-input" id="finance-ledger-type" style="width:auto"><option value="" ${S.financeLedgerType===''?'selected':''}>全部类型</option><option value="RECHARGE" ${S.financeLedgerType==='RECHARGE'?'selected':''}>充值</option><option value="ADJUST" ${S.financeLedgerType==='ADJUST'?'selected':''}>人工调整</option><option value="REVERSE" ${S.financeLedgerType==='REVERSE'?'selected':''}>冲正</option></select><select class="ops-input" id="finance-point-kind" style="width:auto"><option value="" ${S.financePointKind===''?'selected':''}>全部积分账户</option><option value="CUSTOMER" ${S.financePointKind==='CUSTOMER'?'selected':''}>客资积分</option><option value="SUPPLY" ${S.financePointKind==='SUPPLY'?'selected':''}>供客积分</option></select></div>${table(['时间','加盟商','积分账户','类型','变化','余额','外部凭据','操作'],ledgerRows)}${pager(ledgerPage)}</section>${rewardSection(rewardPage,currentRewardRule)}</div></details>`);
  bindLeadPointsSettings(leadPointsSettings);
  bindSupplyTerminationRate();
  bindSupplyTerminationActions();
  document.querySelector('#finance-dashboard-days').onchange=event=>{S.financeDays=Number(event.target.value);finance()};
  document.querySelector('#finance-dashboard-status').onchange=event=>{S.financeRewardStatus=event.target.value;finance()};
  document.querySelector('#finance-dashboard-source').onchange=event=>{S.financeSource=event.target.value;finance()};
  bindPointFlowControls(finance);
  bindPager(ledgerPage,finance);
  document.querySelector('#finance-company-filter').onsubmit=event=>{event.preventDefault();S.financeCompanyKeyword=document.querySelector('#finance-company-keyword').value.trim();S.financeCompanyStatus=document.querySelector('#finance-company-status').value;S.financeCompanyPage=1;finance()};
  document.querySelector('#finance-company-reset').onclick=()=>{S.financeCompanyKeyword='';S.financeCompanyStatus='';S.financeCompanyPage=1;finance()};
  document.querySelector('#finance-company-clear')?.addEventListener('click',()=>{S.financeCompanyId='';S.financeRewardPage=1;S.page=1;finance()});
  document.querySelector('#finance-company-prev').onclick=()=>{S.financeCompanyPage=Math.max(1,S.financeCompanyPage-1);finance()};
  document.querySelector('#finance-company-next').onclick=()=>{S.financeCompanyPage=Math.min(financeCompanyPages,S.financeCompanyPage+1);finance()};
  document.querySelector('#finance-ledger-type').onchange=event=>{S.financeLedgerType=event.target.value;S.page=1;finance()};
  document.querySelector('#finance-point-kind').onchange=event=>{S.financePointKind=event.target.value;S.page=1;finance()};
  document.querySelectorAll('[data-finance-company]').forEach(button=>button.onclick=()=>{S.financeCompanyId=button.dataset.financeCompany;S.financeRewardPage=1;S.page=1;finance()});
  document.querySelectorAll('[data-reconcile-company]').forEach(button=>button.onclick=()=>reconcileCompanyPoints(button.dataset.reconcileCompany,companies));
  document.querySelectorAll('[data-adjust-company]').forEach(button=>button.onclick=()=>adjustCompanyPoints(button.dataset.adjustCompany,companies));
  document.querySelectorAll('[data-recharge-company]').forEach(button=>button.onclick=()=>rechargeCompanyPoints(button.dataset.rechargeCompany,companies,activePackages));
  document.querySelectorAll('[data-recharge-start]').forEach(button=>button.onclick=()=>rechargeCompanyPoints('',companies,activePackages));
  document.querySelectorAll('[data-ledger-reverse]').forEach(button=>button.onclick=()=>reverseLedger(button.dataset.ledgerReverse));
  document.querySelector('#new-package').onclick=newPointsPackage;
  document.querySelector('#new-price-rule').onclick=newPriceRule;
  bindRewardActions(currentRewardRule);
  bindWithdrawalActions(withdrawalPage,withdrawalPolicy);
}

function financeWithdrawalSection(withdrawalPage,policy){
  const rows=(withdrawalPage.items||[]).map(item=>{
    const actions=[];
    if(item.status==='PENDING_REVIEW'){actions.push(`<button class="ops-btn primary" data-wd-review="${esc(item.id)}" data-decision="APPROVE">审核通过</button>`);actions.push(`<button class="ops-btn danger" data-wd-review="${esc(item.id)}" data-decision="REJECT">驳回</button>`);}
    if(item.status==='APPROVED_PENDING_PAYMENT')actions.push(`<button class="ops-btn primary" data-wd-pay="${esc(item.id)}">记录线下付款</button>`);
    if(item.status==='PAID_PENDING_WRITE_OFF')actions.push(`<button class="ops-btn primary" data-wd-confirm="${esc(item.id)}">确认核销扣分</button>`);
    const payable=item.cash_amount_cents_snapshot!=null?Number(item.cash_amount_cents_snapshot)-Number(item.fee_cents_snapshot||0):null;
    return `<tr><td>${esc(item.company_id)}<br><small>${esc(item.requested_by_name||'--')}</small></td><td>${badge(item.status,withdrawalStatusLabel(item.status))}</td><td>${Number(item.points_requested).toLocaleString('zh-CN')} 分</td><td>${payable!=null?`¥${(payable/100).toFixed(2)}${Number(item.fee_cents_snapshot||0)>0?`<br><small>含手续费 ¥${(Number(item.fee_cents_snapshot)/100).toFixed(2)}</small>`:''}`:'--'}</td><td>${esc(item.payee_name||'--')}<br><small>${esc(item.payment_method||'--')}</small></td><td>${fmt(item.created_at)}</td><td>${actions.length?`<div class="ops-actions">${actions.join('')}</div>`:'--'}</td></tr>`;
  }).join('');
  const policyForm=policy?`<form class="ops-filter" id="withdrawal-policy-form"><label>最低提现积分 <input class="ops-input" id="withdrawal-policy-min" type="number" min="1" step="1" value="${Number(policy.min_withdrawal_points||100)}"></label><label>手续费率%（0-100） <input class="ops-input" id="withdrawal-policy-fee" type="number" min="0" max="100" step="0.01" value="${(Number(policy.fee_rate_bp||0)/100).toFixed(2)}"></label><button class="ops-btn primary" type="submit">发布提现策略</button></form>`:`<div class="ops-notice">仅超级管理员可查看与调整提现策略。</div>`;
  return `<section class="ops-card" id="finance-withdrawals"><div class="ops-card-head"><div><h2>供客积分提现</h2><p>加盟商负责人申请 → 超级管理员审核冻结 → 线下付款 → 确认核销扣分。实际应付 = 现金金额快照 - 手续费。</p></div></div>${policyForm}${table(['加盟商 / 申请人','状态','积分','应付金额','收款方','申请时间','操作'],rows.length?rows:['<tr><td colspan="7" class="ops-empty">暂无提现申请</td></tr>'])}${pager(withdrawalPage)}</section>`;
}

const WITHDRAWAL_STATUS_LABEL={PENDING_REVIEW:'待审核',APPROVED_PENDING_PAYMENT:'待付款',PAID_PENDING_WRITE_OFF:'待核销',PAID:'已完成',REJECTED:'已驳回',CANCELLED:'已取消'};
const withdrawalStatusLabel=status=>WITHDRAWAL_STATUS_LABEL[status]||label(status);

function bindWithdrawalActions(withdrawalPage,policy){
  const refresh=()=>finance();
  document.querySelectorAll('[data-wd-review]').forEach(button=>button.onclick=()=>{
    const decision=button.dataset.decision;
    actionForm({title:decision==='APPROVE'?'审核通过提现申请':'驳回提现申请',message:'审核通过会冻结对应供客积分并按当前兑换比例与手续费率生成金额快照。',labelText:decision==='APPROVE'?'审核说明':'驳回原因',required:true,submitLabel:decision==='APPROVE'?'确认通过':'确认驳回'},async note=>{
      await api(`/v1.2/admin/supply-withdrawals/${encodeURIComponent(button.dataset.wdReview)}/review`,{method:'POST',body:JSON.stringify({decision,review_note:note})});
      refresh();
    });
  });
  document.querySelectorAll('[data-wd-pay]').forEach(button=>button.onclick=()=>{
    actionForm({title:'记录线下付款',message:'登记外部付款凭据；确认前可在付款失败流程中退回。',labelText:'外部付款凭据号',required:true,submitLabel:'登记付款'},async reference=>{
      await api(`/v1.2/admin/supply-withdrawals/${encodeURIComponent(button.dataset.wdPay)}/record-transfer`,{method:'POST',body:JSON.stringify({external_reference:reference,amount_cents:0,note:reference})});
      refresh();
    });
  });
  document.querySelectorAll('[data-wd-confirm]').forEach(button=>button.onclick=()=>{
    actionForm({title:'确认核销扣分',message:'确认后将从该加盟商供客积分账户扣减提现积分，该操作不可撤销。',labelText:'核销说明',required:true,submitLabel:'确认核销'},async note=>{
      await api(`/v1.2/admin/supply-withdrawals/${encodeURIComponent(button.dataset.wdConfirm)}/confirm`,{method:'POST',body:JSON.stringify({note})});
      refresh();
    });
  });
  document.querySelector('#withdrawal-policy-form')?.addEventListener('submit',async event=>{
    event.preventDefault();
    const minPoints=Number(document.querySelector('#withdrawal-policy-min').value||0);
    const feePercent=Number(document.querySelector('#withdrawal-policy-fee').value||0);
    if(!Number.isInteger(minPoints)||minPoints<1){toast('最低提现积分必须为正整数',true);return}
    if(!Number.isFinite(feePercent)||feePercent<0||feePercent>100){toast('手续费率须为 0-100 的百分比',true);return}
    try{
      await api('/v1.2/supply-withdrawals/policy',{method:'PUT',body:JSON.stringify({min_withdrawal_points:minPoints,fee_rate_bp:Math.round(feePercent*100)})});
      toast('提现策略已发布');refresh();
    }catch(error){toast(error.message,true)}
  });
}

function rechargeCompanyPoints(companyId,companies,packages){
  const company=companies.find(item=>item.id===companyId);
  const options=packages.map(item=>`<option value="${esc(item.id)}" data-cash="${Number(item.cash_amount_cents)}">${esc(item.name)} · ${Number(item.total_points)} 积分</option>`).join('');
  if(!options){toast('当前没有可用的积分充值档位，请先配置档位',true);return}
  const companyOptions=companies.map(item=>`<option value="${esc(item.id)}" ${item.id===companyId?'selected':''}>${esc(item.name)} · 当前 ${Number(item.points_balance||0).toLocaleString('zh-CN')} 分</option>`).join('');
  const companyField=companyId?'':`<div class="ops-field"><label for="recharge-company">充值加盟商 *</label><select class="ops-input" id="recharge-company">${companyOptions}</select></div>`;
  modal(`为${company?.name||'加盟商'}充值积分`,`<form class="ops-form" id="recharge-form"><div class="ops-notice">请先完成线下收款核实。本操作无需第二位超级管理员复核，但会记录操作人、充值档位、外部凭据与审计。</div>${companyField}<div class="ops-field"><label for="recharge-package">充值档位 *</label><select class="ops-input" id="recharge-package">${options}</select></div><div class="ops-field"><label for="recharge-reference">外部收款凭据号 *</label><input class="ops-input" id="recharge-reference" minlength="3" maxlength="128" placeholder="例如：银行流水号或收款单号"></div><div class="ops-field"><label for="recharge-note">收款核验与凭证说明 *</label><textarea class="ops-textarea" id="recharge-note" minlength="3" maxlength="500" placeholder="填写核验人、凭证位置及到账确认结果"></textarea></div><label class="ops-check"><input type="checkbox" id="recharge-confirmed"> 我已核实本笔线下款项</label><div class="ops-actions"><button class="ops-btn" type="button" id="recharge-cancel">取消</button><button class="ops-btn primary" id="recharge-submit">确认充值</button></div></form>`,()=>{
    const form=document.querySelector('#recharge-form');
    document.querySelector('#recharge-cancel').onclick=closeModal;
    form.onsubmit=async event=>{
      event.preventDefault();
      const packageSelect=document.querySelector('#recharge-package');
      const targetCompanyId=companyId||document.querySelector('#recharge-company').value;
      const external_reference=document.querySelector('#recharge-reference').value.trim();
      const note=document.querySelector('#recharge-note').value.trim();
      const confirmed=document.querySelector('#recharge-confirmed').checked;
      const submit=document.querySelector('#recharge-submit');
      if(external_reference.length<3){toast('请填写至少 3 个字符的外部收款凭据号',true);return}
      if(note.length<3){toast('请填写至少 3 个字符的收款核验与凭证说明',true);return}
      if(!confirmed){toast('请确认已核实线下款项',true);return}
      submit.disabled=true;
      try{
        await api('/points/recharge',{method:'POST',body:JSON.stringify({company_id:targetCompanyId,package_id:packageSelect.value,cash_amount_cents:Number(packageSelect.selectedOptions[0].dataset.cash),external_reference,note,idempotency_key:`recharge-${crypto.randomUUID()}`,confirmed:true})});
        toast('积分充值已入账');
        closeModalFor(form);
        await refreshAfterSuccess(finance,'积分充值已入账');
      }catch(error){submit.disabled=false;toast(error.message,true)}
    };
  });
}
async function reconcileCompanyPoints(companyId,companies){
  const company=companies.find(item=>item.id===companyId);
  const isCurrent=beginModalRequest();
  try{
    const [customer,supply]=await Promise.all(['CUSTOMER','SUPPLY'].map(pointKind=>api(`/points/reconciliation/${encodeURIComponent(companyId)}?point_kind=${pointKind}`)));
    if(!isCurrent())return;
    const balanced=customer.balanced&&supply.balanced;
    const wallet=(name,result)=>`<section class="ops-card"><h3>${name}</h3><div class="ops-detail-grid">${[['流水期末余额',result.expected_closing_balance],['账户快照余额',result.snapshot_balance],['余额差异',result.difference],['流水顺序异常',result.sequence_error_count]].map(([label,value])=>`<div class="ops-detail"><small>${label}</small><b>${esc(value)}</b></div>`).join('')}</div></section>`;
    modal(`${company?.name||'加盟商'} · ${balanced?'两类积分账目一致':'发现对账差异'}`,`${wallet('客资积分',customer)}${wallet('供客积分',supply)}<div class="ops-notice">${balanced?'两类积分的余额、流水与顺序均已核对一致。':'发现差异，请停止人工资金写入，并依据审计记录和不可变流水排查。'}</div>`);
    toast(balanced?'两类积分对账一致':'发现对账异常，请处理',!balanced);
  }catch(error){toast(error.message,true)}
}
function adjustCompanyPoints(companyId,companies){
  const company=companies.find(item=>item.id===companyId);
  modal(`为${company?.name||'加盟商'}人工调账`,`<form class="ops-form" id="adjust-form"><div class="ops-notice">调整会生成不可变流水。请填写关联公司、正负积分值和可复核的原因或凭证说明。</div><div class="ops-field"><label for="adjust-point-kind">积分账户 *</label><select class="ops-input" id="adjust-point-kind"><option value="CUSTOMER">客资积分</option><option value="SUPPLY">供客积分</option></select></div><div class="ops-field"><label for="adjust-delta">调整积分 *</label><input class="ops-input" id="adjust-delta" type="number" inputmode="numeric" placeholder="正数增加，负数扣减"></div><div class="ops-field"><label for="adjust-reason">调账原因及凭证说明 *</label><textarea class="ops-textarea" id="adjust-reason" minlength="3" maxlength="500"></textarea></div><div class="ops-actions"><button class="ops-btn" type="button" id="adjust-cancel">取消</button><button class="ops-btn primary" id="adjust-submit">确认调账</button></div></form>`,()=>{
    const form=document.querySelector('#adjust-form');
    document.querySelector('#adjust-cancel').onclick=closeModal;
    form.onsubmit=async event=>{event.preventDefault();const point_kind=document.querySelector('#adjust-point-kind').value,delta=Number(document.querySelector('#adjust-delta').value),reason=document.querySelector('#adjust-reason').value.trim(),submit=document.querySelector('#adjust-submit');if(!Number.isInteger(delta)||delta===0){toast('请输入非零整数积分',true);return}if(reason.length<3){toast('请填写至少 3 个字符的调账原因及凭证说明',true);return}submit.disabled=true;try{await api('/points/adjust',{method:'POST',body:JSON.stringify({company_id:companyId,point_kind,delta,reason,idempotency_key:`adjust-${crypto.randomUUID()}`})});toast('人工调账已入账');closeModalFor(form);await refreshAfterSuccess(finance,'人工调账已入账')}catch(error){if(form.isConnected)submit.disabled=false;toast(error.message,true)}};
  });
}
function reverseLedger(ledgerId){
  actionForm({title:'确认冲正人工流水',message:'冲正会生成反向流水，不会编辑或删除历史记录。仅人工充值和人工调账可从此处冲正。',labelText:'冲正原因及凭证说明',required:true,minLength:3,submitLabel:'确认冲正',danger:true},async reason=>{await api(`/points/ledgers/${encodeURIComponent(ledgerId)}/reverse`,{method:'POST',body:JSON.stringify({reason,idempotency_key:`reverse-${crypto.randomUUID()}`})});toast('积分流水已冲正');await refreshAfterSuccess(finance,'积分流水已冲正')});
}
function newPointsPackage(){
  modal('新增充值档位',`<form class="ops-form" id="package-form"><div class="ops-field"><label for="package-code">档位代码 *</label><input class="ops-input" id="package-code" maxlength="64" placeholder="例如：V2-50000"></div><div class="ops-field"><label for="package-name">档位名称 *</label><input class="ops-input" id="package-name" maxlength="128" placeholder="例如：5 万积分标准档"></div><div class="ops-field"><label for="package-cash">线下实收金额（元） *</label><input class="ops-input" id="package-cash" type="number" min="0" step="0.01"></div><div class="ops-field"><label for="package-base">基础积分 *</label><input class="ops-input" id="package-base" type="number" min="1" step="1"></div><div class="ops-field"><label for="package-bonus">赠送积分</label><input class="ops-input" id="package-bonus" type="number" min="0" step="1" value="0"></div><div class="ops-field"><label for="package-level">适用等级</label><input class="ops-input" id="package-level" maxlength="32" value="V1"></div><div class="ops-field"><label for="package-entitlement">权益说明</label><textarea class="ops-textarea" id="package-entitlement" maxlength="500"></textarea></div><div class="ops-actions"><button class="ops-btn" type="button" id="package-cancel">取消</button><button class="ops-btn primary" id="package-submit">保存并发布</button></div></form>`,()=>{
    const form=document.querySelector('#package-form');document.querySelector('#package-cancel').onclick=closeModal;
    form.onsubmit=async event=>{event.preventDefault();const code=document.querySelector('#package-code').value.trim(),name=document.querySelector('#package-name').value.trim(),cash=Math.round(Number(document.querySelector('#package-cash').value)*100),base=Number(document.querySelector('#package-base').value),bonus=Number(document.querySelector('#package-bonus').value||0),level=document.querySelector('#package-level').value.trim()||'V1',benefit=document.querySelector('#package-entitlement').value.trim(),submit=document.querySelector('#package-submit');if(code.length<2||name.length<2||!Number.isInteger(cash)||cash<0||!Number.isInteger(base)||base<=0||!Number.isInteger(bonus)||bonus<0){toast('请完整填写充值档位信息',true);return}submit.disabled=true;try{await api('/points/packages',{method:'POST',body:JSON.stringify({code,name,cash_amount_cents:cash,base_points:base,bonus_points:bonus,level_code:level,entitlements:benefit?{benefit_summary:benefit}:{},publish:true})});toast('充值档位已发布');closeModalFor(form);await refreshAfterSuccess(finance,'充值档位已发布')}catch(error){if(form.isConnected)submit.disabled=false;toast(error.message,true)}};
  });
}
function priceRulePriority(values){return 1000-(values.region_code?300:0)-(values.category_code?200:0)-(values.brand_code?100:0)-(values.level_code?50:0)}
async function newPriceRule(){
  const isCurrent=beginModalRequest();
  const [cities,categories,brands]=await Promise.all([
    platformCities(),
    api('/master-data/dictionaries/lead_category'),
    api('/master-data/dictionaries/brand'),
  ]);
  if(!isCurrent())return;
  const cityOptions=`<option value="">全部地区</option>${cities.map(city=>`<option value="${esc(city.code)}">${esc(city.name)}</option>`).join('')}`;
  const dictionaryOptions=(items,label)=>`<option value="">${label}</option>${items.map(item=>`<option value="${esc(item.code)}">${esc(item.label)}</option>`).join('')}`;
  modal('新增客资积分价格规则',`<form class="ops-form" id="price-rule-form"><div class="ops-notice">字段留空表示“全部”，系统会优先匹配地区、类目、品牌和等级更具体的已发布规则。</div><div class="ops-field"><label for="rule-region">适用地区</label><select class="ops-input" id="rule-region">${cityOptions}</select></div><div class="ops-field"><label for="rule-category">业务类目</label><select class="ops-input" id="rule-category">${dictionaryOptions(categories,'全部类目')}</select></div><div class="ops-field"><label for="rule-brand">品牌</label><select class="ops-input" id="rule-brand">${dictionaryOptions(brands,'全部品牌')}</select></div><div class="ops-field"><label for="rule-level">加盟商等级</label><select class="ops-input" id="rule-level"><option value="">全部等级</option><option value="V1">V1</option><option value="V2">V2</option><option value="V3">V3</option></select></div><div class="ops-field"><label for="rule-cost">领取所需积分 *</label><input class="ops-input" id="rule-cost" type="number" min="1" step="1"></div><div class="ops-actions"><button class="ops-btn" type="button" id="price-rule-cancel">取消</button><button class="ops-btn primary" id="price-rule-submit">保存并发布</button></div></form>`,()=>{
    const form=document.querySelector('#price-rule-form');document.querySelector('#price-rule-cancel').onclick=closeModal;
    form.onsubmit=async event=>{event.preventDefault();const values={region_code:document.querySelector('#rule-region').value||null,category_code:document.querySelector('#rule-category').value||null,brand_code:document.querySelector('#rule-brand').value||null,level_code:document.querySelector('#rule-level').value||null},points_cost=Number(document.querySelector('#rule-cost').value),submit=document.querySelector('#price-rule-submit');if(!Number.isInteger(points_cost)||points_cost<=0){toast('请输入正整数领取积分',true);return}submit.disabled=true;try{await api('/points/price-rules',{method:'POST',body:JSON.stringify({...values,points_cost,priority:priceRulePriority(values),publish:true})});toast('积分价格规则已发布');closeModalFor(form);await refreshAfterSuccess(finance,'积分价格规则已发布')}catch(error){if(form.isConnected)submit.disabled=false;toast(error.message,true)}};
  });
}
function ruleSummary(rule){
  const fixed=rule?.calculation_mode==='FIXED';
  const amounts=fixed?[['固定供客积分',`${rule.fixed_points??0} 积分/条`]]:[['历史奖励比例',`${(Number(rule?.ratio_bps||0)/100).toFixed(2).replace(/\.00$/,'')}%`],['最低奖励',`${rule?.min_points||0} 积分`],['最高奖励',rule?.max_points==null?'不设上限':`${rule.max_points} 积分`]];
  const rows=[...amounts,['同一客户短期重复',`${rule?.hard_duplicate_days||0} 天内不计积分`],['再次获得积分',`${rule?.reward_duplicate_days||0} 天后`],['历史记录提醒',`查看 ${rule?.historical_suspect_days||0} 天内记录`]];
  return `<div class="ops-detail-grid">${rows.map(([name,value])=>`<div class="ops-detail"><small>${esc(name)}</small><b>${esc(value)}</b></div>`).join('')}</div>`;
}
function rewardSection(pageData,currentRule){const rows=(pageData.items||[]).map((item,index)=>`<tr><td>${rowSequence(pageData,index)}</td><td><b>${esc(item.customer_name||'未填写')}</b><br><small>${esc(item.lead_code||recordCode(item.lead_id,'KZ'))}</small></td><td>${esc(item.supplier_company_name||recordCode(item.supplier_company_id,'加盟商'))}</td><td>${item.claim_points}</td><td>${item.reward_points}</td><td>${badge(item.status)}</td><td>${fmt(item.reward_due_at)}</td><td><button class="ops-btn" data-reward="${item.id}">查看</button>${item.status==='OBSERVING'?` <button class="ops-btn primary" data-settle="${item.id}">结算</button>`:''}${item.status==='SETTLED'?` <button class="ops-btn danger" data-reverse="${item.id}">撤销奖励</button>`:''}</td></tr>`);const pages=Math.max(1,Math.ceil((pageData.total||0)/(pageData.page_size||20)));const pager=`<div class="ops-pager"><button class="ops-btn" id="finance-reward-prev" ${S.financeRewardPage<=1?'disabled':''}>上一页</button><span>${S.financeRewardPage}/${pages}，共 ${pageData.total||0} 条</span><button class="ops-btn" id="finance-reward-next" ${S.financeRewardPage>=pages?'disabled':''}>下一页</button></div>`;return `${currentRule?`<section class="ops-card"><div class="ops-card-head"><div><h2>供客积分结算规则</h2><p>奖励只结算给提交客资的加盟商；领取客资的加盟商不获得供客奖励。</p></div></div>${ruleSummary(currentRule)}<div class="ops-actions"><button class="ops-btn gold" id="settle-due">结算已到期奖励</button></div></section>`:''}<section class="ops-card"><div class="ops-card-head"><div><h2>供客积分明细</h2><p>每笔奖励直接显示对应客资与提供方；正式退回会冻结或冲回相应奖励。</p></div></div>${table(['序号','对应客资','客资提供方','领取积分','奖励积分','状态','预计结算','操作'],rows)}${pager}</section>`}
function bindRewardActions(currentRule){document.querySelectorAll('[data-reward]').forEach(button=>button.onclick=()=>rewardDetail(button.dataset.reward));document.querySelectorAll('[data-settle]').forEach(button=>button.onclick=()=>settle(button.dataset.settle));document.querySelectorAll('[data-reverse]').forEach(button=>button.onclick=()=>reverse(button.dataset.reverse));document.querySelector('#settle-due')?.addEventListener('click',settleDue);document.querySelector('#finance-reward-prev')?.addEventListener('click',()=>{if(S.financeRewardPage>1){S.financeRewardPage--;finance()}});document.querySelector('#finance-reward-next')?.addEventListener('click',()=>{S.financeRewardPage++;finance()});if(S.id){const id=S.id;S.id='';rewardDetail(id)}}
async function rewardDetail(id){const isCurrent=beginModalRequest(),x=await api(`/v1.2/supplier-rewards/${encodeURIComponent(id)}`);if(isCurrent())modal('奖励详情',`<div class="ops-detail-grid">${[['对应客资',`${x.customer_name||'未填写'} · ${x.lead_code||recordCode(x.lead_id,'KZ')}`],['派发编号',recordCode(x.assignment_id,'PF')],['提供加盟商',x.supplier_company_name||recordCode(x.supplier_company_id,'加盟商')],['接收加盟商',x.receiver_company_name||recordCode(x.receiver_company_id,'加盟商')],['状态',label(x.status)],['领取积分',x.claim_points],['奖励积分',x.reward_points],['开始观察',fmt(x.observed_at)],['交易确认条件时间',fmt(x.reward_due_at)],['实际到账',fmt(x.settled_at)]].map(([a,b])=>`<div class="ops-detail"><small>${a}</small><b>${esc(b??'--')}</b></div>`).join('')}</div><section class="ops-card"><h3>本笔奖励适用规则</h3>${ruleSummary(x.rule_snapshot||{})}</section><div class="ops-notice">正式退回申请会冻结未入账奖励；已入账奖励在退回通过时自动冲回，驳回则保持入账。冲回按业务单幂等执行。</div><button class="ops-btn" id="trace">查看客资详情</button>`,()=>document.querySelector('#trace').onclick=()=>{closeModal();go('trace',x.lead_id)})}
function settle(id){actionForm({title:'确认奖励结算',message:'请核对奖励状态、关联加盟商和积分金额。结算会写入不可变流水与审计。',labelText:'结算说明',required:true,minLength:3,submitLabel:'确认结算'},async note=>{await api(`/v1.2/admin/supplier-rewards/${encodeURIComponent(id)}/settle`,{method:'POST',body:JSON.stringify({note})});toast('结算指令已执行');await refreshAfterSuccess(finance,'结算指令已执行')})}
function settleDue(){actionForm({title:'结算已到期奖励',message:'仅结算符合到期条件的奖励；冻结奖励不会入账。请填写本批处理的核验说明。',labelText:'批量结算说明',required:true,minLength:3,submitLabel:'确认批量结算'},async note=>{await api('/v1.2/admin/supplier-rewards/settle-due',{method:'POST',body:JSON.stringify({limit:500,note})});toast('到期奖励结算已执行');await refreshAfterSuccess(finance,'到期奖励结算已执行')})}
function reverse(id){actionForm({title:'确认奖励冲正',message:'冲正会生成反向流水，不会修改或删除历史记录。',labelText:'冲正原因及凭证说明',required:true,minLength:5,submitLabel:'确认冲正',danger:true},async note=>{await api(`/v1.2/admin/supplier-rewards/${encodeURIComponent(id)}/reverse`,{method:'POST',body:JSON.stringify({reason_code:'ADMIN_ERROR',note})});toast('奖励已冲正');await refreshAfterSuccess(finance,'奖励已冲正')})}
function notificationFailureAdvice(item){if(item.status==='MANUAL_ACTION_REQUIRED')return '请检查接收人是否已绑定微信，以及对应消息模板是否已启用。';if(item.status==='DEAD')return '系统多次发送未成功，请检查消息配置后重新发送。';return '系统发送未成功，可确认配置后重新发送。'}
function notificationFailureDetail(item){
  modal('通知异常详情',`<div class="ops-detail-grid">${[['通知事项',notificationEventLabel(item.event_type)],['当前状态',notificationStatusLabel(item.status)],['已尝试',`${item.attempts||0} 次`],['创建时间',fmt(item.created_at)],['处理建议',notificationFailureAdvice(item)],['通知编号',recordCode(item.id,'TZ')]].map(([name,value])=>`<div class="ops-detail"><small>${esc(name)}</small><b>${esc(value)}</b></div>`).join('')}</div><div class="ops-notice">请先确认接收人绑定与模板配置，再重新发送。系统底层报错不会作为业务说明直接展示。</div><div class="ops-actions"><button class="ops-btn primary" id="retry-notification">重新发送</button></div>`,()=>{const button=document.querySelector('#retry-notification');button.onclick=async()=>{button.disabled=true;try{await retryOutbox(item.id);closeModalFor(button)}catch(error){if(button.isConnected)button.disabled=false;toast(error.message,true)}}});
}
async function retryOutbox(outboxId){await api(`/notifications/outbox/${encodeURIComponent(outboxId)}/retry`,{method:'POST'});toast('已加入重新发送队列');await refreshAfterSuccess(audit,'已加入重新发送队列')}
const AUDIT_FIELD_LABEL={name:'名称',status:'状态',reason:'处理说明',note:'处理说明',username:'登录账号',display_name:'姓名',role_code:'角色',company_id:'加盟商',lead_id:'客资',assignment_id:'派发单',return_id:'退回申诉',points:'积分',points_cost:'所需积分',source_kind:'客资来源',review_status:'审核结果',region_code:'所在地',contact_result:'联系结果',conclusion:'核验结论'};
const auditValue=value=>value==null||value===''?'--':Array.isArray(value)?value.map(auditValue).join('、'):typeof value==='object'?'已记录详情':readableLabel(value,String(value));
Object.assign(AUDIT_FIELD_LABEL,{operation_claim_points:'客资领取积分',supplier_provision_points:'加盟商供客积分',configured:'已启用固定积分',version:'配置版本'});
function auditDetailBlock(title,data){const entries=Object.entries(data||{}).filter(([,value])=>value!=null&&value!=='');if(!entries.length)return '';return `<section class="ops-card"><h3>${esc(title)}</h3><div class="ops-detail-grid">${entries.map(([key,value])=>`<div class="ops-detail"><small>${esc(AUDIT_FIELD_LABEL[key]||'相关信息')}</small><b>${esc(auditValue(value))}</b></div>`).join('')}</div></section>`}
function auditResult(event){const action=String(event.action||''),metadata=event.metadata||{};if(action.endsWith('_FAILED'))return {status:'REJECTED',text:'未完成'};if(action.includes('BLOCKED')||metadata.reason_code==='AUTH_SENSITIVE_ACTION_LOCKED')return {status:'REJECTED',text:'已拦截'};return {status:'APPROVED',text:'已完成'}}
function auditDetail(event){const operationCode=recordCode(event.request_id||event.id,'OP'),result=auditResult(event);modal('操作详情',`<div class="ops-detail-grid">${[['操作人',event.actor_name||'系统自动处理'],['操作时间',fmt(event.created_at)],['处理事项',auditAction(event.action)],['相关记录',`${auditResource(event.resource_type)} · ${recordCode(event.resource_id,'业务')}`],['加盟商',recordCode(event.company_id,'加盟商')],['操作结果',result.text],['操作编号',operationCode]].map(([name,value])=>`<div class="ops-detail"><small>${esc(name)}</small><b>${esc(value)}</b></div>`).join('')}</div>${auditDetailBlock('变更前',event.before)}${auditDetailBlock('变更后',event.after)}${auditDetailBlock('处理说明',event.metadata)}<div class="ops-actions"><button class="ops-btn primary" id="copy-operation-code">复制操作编号</button></div>`,()=>{document.querySelector('#copy-operation-code').onclick=async()=>{try{await navigator.clipboard.writeText(operationCode);toast('操作编号已复制')}catch{toast('浏览器不支持自动复制，请手动复制',true)}}})}
async function audit(){const business=S.id||'';const [d,failedOutbox]=await Promise.all([api(`/v1.2/audit-events${qs({page:S.page,page_size:50,business_id:business})}`),can('notification.retry')?api('/notifications/outbox/failed'):Promise.resolve([])]);const events=d.items||[];const rows=events.map(x=>{const result=auditResult(x);return `<tr data-audit-row="${esc(x.id)}"><td>${fmt(x.created_at)}</td><td><b>${esc(x.actor_name||'系统自动处理')}</b><br><small>${esc(x.actor_user_id?recordCode(x.actor_user_id,'账号'):'系统任务')}</small></td><td><b>${esc(auditAction(x.action))}</b><br><small>${esc(auditResource(x.resource_type))} · ${esc(recordCode(x.resource_id,'业务'))}</small></td><td>${badge(result.status)}<br><small>${esc(result.text)}</small></td><td>${esc(recordCode(x.request_id||x.id,'OP'))}</td><td><button class="ops-btn" data-audit-detail="${esc(x.id)}">查看详情</button></td></tr>`});const failureRows=(failedOutbox||[]).map(item=>`<tr data-outbox-detail="${esc(item.id)}"><td>${esc(notificationEventLabel(item.event_type))}</td><td>${esc(notificationStatusLabel(item.status))}</td><td>${esc(notificationFailureAdvice(item))}</td><td>${item.attempts||0} 次</td><td>${fmt(item.created_at)}</td><td><button class="ops-btn primary" data-outbox-retry="${esc(item.id)}">重新发送</button></td></tr>`);const failurePanel=can('notification.retry')?`<section class="ops-card"><div class="ops-card-head"><div><h2>通知发送异常</h2><p>仅显示需要处理的消息；双击某一条可查看详情，重新发送前请先确认接收人和消息模板配置。</p></div></div>${table(['通知内容','当前状态','处理建议','已尝试','创建时间','操作'],failureRows)}</section>`:'';shell(`<div class="ops-filter"><input class="ops-input" id="business" placeholder="输入客资、派发、退回或操作编号" value="${esc(business)}"><button class="ops-btn primary" id="query">查询记录</button><button class="ops-btn gold" id="trace" ${business?'':'disabled'}>查看客资详情</button></div><section class="ops-card"><div class="ops-card-head"><div><h2>操作日志</h2><p>每条记录均可查看谁在何时处理了哪项业务；双击表格行或点击详情均可展开。操作编号仅用于查询与追溯，不可编辑。</p></div></div>${table(['时间','操作人','处理事项','操作结果','操作编号','详情'],rows)}${pager(d)}</section>${failurePanel}`);bindPager(d,audit);document.querySelector('#query').onclick=()=>go('audit',document.querySelector('#business').value.trim());document.querySelector('#trace').onclick=()=>go('trace',document.querySelector('#business').value.trim());const eventById=Object.fromEntries(events.map(event=>[event.id,event]));document.querySelectorAll('[data-audit-detail]').forEach(button=>button.onclick=()=>auditDetail(eventById[button.dataset.auditDetail]));document.querySelectorAll('[data-audit-row]').forEach(row=>row.ondblclick=()=>auditDetail(eventById[row.dataset.auditRow]));const failedById=Object.fromEntries((failedOutbox||[]).map(item=>[item.id,item]));document.querySelectorAll('[data-outbox-detail]').forEach(row=>row.ondblclick=()=>notificationFailureDetail(failedById[row.dataset.outboxDetail]));document.querySelectorAll('[data-outbox-retry]').forEach(button=>button.onclick=async()=>{button.disabled=true;try{await retryOutbox(button.dataset.outboxRetry)}catch(error){button.disabled=false;toast(error.message,true)}})}
function latestItem(items){return items?.length?items[items.length-1]:null}
function traceStep(title,status,detail,iconName){return `<article class="ops-trace-step"><i aria-hidden="true">${icon(iconName)}</i><div><small>${esc(title)}</small><b>${esc(status||'未涉及')}</b><p>${esc(detail||'')}</p></div></article>`}
function traceEffectiveRecognition(assignment){if(assignment?.status==='RETURNED')return assignment.release_reason==='V12_RETURN_REGION_CORRECTED'?'区域更正，已返还领取积分':'退回审核通过 · 已判无效';if(assignment?.status==='RETURN_PENDING')return '退回审核中';if(assignment?.transaction_confirmed_at){const reason={MANUAL_CONFIRMED:'人工确认有效',CLAIM_48H:'领取满48小时自动有效',RETURN_REJECTED:'退回驳回后确认有效'}[assignment.transaction_confirmation_policy]||'已确认有效';return `${reason} · ${fmt(assignment.transaction_confirmed_at)}`}return assignment?.claimed_at?'等待领取满48小时':'待领取'}
function traceNextStep(lead,assignment,task,returnRequest){
  if(returnRequest&&['DRAFT','SUBMITTED','VERIFYING','REVIEWING','NEED_MORE_EVIDENCE'].includes(returnRequest.status))return '等待退回审核完成';
  if(task&&['PENDING','ASSIGNED','IN_PROGRESS','SUBMITTED'].includes(task.status))return task.status==='SUBMITTED'?'等待运营确认核验结论':'等待电销完成电话核验';
  if(lead?.status==='DRAFT'||lead?.review_status==='DRAFT')return '等待补齐客户信息后提交核实';
  if(lead?.status==='READY_DISPATCH')return '等待运营派发给合适的加盟商';
  if(assignment?.status==='PENDING_CLAIM'||assignment?.status==='WAITING_CLAIM')return '等待加盟商领取';
  if(assignment?.status==='CLAIMED'||assignment?.status==='FOLLOWING')return '等待加盟商更新跟进结果';
  return '本条客资已完成当前环节';
}
function traceTimeline(items){return (items||[]).map(item=>{
  const title=item.kind==='NOTIFICATION'?'发送消息提醒':auditAction(item.action);
  const actor=item.kind==='NOTIFICATION'?'系统提醒':item.actor_name||'系统自动处理';
  return `<article class="ops-trace-event"><time>${esc(fmt(item.at))}</time><i aria-hidden="true"></i><div><b>${esc(title)}</b><small>${esc(actor)}</small>${item.summary?`<p>${esc(item.summary)}</p>`:''}</div></article>`;
}).join('')||'<div class="ops-empty">当前没有可显示的处理记录</div>'}
async function fullTrace(){
  if(!S.id){shell('<section class="ops-card ops-empty">请选择一条客资后查看详情。</section>');return}
  const d=await api(`/v1.2/trace/${encodeURIComponent(S.id)}`);
  const lead=d.lead||{},assignments=d.assignments||[],returns=d.returns||[],tasks=d.verification_tasks||[],followups=d.followups||[],rewards=d.supplier_rewards||[],ledgers=d.points_ledgers||[];
  const assignment=lead.current_assignment_id?assignments.find(item=>item.id===lead.current_assignment_id):null;
  const scopedReturns=assignment?returns.filter(item=>item.assignment_id===assignment.id):[];
  const scopedTasks=assignment?tasks.filter(item=>item.assignment_id===assignment.id):['PENDING_REVIEW','PENDING_TELESALES_VERIFY','PENDING_OPERATION_DISPOSITION'].includes(lead.status)?tasks.filter(item=>!item.assignment_id):[];
  const scopedRewards=assignment?rewards.filter(item=>item.assignment_id===assignment.id):[];
  const scopedFollowups=assignment?followups.filter(item=>item.assignment_id===assignment.id):[];
  const returnRequest=latestItem(scopedReturns),task=latestItem(scopedTasks),followup=latestItem(scopedFollowups),reward=latestItem(scopedRewards);
  const evidence=scopedReturns.flatMap(item=>item.evidences||[]);
  const steps=[
    traceStep('客资提交',label(lead.status),lead.submitted_at?`提交于 ${fmt(lead.submitted_at)}`:`录入于 ${fmt(lead.created_at)}`,'file-text'),
    traceStep('资料确认',label(lead.review_status),lead.review_note||'等待电销核实资料与客户意向','user-check'),
    traceStep('电话核验',task?label(task.status):'未安排',task?`${task.assignee_name||'待分配'}${task.conclusion?`，结论：${label(task.conclusion)}`:''}`:'本条客资暂不需要电话核验','phone'),
    traceStep('派发领取',assignment?label(assignment.status):'待派发',assignment?`${assignment.receiver_company_name||assignment.company_name||'待确定加盟商'}${assignment.claimed_at?`，领取于 ${fmt(assignment.claimed_at)}`:''}`:'等待运营选择接收加盟商','hand-claim'),
    traceStep('跟进反馈',followup?label(followup.status):'暂无反馈',followup?.note||'加盟商领取后会在这里记录跟进结果','file-text'),
    traceStep('退回审核',returnRequest?label(returnRequest.status):'未发起',returnRequest?`${label(returnRequest.reason_code)}${returnRequest.final_decision_reason?`：${returnRequest.final_decision_reason}`:''}`:'如客户信息无效，加盟商可提交材料申请退回','rotate-ccw'),
  ];
  const pointsText=reward?`${reward.reward_points||0} 积分${reward.status?`，${label(reward.status)}`:''}`:'本条客资暂未产生供资奖励';
  const transactionConfirmedAt=assignment?.transaction_confirmed_at;
  const returnIds=new Set(scopedReturns.map(item=>item.id));
  const rewardLedgerIds=new Set([reward?.ledger_id,reward?.reversal_ledger_id].filter(Boolean));
  const currentLedgers=assignment?ledgers.filter(item=>item.business_id===assignment.id||returnIds.has(item.business_id)||item.business_id===reward?.id||rewardLedgerIds.has(item.id)):[];
  const recordedClaim=currentLedgers.filter(item=>item.business_id===assignment?.id&&item.ledger_type==='CLAIM').reduce((sum,item)=>sum+Math.abs(Number(item.delta||0)),0);
  const receiverPoints=recordedClaim||Number(assignment?.claim_points??assignment?.points_price??0);
  const receiverRefund=currentLedgers.filter(item=>returnIds.has(item.business_id)&&item.ledger_type==='RETURN').reduce((sum,item)=>sum+Math.max(0,Number(item.delta||0)),0);
  const supplierPoints=reward&&['SETTLED','REVERSED'].includes(reward.status)?Number(reward.reward_points||0):0;
  const supplierReversal=reward?.status==='REVERSED'?Number(reward.reward_points||0):0;
  const summary=`<section class="ops-card ops-trace-customer"><div class="ops-card-head"><div><h2>${esc(lead.customer_name||'客资详情')}</h2><p>${esc(recordCode(lead.id||d.business_id,'KZ'))} · ${esc(lead.city||'待补充地区')} ${esc(lead.district||'')}</p></div><div>${badge(lead.status)}</div></div><div class="ops-detail-grid">${[['客资来源',label(lead.source_kind)],['联系电话',lead.phone||lead.phone_masked],['提交人',lead.submitter_name],['所在地',`${lead.city||''} ${lead.district||''}`],['核验结果',label(lead.review_status)],['当前处理',traceNextStep(lead,assignment,task,returnRequest)]].map(([a,b])=>`<div class="ops-detail"><small>${a}</small><b>${esc(b||'待确认')}</b></div>`).join('')}</div><div class="ops-trace-need"><small>客户需求</small><p>${esc(lead.need_summary||'暂未填写客户需求')}</p></div></section>`;
  const main=`<div class="ops-trace-main">${summary}<section class="ops-card"><div class="ops-card-head"><div><h2>处理进度</h2><p>按实际发生顺序展示，未涉及的环节会明确标注。</p></div></div><div class="ops-trace-steps">${steps.join('')}</div></section><section class="ops-card"><div class="ops-card-head"><div><h2>处理记录</h2><p>每次处理都会保留时间、处理人和说明。</p></div></div><div class="ops-trace-timeline">${traceTimeline(d.timeline)}</div></section></div>`;
  const dispatchInfo=assignment?`<div class="ops-detail-grid">${[['派发编号',recordCode(assignment.id,'PF')],['接收加盟商',assignment.receiver_company_name||assignment.company_name],['派发时间',fmt(assignment.assigned_at)],['派发状态',label(assignment.status)],['接收确认',assignment.claimed_at?`已确认 · ${fmt(assignment.claimed_at)}`:'待确认'],['有效认定',traceEffectiveRecognition(assignment)],['领取积分',`${assignment.claim_points??assignment.points_price??0} 积分`],['当前跟进',label(assignment.current_follow_status)]].map(([a,b])=>`<div class="ops-detail"><small>${a}</small><b>${esc(b||'待确认')}</b></div>`).join('')}</div>`:'<div class="ops-empty">客资尚未派发</div>';
  const returnInfo=returnRequest?`<div class="ops-detail-grid">${[['退回状态',label(returnRequest.status)],['退回原因',label(returnRequest.reason_code)],['申请时间',fmt(returnRequest.submitted_at)],['审核结果',returnRequest.review_note||returnRequest.final_decision_reason||'等待审核'],['返还积分',returnRequest.refund_points==null?'待审核':`${returnRequest.refund_points} 积分`],['核验结论',label(returnRequest.verification?.conclusion)]].map(([a,b])=>`<div class="ops-detail"><small>${a}</small><b>${esc(b||'待确认')}</b></div>`).join('')}</div>${evidence.length?`<h3 class="ops-trace-subtitle">申诉证据</h3><div class="ops-detail-grid">${evidenceList(evidence)}</div>`:''}`:'<div class="ops-empty">当前没有退回申请</div>';
  const rewardInfo=`<div class="ops-detail-grid">${[['接收方消耗',`${receiverPoints} 积分`],['接收方退款',`${receiverRefund} 积分`],['供资奖励',pointsText],['奖励冲回',`${supplierReversal} 积分`],['平台净积分',`${receiverPoints-receiverRefund-supplierPoints+supplierReversal} 积分`],['交易确认时间',fmt(transactionConfirmedAt)],['奖励实际到账',fmt(reward?.settled_at)],['积分记录',currentLedgers.length?`${currentLedgers.length} 笔`:'暂无'],['最近积分变化',currentLedgers.length?`${currentLedgers[currentLedgers.length-1].delta>0?'+':''}${currentLedgers[currentLedgers.length-1].delta} 积分`:'--'],['消息提醒',d.notifications?.length?`${d.notifications.length} 条`:'暂无']].map(([a,b])=>`<div class="ops-detail"><small>${a}</small><b>${esc(b)}</b></div>`).join('')}</div>`;
  const side=`<aside class="ops-trace-side"><section class="ops-card"><h2>当前处理</h2><div class="ops-trace-current">${badge(returnRequest?.status||task?.status||assignment?.status||lead.status)}<b>${esc(traceNextStep(lead,assignment,task,returnRequest))}</b><p>${esc(task?.assignee_name?`当前由 ${task.assignee_name} 处理`:'请根据当前状态继续处理')}</p></div></section><section class="ops-card"><h2>派发信息</h2>${dispatchInfo}</section><section class="ops-card"><h2>退回审核</h2>${returnInfo}</section><section class="ops-card"><h2>积分与奖励</h2>${rewardInfo}</section></aside>`;
  shell(`<div class="ops-trace-actions"><button class="ops-btn" id="trace-back">返回上一页</button><button class="ops-btn primary" id="trace-log">查看处理日志</button></div><div class="ops-trace-layout">${main}${side}</div>`);
  document.querySelector('#trace-back').onclick=()=>history.length>1?history.back():go('leads');
  document.querySelector('#trace-log').onclick=()=>go('audit',d.business_id);
}
function redirectToAllowedSurface(){
  const roles=new Set(S.me?.roles||[]);
  if(roles.has('SUPER_ADMIN')||roles.has('OPERATION')){location.replace('/admin/v12-operations.html');return true}
  if(roles.has('TELESALES')){location.replace('/h5/call/');return true}
  if(roles.has('FRANCHISE_OWNER')||roles.has('FRANCHISE_EMPLOYEE')){location.replace('/h5/');return true}
  return false;
}
function renderLogin(){
  zsSetSafeHtml(app, `<div class="ops-login-shell"><section class="ops-login-brand"><img src="./logo.png" alt="合家美宅" class="ops-login-logo"><div><span>合家美宅</span><h1>客资管理平台</h1><p>客资流转、加盟商协同与经营决策</p></div></section><section class="ops-card ops-login-card"><div class="ops-card-head"><div><h2>平台管理登录</h2></div></div><form class="ops-form" id="platform-login-form"><div class="ops-field"><label for="username">登录账号</label><input class="ops-input" id="username" autocomplete="username" required></div><div class="ops-field"><label for="password">登录密码</label><div class="ops-password-input"><input class="ops-input" id="password" type="password" autocomplete="current-password" required><button class="ops-password-toggle" id="login-password-toggle" type="button" aria-label="显示密码" aria-pressed="false">${icon('eye')}</button></div></div><button class="ops-btn primary ops-login-submit" id="login-btn" type="submit">登录工作台</button></form></section></div>`);
  const passwordInput=document.querySelector('#password'),passwordToggle=document.querySelector('#login-password-toggle');
  passwordToggle.onclick=()=>{const visible=passwordInput.type==='password';passwordInput.type=visible?'text':'password';passwordToggle.setAttribute('aria-label',visible?'隐藏密码':'显示密码');passwordToggle.setAttribute('aria-pressed',String(visible));zsSetSafeHtml(passwordToggle,icon(visible?'eye-off':'eye'))};
  document.querySelector('#platform-login-form').onsubmit=async event=>{
    event.preventDefault();
    const submit=document.querySelector('#login-btn');
    submit.disabled=true;
    try{
      await api('/auth/login',{method:'POST',body:JSON.stringify({username:document.querySelector('#username').value.trim(),password:document.querySelector('#password').value})});
      location.replace('/admin/');
    }catch(error){submit.disabled=false;toast(error.message,true)}
  };
}
function renderNoAccess(){zsSetSafeHtml(app, `<div class="ops-standalone"><section class="ops-card"><h1>当前账号无管理后台权限</h1><p class="ops-muted">请使用与当前身份匹配的工作台，或联系超级管理员核对角色。</p><a class="ops-btn" href="/admin/">返回登录页</a></section></div>`)}
function renderLoadError(message='页面加载失败'){zsSetSafeHtml(app, `<div class="ops-standalone"><section class="ops-card"><h1>暂时无法加载</h1><p class="ops-muted">${esc(message)}</p><button class="ops-btn primary" id="retry-boot">重试</button></section></div>`);document.querySelector('#retry-boot').onclick=boot}
async function boot(){
  try{
    S.me=await api('/auth/me');
    let notificationLoadError='';
    const notifications=await api('/notifications?unread_only=true&page=1&page_size=5').catch(error=>{notificationLoadError=error.message||'请求失败';return {items:[],total:0,unread_total:0}});
    S.accountNotifications=notifications.items||[];
    S.unreadNotifications=Number(notifications.unread_total ?? notifications.total ?? 0);
    if(!syncRouteFromUrl({canonicalize:true})){
      if(!redirectToAllowedSurface())renderNoAccess();
      return;
    }
    render();
    if(notificationLoadError)toast(`消息提醒暂未加载：${notificationLoadError}`,true);
  }catch(error){if(['AUTH_REQUIRED','AUTH_INVALID'].includes(error.code))renderLogin();else renderLoadError(error.message)}
}
window.addEventListener('popstate',()=>{if(syncRouteFromUrl())render()});
boot();
