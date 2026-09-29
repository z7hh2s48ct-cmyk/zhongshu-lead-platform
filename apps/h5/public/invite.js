const API='/api/v1';
const statusEl=document.querySelector('#invite-status');
const detailsEl=document.querySelector('#invite-details');
const confirmButton=document.querySelector('#invite-confirm');
const companyEl=document.querySelector('#invite-company');
const ownerEl=document.querySelector('#invite-owner');
const expiresEl=document.querySelector('#invite-expires');
function inviteFromUrl(){
  const hashToken=new URLSearchParams(String(location.hash||'').replace(/^#/,'')).get('invite');
  const queryToken=new URLSearchParams(location.search).get('invite');
  return hashToken||queryToken||'';
}
const rawInvite=inviteFromUrl();
history.replaceState(null,'',location.pathname);

function formatTime(value){
  if(!value)return'--';
  const date=new Date(value);
  return Number.isNaN(date.getTime())?'--':date.toLocaleString('zh-CN');
}

async function request(path,options={}){
  const headers={...(options.headers||{})};
  if(options.body)headers['Content-Type']='application/json';
  const response=await fetch(API+path,{...options,headers,credentials:'include'});
  let payload={};
  try{payload=await response.json()}catch{}
  if(!response.ok||payload.code!=='OK')throw new Error(payload.message||'邀请核验失败，请联系平台管理员。');
  return payload.data;
}

function showError(message){
  statusEl.textContent=message;
  statusEl.className='invite-status error';
  confirmButton.disabled=true;
}

async function loadInvite(){
  if(rawInvite.length<16){
    showError('邀请链接无效，请联系平台管理员重新发送。');
    return;
  }
  try{
    const invite=await request('/auth/invites/preview',{
      method:'POST',
      body:JSON.stringify({invite:rawInvite}),
    });
    companyEl.textContent=invite.company_name||'--';
    ownerEl.textContent=invite.owner_name||'待确认';
    expiresEl.textContent=formatTime(invite.expires_at);
    detailsEl.hidden=false;
    const replacing=invite.purpose==='OWNER_WECHAT_REBIND';
    statusEl.textContent=replacing?'邀请有效。请使用新的微信确认换绑；成功前原微信仍可登录。':'邀请有效，请确认绑定。';
    confirmButton.textContent=replacing?'确认更换微信':'确认绑定';
    if(replacing)document.querySelector('#invite-tip').textContent='换绑成功后，原微信将无法继续登录；负责人账号、历史记录和权限保持不变。';
    statusEl.className='invite-status success';
    confirmButton.disabled=false;
  }catch(error){
    showError(error.message);
  }
}

confirmButton.addEventListener('click',async()=>{
  if(confirmButton.disabled)return;
  confirmButton.disabled=true;
  statusEl.textContent='正在准备微信授权…';
  statusEl.className='invite-status';
  try{
    const result=await request('/auth/invites/confirm-start',{
      method:'POST',
      body:JSON.stringify({invite:rawInvite,return_url:'/h5/v12-workbench.html'}),
    });
    if(!result.authorization_url)throw new Error('暂时无法发起微信授权，请联系平台管理员。');
    location.assign(result.authorization_url);
  }catch(error){
    confirmButton.disabled=false;
    showError(error.message);
  }
});

loadInvite();
