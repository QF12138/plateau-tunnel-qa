// Run through the named Playwright CLI session against the isolated preview only:
// playwright-cli -s=workbench-session run-code (Get-Content -Raw tests/workbench_session_ui_check.js)
async page => {
  const base = 'http://127.0.0.1:8766';
  if (!page.url().startsWith(base + '/')) throw new Error('This check only targets the isolated 8766 preview.');
  const results = [];
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const check = (condition, label) => { if (!condition) throw new Error(label); results.push(label); };
  async function login(target = page) {
    await target.goto(base + '/login');
    if (target.url().startsWith(base + '/login')) {
      await target.getByLabel('登录账号', {exact:true}).fill('preview-admin');
      await target.getByLabel('密码', {exact:true}).fill('Preview-Admin-2026');
      await target.getByRole('button', {name:'登录工作台', exact:true}).click();
      await target.waitForURL(base + '/');
    }
    await target.locator('#session-username').filter({hasText:'preview-admin'}).waitFor();
    await target.getByRole('button', {name:'查询已发布知识', exact:true}).waitFor();
  }
  await login();
  for (const width of [1440, 768, 390, 360]) {
    await page.setViewportSize({width, height:width === 1440 ? 1000 : 844});
    check(await page.locator('#session-username').isVisible(), `Current account visible at ${width}px`);
    check(await page.locator('#session-role').textContent() === '管理员', `Role displayed separately at ${width}px`);
    const fits = await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1);
    check(fits, `No horizontal page overflow at ${width}px`);
    if ([1440, 390].includes(width)) await page.screenshot({path:`output/playwright/workbench-session-after-${width}.png`, animations:'disabled'});
  }
  await page.context().clearCookies();
  await page.getByRole('textbox', {name:'输入科研或施工问题'}).fill('会话过期检查，不生成问答');
  await page.getByRole('button', {name:'查询已发布知识', exact:true}).click();
  await page.waitForURL(/\/login\?reason=expired/);
  check(true, 'Ask 401 returns to login with expired reason');
  await login();
  const askPath = '**/api/v1/qa/ask';
  await page.route(askPath, route => route.fulfill({status:200, contentType:'application/json', body:JSON.stringify({
    message_id:'ui-only-no-database-write', answer:'界面测试回答', citations:[], warnings:[],
    confidence_level:'低', model_version:'UI test', knowledge_version:1,
  })}));
  await page.route('**/api/v1/qa/reports/*', async route => {
    await page.context().clearCookies();
    await route.fulfill({status:401, contentType:'application/json', body:JSON.stringify({error_code:'PUBLIC_AUTH_REQUIRED', message:'登录已过期'})});
  });
  await page.getByRole('textbox', {name:'输入科研或施工问题'}).fill('报告会话检查');
  await page.getByRole('checkbox', {name:'查询成功后生成问答报告'}).check();
  await page.getByRole('button', {name:'查询已发布知识', exact:true}).click();
  await page.waitForURL(/\/login\?reason=expired/);
  check(true, 'Report 401 returns to login with expired reason');
  await page.unroute(askPath);
  await page.unroute('**/api/v1/qa/reports/*');
  await login();
  // A plain permission rejection is not an expired or changed session.
  await page.route(askPath, route => route.fulfill({status:403, contentType:'application/json', body:JSON.stringify({error_code:'FORBIDDEN', message:'权限不足测试'})}));
  await page.getByRole('textbox', {name:'输入科研或施工问题'}).fill('权限错误分类检查');
  await page.getByRole('button', {name:'查询已发布知识', exact:true}).click();
  await page.locator('#result').filter({hasText:'权限不足测试'}).waitFor();
  check(await page.locator('#session-warning').isHidden(), 'Ordinary 403 does not invalidate the current session');
  await page.unroute(askPath);
  const other = await page.context().newPage();
  await other.goto(base + '/');
  await other.locator('#session-username').filter({hasText:'preview-admin'}).waitFor();
  await other.getByRole('button', {name:'退出登录', exact:true}).click();
  await other.waitForURL(/\/login\?reason=logged_out/);
  await login(other);
  await other.close();
  let askRequests = 0;
  const countRequests = request => { if (request.url().endsWith('/api/v1/qa/ask')) askRequests++; };
  page.on('request', countRequests);
  await page.getByRole('textbox', {name:'输入科研或施工问题'}).fill('跨标签会话校验，不生成问答');
  await page.getByRole('button', {name:'查询已发布知识', exact:true}).click();
  await page.locator('#session-warning').waitFor();
  check(await page.getByRole('button', {name:'查询已发布知识', exact:true}).isDisabled(), 'Changed identity disables new submissions');
  check(await page.locator('#session-role').textContent() === '身份待确认', 'Changed identity has explicit account state');
  check(askRequests === 1, 'Rejected write is not automatically replayed');
  await page.getByRole('button', {name:'刷新身份', exact:true}).focus();
  check(await page.locator('#session-refresh').evaluate(element => element === document.activeElement), 'Recovery action is keyboard focusable');
  await page.screenshot({path:'output/playwright/workbench-session-changed-mobile.png', animations:'disabled'});
  await page.getByRole('button', {name:'刷新身份', exact:true}).press('Enter');
  await page.locator('#session-username').filter({hasText:'preview-admin'}).waitFor();
  check(await page.locator('#session-warning').isHidden(), 'Explicit refresh restores the verified session');
  check(askRequests === 1, 'Refresh does not replay the rejected write');
  page.off('request', countRequests);
  // Exercise the browser back-forward cache restoration hook without mutating data.
  const restored = page.waitForEvent('framenavigated', frame => frame === page.mainFrame());
  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true})));
  await restored;
  await page.waitForLoadState('domcontentloaded');
  await page.locator('#session-username').filter({hasText:'preview-admin'}).waitFor();
  check(await page.locator('#session-role').textContent() === '管理员', 'Persisted pageshow revalidates through reload');
  await page.context().clearCookies();
  await page.getByRole('button', {name:'退出登录', exact:true}).click();
  await page.waitForURL(/\/login\?reason=expired/);
  check(true, 'Expired logout uses the same 401 login recovery');
  await login();
  // Verify the ordinary-user presentation without enabling/changing any real account.
  await page.route('**/api/v1/auth/me', route => route.fulfill({status:200, contentType:'application/json', body:JSON.stringify({
    user_id:'preview-reader', username:'preview-reader', role:'field_engineer', can_manage:false, csrf_token:null,
  })}));
  await page.reload();
  await page.locator('#session-username').filter({hasText:'preview-reader'}).waitFor();
  check(await page.locator('#session-role').textContent() === '普通用户', 'Ordinary role remains visibly distinct');
  check(await page.locator('#accounts-entry').isHidden() && await page.locator('.settings-entry').isHidden(), 'Ordinary-user presentation hides administrative controls');
  check(!(await page.locator('#gateway-status').textContent()).includes('preview-reader'), 'Model status no longer doubles as account identity');
  await page.unroute('**/api/v1/auth/me');
  await page.reload();
  await page.locator('#session-username').filter({hasText:'preview-admin'}).waitFor();
  check(errors.length === 0, `No unhandled browser errors (${errors.join('; ')})`);
  return {passed:results.length, results};
}
