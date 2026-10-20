// Run through playwright-cli against the isolated auth_ui_preview app only.
async page => {
  const base = 'http://127.0.0.1:8766';
  if (!page.url().startsWith(base + '/')) throw new Error('Isolated preview only.');
  const checks = [], errors = [];
  const check = (ok, label) => { if (!ok) throw new Error(label); checks.push(label); };
  const errorListener = error => errors.push(error.message);
  page.on('pageerror', errorListener);
  try {
    await page.reload();
    await page.locator('#session-username').filter({hasText:'preview-admin'}).waitFor();
    await page.getByRole('button', {name:'文档入库', exact:true}).click();
    await page.locator('#ingest-file').setInputFiles('test_data/knowledge_preprocessing/关键知识切片示例.md');
    await page.locator('#ingest-chunk-model').selectOption('key-v2');
    check(await page.locator('#ingest-chunk-model').inputValue() === 'key-v2', 'Key-content strategy is available');
    const libraryBefore = await page.locator('#ingest-library-state').textContent();
    const preview = async () => {
      const pending = page.waitForResponse(r => r.url().endsWith('/data/documents/preview') && r.request().method() === 'POST');
      await page.getByRole('button', {name:'预览知识块', exact:true}).click();
      const response = await pending;
      check(response.status() === 200, 'Preview endpoint succeeds');
      await page.locator('#ingest-preview-panel').waitFor({state:'visible'});
      return response.json();
    };
    const first = await preview();
    check(first.accepted && first.chunks.length > 0, 'Preview has source-preserving blocks');
    check(first.processing.excluded_by_reason.contents > 0 && first.processing.excluded_by_reason.references > 0, 'Exclusion reasons visible');
    check(first.chunks.some(c => c.text.includes('不得直接用于现场设计')), 'Safety limitation retained');
    check(first.chunks.some(c => c.text.includes('2 m²')), 'Units retained');
    check(!first.chunks.some(c => c.text.includes('虚构条目')), 'Bibliography not in retrieval blocks');
    check(await page.locator('#ingest-library-state').textContent() === libraryBefore, 'Preview does not change library count');
    for (const width of [360, 768, 1440]) {
      await page.setViewportSize({width, height:960});
      await page.locator('#ingest-preview-panel').scrollIntoViewIfNeeded();
      check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'No horizontal overflow at ' + width);
      const item = page.locator('.knowledge-preview-item').nth(2);
      await item.locator('summary').click();
      check(await item.locator('pre').isVisible(), 'Block content expandable at ' + width);
      const pre = await item.locator('pre').boundingBox();
      check(pre && pre.width <= width && pre.height <= 440, 'Long block stays bounded at ' + width);
      await item.locator('summary').click();
    }
    await page.locator('#ingest-preview-close').focus();
    await page.keyboard.press('Enter');
    check(await page.locator('#ingest-preview-panel').isHidden(), 'Preview closes with keyboard');
    check(await page.locator('#ingest-preview').evaluate(el => el === document.activeElement), 'Focus returns to preview button');
    await page.locator('#ingest-chunk-model').selectOption('structural-v1');
    const full = await preview();
    check(full.chunks.some(c => c.text.includes('虚构条目')), 'Full-content strategy retains bibliography');
    await page.locator('#ingest-chunk-model').selectOption('key-v2');
    check(await page.locator('#ingest-preview-panel').isHidden(), 'Changing strategy invalidates stale preview');
    await page.locator('#ingest-chunk-overlap').fill('800');
    await page.getByRole('button', {name:'预览知识块', exact:true}).click();
    check((await page.locator('#ingest-result').textContent()).includes('重叠上限必须小于'), 'Invalid overlap has actionable feedback');
    await page.locator('#ingest-chunk-overlap').fill('120');
    await preview();
    await page.locator('#ingest-preview-panel').scrollIntoViewIfNeeded();
    await page.screenshot({path:'output/playwright/knowledge-preview-desktop.png'});
    await page.setViewportSize({width:390,height:844});
    await page.locator('#ingest-preview-panel').scrollIntoViewIfNeeded();
    await page.screenshot({path:'output/playwright/knowledge-preview-mobile.png'});
    await page.setViewportSize({width:1440,height:960});
    const upload = page.waitForResponse(r => r.url().endsWith('/data/documents/upload') && r.request().method() === 'POST');
    await page.getByRole('button', {name:'上传并切片入库', exact:true}).click();
    const saved = await (await upload).json();
    check(saved.accepted, 'Upload succeeds in isolated database');
    await page.locator('#ingest-result.success').waitFor();
    check(saved.processing.chunk_count === first.processing.chunk_count, 'Preview and persisted chunk count agree');
    check(await page.locator('#ingest-preview-panel').isHidden(), 'Saved document no longer says not-ingested');
    check(await page.locator('#ingest-processing-summary details').count() > 0, 'Saved processing report is inspectable');
    await page.getByRole('button', {name:'资料治理', exact:true}).click();
    await page.locator('.inspect-chunks').first().click();
    await page.locator('.knowledge-full-text').first().locator('summary').click();
    check(await page.locator('.knowledge-full-text pre').first().isVisible(), 'Governance shows full block body');
    check((await page.locator('.chunk-list').textContent()).includes('排除'), 'Governance includes processing report');
    check(errors.length === 0, 'No browser runtime errors');
    return {passed:checks.length, checks};
  } finally { page.off('pageerror', errorListener); }
}
