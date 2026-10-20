// Synthetic accounts and isolated 8766 fixture only. Run with playwright-cli run-code.
async page => {
  const base='http://127.0.0.1:8766', results=[], errors=[], releases=new Set();
  if(!page.url().startsWith(base+'/'))throw new Error('Only the isolated 8766 preview is allowed');
  const check=(value,name)=>{if(!value)throw new Error(name);results.push(name);};
  const onError=error=>errors.push(error.message);
  const onDialog=dialog=>dialog.type()==='beforeunload'?dialog.accept():dialog.dismiss();
  const pattern='**/api/v1/accounts**';
  let listReply, saveReply, getCount=0, postCount=0, unexpectedWrites=0, routed=false;
  const json=(body,status=200)=>({status,contentType:'application/json',body:JSON.stringify(body)});
  const held=(body,status=200)=>{
    let release, started;
    const gate=new Promise(resolve=>{release=()=>{releases.delete(release);resolve();};});
    const requested=new Promise(resolve=>{started=resolve;});
    releases.add(release);
    return {body,status,gate,requested,started,release};
  };
  const handler=async route=>{
    const request=route.request(), url=request.url();
    if(!url.startsWith(base+'/')){await route.abort();return;}
    const pathname=url.slice(base.length).split('?')[0];
    if(request.method()==='GET'&&pathname==='/api/v1/accounts'){
      getCount++;
      const reply=listReply;
      reply.started?.();
      if(reply.gate)await reply.gate;
      await route.fulfill(json(reply.body,reply.status));
      return;
    }
    if(request.method()==='POST'&&pathname==='/api/v1/accounts'&&saveReply){
      postCount++;
      const reply=saveReply;
      reply.started?.();
      if(reply.gate)await reply.gate;
      await route.fulfill(json(reply.body,reply.status));
      return;
    }
    // Account writes never reach the preview database, including unexpected ones.
    unexpectedWrites++;
    await route.fulfill(json({message:'Unexpected account request blocked by isolated regression test'},503));
  };
  const idle=()=>page.waitForFunction(()=>document.querySelector('#account-list').getAttribute('aria-busy')==='false'&&!document.querySelector('#refresh-accounts').disabled);
  const refresh=async reply=>{
    listReply=reply;
    const before=getCount;
    await page.getByRole('button',{name:'刷新列表',exact:true}).click();
    await idle();
    check(getCount===before+1,'Refresh performs exactly one list request ('+(results.length+1)+')');
  };
  const closeEditor=async()=>{
    if(await page.locator('#account-form').isVisible()){
      await page.getByRole('button',{name:'取消',exact:true}).click();
      if(await page.getByRole('dialog').isVisible())await page.getByRole('button',{name:'放弃修改',exact:true}).click();
      await page.locator('#editor-empty').waitFor();
    }
  };
  page.on('pageerror',onError);
  page.on('dialog',onDialog);
  try{
    await page.context().clearCookies();
    await page.goto(base+'/login?next=%2Faccounts');
    await page.getByLabel('登录账号',{exact:true}).fill('preview-admin');
    await page.getByLabel('密码',{exact:true}).fill('Preview-Admin-2026');
    await page.getByRole('button',{name:'登录工作台',exact:true}).click();
    await page.waitForURL(base+'/accounts');
    await idle();
    const sourceResponse=await page.request.get(base+'/api/v1/accounts');
    check(sourceResponse.ok(),'Isolated fixture list is available');
    const source=await sourceResponse.json();
    const sourceAdmin=source.accounts?.find(account=>account.username==='preview-admin');
    const sourceReader=source.accounts?.find(account=>account.username==='tunnel-reader');
    check(Boolean(sourceAdmin&&sourceReader),'Required synthetic administrator and reader fixtures exist');
    const admin={...sourceAdmin,display_name:'系统管理员',enabled:true,password_configured:true};
    const reader={...sourceReader,display_name:'普通用户',enabled:true,password_configured:true};
    const accounts=[admin,reader], valid={accounts};
    const row=account=>page.getByRole('button',{name:`设置 ${account.display_name}（${account.username}）`,exact:true});
    listReply={body:{}};
    await page.route(pattern,handler);
    routed=true;
    await page.reload();
    await page.locator('#account-list').filter({hasText:'账号列表暂不可用'}).waitFor();
    await idle();
    check((await page.locator('#page-feedback').textContent()).trim().length>0,'Malformed initial list displays an actionable error');
    check(await page.getByRole('button',{name:'刷新列表',exact:true}).isEnabled(),'Malformed initial list leaves retry available');
    await page.getByLabel('搜索账号或姓名',{exact:true}).fill('preview');
    await page.getByLabel('角色',{exact:true}).selectOption('system_admin');
    await page.getByLabel('状态',{exact:true}).selectOption('enabled');
    check(errors.length===0,'Search and filters remain safe after malformed initial list');
    await page.getByRole('button',{name:'清除筛选',exact:true}).click();
    await refresh({body:valid});
    check(await page.locator('.account-row').count()===2,'Retry recovers from malformed initial list');
    await refresh({body:{accounts:[admin,{...reader,created_at:{toString:null}}]}});
    await row(reader).click();
    check(await page.getByLabel('登录账号',{exact:true}).inputValue()===reader.username,'Optional malformed date does not prevent selecting a valid account');
    check(await page.locator('#account-created').isHidden(),'Unusable display-only date is hidden safely');
    await closeEditor();
    await refresh({body:valid});
    await row(admin).click();
    await page.getByLabel('显示名称',{exact:true}).fill('列表失败时保留的草稿');
    await page.getByLabel('管理员当前密码',{exact:true}).fill('Preview-Admin-2026');
    await page.getByLabel('搜索账号或姓名',{exact:true}).fill('tunnel');
    await page.getByLabel('角色',{exact:true}).selectOption('field_engineer');
    await page.getByLabel('状态',{exact:true}).selectOption('enabled');
    const oldList=await page.locator('#account-list').textContent();
    const oldCount=await page.locator('#account-count').textContent();
    const malformed=[
      ['missing accounts',{}],
      ['null accounts',{accounts:null}],
      ['non-array accounts',{accounts:{}}],
      ['null item',{accounts:[admin,null]}],
      ['missing account fields',{accounts:[admin,{id:reader.id}]}],
      ['invalid role',{accounts:[admin,{...reader,role:'unknown-role'}]}],
      ['non-boolean enabled',{accounts:[admin,{...reader,enabled:'false'}]}],
      ['non-boolean password state',{accounts:[admin,{...reader,password_configured:null}]}],
      ['empty account ID',{accounts:[admin,{...reader,id:''}]}],
      ['duplicate account IDs',{accounts:[admin,{...reader,id:admin.id}]}]
    ];
    for(const [name,body] of malformed){
      await refresh({body});
      check((await page.locator('#page-feedback').textContent()).includes('列表刷新失败'),'Malformed '+name+' is reported as a list failure');
      check(await page.locator('#account-list').textContent()===oldList&&await page.locator('#account-count').textContent()===oldCount,'Malformed '+name+' preserves the previous directory atomically');
      check(await page.getByLabel('显示名称',{exact:true}).inputValue()==='列表失败时保留的草稿'&&await page.getByLabel('管理员当前密码',{exact:true}).inputValue()==='Preview-Admin-2026','Malformed '+name+' preserves draft fields');
      check(await page.getByLabel('搜索账号或姓名',{exact:true}).inputValue()==='tunnel'&&await page.getByLabel('角色',{exact:true}).inputValue()==='field_engineer'&&await page.getByLabel('状态',{exact:true}).inputValue()==='enabled','Malformed '+name+' preserves search and filters');
      await page.getByLabel('搜索账号或姓名',{exact:true}).fill('no-such-account-sync');
      check(await page.locator('.directory-empty').isVisible(),'Search works after malformed '+name);
      await page.getByLabel('搜索账号或姓名',{exact:true}).fill('tunnel');
    }
    check(errors.length===0,'Malformed list variants produce no unhandled page errors');
    await closeEditor();
    await page.getByRole('button',{name:'清除筛选',exact:true}).click();
    await refresh({body:valid});

    const fake={...reader,id:'sync-fake-'+Date.now().toString(36),username:'sync-fake-'+Date.now().toString(36),display_name:'仅在浏览器中模拟的账号',relogin_required:false};
    await page.getByRole('button',{name:'＋ 新增账号',exact:true}).click();
    await page.getByLabel('登录账号',{exact:true}).fill(fake.username);
    await page.getByLabel('显示名称',{exact:true}).fill(fake.display_name);
    await page.getByLabel('设置新密码',{exact:true}).fill('Preview-Reader-2026');
    await page.getByLabel('再次输入新密码',{exact:true}).fill('Preview-Reader-2026');
    await page.getByLabel('管理员当前密码',{exact:true}).fill('Preview-Admin-2026');
    listReply={body:{}};
    saveReply=held(fake);
    await page.getByRole('button',{name:'保存账号设置',exact:true}).click();
    await saveReply.requested;
    await page.locator('#account-form').evaluate(form=>{form.requestSubmit();form.requestSubmit();});
    saveReply.release();
    await page.locator('#page-feedback').filter({hasText:'账号已保存，但列表同步暂不可用'}).waitFor();
    await idle();
    check(postCount===1,'Committed save performs exactly one intercepted POST');
    check((await page.locator('#form-feedback').textContent()).includes('账号已保存'),'Malformed post-save list preserves committed success');
    check(await page.getByLabel('登录账号',{exact:true}).inputValue()===fake.username&&await page.getByLabel('显示名称',{exact:true}).inputValue()===fake.display_name,'Malformed post-save list keeps the committed account selected');
    check(await row(fake).isVisible()&&await page.locator('.account-row').count()===3,'Malformed post-save list retains the committed row and earlier rows');
    check(await page.getByLabel('设置新密码',{exact:true}).inputValue()===''&&await page.getByLabel('再次输入新密码',{exact:true}).inputValue()===''&&await page.getByLabel('管理员当前密码',{exact:true}).inputValue()==='','Confirmed save clears all secret fields');
    await page.getByLabel('搜索账号或姓名',{exact:true}).fill(fake.username);
    check(await row(fake).isVisible(),'Search works after malformed post-save list');
    await page.getByRole('button',{name:'清除筛选',exact:true}).click();
    await refresh({body:{accounts:[...accounts,fake]}});
    check(postCount===1&&unexpectedWrites===0,'Post-save retry only reads and never duplicates the write');
    await page.getByLabel('管理员当前密码',{exact:true}).fill('Preview-Admin-2026');
    await page.getByRole('button',{name:'保存账号设置',exact:true}).click();
    await page.locator('#form-feedback').filter({hasText:'无需保存'}).waitFor();
    check(postCount===1&&unexpectedWrites===0,'Committed baseline prevents an unchanged account from being resubmitted');
    saveReply=null;

    const prepareRace=async label=>{
      await closeEditor();
      await refresh({body:valid});
      await row(admin).click();
      await page.getByLabel('显示名称',{exact:true}).fill(label);
      await page.getByLabel('管理员当前密码',{exact:true}).fill('Preview-Admin-2026');
    };
    const openRace=async reply=>{
      listReply=reply;
      await page.getByRole('button',{name:'刷新列表',exact:true}).click();
      await reply.requested;
      await row(reader).click();
      await page.getByRole('dialog').waitFor();
    };
    const selectedReader=()=>page.waitForFunction(username=>document.querySelector('#account-username').value===username,reader.username);
    const unchangedDraft=async label=>await page.getByLabel('登录账号',{exact:true}).inputValue()===admin.username&&await page.getByLabel('显示名称',{exact:true}).inputValue()===label&&await page.getByLabel('管理员当前密码',{exact:true}).inputValue()==='Preview-Admin-2026';

    await prepareRace('刷新先完成的管理员草稿');
    const firstReader={...reader,display_name:'刷新已完成后的最新用户名称'};
    const first=held({accounts:[admin,firstReader]});
    await openRace(first);
    first.release();
    await idle();
    check(await unchangedDraft('刷新先完成的管理员草稿'),'Refresh completion preserves draft while discard dialog remains open');
    await page.getByRole('button',{name:'放弃修改',exact:true}).click();
    await selectedReader();
    check(await page.getByLabel('显示名称',{exact:true}).inputValue()===firstReader.display_name,'Discard after refresh resolves the target from the latest directory');

    await prepareRace('先确认放弃的管理员草稿');
    const secondReader={...reader,display_name:'等待刷新后得到的最新用户名称'};
    const second=held({accounts:[admin,secondReader]});
    await openRace(second);
    await page.getByRole('button',{name:'放弃修改',exact:true}).click();
    await page.getByRole('dialog').waitFor({state:'hidden'});
    check(await unchangedDraft('先确认放弃的管理员草稿'),'Discard before refresh completes waits without clearing the draft');
    check(await page.getByRole('button',{name:'保存账号设置',exact:true}).isDisabled(),'Pending selection cannot save against in-flight metadata');
    second.release();
    await idle();
    await selectedReader();
    check(await page.getByLabel('显示名称',{exact:true}).inputValue()===secondReader.display_name,'Waiting selection adopts the refreshed target after response arrival');

    for(const failure of [{name:'HTTP failure',body:{message:'模拟列表暂不可用'},status:503},{name:'malformed success',body:{},status:200}]){
      for(const finishFirst of [true,false]){
        const label='失败刷新保留草稿 '+failure.name+' '+finishFirst;
        const order=finishFirst?'refresh first':'discard first';
        await prepareRace(label);
        const reply=held(failure.body,failure.status);
        await openRace(reply);
        if(finishFirst){reply.release();await idle();}
        await page.getByRole('button',{name:'放弃修改',exact:true}).click();
        if(!finishFirst){reply.release();await idle();}
        await page.locator('#page-feedback').filter({hasText:'失败'}).waitFor();
        check(await unchangedDraft(label),'Selection after '+failure.name+' preserves the administrator draft and secret ('+order+')');
        check(await page.locator('#account-form').isVisible()&&await row(reader).isVisible(),'Selection after '+failure.name+' keeps the existing editor and directory ('+order+')');
      }
    }

    for(const finishFirst of [true,false]){
      const label='目标消失时保留草稿 '+finishFirst;
      await prepareRace(label);
      const removed=held({accounts:[admin]});
      await openRace(removed);
      if(finishFirst){removed.release();await idle();}
      await page.getByRole('button',{name:'放弃修改',exact:true}).click();
      if(!finishFirst){removed.release();await idle();}
      await page.waitForFunction(()=>document.querySelector('#page-feedback').textContent.includes('保留')&&!document.querySelector('#page-feedback').classList.contains('success'));
      check(await unchangedDraft(label),'Missing target preserves the current draft ('+(finishFirst?'refresh first':'discard first')+')');
      check(await page.locator('#account-form').isVisible()&&await page.locator('#editor-kicker').textContent()==='编辑账号','Missing target never opens a blank new-account editor ('+(finishFirst?'refresh first':'discard first')+')');
      check(!(await page.locator('#page-feedback').textContent()).includes('账号列表已刷新'),'Missing target reports a selection notice instead of only refresh success');
    }
    check(postCount===1&&unexpectedWrites===0,'All race scenarios remain read-only and all writes stayed mocked');
    check(errors.length===0,'No unhandled browser errors');
    await closeEditor();
    await page.getByRole('button',{name:'退出登录',exact:true}).click();
    await page.waitForURL('**/login?reason=logged_out');
    return {status:'passed',assertions:results.length,getCount,postCount,unexpectedWrites,checks:results};
  }finally{
    for(const release of [...releases])release();
    await page.context().clearCookies();
    if(page.url()!==base+'/login?reason=logged_out')await page.goto(base+'/login?reason=logged_out');
    if(routed)await page.unroute(pattern,handler);
    page.off('pageerror',onError);
    page.off('dialog',onDialog);
  }
}
