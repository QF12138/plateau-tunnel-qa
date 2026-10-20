(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const message = (id, text, success=false) => { $(id).textContent=text; $(id).classList.toggle('success',success); };
  let csrf='', me=null, accounts=[], selected=null, allowNavigation=false;
  function retryDelay(response, data) {
    const raw=response.headers.get('Retry-After')?.trim();
    let seconds;
    if(raw){
      if(/^\d+$/.test(raw))seconds=Number(raw);
      else {const date=Date.parse(raw);if(Number.isFinite(date))seconds=Math.ceil((date-Date.now())/1000);}
    }
    if(!Number.isFinite(seconds)){
      const fallback=data?.details?.retry_after_seconds;
      seconds=typeof fallback==='number'&&Number.isFinite(fallback)?fallback:60;
    }
    return Math.max(1,Math.min(3600,Math.ceil(seconds)));
  }
  async function api(path, options={}) {
    let response;
    try{response=await fetch(path,{credentials:'same-origin',signal:AbortSignal.timeout(15000),...options,headers:{'Content-Type':'application/json','X-CSRF-Token':csrf,...options.headers}});}
    catch{throw new Error('连接失败或超时，请检查网络后重试。');}
    if(response.status===401 && document.body.dataset.page!=='login'){
      allowNavigation=true;clearSecrets();location.replace('/login?reason=expired&next=%2Faccounts');
      throw new Error('登录已过期，请重新登录。');
    }
    let data=null;
    try{const parsed=await response.json();if(parsed&&typeof parsed==='object'&&!Array.isArray(parsed))data=parsed;}
    catch{/* A gateway may return HTML; HTTP status and retry headers still apply. */}
    if(!response.ok) {
      const retryAfter=retryDelay(response,data);
      const fallback=response.status===429?`尝试次数过多，请 ${retryAfter} 秒后重试。`
        :response.status===401?'账号或密码错误，或账号尚未启用。'
        :response.status>=500?'服务暂不可用，请稍后重试。':'请求未完成，请刷新页面后重试。';
      const error=new Error(typeof data?.message==='string'&&data.message.trim()?data.message:fallback);
      error.status=response.status;error.code=data?.error_code;
      if(error.code==='CSRF_REJECTED' && document.body.dataset.page!=='login'){
        blockSession();error.message='登录身份已变化，请先重新验证身份。本次修改未提交。';
      }
      error.retryAfter=retryAfter;
      throw error;
    }
    if(!data)throw new Error('服务返回了无法识别的响应，请稍后重试。');
    return data;
  }
  if(document.body.dataset.page==='login') {
    const form=$('login-form'), username=$('username'), password=$('password');
    const button=$('login-submit'), remember=$('remember-username'), toggle=$('toggle-password');
    const storageKey='tunnel.login.username', params=new URLSearchParams(location.search);
    const next=params.get('next')==='/accounts'?'/accounts':'/';
    let busy=false, cooldownUntil=0, cooldownTimer;
    const feedback=(text,tone='error')=>{message('form-feedback',text,tone==='success');$('form-feedback').dataset.tone=tone;};
    const reasons={expired:'登录已过期或账号信息已变更，请重新登录。',logged_out:'你已安全退出，可以使用其他账号登录。',password_changed:'账号信息已更新，请使用最新的账号和密码登录。'};
    const sessionNotice=[reasons[params.get('reason')],next==='/accounts'?'请使用管理员账号登录，继续管理用户。':''].filter(Boolean).join(' ');
    if(sessionNotice){$('session-feedback').textContent=sessionNotice;$('session-feedback').hidden=false;}
    try{const saved=localStorage.getItem(storageKey);if(saved&&saved.length<=100){username.value=saved;remember.checked=true;}}catch{/* Storage can be disabled on shared devices. Login still works. */}
    remember.addEventListener('change',()=>{if(!remember.checked){try{localStorage.removeItem(storageKey);}catch{}}});
    function maskPassword(){password.type='password';toggle.textContent='显示';toggle.setAttribute('aria-label','显示密码');toggle.setAttribute('aria-pressed','false');}
    toggle.addEventListener('click',()=>{const shown=password.type==='password';password.type=shown?'text':'password';toggle.textContent=shown?'隐藏':'显示';toggle.setAttribute('aria-label',shown?'隐藏密码':'显示密码');toggle.setAttribute('aria-pressed',String(shown));});
    function capsLock(event){$('caps-lock').hidden=!event.getModifierState?.('CapsLock');}
    password.addEventListener('keydown',capsLock);password.addEventListener('keyup',capsLock);
    password.addEventListener('blur',()=>{$('caps-lock').hidden=true;});
    [username,password].forEach(input=>input.addEventListener('input',()=>{
      // Credentials are validated together; editing either field clears the old pair error.
      username.removeAttribute('aria-invalid');password.removeAttribute('aria-invalid');
      if(!busy&&Date.now()>=cooldownUntil)feedback('');
    }));
    function controls(){
      const remaining=Math.max(0,Math.ceil((cooldownUntil-Date.now())/1000));
      button.disabled=busy||remaining>0;
      $('submit-label').textContent=busy?'正在登录…':remaining?`${remaining} 秒后可重试`:'登录工作台';
      form.setAttribute('aria-busy',String(busy));username.readOnly=busy;password.readOnly=busy;toggle.disabled=busy;remember.disabled=busy;
      if(!remaining&&cooldownTimer){clearInterval(cooldownTimer);cooldownTimer=undefined;feedback('现在可以重新尝试登录。','info');}
    }
    form.addEventListener('submit',async event=>{
      event.preventDefault();if(busy||Date.now()<cooldownUntil)return;
      if(!username.value.trim()){username.setAttribute('aria-invalid','true');feedback('请输入登录账号。');username.focus();return;}
      busy=true;controls();feedback('正在验证账号，请稍候。','info');maskPassword();
      try{
        const result=await api('/api/v1/auth/login',{method:'POST',body:JSON.stringify({username:username.value.trim(),password:password.value,next})});
        if(result.ok!==true||!['/','/accounts'].includes(result.redirect_to))throw new Error('登录结果未确认，请稍后重试。');
        try{if(remember.checked)localStorage.setItem(storageKey,username.value.trim());else localStorage.removeItem(storageKey);}catch{/* Remembering an account is optional. */}
        password.value='';feedback('登录成功，正在进入工作台。','success');$('submit-label').textContent='登录成功';
        location.replace(result.redirect_to==='/accounts'?'/accounts':'/');
      }catch(error){
        busy=false;
        if(error.status===401){password.value='';username.setAttribute('aria-invalid','true');password.setAttribute('aria-invalid','true');}
        if(error.status===429){cooldownUntil=Date.now()+Math.max(1,Math.min(3600,error.retryAfter))*1000;clearInterval(cooldownTimer);cooldownTimer=setInterval(controls,1000);}
        controls();feedback(error.status?error.message:error.message+' 输入内容已保留。');
        if(error.status===401)password.focus();
      }
    });
    window.addEventListener('pagehide',()=>{password.value='';maskPassword();clearInterval(cooldownTimer);});
    window.addEventListener('pageshow',event=>{if(event.persisted)location.reload();});
    controls();$('login-startup').hidden=true;
    return;
  }
  const form=$('account-form'), fields=$('account-fields');
  let baseline='', saving=false, signingOut=false, loading=false, switching=false, sessionBlocked=false, refreshPromise=null;
  const secretIds=['new-password','confirm-password','admin-password'];
  const stateLabel=a=>a.enabled?'已启用':a.password_configured?'已停用':'待设置密码';
  function verifiedIdentity(identity){
    // Null CSRF is valid for the local/API authentication modes; malformed headers are not.
    const roles=['field_engineer','researcher','reviewer','data_admin','system_admin','external_system'];
    if(!identity||!['user_id','username'].every(key=>typeof identity[key]==='string'&&identity[key].trim())
      ||!roles.includes(identity.role)||typeof identity.can_manage!=='boolean'
      ||identity.can_manage!==(identity.role==='system_admin')
      ||!(identity.csrf_token===null||(typeof identity.csrf_token==='string'&&/^[\x21-\x7e]+$/.test(identity.csrf_token)))){
      throw new Error('身份信息不完整或格式异常，请重试连接。');
    }
    return identity;
  }
  function validAccount(account){
    return account&&typeof account==='object'&&!Array.isArray(account)
      &&['id','username','display_name'].every(key=>typeof account[key]==='string'&&account[key].trim().length>0)
      &&['system_admin','field_engineer'].includes(account.role)
      &&typeof account.enabled==='boolean'&&typeof account.password_configured==='boolean';
  }
  function verifiedAccounts(response){
    const list=response?.accounts;
    if(!Array.isArray(list)||!list.every(validAccount)||new Set(list.map(account=>account.id)).size!==list.length){
      throw new Error('账号列表响应不完整或格式异常，请重试。');
    }
    return list;
  }
  function verifiedSave(result,targetId,data,password){
    const own=Boolean(targetId&&targetId===me.user_id);
    if(!validAccount(result)||(targetId&&result.id!==targetId)
      ||result.username!==data.username.toLowerCase()||result.display_name!==data.display_name
      ||result.role!==data.role||result.enabled!==data.enabled
      ||(password&&!result.password_configured)
      ||(targetId?result.relogin_required!==own:result.relogin_required!==undefined&&result.relogin_required!==false)){
      const error=new Error('保存结果未确认，当前输入已保留；请先刷新列表核对，避免重复创建账号。');
      error.code='ACCOUNT_RESPONSE_INVALID';throw error;
    }
    return result;
  }
  function clearSecrets(){secretIds.forEach(id=>{$(id).value='';});$('confirm-password').setCustomValidity('');$('confirm-password').removeAttribute('aria-invalid');$('password-match').textContent='';}
  function metadata(){return {username:$('account-username').value.trim(),display_name:$('display-name').value.trim(),role:$('account-role').value,enabled:$('account-enabled').value==='true'};}
  function dirty(){return !form.hidden&&(JSON.stringify(metadata())!==baseline||secretIds.some(id=>$(id).value.length>0));}
  function notice(text,success=false){message('page-feedback',text,success);$('account-notice').hidden=!text;}
  function blockSession(){
    sessionBlocked=true;clearSecrets();updateControls();
    notice('登录身份已变化。为避免用错误身份保存，已暂停编辑；本次修改未提交。');
    $('session-refresh').hidden=false;
  }
  function updateControls(){
    const locked=saving||signingOut||switching||sessionBlocked, unavailable=locked||!me;
    fields.disabled=unavailable;
    form.setAttribute('aria-busy',String(saving||signingOut||switching));
    $('account-save').disabled=unavailable||loading;$('cancel-edit').disabled=locked;
    $('new-account').disabled=unavailable||loading;
    $('refresh-accounts').disabled=unavailable||loading;
    $('logout').disabled=unavailable;
    $('logout').textContent=signingOut?'正在退出…':'退出登录';
    ['account-search','filter-role','filter-status','clear-filters'].forEach(id=>{$(id).disabled=unavailable;});
    document.querySelectorAll('.account-row').forEach(el=>{el.disabled=locked;});
    $('account-save').textContent=saving?'正在保存…':'保存账号设置';
    $('refresh-accounts').textContent=loading?'正在刷新…':'刷新列表';
    $('dirty-note').textContent=signingOut?'正在退出登录…':saving?'正在保存，请稍候':switching?'正在同步最新账号资料…':loading?'正在刷新列表，完成后可保存':dirty()?'有未保存的修改':'尚未修改';
    $('dirty-note').classList.toggle('is-dirty',dirty());
    $('dirty-note').hidden=form.hidden;
  }
  function updateFormHints(){
    const own=selected?.id===me?.user_id, admin=$('account-role').value==='system_admin', enabled=$('account-enabled').value==='true';
    $('role-hint').textContent=own?'当前登录的管理员不能停用或降级。':!enabled?'停用后，该账号不能登录；保存后已有登录也会失效。':admin?'管理员可管理资料、知识、系统设置和用户账号，请谨慎分配。':'普通用户可知识问答、浏览图谱与本人审计，不能修改资料或系统设置。';
    $('role-hint').classList.toggle('caution',!own&&(!enabled||admin));
    $('save-impact').textContent=own?'保存当前账号后将退出登录，请记住修改后的登录名和密码。':'保存后，该账号需重新登录；其他账号不受影响。';
    const first=$('new-password').value, second=$('confirm-password').value;
    const mismatch=Boolean(second&&first!==second);
    $('confirm-password').setCustomValidity(mismatch?'两次输入的新密码不一致。':'');
    $('confirm-password').setAttribute('aria-invalid',String(mismatch));
    $('password-match').textContent=second?(mismatch?'两次输入的新密码不一致。':'两次密码一致。'):'';
    $('password-match').classList.toggle('matches',Boolean(second&&!mismatch));
    updateControls();
  }
  async function canDiscard(){
    if(saving||signingOut||switching)return false;
    if(!dirty())return true;
    const dialog=$('discard-dialog');
    if(dialog.open)return false;
    return new Promise(resolve=>{
      dialog.returnValue='keep';
      dialog.addEventListener('close',()=>resolve(dialog.returnValue==='discard'),{once:true});
      dialog.showModal();
    });
  }
  $('keep-editing').addEventListener('click',()=>$('discard-dialog').close('keep'));
  $('discard-changes').addEventListener('click',()=>$('discard-dialog').close('discard'));
  window.addEventListener('beforeunload',event=>{if(!allowNavigation&&(dirty()||saving||signingOut)){event.preventDefault();event.returnValue='';}});
  window.addEventListener('pagehide',clearSecrets);
  window.addEventListener('pageshow',event=>{if(event.persisted){allowNavigation=true;location.reload();}});
  async function selectAccount(id){
    if(saving||signingOut||switching||sessionBlocked)return;
    // Capture the refresh before the dialog: it may finish while the user decides.
    let pending=refreshPromise;
    if(!await canDiscard())return;
    switching=true;updateControls();
    try{
      while(pending){await pending;pending=refreshPromise;}
      if(sessionBlocked||allowNavigation)return;
      const current=accounts.find(account=>account.id===id);
      if(!current){notice('所选账号已不在最新列表中，当前输入已保留。请刷新列表后重新选择。');return;}
      edit(current);
    }catch(error){
      if(!sessionBlocked&&!allowNavigation)notice('账号切换失败：未能取得最新资料，当前输入已保留。请刷新列表后重新选择。');
    }finally{switching=false;updateControls();}
  }
  function renderList(){
    const term=$('account-search').value.trim().toLowerCase(), role=$('filter-role').value, status=$('filter-status').value;
    const visible=accounts.filter(a=>(a.username+' '+a.display_name).toLowerCase().includes(term)
      &&(role==='all'||a.role===role)&&(status==='all'||(status==='enabled'&&a.enabled)||(status==='disabled'&&!a.enabled&&a.password_configured)||(status==='pending'&&!a.password_configured)));
    $('account-list').replaceChildren();
    $('account-count').textContent=`显示 ${visible.length} / ${accounts.length} 个账号`;
    $('clear-filters').hidden=!term&&role==='all'&&status==='all';
    const totals=[['账号总数',accounts.length],['已启用',accounts.filter(a=>a.enabled).length],['待设置密码',accounts.filter(a=>!a.password_configured).length]];
    $('account-overview').replaceChildren(...totals.map(([label,value])=>{const item=document.createElement('span'),number=document.createElement('strong');number.textContent=String(value);item.append(number,document.createTextNode(label));return item;}));
    for(const account of visible){
      const button=document.createElement('button');button.type='button';button.className='account-row'+(selected?.id===account.id?' active':'');button.setAttribute('aria-label',`设置 ${account.display_name}（${account.username}）`);button.setAttribute('aria-pressed',String(selected?.id===account.id));
      const title=document.createElement('strong');title.textContent=account.display_name+(account.id===me?.user_id?' · 当前账号':'');
      const sub=document.createElement('small');sub.textContent=`${account.username} · ${account.role==='system_admin'?'管理员':'普通用户'}`;
      const badge=document.createElement('span');badge.className='badge'+(account.enabled?'':' off');badge.textContent=stateLabel(account);
      button.append(title,sub,badge);button.addEventListener('click',()=>selectAccount(account.id));$('account-list').append(button);
    }
    if(!visible.length){const empty=document.createElement('p');empty.className='directory-empty';empty.textContent='没有匹配的账号。试试其他关键词或清除筛选。';$('account-list').append(empty);}
    $('account-list').setAttribute('aria-busy',String(loading));updateControls();
  }
  function edit(account=null,{focus=true}={}){
    selected=account;form.reset();form.hidden=false;$('editor-empty').hidden=true;
    $('editor-title').textContent=account?account.display_name:'新增账号';$('editor-kicker').textContent=account?'编辑账号':'分配访问权限';
    $('editor-status').hidden=false;$('editor-status').textContent=account?stateLabel(account):'新账号';$('editor-status').classList.toggle('off',Boolean(account&&!account.enabled));
    $('editor-description').textContent=account?.id===me?.user_id?'这是你当前登录的管理员账号。修改后需要重新登录。':'设置成员的登录信息、权限和账号状态。';
    $('account-username').value=account?.username||'';$('display-name').value=account?.display_name||'';
    $('account-role').value=account?.role||'field_engineer';$('account-enabled').value=String(account?.enabled??true);
    $('account-role').disabled=account?.id===me?.user_id;$('account-enabled').disabled=account?.id===me?.user_id;
    $('password-hint').textContent=account?.password_configured?'不修改密码可留空；新密码至少 8 位。':'启用账号前需设置至少 8 位密码；可先保存为停用账号。';
    $('new-password').placeholder=account?.password_configured?'留空则保留原密码':'至少 8 位';
    const raw=typeof account?.created_at==='string'?account.created_at:'', instant=raw?new Date(/(Z|[+-]\d{2}:\d{2})$/.test(raw)?raw:raw+'Z'):null;
    $('account-created').hidden=!instant||Number.isNaN(instant.getTime());
    $('account-created').textContent=instant&&!Number.isNaN(instant.getTime())?'创建于 '+new Intl.DateTimeFormat('zh-CN',{dateStyle:'medium',timeZone:'Asia/Shanghai'}).format(instant):'';
    baseline=JSON.stringify(metadata());message('form-feedback','');updateFormHints();renderList();
    if(focus){$('editor-title').focus({preventScroll:true});if(matchMedia('(max-width:900px)').matches)$('account-editor').scrollIntoView({block:'start',behavior:'auto'});}
  }
  function closeEditor(){
    selected=null;form.reset();form.hidden=true;$('editor-empty').hidden=false;
    $('editor-title').textContent='选择一个账号';$('editor-kicker').textContent='账号设置';$('editor-description').textContent='从账号目录选择成员，或新增一个用户账号。';
    $('editor-status').hidden=true;$('account-created').hidden=true;baseline='';renderList();$('new-account').focus();
  }
  function loadAccounts({initial=false}={}){
    if(refreshPromise)return refreshPromise;
    loading=true;updateControls();$('account-list').setAttribute('aria-busy','true');
    refreshPromise=(async()=>{try{
      const data=await api('/api/v1/accounts');
      // Reject the entire response before changing the usable directory or draft.
      accounts=verifiedAccounts(data);renderList();
      // Refresh metadata only when it will not overwrite an unsaved form.
      if(selected&&!dirty()){const updated=accounts.find(a=>a.id===selected.id);if(updated)edit(updated,{focus:false});}
      if(!initial&&!sessionBlocked)notice('账号列表已刷新。',true);
    }finally{loading=false;refreshPromise=null;$('account-list').setAttribute('aria-busy','false');updateControls();}})();
    return refreshPromise;
  }
  ['account-search','filter-role','filter-status'].forEach(id=>$(id).addEventListener(id==='account-search'?'input':'change',renderList));
  $('clear-filters').addEventListener('click',()=>{$('account-search').value='';$('filter-role').value='all';$('filter-status').value='all';renderList();$('account-search').focus();});
  $('refresh-accounts').addEventListener('click',async()=>{try{await loadAccounts();}catch(error){if(!sessionBlocked)notice('列表刷新失败，当前编辑内容已保留。'+error.message);}});
  $('new-account').addEventListener('click',async()=>{if(await canDiscard())edit();});
  $('cancel-edit').addEventListener('click',async()=>{if(await canDiscard())closeEditor();});
  document.querySelector('.back-link').addEventListener('click',async event=>{event.preventDefault();if(await canDiscard()){allowNavigation=true;location.assign('/');}});
  $('session-refresh').addEventListener('click',async()=>{if(await canDiscard()){allowNavigation=true;location.replace('/login?next=%2Faccounts');}});
  $('logout').addEventListener('click',async()=>{
    if(!me||sessionBlocked)return;
    if(!await canDiscard()||signingOut)return;
    signingOut=true;updateControls();notice('正在退出登录…');
    try{
      const result=await api('/api/v1/auth/logout',{method:'POST'});
      if(result.ok!==true)throw new Error('服务未确认退出。');
      allowNavigation=true;clearSecrets();location.replace('/login?reason=logged_out');
    }catch(error){
      signingOut=false;
      if(!sessionBlocked&&!allowNavigation)notice('退出结果未确认，当前登录可能仍然有效。'+error.message+' 请重试退出；共用设备请勿直接离开。');
      updateControls();
    }
  });
  form.addEventListener('input',()=>{updateFormHints();message('form-feedback','');});
  form.addEventListener('change',updateFormHints);
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(!me||saving||signingOut||loading||switching||sessionBlocked)return;
    const password=$('new-password').value, data=metadata();
    if(password!==$('confirm-password').value){message('form-feedback','两次输入的新密码不一致。');$('confirm-password').focus();return;}
    if(!data.display_name){message('form-feedback','显示名称不能为空。');$('display-name').focus();return;}
    if(data.enabled&&!password&&!selected?.password_configured){message('form-feedback','启用账号前请先设置密码。');$('new-password').focus();return;}
    if(selected&&JSON.stringify(data)===baseline&&!password){message('form-feedback','账号设置没有变化，无需保存。');return;}
    const payload={...data,password:password||null,admin_password:$('admin-password').value}, targetId=selected?.id;
    saving=true;updateControls();message('form-feedback','正在保存，请勿关闭页面…');notice('');
    let result;
    try{
      const response=await api('/api/v1/accounts'+(targetId?'/'+encodeURIComponent(targetId):''),{method:targetId?'PUT':'POST',body:JSON.stringify(payload)});
      result=verifiedSave(response,targetId,data,password);
    }
    catch(error){
      saving=false;$('admin-password').value='';updateControls();
      message('form-feedback',error.status||error.code==='ACCOUNT_RESPONSE_INVALID'?error.message:error.message+' 请求结果尚未确认；请先刷新列表核对，避免重复创建账号。');return;
    }
    clearSecrets();allowNavigation=Boolean(result.relogin_required);
    if(result.relogin_required){location.replace('/login?reason=password_changed&next=%2Faccounts');return;}
    // The write has committed: adopt its returned identity before any refresh.
    const index=accounts.findIndex(a=>a.id===result.id);if(index<0)accounts.push(result);else accounts[index]=result;
    edit(result,{focus:false});message('form-feedback','账号已保存；该账号原有登录已失效。',true);
    try{
      const refreshed=await api('/api/v1/accounts');accounts=verifiedAccounts(refreshed);renderList();
    }catch(error){
      if(!sessionBlocked)notice('账号已保存，但列表同步暂不可用；可点击“刷新列表”重试，无需再次保存。');
    }finally{saving=false;updateControls();}
  });
  (async()=>{
    updateControls();
    try{
      me=verifiedIdentity(await api('/api/v1/auth/me'));csrf=me.csrf_token||'';
      if(!me.can_manage){allowNavigation=true;location.replace('/');return;}
      $('signed-in').textContent=me.username+' · 管理员';updateControls();
      await loadAccounts({initial:true});
    }catch(error){
      if(!sessionBlocked)notice(error.message);
      $('account-list').setAttribute('aria-busy','false');
      if(!me){$('signed-in').textContent='身份验证未完成';$('account-overview').textContent='身份确认后显示账号状态';$('session-refresh').hidden=false;$('session-refresh').textContent='重试连接';$('account-list').textContent='身份信息暂未读取，请点击“重试连接”。';}
      else $('account-list').textContent='账号列表暂不可用，请点击“刷新列表”重试。';
    }
  })();
})();

