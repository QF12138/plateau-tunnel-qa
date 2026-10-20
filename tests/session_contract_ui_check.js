// Run with playwright-cli run-code against the isolated 8766 fixture only.
// Identity/logout failures are mocked; real writes are limited to fixture login/logout.
async page => {
  const base='http://127.0.0.1:8766', checks=[], errors=[], dialogs=[], releases=new Set();
  if(!page.url().startsWith(base+'/'))throw new Error('Only the isolated 8766 preview is allowed');
  const check=(value,label)=>{if(!value)throw new Error(label);checks.push(label);};
  const pattern='**/api/v1/**';
  let identityReply, logoutReply, identityRequests=0, logoutRequests=0, unexpectedWrites=0;
  let accountRequests=0, settingsRequests=0, routed=false;
  const onError=error=>errors.push(error.message);
  const onDialog=async dialog=>{dialogs.push(dialog.type()+': '+dialog.message());await dialog.dismiss();};
  const json=body=>({status:200,contentType:'application/json',body:JSON.stringify(body)});
  const held=body=>{
    let release, started;
    const gate=new Promise(resolve=>{release=()=>{releases.delete(release);resolve();};});
    const requested=new Promise(resolve=>{started=resolve;});
    releases.add(release);
    return {body,gate,requested,started,release};
  };
  const handler=async route=>{
    const request=route.request(),url=request.url();
    if(!url.startsWith(base+'/')){await route.abort();return;}
    const path=url.slice(base.length).split('?')[0], method=request.method();
    if(path==='/api/v1/auth/me'&&method==='GET'){
      identityRequests++;
      if(identityReply){await route.fulfill(json(identityReply.body));return;}
    }
    if(path==='/api/v1/accounts')accountRequests++;
    if(path==='/api/v1/settings')settingsRequests++;
    if(path==='/api/v1/auth/logout'&&method==='POST'){
      logoutRequests++;
      if(logoutReply){
        const reply=logoutReply;reply.started?.();
        if(reply.gate)await reply.gate;
        await route.fulfill(json(reply.body));return;
      }
    }
    if(!['GET','HEAD','OPTIONS'].includes(method)&&!['/api/v1/auth/login','/api/v1/auth/logout'].includes(path)){
      unexpectedWrites++;
      await route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({message:'Unexpected fixture write blocked by regression test'})});return;
    }
    await route.continue();
  };
  const pages=[
    {path:'/accounts',name:'Accounts',retry:'#session-refresh',logout:'#logout',feedback:'#page-feedback'},
    {path:'/',name:'Workbench',retry:'#identity-retry',logout:'#session-logout',feedback:'#session-feedback'}
  ];
  async function healthy(view){
    if(view.path==='/accounts'){
      await page.locator('#signed-in').filter({hasText:'preview-admin'}).waitFor();
      await page.waitForFunction(()=>!document.querySelector('#new-account').disabled&&document.querySelector('#account-list').getAttribute('aria-busy')==='false');
    }else{
      await page.locator('#session-username').filter({hasText:'preview-admin'}).waitFor();
      await page.waitForFunction(()=>!document.querySelector('#session-logout').disabled);
    }
  }
  async function login(view){
    await page.goto(base+'/login?next='+encodeURIComponent(view.path));
    await page.getByLabel('登录账号',{exact:true}).fill('preview-admin');
    await page.getByLabel('密码',{exact:true}).fill('Preview-Admin-2026');
    await page.getByRole('button',{name:'登录工作台',exact:true}).click();
    await page.waitForURL(base+view.path);
    await healthy(view);
    const response=await page.request.get(base+'/api/v1/auth/me');
    check(response.ok(),view.name+' fixture identity is available after real login');
    return response.json();
  }
  async function assertLayout(view,label){
    for(const width of [360,768,1440]){
      await page.setViewportSize({width,height:900});
      check(await page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth+1),view.name+' '+label+' fits '+width+'px');
      if(label==='identity error')check(await page.locator(view.retry).isVisible(),view.name+' retry is visible at '+width+'px');
      if(width===360)await page.screenshot({path:'output/playwright/session-contract-'+view.name.toLowerCase()+'-'+label.replaceAll(' ','-')+'-360.png',animations:'disabled'});
    }
  }
  page.on('pageerror',onError);page.on('dialog',onDialog);
  try{
    await page.context().clearCookies();
    await page.route(pattern,handler);routed=true;
    for(const view of pages){
      const valid=await login(view);
      const without=(key)=>{const copy={...valid};delete copy[key];return copy;};
      const malformed=[
        ['empty object',{}],['null',null],['array',[]],
        ['string capability',{...valid,can_manage:'false'}],
        ['missing capability',without('can_manage')],
        ['missing ID',without('user_id')],['blank ID',{...valid,user_id:' '}],
        ['missing username',without('username')],['blank username',{...valid,username:' '}],
        ['unknown role',{...valid,role:'unknown-role'}],
        ['elevated ordinary role',{...valid,role:'field_engineer',can_manage:true}],
        ['administrator without capability',{...valid,can_manage:false}],
        ['missing CSRF',without('csrf_token')],['object CSRF',{...valid,csrf_token:{}}],
        ['boolean CSRF',{...valid,csrf_token:false}],['empty CSRF',{...valid,csrf_token:''}],
        ['header newline CSRF',{...valid,csrf_token:'token\r\ninjected:true'}],
        ['non-ASCII CSRF',{...valid,csrf_token:'中文令牌'}]
      ];
      for(const [label,body] of malformed){
        identityReply={body};
        const beforeAccounts=accountRequests,beforeSettings=settingsRequests,beforeIdentity=identityRequests;
        await page.goto(base+view.path);
        const retry=page.locator(view.retry);
        await retry.filter({hasText:'重试连接'}).waitFor();
        check(page.url()===base+view.path,view.name+' '+label+' stays on the current page');
        check(await retry.isEnabled(),view.name+' '+label+' has an enabled explicit retry');
        check(await page.locator(view.logout).isDisabled(),view.name+' '+label+' cannot logout with unverified identity');
        if(view.path==='/accounts'){
          check(await page.locator('#new-account').isDisabled()&&await page.locator('#account-save').isDisabled(),view.name+' '+label+' disables account writes');
          check((await page.locator('#page-feedback').textContent()).trim().length>0,view.name+' '+label+' explains the identity failure');
        }else{
          check(await page.getByRole('button',{name:'查询已发布知识',exact:true}).isDisabled(),view.name+' '+label+' disables question submission');
          check(await page.locator('#accounts-entry').isHidden()&&await page.locator('.settings-entry').isHidden(),view.name+' '+label+' hides administrative entry points');
        }
        check(accountRequests===beforeAccounts&&settingsRequests===beforeSettings,view.name+' '+label+' does not fetch accounts or settings before verification');
        check(identityRequests===beforeIdentity+1,view.name+' '+label+' does not automatically repeat verification');
        if(label==='empty object')await assertLayout(view,'identity error');
        identityReply=undefined;
        await retry.click();
        await healthy(view);
        check(page.url()===base+view.path,view.name+' '+label+' retry recovers without a login detour');
        check(await page.locator(view.logout).isEnabled(),view.name+' '+label+' retry restores verified controls');
      }

      identityReply={body:{...valid,csrf_token:null}};
      await page.goto(base+view.path);await healthy(view);
      check(await page.locator(view.logout).isEnabled(),view.name+' permits null CSRF for the local/Basic identity contract');
      identityReply=undefined;
      await page.reload();await healthy(view);
      for(const [label,body] of [['missing success',{}],['false success',{ok:false}],['string success',{ok:'true'}],['null success',null]]){
        logoutReply=held(body);
        const before=logoutRequests;
        await page.locator(view.logout).click();
        await logoutReply.requested;
        check(await page.locator(view.logout).isDisabled(),view.name+' '+label+' disables logout while pending');
        // Event dispatch deliberately checks the handler guard as well as the disabled button.
        await page.locator(view.logout).evaluate(button=>{button.dispatchEvent(new MouseEvent('click',{bubbles:true}));button.dispatchEvent(new MouseEvent('click',{bubbles:true}));});
        logoutReply.release();
        await page.locator(view.feedback).filter({hasText:'退出结果未确认'}).waitFor();
        await page.waitForFunction(selector=>!document.querySelector(selector).disabled,view.logout);
        check(logoutRequests===before+1,view.name+' '+label+' prevents duplicate or automatic logout attempts');
        check(page.url()===base+view.path,view.name+' '+label+' does not navigate or claim logout');
        check(!(await page.locator(view.feedback).textContent()).includes('安全退出'),view.name+' '+label+' does not claim safe logout');
        check(await page.locator(view.logout).isEnabled(),view.name+' '+label+' leaves an explicit manual retry');
        const session=await page.request.get(base+'/api/v1/auth/me');
        check(session.ok(),view.name+' '+label+' preserves the fixture session because the request was mocked');
        if(label==='missing success')await assertLayout(view,'logout error');
      }
      logoutReply=undefined;
      const before=logoutRequests;
      await page.locator(view.logout).click();
      await page.waitForURL('**/login?reason=logged_out');
      check(logoutRequests===before+1,view.name+' confirmed logout uses exactly one real fixture request');
      check((await page.locator('#session-feedback').textContent()).includes('安全退出'),view.name+' confirmed logout shows success only after acknowledgement');
      const after=await page.request.get(base+'/api/v1/auth/me');
      check(after.status()===401,view.name+' confirmed logout invalidates the actual fixture session');
    }
    check(unexpectedWrites===0,'No account, settings, document or question writes were attempted');
    check(dialogs.length===0,'Errors use inline feedback without blocking alerts');
    check(errors.length===0,'No unhandled browser errors: '+errors.join('; '));
    return {status:'passed',assertions:checks.length,identityRequests,logoutRequests,unexpectedWrites,checks};
  }finally{
    for(const release of [...releases])release();
    identityReply=undefined;logoutReply=undefined;
    if(routed)await page.unroute(pattern,handler);
    await page.context().clearCookies();
    await page.goto(base+'/login?reason=logged_out');
    page.off('pageerror',onError);page.off('dialog',onDialog);
  }
}
