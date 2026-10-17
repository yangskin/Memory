// Run with: node --test tests/unit/test_shared_page_rendering.cjs
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const page = readFileSync(join(__dirname, '../../web/shared.html'), 'utf8');
function section(start, end) {
  return page.slice(page.indexOf(start), page.indexOf(end, page.indexOf(start)));
}
const context = vm.createContext({});
vm.runInContext(
  section('  function fmtTime(', '  function pickText(') +
  section('  function typeClass(', '  function boardMatches('), context
);
const post = (content, isRoot = true) => context.renderBoardPost({
  post_type: 'note', status: 'open', content
}, isRoot);
const body = html => html.match(/<div class="body">([\s\S]*?)<\/div><\/div>$/)[1];

test('board JSON quotes and ampersands are escaped once', () => {
  const html = body(post('{"protocol":"example.hub-health/1","state":"running & ready"}'));
  assert.equal(html, '{&quot;protocol&quot;:&quot;example.hub-health/1&quot;,&quot;state&quot;:&quot;running &amp; ready&quot;}');
  assert.ok(!html.includes('&amp;quot;'));
});

test('board preserves plain text, blank lines, indentation and Markdown literally in roots and replies', () => {
  const text = '# 原始标题\n\n  **明文** `代码`\n[链接](https://example.com)\n&lt;原始实体&gt;';
  const expected = '# 原始标题\n\n  **明文** `代码`\n[链接](https://example.com)\n&amp;lt;原始实体&amp;gt;';
  assert.equal(body(post(text)), expected);
  assert.equal(body(post(text, false)), expected);
  assert.match(page, /\.board-post \.body \{[^}]*white-space: pre-wrap;[^}]*overflow-wrap: anywhere;/);
});

test('board displays untrusted HTML as text without creating executable tags', () => {
  const html = body(post('<img src=x onerror="alert(1)">\n<script>alert(2)</script>'));
  assert.ok(!html.includes('<img'));
  assert.ok(!html.includes('<script'));
  assert.ok(html.includes('&lt;img src=x onerror=&quot;alert(1)&quot;&gt;'));
});

test('brief Markdown escapes normal text and fenced code once while retaining formatting', () => {
  const html = context.renderMarkdown('# "标题" & <b>\n- **"内容"**\n```json\n{"key":"<script>"}\n```');
  assert.ok(html.includes('<h1>&quot;标题&quot; &amp; &lt;b&gt;</h1>'));
  assert.ok(html.includes('<li><strong>&quot;内容&quot;</strong></li>'));
  assert.ok(html.includes('{&quot;key&quot;:&quot;&lt;script&gt;&quot;}'));
  assert.ok(!html.includes('&amp;quot;'));
  assert.ok(!html.includes('<script>'));
});
