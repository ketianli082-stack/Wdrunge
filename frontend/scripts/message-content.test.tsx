import test from 'node:test';
import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';
import MessageContent from '../src/MessageContent';

const render = (content: string, extra = {}) => renderToStaticMarkup(<MessageContent message={{ id: 'message', sender: 'leader', content, ...extra }} />);

test('renders delivery documents with headings, lists, GFM tables, tasks, links and code', () => {
  const html = render('# 交付材料\n\n**已完成**，~~旧方案~~\n\n- [x] 接口对接\n- [ ] 联调\n\n| 文件 | 状态 |\n| --- | --- |\n| App.tsx | 完成 |\n\n> 注意兼容性\n\n[文档](https://example.com)\n\n```ts\nconst element = "<div>";\n```');
  for (const fragment of ['<h1>交付材料</h1>', '<strong>已完成</strong>', '<del>旧方案</del>', 'type="checkbox"', 'checked=""', '<table>', '<th>文件</th>', '<blockquote>', 'target="_blank"', 'rel="noopener noreferrer"', 'class="language-ts"', '&lt;div&gt;']) assert.ok(html.includes(fragment), fragment);
});

test('keeps multi-worker mentions and independent read receipts around Markdown summaries', () => {
  const html = render('stored legacy text', { mentions: [
    { worker: 'frontend', name: '前端开发', summary: '**完成页面**\n\n- 支持移动端' },
    { worker: 'backend', name: '后端开发', summary: '检查 `GET /api/team`' },
  ], readBy: ['frontend'] });
  assert.ok(html.includes('@林青禾（前端开发）'));
  assert.ok(html.includes('aria-label="林青禾（前端开发）已读"'));
  assert.ok(html.includes('aria-label="陆野（后端开发）未读"'));
  assert.ok(html.includes('<strong>完成页面</strong>'));
  assert.ok(html.includes('<code>GET /api/team</code>'));
  assert.equal((html.match(/class="mention-token"/g) || []).length, 2);
  assert.ok(!html.includes('stored legacy text'));
});

test('preserves role-only single mentions in saved conversations without losing their body', () => {
  const html = render('@后端开发 **检查接口**\n\n```json\n{"ok":true}\n```', { mention: 'backend', read: true });
  assert.ok(html.includes('@陆野（后端开发）'));
  assert.ok(html.includes('aria-label="陆野（后端开发）已读"'));
  assert.ok(html.includes('<strong>检查接口</strong>'));
  assert.ok(html.includes('language-json'));
});

test('ordinary @ text and code do not acquire fabricated read receipts', () => {
  const html = render('联系 @林青禾（前端开发）\n下一行\n\n`@陆野（后端开发）`');
  assert.ok(html.includes('@林青禾（前端开发）'));
  assert.ok(html.includes('<br/>'));
  assert.ok(html.includes('<code>@陆野（后端开发）</code>'));
  assert.ok(!html.includes('read-indicator'));
});

test('does not execute raw HTML or unsafe Markdown links', () => {
  const html = render('<script>alert(1)</script>\n\n<img src=x onerror="alert(1)">\n\n[click](javascript:alert%281%29)');
  assert.ok(!html.includes('<script'));
  assert.ok(!html.includes('onerror'));
  assert.ok(!html.includes('javascript:'));
});

test('tolerates incomplete Markdown arriving in streaming chunks', () => {
  const document = '# 结果\n\n**完成**\n\n```ts\nconst result = true;\n```\n\n| 状态 |\n| --- |\n| 完成 |';
  for (let i = 0; i <= document.length; i++) assert.doesNotThrow(() => render(document.slice(0, i)));
  assert.ok(render(document).includes('<table>'));
});
