// Exercise partial-success feedback and recovery only in the isolated fixture.
async page => {
  if (!page.url().startsWith('http://127.0.0.1:8766/')) throw new Error('Isolated preview only');
  const checks = [];
  const check = (ok, text) => {if(!ok)throw new Error(text);checks.push(text)};
  const upload = '**/api/v1/data/documents/upload';
  await page.unroute(upload);
  await page.getByRole('button',{name:'文档入库',exact:true}).click();
  await page.locator('#ingest-file').setInputFiles('test_data/knowledge_preprocessing/关键知识切片示例.md');
  // Capture a real successful upload first. Replaying a multipart upload with
  // route.fetch can consume the file stream in browser/CLI versions; do not
  // confuse that test-harness artifact with an application failure.
  const firstResponse = page.waitForResponse(r=>r.url().endsWith('/data/documents/upload')&&r.request().method()==='POST');
  await page.getByRole('button',{name:'上传并切片入库',exact:true}).click();
  const saved = await (await firstResponse).json();
  check(saved.accepted===true,'Real document upload succeeds');
  await page.getByRole('button',{name:'上传并切片入库',exact:true}).waitFor({state:'visible'});
  await page.route(upload, async route => {
    const partial = {...saved, graph:{...saved.graph,status:'failed',retryable:true,message:'文档及知识块已保存，图谱同步暂未完成；可重试图谱同步，无需重复切分。'}};
    await route.fulfill({status:202,json:partial});
  });
  try {
    await page.getByRole('button',{name:'上传并切片入库',exact:true}).click();
    const retry = page.getByRole('button',{name:'重试图谱同步',exact:true});
    await retry.waitFor();
    check((await page.locator('#ingest-graph-status').textContent()).includes('已保存'),'Partial success does not claim document loss');
    check(await page.locator('#ingest-graph-status').getAttribute('class')==='warning','Graph failure is visibly distinguished');
    const pending = page.waitForResponse(r=>r.url().endsWith('/sync-graph')&&r.request().method()==='POST');
    await retry.click();
    const response=await pending;
    check(response.status()===200,'Retry endpoint succeeds');
    const result=await response.json();
    check((result.graph||result).status==='completed','Retry reports completed graph state');
    await retry.waitFor({state:'detached'});
    check((await page.locator('#ingest-graph-status').textContent()).includes('已同步图谱'),'Recovery feedback rendered');
    await page.screenshot({path:'output/playwright/graph-sync-recovered.png'});
    return {passed:checks.length,checks};
  } finally {await page.unroute(upload)}
}
