// Read-only account layout regression; synthetic login and GET-only fixture overrides.
// Run with playwright-cli run-code against the isolated 8766 preview, never production.
async page => {
  const base='http://127.0.0.1:8766', results=[], errors=[], dimensions=[360,768,1440];
  if(!page.url().startsWith(base+'/'))throw new Error('Only the isolated 8766 preview is allowed');
  const check=(ok,note)=>{if(!ok)throw new Error(note);results.push(note);};
  const longName='国铁川藏科创中心高原铁路隧道施工技术研究与科研成果转化联合工作组现场工程师';
  let longContent=false, writes=0;
  const onError=error=>errors.push(error.message);
  const closeEditor=async()=>{
    if(await page.locator('#account-form').isVisible()){
      await page.locator('#cancel-edit').click();
      if(await page.locator('#discard-dialog').isVisible())await page.locator('#discard-changes').click();
      await page.locator('#account-form').waitFor({state:'hidden'});
    }
  };
  const accountRoute=async route=>{
    if(route.request().method()!=='GET'){
      writes++;
      await route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({message:'This layout test never writes account data'})});
      return;
    }
    const response=await route.fetch(), data=await response.json();
    data.accounts=data.accounts.filter(account=>['preview-admin','tunnel-reader'].includes(account.username));
    const reader=data.accounts.find(account=>account.username==='tunnel-reader');
    if(!reader)throw new Error('Synthetic tunnel-reader fixture is required');
    reader.display_name=longContent?longName:'普通用户';
    await route.fulfill({response,json:data});
  };
  page.on('pageerror',onError);
  await page.route('**/api/v1/accounts**',accountRoute);
  try{
    await page.context().clearCookies();
    await page.goto(base+'/login?next=%2Faccounts');
    await page.getByLabel('登录账号',{exact:true}).fill('preview-admin');
    await page.getByLabel('密码',{exact:true}).fill('Preview-Admin-2026');
    await page.getByRole('button',{name:'登录工作台',exact:true}).click();
    await page.waitForURL(base+'/accounts');
    await page.locator('#account-count').filter({hasText:'显示 2 / 2 个账号'}).waitFor();
    const contrastMetrics=await page.locator('#account-search, #filter-role, #filter-status').evaluateAll(elements=>{
      const lum=color=>{const channels=color.match(/[\d.]+/g).slice(0,3).map(Number).map(value=>{value/=255;return value<=.04045?value/12.92:((value+.055)/1.055)**2.4;});return channels[0]*.2126+channels[1]*.7152+channels[2]*.0722;};
      const contrast=(a,b)=>{const aa=lum(a),bb=lum(b);return (Math.max(aa,bb)+.05)/(Math.min(aa,bb)+.05);};
      return elements.map(element=>{const css=getComputedStyle(element);return {id:element.id,inside:contrast(css.borderTopColor,css.backgroundColor),cardTop:contrast(css.borderTopColor,'rgb(19,42,61)')};});
    });
    for(const metric of contrastMetrics){
      check(metric.inside>=3,'Control boundary contrasts with fill: '+metric.id);
      check(metric.cardTop>=3,'Control boundary contrasts with card top: '+metric.id);
    }
    for(const width of dimensions){
      await page.setViewportSize({width,height:900});
      for(const useLongName of [false,true]){
        longContent=useLongName;
        await page.goto(base+'/accounts');
        await page.locator('#account-count').filter({hasText:'显示 2 / 2 个账号'}).waitFor();
        const label='设置 '+(useLongName?longName:'普通用户')+'（tunnel-reader）';
        await page.getByRole('button',{name:label,exact:true}).click();
        const kind=useLongName?'long':'normal';
        check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'No overflow '+kind+' '+width);
        check(await page.locator('#editor-title').innerText()===(useLongName?longName:'普通用户'),'Full selected name '+kind+' '+width);
        check(await page.locator('#account-list .account-row[aria-pressed="true"]').count()===1,'One selected account '+kind+' '+width);
        const fields=await page.locator('#account-form input,#account-form select').evaluateAll(elements=>elements.map(element=>({font:parseFloat(getComputedStyle(element).fontSize),height:element.getBoundingClientRect().height})));
        check(fields.every(field=>field.font>=16&&field.height>=44),'Readable tappable form fields '+kind+' '+width);
        check(await page.locator('#editor-title').evaluate(element=>document.activeElement===element),'Selection focuses editor heading '+kind+' '+width);
        await page.keyboard.press('Tab');
        check(await page.locator('#account-username').evaluate(element=>document.activeElement===element&&parseFloat(getComputedStyle(element).outlineWidth)>=2),'Keyboard focus visible '+kind+' '+width);
        const targets=await page.locator('#refresh-accounts,.directory-note summary,#cancel-edit,#account-save').evaluateAll(elements=>elements.map(element=>element.getBoundingClientRect().height));
        check(targets.every(height=>height>=44),'Secondary and primary controls >=44px '+kind+' '+width);
        const back=page.getByRole('link',{name:'↑ 返回账号目录（保留当前输入）',exact:true});
        check(await back.isVisible()===(width<=900),'Directory link breakpoint '+kind+' '+width);
        if(width<=900){
          const draft='保留输入 · '+kind+' · '+width;
          await page.getByLabel('显示名称',{exact:true}).fill(draft);
          await page.getByLabel('设置新密码',{exact:true}).fill('Layout-Only-Secret');
          await page.getByLabel('再次输入新密码',{exact:true}).fill('Layout-Only-Secret');
          check((await back.boundingBox()).height>=44,'Directory return touch target '+kind+' '+width);
          await back.focus();await page.keyboard.press('Enter');
          await page.waitForFunction(()=>document.activeElement?.id==='account-directory');
          check(await page.locator('#account-directory').evaluate(element=>element.getBoundingClientRect().top>=0&&element.getBoundingClientRect().top<100),'Return reveals directory '+kind+' '+width);
          check(await page.locator('#account-directory').evaluate(element=>parseFloat(getComputedStyle(element).outlineWidth)>=2),'Directory focus visible '+kind+' '+width);
          check(await page.getByLabel('显示名称',{exact:true}).inputValue()===draft,'Return preserves unsaved name '+kind+' '+width);
          check(await page.getByLabel('设置新密码',{exact:true}).inputValue()==='Layout-Only-Secret','Return preserves in-memory password '+kind+' '+width);
          check(await page.locator('#account-list .account-row[aria-pressed="true"]').getAttribute('aria-label')===label,'Return keeps selected account '+kind+' '+width);
          check(!await page.getByRole('dialog').isVisible(),'Return does not discard or prompt '+kind+' '+width);
          await page.keyboard.press('Tab');
          check(await page.locator('#refresh-accounts').evaluate(element=>document.activeElement===element),'Keyboard continues within directory '+kind+' '+width);
          await page.locator('#account-editor').scrollIntoViewIfNeeded();
        }
        if(useLongName)await page.screenshot({path:'output/playwright/accounts-layout-long-'+width+'.png',fullPage:true});
        await closeEditor();
      }
    }
    check(writes===0,'No account mutation attempted');
    check(errors.length===0,'No unhandled page errors');
    return {status:'passed',assertions:results.length,dimensions,contrastMetrics,accountWrites:writes,checks:results};
  }finally{
    await closeEditor();
    await page.unroute('**/api/v1/accounts**',accountRoute);
    await page.context().clearCookies();
    await page.goto(base+'/login?reason=logged_out');
    page.off('pageerror',onError);
  }
}
