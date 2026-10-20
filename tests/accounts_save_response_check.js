// Malformed-save regression. Only mocked writes against the isolated 8766 fixture.
async page => {
  const base='http://127.0.0.1:8766', results=[], errors=[];
  if(!page.url().startsWith(base+'/'))throw new Error('Only isolated preview is allowed');
  const check=(ok,label)=>{if(!ok)throw new Error(label);results.push(label);};
  const onError=error=>errors.push(error.message);page.on('pageerror',onError);
  await page.context().clearCookies();
  await page.goto(base+'/login?next=%2Faccounts');
  await page.getByLabel('登录账号',{exact:true}).fill('preview-admin');
  await page.getByLabel('密码',{exact:true}).fill('Preview-Admin-2026');
  await page.getByRole('button',{name:'登录工作台',exact:true}).click();
  await page.waitForURL(base+'/accounts');
  await page.locator('#account-count').filter({hasText:'个账号'}).waitFor();
  const list=(await (await page.request.get(base+'/api/v1/accounts')).json()).accounts;
  const reader=list.find(a=>a.username==='tunnel-reader');
  await page.getByRole('button',{name:`设置 ${reader.display_name}（tunnel-reader）`,exact:true}).click();
  await page.getByLabel('显示名称',{exact:true}).fill('必须保留的账号草稿');
  await page.getByLabel('设置新密码',{exact:true}).fill('Synthetic-New-Password');
  await page.getByLabel('再次输入新密码',{exact:true}).fill('Synthetic-New-Password');
  const count=await page.locator('#account-count').textContent();
  let writes=0, transform=result=>result;
  const writePath='**/api/v1/accounts/*';
  await page.route(writePath,route=>{
    writes++;const input=route.request().postDataJSON();
    const result={...reader,username:input.username,display_name:input.display_name,role:input.role,enabled:input.enabled,password_configured:true,relogin_required:false};
    return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(transform(result))});
  });
  const variants=[
    ['empty object',()=>({})],['null',()=>null],
    ['wrong ID',result=>({...result,id:'different-account'})],
    ['wrong boolean',result=>({...result,enabled:'false'})],
    ['unknown role',result=>({...result,role:'unknown'})],
    ['missing re-login flag',result=>{delete result.relogin_required;return result;}],
    ['incorrect re-login flag',result=>({...result,relogin_required:true})],
    ['unconfirmed password',result=>({...result,password_configured:false})],
    ['wrong submitted name',result=>({...result,display_name:'not the submitted value'})],
    ['wrong submitted username',result=>({...result,username:'different-user'})]
  ];
  for(const [name,mutate] of variants){
    transform=mutate;
    await page.getByLabel('管理员当前密码',{exact:true}).fill('Preview-Admin-2026');
    await page.getByRole('button',{name:'保存账号设置',exact:true}).click();
    await page.locator('#form-feedback').filter({hasText:'未确认'}).waitFor();
    check(await page.getByLabel('显示名称',{exact:true}).inputValue()==='必须保留的账号草稿',name+': metadata preserved');
    check(await page.getByLabel('设置新密码',{exact:true}).inputValue()==='Synthetic-New-Password',name+': new password preserved in form only');
    check(await page.getByLabel('管理员当前密码',{exact:true}).inputValue()==='',name+': administrator confirmation cleared');
    check(await page.getByRole('button',{name:'保存账号设置',exact:true}).isEnabled(),name+': form unlocked');
    check(!await page.locator('#form-feedback').evaluate(el=>el.classList.contains('success')),name+': no false success');
    check(await page.locator('#account-count').textContent()===count,name+': directory unchanged');
  }
  check(writes===variants.length,'No automatic retry or duplicate writes');
  await page.unroute(writePath);
  await page.getByRole('button',{name:'＋ 新增账号',exact:true}).click();
  await page.getByRole('button',{name:'放弃修改',exact:true}).click();
  await page.getByRole('heading',{name:'新增账号',exact:true}).waitFor();
  await page.getByLabel('登录账号',{exact:true}).fill('synthetic-new-draft');
  await page.getByLabel('显示名称',{exact:true}).fill('新增账号草稿');
  await page.getByLabel('账号状态',{exact:true}).selectOption('false');
  await page.getByLabel('管理员当前密码',{exact:true}).fill('Preview-Admin-2026');
  await page.route('**/api/v1/accounts',route=>route.request().method()==='POST'
    ?route.fulfill({status:201,contentType:'application/json',body:'{}'}):route.continue());
  await page.getByRole('button',{name:'保存账号设置',exact:true}).click();
  await page.locator('#form-feedback').filter({hasText:'未确认'}).waitFor();
  check(await page.getByLabel('登录账号',{exact:true}).inputValue()==='synthetic-new-draft','Malformed create retains new-account draft');
  check(await page.locator('#account-count').textContent()===count,'Malformed create cannot insert a phantom account');
  check((await page.locator('#form-feedback').textContent()).includes('刷新列表核对'),'Unconfirmed save explains reconciliation');
  await page.setViewportSize({width:360,height:900});
  await page.locator('#form-feedback').scrollIntoViewIfNeeded();
  await page.screenshot({path:'output/playwright/account-save-unconfirmed-mobile.png'});
  await page.unroute('**/api/v1/accounts');
  await page.getByRole('button',{name:'取消',exact:true}).click();
  await page.getByRole('button',{name:'放弃修改',exact:true}).click();
  await page.locator('#editor-empty').waitFor();
  await page.getByRole('button',{name:'退出登录',exact:true}).click();
  await page.waitForURL('**/login?reason=logged_out');
  check(errors.length===0,'No uncaught page errors');page.off('pageerror',onError);
  return {status:'passed',assertions:results.length,mockedUpdates:writes,productionWrites:false};
}
