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
  const html = body(post('{"protocol":"example/1","state":"running & ready"}'));
  assert.equal(html, '{&quot;protocol&quot;:&quot;example/1&quot;,&quot;state&quot;:&quot;running &amp; ready&quot;}');
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

function health(overrides = {}) {
  return { protocol: 'memory.hub-health/1', device: '测试设备', observed_at: new Date().toISOString(),
    services: { bridge: { state: 'running' }, receiver: { state: 'running' }, replies: { state: 'running' } },
    codex_execution: { coverage: 'unbound', instances: [] }, ...overrides };
}
function instance(state, report) {
  return { task: '海克斯检查', thread_id: 'hidden-thread', execution: { state, checked_at: new Date().toISOString() }, reported: report };
}
test('health cards show only Chinese summaries and no raw metadata', () => {
  const html = post(JSON.stringify(health({ runtime_signature: 'hidden-signature' })));
  assert.match(html, /设备：测试设备/);
  assert.match(html, /派发运行中；接收运行中；回信运行中/);
  assert.match(html, /未绑定对话，执行状态未知/);
  for (const raw of ['protocol', 'runtime_signature', 'hidden-signature', 'hub-health', '未解决']) assert.ok(!html.includes(raw));
});
test('current task reports and abnormal service states remain readable', () => {
  const data = health({ services: { bridge: { state: 'backoff' } }, codex_execution: { instances: [instance('waiting_for_user', {
    freshness: 'current_turn', reported_at: new Date().toISOString(), step: '已完成源码检查', blocker: '等待确认'
  })] } });
  const html = body(post(JSON.stringify(data)));
  assert.match(html, /派发异常退出，等待重试/);
  assert.match(html, /海克斯检查 · 等待用户/);
  assert.match(html, /进度：已完成源码检查/);
  assert.match(html, /阻塞：等待确认/);
  assert.ok(!html.includes('hidden-thread'));
});
test('stale and previous-turn reports cannot imply a running task', () => {
  for (const freshness of ['stale', 'previous_turn']) {
    const html = body(post(JSON.stringify(health({ codex_execution: { instances: [instance('idle', {
      freshness, reported_at: new Date().toISOString(), step: '正在构建', blocker: '旧阻塞'
    })] } }))));
    assert.match(html, /海克斯检查 · 空闲/);
    assert.match(html, /旧进度已过时/);
    assert.ok(!html.includes('正在构建'));
    assert.ok(!html.includes('旧阻塞'));
  }
});
test('expired observations and missing fields are explicit instead of healthy', () => {
  const html = body(post(JSON.stringify(health({ observed_at: '2000-01-01T00:00:00Z' }))));
  assert.match(html, /状态已过期/);
  assert.ok(!html.includes('运行中'));
  assert.match(body(post('{"protocol":"memory.hub-health/1"}')), /状态已过期/);
  const unknown = body(post(JSON.stringify(health({ codex_execution: { instances: [instance('new-state')] } }))));
  assert.match(unknown, /海克斯检查 · 状态未知/);
});
test('execution-only cards are summarized and untrusted report text is escaped', () => {
  const html = post(JSON.stringify({ protocol: 'memory.codex-execution/1', device: '<img src=x>',
    observed_at: new Date().toISOString(), instances: [instance('running', {
      freshness: 'current_turn', reported_at: new Date().toISOString(), step: '<script>alert(1)</script>'
    })] }));
  assert.match(html, /海克斯检查 · 运行中/);
  assert.ok(!html.includes('<script>'));
  assert.ok(!html.includes('<img'));
  assert.match(html, /&lt;script&gt;/);
});

test('truncated automatic health messages never expose raw JSON', () => {
  const html = context.renderBoardPost({ author_agent_id: 'hub-health', content: '{"protocol":"memory.hub-health/1", "signature": "secret' }, true);
  assert.match(html, /数据不完整/);
  assert.ok(!html.includes('signature'));
});

test('status channels stay separate from message filters, headings, and counts', () => {
  const elements = { boardArea: {}, boardFilter: { value: 'resolved' }, boardSearch: { value: '不匹配' },
    boardTotal: {}, boardOpen: {}, boardResolved: {}, statusArea: { hidden: true }, statusBody: {} };
  context.window = {};
  context.document = { getElementById: id => elements[id] };
  vm.runInContext(section('  function boardMatches(', '  var TASK_COLORS'), context);
  const item = { post_id: 'hidden-post', thread_id: 'hidden-thread', status: 'open',
    content: JSON.stringify(health({ codex_execution: { instances: [instance('idle')] } })) };
  const message = { post_id: 'message-post', thread_id: 'message-thread', status: 'open', content: '普通留言' };
  context.renderBoard({ items: [message], status_channels: [item] });
  assert.equal(elements.statusArea.hidden, false);
  assert.match(elements.statusBody.innerHTML, /海克斯检查 · 空闲/);
  assert.ok(!elements.statusBody.innerHTML.includes('hidden-thread'));
  assert.ok(!elements.statusBody.innerHTML.includes('protocol'));
  assert.match(elements.boardArea.innerHTML, /没有符合当前筛选的留言/);
  assert.equal(elements.boardTotal.textContent, 1);
  assert.equal(elements.boardOpen.textContent, 1);
  assert.equal(elements.boardResolved.textContent, 0);

  elements.boardFilter.value = 'all';
  elements.boardSearch.value = '';
  context.renderBoard({ items: [message], status_channels: [item] });
  assert.match(elements.boardArea.innerHTML, /普通留言/);
  assert.ok(!elements.boardArea.innerHTML.includes('海克斯检查'));

  // 只有状态时，留言区仍为空；响应省略状态时必须清理上一次的快照。
  context.renderBoard({ items: [], status_channels: [item] });
  assert.match(elements.boardArea.innerHTML, /留言板为空/);
  assert.equal(elements.boardTotal.textContent, 0);
  assert.equal(elements.statusArea.hidden, false);
  context.renderBoard({ items: [message] });
  assert.equal(elements.statusArea.hidden, true);
  assert.equal(elements.statusBody.innerHTML, '');
  context.renderBoard({ items: [], status_channels: [message] });
  assert.equal(elements.statusArea.hidden, true);
  assert.equal(elements.statusBody.innerHTML, '');
});


test('status schemas accept producer namespaces without binding to a consumer project', () => {
  // 旧生产方样本分片构造；公开源码不保存消费项目标识。
  for (const namespace of ['memory', 'tenant-a', 'org.device', 'p1' + '11']) {
    const html = body(post(JSON.stringify(health({ protocol: namespace + '.hub-health/1' }))));
    assert.match(html, /派发运行中；接收运行中；回信运行中/);
    const execution = body(post(JSON.stringify({ protocol: namespace + '.codex-execution/1',
      device: '测试设备', observed_at: new Date().toISOString(), instances: [instance('idle')] })));
    assert.match(execution, /海克斯检查 · 空闲/);
    assert.ok(!execution.includes('通讯：'));
  }
});

test('unknown versions, types, and malformed namespaces remain ordinary escaped messages', () => {
  for (const protocol of [null, 123, ['memory.hub-health/1'], 'memory.hub-health/2',
    'memory.other/1', '.hub-health/1', 'bad namespace.hub-health/1', 'memory..hub-health/1',
    'memory.hub-health/1-extra', 'memory.hub-health/1\n', '<img>.hub-health/1']) {
    const content = JSON.stringify(health({ protocol }));
    assert.equal(context.boardStatusPayload({ content }), null);
    const html = body(post(content));
    assert.ok(html.includes('&quot;protocol&quot;'));
    assert.ok(!html.includes('<img>'));
    assert.ok(!html.includes('通讯：'));
  }
});
