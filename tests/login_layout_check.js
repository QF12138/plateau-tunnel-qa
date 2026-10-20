// Read-only layout regression, run via playwright-cli against the isolated preview.
async page => {
  const base = 'http://127.0.0.1:8766';
  let checks = 0;
  const assert = (ok, note) => { if (!ok) throw new Error(note); checks++; };
  const errors = [];
  const onError = error => errors.push(error.message);
  page.on('pageerror', onError);
  await page.context().clearCookies();
  await page.goto(base + '/login');
  const dimensions = [[320,740],[360,800],[768,900],[960,900],[961,800],[1024,768],[1440,900]];
  for (const [width,height] of dimensions) {
    await page.setViewportSize({width,height});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Horizontal overflow '+width);
    const inputs = await page.locator('#username, #password').evaluateAll(elements => elements.map(el => ({
      font:parseFloat(getComputedStyle(el).fontSize), height:el.getBoundingClientRect().height
    })));
    assert(inputs.every(input => input.font >= 16 && input.height >= 44), 'Readable/tappable inputs '+width);
    assert((await page.locator('#toggle-password').boundingBox()).height >= 44, 'Password touch target '+width);
    assert(await page.locator('.tunnel-section').isVisible() === (width > 960), 'Decoration breakpoint '+width);
    if (width===768 || width===1024) await page.screenshot({path:'output/playwright/login-polish-'+width+'.png',fullPage:true});
  }
  // Approximate a 1440px-wide desktop at 200% zoom using its 720 CSS-pixel viewport.
  await page.setViewportSize({width:720,height:450});
  await page.locator('.login-help summary').click();
  await page.locator('.login-help p').scrollIntoViewIfNeeded();
  assert(await page.locator('.login-help p').isVisible(), 'Help reachable with enlarged/short viewport');
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Zoom-equivalent overflow');
  await page.setViewportSize({width:320,height:568});
  await page.goto(base+'/login?reason=expired&next=%2Faccounts');
  assert(await page.locator('#session-feedback').isVisible(), 'Session notice at narrow width');
  assert((await page.locator('#session-feedback').innerText()).includes('管理员'), 'Admin return explanation');
  const loginPath='**/api/v1/auth/login';
  await page.route(loginPath, route => route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({message:'服务暂不可用，请稍后重试。当前输入内容已保留；如果长时间无法登录，请联系管理员检查服务连接。'})}));
  await page.getByLabel('登录账号',{exact:true}).fill('layout-test');
  await page.getByLabel('密码',{exact:true}).fill('Synthetic-Layout-Only');
  await page.getByRole('button',{name:'登录工作台',exact:true}).click();
  await page.locator('#form-feedback').filter({hasText:'服务暂不可用'}).waitFor();
  const feedback=await page.locator('#form-feedback').boundingBox();
  const button=await page.locator('#login-submit').boundingBox();
  assert(feedback.y+feedback.height<=button.y, 'Long error does not overlap submit');
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Long error no overflow');
  await page.screenshot({path:'output/playwright/login-polish-error-mobile.png',fullPage:true});
  await page.unroute(loginPath);
  await page.goto(base+'/login');
  await page.setViewportSize({width:1440,height:900});
  await page.screenshot({path:'output/playwright/login-polish-final-desktop.png'});
  await page.setViewportSize({width:360,height:800});
  await page.screenshot({path:'output/playwright/login-polish-final-mobile.png'});
  assert(errors.length===0,'No page JavaScript errors: '+errors.join('; '));
  page.off('pageerror',onError);
  return {status:'passed',checks,dimensions,zoomEquivalent:'720×450 CSS pixels',productionMutations:false};
}
