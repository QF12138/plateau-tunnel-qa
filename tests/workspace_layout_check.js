// Read-only UI regression, run with the Playwright CLI against the isolated preview.
// playwright-cli -s=workspace-layout run-code (Get-Content -Raw tests/workspace_layout_check.js)
async page => {
  const base = 'http://127.0.0.1:8766';
  if (!page.url().startsWith(base + '/')) throw new Error('Only the isolated 8766 preview is allowed.');
  const checks = [], errors = [], unexpectedWrites = [];
  const check = (ok, label) => { if (!ok) throw new Error(label); checks.push(label); };
  const onError = error => errors.push(error.message);
  const onConsole = message => { if (message.type() === 'error') errors.push(message.text()); };
  const guardPattern = '**/*';
  const guard = async route => {
    const request = route.request(), url = request.url(), method = request.method();
    const local = url.startsWith(base + '/');
    const safeMethod = ['GET', 'HEAD', 'OPTIONS'].includes(method);
    const authWrite = method === 'POST' && [base + '/api/v1/auth/login', base + '/api/v1/auth/logout'].includes(url);
    if (!local || (!safeMethod && !authWrite)) {
      unexpectedWrites.push(method + ' ' + url);
      await route.abort();
    } else await route.continue();
  };
  const graphPattern = '**/api/v1/graph/workspace?limit=500';
  const evidencePattern = '**/api/v1/knowledge/evidence/*/ui-*';
  let graphMock = null;
  let evidenceMock = null;
  page.on('pageerror', onError);
  page.on('console', onConsole);
  await page.route(guardPattern, guard);
  const panel = name => page.locator(`[data-view-panel="${name}"]`);
  const trigger = name => page.locator(`[data-view="${name}"]`);
  const top = async () => {
    await page.evaluate(() => window.scrollTo({top:0, behavior:'instant'}));
    await page.waitForFunction(() => window.scrollY <= 1);
  };
  const fits = () => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1);
  const views = [['qa','知识问答'],['graph','知识图谱'],['data','资料治理'],['ingest','文档入库'],['audit','运行审计'],['settings','系统设置']];
  const widths = [360, 390, 768, 1024, 1440];
  try {
    await page.context().clearCookies();
    await page.goto(base + '/login');
    await page.getByLabel('登录账号', {exact:true}).fill('preview-admin');
    await page.getByLabel('密码', {exact:true}).fill('Preview-Admin-2026');
    await page.getByRole('button', {name:'登录工作台', exact:true}).click();
    await page.waitForURL(base + '/');
    await page.locator('#session-username').filter({hasText:'preview-admin'}).waitFor();
    await page.locator('#submit:not([disabled])').waitFor();

    await page.reload();
    await page.locator('#session-username').filter({hasText:'preview-admin'}).waitFor();
    await page.keyboard.press('Tab');
    const skip = page.locator('a[href="#main-content"]');
    check(await skip.count() === 1, 'Workbench provides a main-content skip link');
    check(await skip.evaluate(el => el === document.activeElement), 'Skip link is first in keyboard navigation');
    const skipBox = await skip.boundingBox();
    check(skipBox && skipBox.x >= 0 && skipBox.y >= 0, 'Focused skip link is visibly placed');
    await page.keyboard.press('Enter');
    check(await page.locator('#main-content').evaluate(el => el === document.activeElement), 'Skip link transfers keyboard focus to main content');

    for (const width of widths) {
      await page.setViewportSize({width, height:900});
      for (const [name, heading] of views) {
        await trigger(name).click();
        await panel(name).waitFor({state:'visible'});
        await top();
        check(await panel(name).getByRole('heading', {name:heading, exact:true}).isVisible(), `${name}: clear page heading at ${width}px`);
        check(await page.locator('[data-view][aria-current="page"]').count() === 1 && await trigger(name).getAttribute('aria-current') === 'page', `${name}: exactly one current navigation item at ${width}px`);
        check(await fits(), `${name}: no page horizontal overflow at ${width}px`);
        const nav = await page.locator('.nav-item').evaluateAll(items => items.map(item => {
          const box = item.getBoundingClientRect();
          return {left:box.left, right:box.right, width:box.width, height:box.height};
        }));
        check(nav.length === 5 && nav.every(item => item.width >= 44 && item.height >= 44), `${name}: all five menu touch targets are at least 44px at ${width}px`);
        if (width <= 1179) {
          check(nav.every(item => item.left >= -1 && item.right <= width + 1), `${name}: all navigation labels fit without horizontal scrolling at ${width}px`);
          const gear = await page.locator('.settings-entry').evaluate(el => {
            const box = el.getBoundingClientRect(), header = el.closest('.side').getBoundingClientRect(), main = document.querySelector('main').getBoundingClientRect();
            return {position:getComputedStyle(el).position, contained:box.left >= header.left && box.right <= header.right && box.top >= header.top && box.bottom <= header.bottom, outsideMain:box.bottom <= main.top + 1, width:box.width, height:box.height};
          });
          check(['static','relative'].includes(gear.position) && gear.contained && gear.outsideMain, `${name}: settings gear stays inside header and does not overlay content at ${width}px`);
          check(gear.width >= 44 && gear.height >= 44, `${name}: settings gear has a 44px touch target at ${width}px`);
        }
        if (name === 'qa') {
          for (const id of ['scenario','mode']) {
            check(await page.locator(`label[for="${id}"]`).isVisible(), `${id}: visible select label at ${width}px`);
          }
          if (width < 768) {
            const scenario = await page.locator('#scenario').boundingBox(), mode = await page.locator('#mode').boundingBox(), submit = await page.locator('#submit').boundingBox(), controls = await page.locator('.controls').boundingBox();
            check(Math.abs(scenario.y - mode.y) <= 2 && scenario.x + scenario.width <= mode.x + 1, `QA selectors remain in two columns at ${width}px`);
            check(submit.y >= scenario.y + scenario.height && Math.abs(submit.x - controls.x) <= 1 && Math.abs(submit.width - controls.width) <= 2, `QA submit occupies its own full-width row at ${width}px`);
          }
        }
        if ([390,1440].includes(width)) {
          await page.screenshot({path:`output/playwright/workspace-after-${name}-${width}.png`, animations:'disabled'});
        }
      }
    }

    await trigger('data').click();
    await page.locator('#refresh-governance').click();
    await page.waitForFunction(() => document.querySelector('#gateway-status').textContent === '工作台数据 · 已同步');
    check(!(await page.locator('#gateway-status').textContent()).includes('DeepSeek'), 'Overview does not falsely claim a DeepSeek connection for a local model');

    // Long settings pages should not make the desktop navigation disappear.
    await page.setViewportSize({width:1440, height:900});
    await trigger('settings').click();
    await page.locator('[data-view-panel="settings"] .settings-card').last().scrollIntoViewIfNeeded();
    check(await page.evaluate(() => scrollY > 250), 'Settings provides a genuine long-scroll case');
    const side = await page.locator('.side').evaluate(el => ({position:getComputedStyle(el).position, top:el.getBoundingClientRect().top, height:el.getBoundingClientRect().height}));
    check(side.position === 'sticky' && Math.abs(side.top) <= 1 && side.height <= 901, 'Desktop sidebar remains sticky after long content scroll');
    check(await trigger('qa').isVisible() && (await trigger('qa').boundingBox()).y >= 0, 'Desktop navigation remains on screen after long scroll');
    await page.screenshot({path:'output/playwright/workspace-after-settings-desktop-scroll.png', animations:'disabled'});

    // Exercise client-only form controls; do not submit questions, files or settings.
    await trigger('qa').click();
    await page.locator('#scenario').selectOption('risk_analysis');
    await page.locator('#mode').selectOption('brief');
    await page.locator('#block-scope-all').uncheck();
    check(await page.locator('#submit').isDisabled(), 'Empty knowledge selection disables query');
    await page.locator('input[name="block-source-type"][value="research"]').check();
    check(await page.locator('#submit').isEnabled(), 'One knowledge category restores query control');
    await page.locator('#report-enabled').check();
    check(await page.locator('#report-format').isEnabled(), 'Report checkbox enables format selection');
    await trigger('ingest').click();
    await page.locator('#ingest-type').selectOption('engineering_case');
    check(await page.locator('#ingest-project-field').isVisible(), 'Engineering-case documents reveal project field');
    await page.locator('#ingest-type').selectOption('standard');
    check(await page.locator('#ingest-project-field').isHidden(), 'Other document types hide project field');
    await trigger('qa').click();
    check(await page.locator('#scenario').inputValue() === 'risk_analysis' && await page.locator('#mode').inputValue() === 'brief', 'QA selections survive view navigation');

    // A synthetic read-only graph makes canvas and editor smoke checks independent of stored documents.
    const nodes = [{id:'ui-rock',name:'岩爆',type:'地质风险',domain:'environment',aliases:[],properties:{},confidence:1,security_level:'internal'}, {id:'ui-monitor',name:'微震监测',type:'监测方法',domain:'method',aliases:[],properties:{},confidence:1,security_level:'internal'}];
    graphMock = route => route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({nodes,edges:[{id:'ui-edge',source:'ui-monitor',target:'ui-rock',relation_type:'监测',confidence:1,evidence_id:'ui-only',applicable_conditions:{}}],knowledge_version:1})});
    await page.route(graphPattern, graphMock);
    evidenceMock = route => route.fulfill({status:200,contentType:'application/json',body:'[]'});
    await page.route(evidencePattern, evidenceMock);
    await page.setViewportSize({width:390, height:900});
    await trigger('graph').click();
    await page.locator('#graph-refresh').click();
    await page.locator('#knowledge-map').waitFor();
    check(await page.locator('.graph-node').count() === 2, 'Read-only graph renders both related entities');
    check((await page.locator('.graph-edge-label').allTextContents()).some(label => label.includes('监测')), 'Relationship remains readable on graph connection');
    check(await fits(), 'Rendered graph stays within mobile page');
    const toolbar = await page.locator('.graph-toolbar').boundingBox(), legend = await page.locator('.graph-layer-legend').boundingBox();
    check(toolbar.y + toolbar.height <= legend.y + 1 || legend.y + legend.height <= toolbar.y + 1 || toolbar.x + toolbar.width <= legend.x + 1 || legend.x + legend.width <= toolbar.x + 1, 'Graph toolbar and layer legend do not overlap');
    check((await page.locator('.graph-tool').evaluateAll(items => items.map(item => item.getBoundingClientRect()))).every(box => box.width >= 44 && box.height >= 44), 'Graph toolbar controls have 44px touch targets');
    await page.getByRole('button', {name:'放大',exact:true}).click();
    await page.getByRole('button', {name:'复位视图',exact:true}).click();
    check(await page.locator('#knowledge-map').isVisible(), 'Graph zoom and reset controls remain usable');
    await page.waitForFunction(() => !document.querySelector('.graph-stage').classList.contains('physics-active'));
    await page.getByRole('button', {name:'复位视图',exact:true}).click();
    const renderedFonts = await page.locator('.node-label, .graph-edge-label').evaluateAll(items => items.map(item => {
      const matrix=item.getScreenCTM();return parseFloat(getComputedStyle(item).fontSize)*Math.hypot(matrix.a,matrix.b);
    }));
    check(renderedFonts.every(size => size >= 14), 'Small mobile graph labels render at least 14 screen pixels, not 5px');
    const nodesFit = () => page.locator('#knowledge-map').evaluate(svg => {
      const canvas=svg.getBoundingClientRect();return [...svg.querySelectorAll('.graph-node')].every(node => {
        const box=node.getBoundingClientRect();return box.left>=canvas.left && box.right<=canvas.right && box.top>=canvas.top && box.bottom<=canvas.bottom;
      });
    });
    check(await nodesFit(), 'Auto-fit includes every visible entity and its label');
    const cameraScale = () => page.locator('#graph-viewport').evaluate(el => el.transform.baseVal.consolidate().matrix.a);
    const initialScale = await cameraScale();
    await page.getByRole('button', {name:'放大',exact:true}).click();
    check(await cameraScale() > initialScale, 'Zoom increases graph camera scale');
    await page.getByRole('button', {name:'复位视图',exact:true}).click();
    check(Math.abs(await cameraScale()-initialScale)<.01, 'Reset fits the graph instead of returning to tiny world scale');
    await page.setViewportSize({width:1440,height:900});
    await page.waitForFunction(() => {const svg=document.querySelector('#knowledge-map');return Math.abs(svg.viewBox.baseVal.width-svg.getBoundingClientRect().width)<1;});
    check(await nodesFit(), 'Graph refits after switching to desktop size');
    await page.setViewportSize({width:390,height:900});
    await page.waitForFunction(() => {const svg=document.querySelector('#knowledge-map');return Math.abs(svg.viewBox.baseVal.width-svg.getBoundingClientRect().width)<1;});
    check(await nodesFit(), 'Graph refits after switching back to mobile size');
    await page.locator('#knowledge-map').scrollIntoViewIfNeeded();
    const nodeCenter = id => page.locator(`.graph-node[data-node-id="${id}"] .node-core`).evaluate(el => {
      const box=el.getBoundingClientRect();return {x:box.x+box.width/2,y:box.y+box.height/2};
    });
    const dragStart=await nodeCenter('ui-rock'), neighborStart=await nodeCenter('ui-monitor');
    await page.mouse.move(dragStart.x,dragStart.y);await page.mouse.down();
    await page.mouse.move(dragStart.x+45,dragStart.y+18,{steps:8});
    await page.waitForFunction(({id,x})=>{const rect=document.querySelector(`.graph-node[data-node-id="${id}"] .node-core`).getBoundingClientRect();return rect.x+rect.width/2>x+30;},{id:'ui-rock',x:dragStart.x});
    check((await nodeCenter('ui-rock')).x > dragStart.x+30, 'Dragging follows the pointer at the fitted camera scale');
    await page.waitForFunction(({id,x,y})=>{const rect=document.querySelector(`.graph-node[data-node-id="${id}"] .node-core`).getBoundingClientRect();return Math.hypot(rect.x+rect.width/2-x,rect.y+rect.height/2-y)>1;},{id:'ui-monitor',...neighborStart});
    check(Math.hypot((await nodeCenter('ui-monitor')).x-neighborStart.x,(await nodeCenter('ui-monitor')).y-neighborStart.y)>1, 'Connected entity is still smoothly pulled by the spring');
    await page.mouse.up();
    await page.waitForFunction(() => !document.querySelector('.graph-stage').classList.contains('physics-active'));
    await page.getByRole('button', {name:'复位视图',exact:true}).click();
    await page.locator('#knowledge-map').scrollIntoViewIfNeeded();
    const canvas=await page.locator('#knowledge-map').boundingBox(), beforePan=await page.locator('#graph-viewport').getAttribute('transform');
    await page.mouse.move(canvas.x+10,canvas.y+10);await page.mouse.down();await page.mouse.move(canvas.x+35,canvas.y+30,{steps:4});await page.mouse.up();
    check(await page.locator('#graph-viewport').getAttribute('transform')!==beforePan, 'Canvas panning still works after responsive fitting');
    await page.getByRole('button', {name:'复位视图',exact:true}).click();
    await page.locator('.graph-node[data-node-id="ui-monitor"]').press('Enter');
    check(await page.locator('#graph-inspector h4').textContent() === '微震监测', 'Graph entity is selectable by keyboard');
    await page.locator('#graph-add-entity').click();
    check(await page.locator('#graph-entity-form').isVisible(), 'Graph entity editor opens without saving');
    await page.locator('#graph-entity-name').fill('仅界面校验，不保存');
    check(await fits(), 'Graph entity editor fits mobile page');
    await page.getByRole('button', {name:'关闭实体编辑',exact:true}).click();
    check(await page.locator('#graph-entity-form').isHidden(), 'Graph entity draft can be closed without writes');
    await page.locator('#knowledge-map').scrollIntoViewIfNeeded();
    await page.screenshot({path:'output/playwright/workspace-after-graph-populated-mobile.png', animations:'disabled'});

    await page.emulateMedia({reducedMotion:'reduce'});
    await trigger('qa').click();
    const decoration = await page.locator('.brand-sigil, .arch > span, .dot, .view:not([hidden])').evaluateAll(items => items.map(item => ({name:getComputedStyle(item).animationName, after:getComputedStyle(item,'::after').animationName, transition:getComputedStyle(item).transitionDuration})));
    check(decoration.length > 0 && decoration.every(item => item.name === 'none' && item.after === 'none'), 'Reduced-motion preference disables decorative animations');
    check(unexpectedWrites.length === 0, 'Only isolated login/logout writes occurred');
    check(errors.length === 0, 'No browser JavaScript or console errors: ' + errors.join('; '));
    return {status:'passed',checks:checks.length,widths,views:views.map(item=>item[0]),unexpectedWrites,productionAccess:false};
  } finally {
    await page.mouse.up();
    if (graphMock) await page.unroute(graphPattern, graphMock);
    if (evidenceMock) await page.unroute(evidencePattern, evidenceMock);
    await page.emulateMedia({reducedMotion:'no-preference'});
    await page.context().clearCookies();
    await page.goto(base + '/login?reason=logged_out');
    await page.unroute(guardPattern, guard);
    page.off('pageerror', onError);
    page.off('console', onConsole);
  }
}
