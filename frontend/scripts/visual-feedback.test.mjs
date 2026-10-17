/** Run against a Vite dev server; set WDRUNGE_PLAYWRIGHT_MODULE for an existing installation. */
import { createServer } from 'node:http';
import { createRequire } from 'node:module';
import assert from 'node:assert/strict';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.WDRUNGE_PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.WDRUNGE_TEST_UI || 'http://127.0.0.1:1420';
const server = createServer((req, res) => {
  res.setHeader('Content-Type', 'text/html; charset=utf-8');
  res.end(`<!doctype html><html><head><style>body{margin:20px}#card{width:440px;padding:20px;border:1px solid #ccc}#title{width:240px;height:32px;margin:0 0 20px}#save{display:block;width:100px;height:36px}input{margin-top:30px}</style></head><body><section id="card" data-wdrunge-source="src/Card.tsx:12"><h2 id="title">${req.url === '/next' ? '下一页标题' : '个人博客'}</h2><button id="save" onclick="window.clicked=true">保存文章</button><input type="password" value="private-secret"><div contenteditable="true">private-editable</div></section><a id="next-link" href="/next">下一页</a>${req.url === '/plain' ? '' : `<script src="${base}/visual-inspector.js" defer></script>`}</body></html>`);
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const target = `http://127.0.0.1:${server.address().port}`;
let browser;
try {
  browser = await chromium.launch({ headless: true, ...(process.env.WDRUNGE_TEST_BROWSER ? { channel: process.env.WDRUNGE_TEST_BROWSER } : {}) });
  const page = await browser.newPage({ viewport: { width: 1200, height: 900 } });
  await page.goto(`${base}/dev/visual-feedback.html`);
  async function waitFor(fn) { await page.waitForFunction(fn); }
  async function open(path = '/') {
    await page.getByLabel('预览网站地址').fill(target + path);
    await page.getByRole('button', { name: '打开', exact: true }).click();
  }
  await page.getByLabel('预览网站地址').fill('https://example.com');
  await page.getByRole('button', { name: '打开', exact: true }).click();
  await page.getByRole('alert').waitFor();
  assert.equal(await page.locator('iframe').count(), 0);
  await open();
  await page.getByText('已连接，可点选元素', { exact: true }).waitFor();
  const frame = page.frameLocator('iframe');
  await page.getByRole('button', { name: '选择修改位置', exact: true }).click();
  await frame.locator('#save').click();
  await page.getByText('已选中 button', { exact: true }).waitFor();
  assert.equal(await frame.locator('body').evaluate(() => Boolean(window.clicked)), false, 'inspection must not trigger button action');
  await page.getByLabel('网页修改要求').fill('点击后保存文章到数据库，同时把按钮文字加粗');
  await page.getByRole('button', { name: '发送修改要求', exact: true }).click();
  await waitFor(() => window.feedbackRequests.length === 1);
  const message = await page.evaluate(() => window.feedbackRequests[0]);
  assert.match(message, /保存文章到数据库/);
  assert.match(message, /src\/Card.tsx:12/);
  assert.match(message, /#save/);
  assert.doesNotMatch(message, /private-secret|private-editable/);
  await page.getByRole('button', { name: '选择父级区域' }).click();
  await page.getByText('已选中 section', { exact: true }).waitFor();
  await page.getByLabel('网页修改要求').fill('这个区域背景改成浅蓝色');
  await page.getByRole('button', { name: '发送修改要求', exact: true }).click();
  await waitFor(() => window.feedbackRequests.length === 2);
  assert.doesNotMatch(await page.evaluate(() => window.feedbackRequests[1]), /private-secret|private-editable/);
  await page.getByRole('button', { name: '清空选择', exact: true }).click();
  await page.getByRole('button', { name: '浏览网页', exact: true }).click();
  await frame.locator('#save').click();
  assert.equal(await frame.locator('body').evaluate(() => window.clicked), true, 'normal mode restores interaction');
  // Parent-origin spoofing and malformed selections must not change the selected state.
  await page.evaluate(() => window.postMessage({ type: 'wdrunge:selected', token: 'fake', selection: {} }, '*'));
  assert.equal(await page.getByText('已选中 section', { exact: true }).count(), 0);
  await page.getByRole('button', { name: '刷新', exact: true }).click();
  await page.getByText('已连接，可点选元素', { exact: true }).waitFor();
  await page.getByRole('button', { name: '选择修改位置', exact: true }).click();
  await frame.locator('#title').hover();
  await page.keyboard.press('Escape');
  await page.getByText('已连接，可点选元素', { exact: true }).waitFor();

  // Persistent selection mode: switch without cancel; clicking the same target toggles it off.
  await page.getByRole('button', { name: '选择修改位置', exact: true }).click();
  await frame.locator('#title').click();
  await page.getByText('已选中 h2', { exact: true }).waitFor();
  await frame.locator('#save').click();
  await page.getByText('已选中 button', { exact: true }).waitFor();
  await frame.locator('#save').click();
  await page.waitForFunction(() => !document.querySelector('.website-annotation'));
  await frame.locator('#title').click();
  await frame.locator('#save').click({ modifiers: ['Shift'] });
  await page.getByText('已选中 2 个元素', { exact: true }).waitFor();
  await page.getByLabel('网页修改要求').fill('两个元素统一加粗');
  await page.getByRole('button', { name: '发送修改要求', exact: true }).click();
  await waitFor(() => window.feedbackRequests.length === 3);
  assert.match(await page.evaluate(() => window.feedbackRequests[2]), /commonAncestor/);
  await page.getByRole('button', { name: '移除第 2 个元素' }).click();
  await page.getByText('已选中 h2', { exact: true }).waitFor();

  // Dynamic attributes/text are refreshed, and the request reads the DOM again before sending.
  await frame.locator('#title').evaluate(element => { element.textContent = '动态更新标题'; element.style.color = 'rgb(255, 0, 0)'; });
  await page.locator('.website-selection-list').getByText('h2 · 动态更新标题', { exact: true }).waitFor();
  await page.getByLabel('网页修改要求').fill('基于最新标题修改');
  await frame.locator('#title').evaluate(element => { element.style.fontWeight = '900'; });
  await page.getByRole('button', { name: '发送修改要求', exact: true }).click();
  await waitFor(() => window.feedbackRequests.length === 4);
  const dynamic = await page.evaluate(() => window.feedbackRequests[3]);
  assert.match(dynamic, /动态更新标题/); assert.match(dynamic, /"fontWeight":"900"/);
  await frame.locator('#title').evaluate(element => element.replaceWith(Object.assign(document.createElement('h2'), { id: 'title', textContent: '替换后的标题' })));
  await page.waitForFunction(() => !document.querySelector('.website-annotation'));
  await frame.locator('#title').click();
  await page.getByText('已选中 h2', { exact: true }).waitFor();

  // SPA routes invalidate prior references; address typing must survive heartbeat messages.
  await frame.locator('body').evaluate(() => history.pushState({}, '', '/spa?tab=design'));
  await page.waitForFunction(() => !document.querySelector('.website-annotation'));
  await page.waitForFunction(() => document.querySelector('[aria-label="预览网站地址"]').value.endsWith('/spa?tab=design'));
  await page.getByLabel('预览网站地址').fill(target + '/user-typing');
  const start = Date.now();
  await page.waitForFunction(start => Date.now() - start > 1000, start);
  assert.equal(await page.getByLabel('预览网站地址').inputValue(), target + '/user-typing');
  await page.getByRole('button', { name: '浏览网页', exact: true }).click();
  await frame.locator('#next-link').click();
  await page.getByText('已连接，可点选元素', { exact: true }).waitFor();
  await page.waitForFunction(() => document.querySelector('[aria-label="预览网站地址"]').value.endsWith('/next'));
  await page.getByRole('button', { name: '选择修改位置', exact: true }).click();
  await frame.locator('#title').click();
  await page.getByText('已选中 h2', { exact: true }).waitFor();

  // Real drag selection: include title and button, not the whole card.
  assert.equal(await page.getByRole('button', { name: '框选区域', exact: true }).count(), 0, 'point and region use one mode');
  const titleBox = await frame.locator('#title').boundingBox(), buttonBox = await frame.locator('#save').boundingBox();
  await page.mouse.move(titleBox.x - 3, titleBox.y - 3);
  await page.mouse.down();
  await page.mouse.move(titleBox.x + titleBox.width + 3, buttonBox.y + buttonBox.height + 3, { steps: 10 });
  await page.mouse.up();
  await page.getByText('已框选区域 · 2 个元素', { exact: true }).waitFor();
  await page.getByLabel('网页修改要求').fill('这个区域增加间距');
  await page.getByRole('button', { name: '发送修改要求', exact: true }).click();
  await waitFor(() => window.feedbackRequests.length === 5);
  const regionMessage = await page.evaluate(() => window.feedbackRequests[4]);
  assert.match(regionMessage, /"region":/); assert.match(regionMessage, /#title/); assert.match(regionMessage, /#save/);
  assert.doesNotMatch(regionMessage, /private-secret|private-editable/);

  // Click immediately after a drag, with no mode switch; small hand jitter stays a click.
  await frame.locator('#save').click();
  await page.getByText('已选中 h2', { exact: true }).waitFor(); // Clicking an already selected member removes it.
  await frame.locator('#save').click();
  await page.getByText('已选中 button', { exact: true }).waitFor();
  const jitterBox = await frame.locator('#title').boundingBox();
  await page.mouse.move(jitterBox.x + 20, jitterBox.y + 10);
  await page.mouse.down();
  await page.mouse.move(jitterBox.x + 22, jitterBox.y + 11);
  await page.mouse.up();
  await page.getByText('已选中 h2', { exact: true }).waitFor();
  assert.equal(await frame.locator('body').evaluate(() => Boolean(window.clicked)), false, 'drag and click do not execute the page button');
  assert.equal(await frame.locator('body').evaluate(() => getSelection().toString()), '', 'drag does not select page text');
  const additiveBox = await frame.locator('#save').boundingBox();
  await page.keyboard.down('Shift');
  await page.mouse.move(additiveBox.x - 2, additiveBox.y - 2);
  await page.mouse.down();
  await page.mouse.move(additiveBox.x + additiveBox.width + 2, additiveBox.y + additiveBox.height + 2, { steps: 6 });
  await page.mouse.up();
  await page.keyboard.up('Shift');
  await page.getByText('已框选区域 · 2 个元素', { exact: true }).waitFor();

  await open('/plain');
  await page.getByRole('button', { name: '选择修改位置', exact: true }).waitFor();
  await page.waitForFunction(() => document.querySelector('iframe')?.contentWindow !== null);
  assert.equal(await page.getByRole('button', { name: '选择修改位置', exact: true }).isDisabled(), true);
  await page.getByText('网页可以浏览，但还不能点选？接入选择脚本', { exact: true }).click();
  await page.getByRole('button', { name: '让 Agent 接入', exact: true }).click();
  await waitFor(() => window.feedbackRequests.length === 6);
  assert.match(await page.evaluate(() => window.feedbackRequests[5]), /仅限开发环境/);
  const checks = await page.evaluate(() => {
    const { localPreviewUrl, parseSelection } = window.feedbackTest;
    return { bad: ['javascript:alert(1)', 'http://evil.test', 'http://localhost.evil.test', 'http://a:b@localhost:8101'].map(value => {
      try { localPreviewUrl(value); return false; } catch { return true; }
    }), malformed: parseSelection({ url: 'http://localhost', rect: {} }) };
  });
  assert(checks.bad.every(Boolean)); assert.equal(checks.malformed, null);
  // Exercise the production App → existing chat stream boundary without paid model calls.
  const app = await browser.newPage({ viewport: { width: 1500, height: 1000 } });
  let submitted = null;
  let execution = { running: false, paused: false, stopping: false, pending_tasks: 0, active_goals: 0, task_states: {} };
  const timestamp = new Date().toISOString();
  const project = { id: 'visual-project', name: 'Visual test', workspace_path: 'E:/demo', created_at: timestamp, updated_at: timestamp,
    conversations: [{ id: 'visual-chat', project_id: 'visual-project', title: 'Visual test', status: 'idle', created_at: timestamp, updated_at: timestamp }] };
  await app.route('**/api/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': 'content-type', 'Access-Control-Allow-Methods': 'GET,POST' } });
    const headers = { 'Access-Control-Allow-Origin': '*' };
    if (path.endsWith('/messages/stream')) {
      submitted = request.postDataJSON();
      return route.fulfill({ headers, contentType: 'text/event-stream', body: 'event: delta\ndata: {"content":"已收到修改要求","message_id":"reply"}\n\nevent: done\ndata: {}\n\n' });
    }
    let body = {};
    if (path === '/api/projects') body = [project];
    else if (path === '/api/team') body = { agents: [] };
    else if (path.endsWith('/events')) body = { events: [], cursor: 0, has_more: false };
    else if (path.endsWith('/execution')) body = execution;
    else if (path === '/api/conversations/visual-chat') body = { ...project.conversations[0], project_name: project.name, workspace_path: project.workspace_path, activities: [], messages: submitted ? [{ id: 'user', sender: 'user', content: submitted.content, kind: 'user', status: 'completed', sequence: 1, read_by: [], completed_by: [], created_at: timestamp }] : [] };
    else if (path.includes('/delivery/')) body = { entries: [], revision: 'test', truncated: false };
    return route.fulfill({ headers, contentType: 'application/json', body: JSON.stringify(body) });
  });
  await app.goto(base);
  await app.getByRole('button', { name: '网站预览 ↗', exact: true }).click();
  await app.getByLabel('预览网站地址').fill(target);
  await app.getByRole('button', { name: '打开', exact: true }).click();
  await app.getByText('已连接，可点选元素', { exact: true }).waitFor();
  await app.getByRole('button', { name: '选择修改位置', exact: true }).click();
  await app.frameLocator('iframe').locator('#title').click();
  await app.getByLabel('网页修改要求').fill('把标题改成蓝色并加粗');
  await app.getByRole('button', { name: '发送修改要求', exact: true }).click();
  await app.waitForFunction(() => document.querySelector('.chat-feed')?.textContent.includes('把标题改成蓝色并加粗'));
  assert(submitted?.client_message_id);
  assert.match(submitted.content, /把标题改成蓝色并加粗/);
  assert.match(submitted.content, /#title/);
  await app.waitForFunction(() => !document.querySelector('.processing'));
  execution = { ...execution, running: true, pending_tasks: 1, task_states: { review: 1 } };
  await app.getByText('代码已交付 · 1 项待 Leader 验收', { exact: true }).waitFor();
  execution = { ...execution, running: false, pending_tasks: 0, task_states: {} };
  await app.waitForFunction(() => !document.querySelector('.processing'));
  if (process.env.WDRUNGE_TEST_SCREENSHOT) await app.screenshot({ path: process.env.WDRUNGE_TEST_SCREENSHOT });
  console.log('PASS: cross-origin preview, selection, parent scope, private fields, editing request, normal clicks, refresh, Escape, setup, URL/message validation');
  console.log('PASS: production App sends selected element and requirement through the existing persisted-chat stream API (mock backend, no model calls)');
  console.log('PASS: continuous/toggle/Shift selection, live DOM updates, detached elements, SPA/full navigation, heartbeat input, drag regions, truthful review and idle status');
} finally {
  await browser?.close();
  await new Promise(resolve => server.close(resolve));
}
