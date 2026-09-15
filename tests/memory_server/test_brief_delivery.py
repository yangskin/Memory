"""交互预算及真实 MCP 最后一层输出保留证据的合同。"""
import json
import threading
import time
from dataclasses import replace

import pytest

from servers.memory_server.memory_brief_deadline import within_budget
from servers.memory_server.memory_llm import LLMRequestError
from servers.memory_server.memory_response_budget import finalize_mcp_response
from servers.memory_server.memory_task_brief import build_task_brief
from servers.memory_server.memory_config import load_config
from servers.memory_server.server import _dispatch_tool


def test_slow_enhancement_returns_within_budget_and_can_finish_later():
    release = threading.Event()
    finished = threading.Event()
    def slow():
        release.wait(3)
        finished.set()
        return {'late': True}
    start = time.monotonic()
    try:
        with pytest.raises(LLMRequestError, match='timeout'):
            within_budget(slow, 0.05)
        assert time.monotonic() - start < 1
    finally:
        release.set()
        assert finished.wait(1)
    assert within_budget(lambda: 'ready', 1) == 'ready'


def test_saturated_enhancers_do_not_spawn_unbounded_threads(monkeypatch):
    import servers.memory_server.memory_brief_deadline as budget
    monkeypatch.setattr(budget, '_SLOTS', threading.BoundedSemaphore(1))
    budget._SLOTS.acquire()
    with pytest.raises(LLMRequestError, match='busy'):
        budget.within_budget(lambda: pytest.fail('must not start'), 1)
    budget._SLOTS.release()


def test_bounded_task_context_retains_token_brief_and_citations():
    result = dict(ok=True, error=None, message='ready', context_token='ctx_example', task_id='task_example',
        **{f'diagnostic_{i}': '诊断'*10000 for i in range(20)})
    result['task_brief'] = dict(ok=True, message='brief', generation={'mode':'deterministic','status':'timeout'},
        provenance={'record_ids':['mem_expected']}, brief_markdown='当前事实 [mem_expected]：压力阈值必须经过验证。'+'说明'*12000)
    compact = finalize_mcp_response(result)
    assert compact['context_token'] == 'ctx_example'
    assert compact['task_brief']['provenance']['record_ids'][0] == 'mem_expected'
    assert '当前事实' in compact['task_brief']['brief_markdown']
    assert compact['task_brief']['generation']['status'] == 'timeout'
    assert len(json.dumps(compact, ensure_ascii=False)) < 12000


def test_expired_interactive_budget_keeps_full_deterministic_evidence(repo):
    config = load_config(repo)
    task = _dispatch_tool(config, 'memory_read', dict(operation='task_context', agent_id='codex', user='alice',
        user_goal='check pressure threshold', client_session_id='budget-contract', include_task_brief=False))
    config = replace(config, llm_defaults={'capabilities': {'generate_task_brief': {'enabled':True, 'interactive_budget_seconds':0.000001}}})
    class Client:
        def chat(self,*args,**kwargs):
            pytest.fail('no network request after budget exhausted')
    brief = build_task_brief(config, task_context=task, current_task=task['current_task'],
        client_factory=lambda _: Client(), use_llm=True, refresh=True)
    assert brief['ok'] and brief['generation']['mode'] == 'deterministic'
    assert brief['generation']['status'] == 'timeout'


def test_large_authority_prefix_does_not_crowd_out_memory_experience():
    from servers.memory_server.memory_response_budget import _bounded_brief
    text = '## 当前意图\n核对压力。\n## 权威信息地图 · Rules\n' + '规则说明。' * 2000
    text += '\n## 任务相关经验\n- [mem_expected] 压力阈值必须先验证。\n## 冲突与缺口\n尚无验证结果。'
    compact = _bounded_brief(text, 1024)
    assert '[mem_expected] 压力阈值必须先验证。' in compact
    assert '当前意图' in compact and '尚无验证结果' in compact
    assert len(compact) <= 1024


def test_interactive_budget_survives_config_loading_and_rejects_invalid_value(repo):
    from servers.memory_server.memory_config import MemoryConfigError
    path = repo / '.ai-memory/config.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    data['llm_defaults'] = {'capabilities': {'generate_task_brief': {'enabled':True, 'interactive_budget_seconds':30}}}
    path.write_text(json.dumps(data), encoding='utf-8')
    assert load_config(repo).llm_defaults['capabilities']['generate_task_brief']['interactive_budget_seconds'] == 30
    data['llm_defaults']['capabilities']['generate_task_brief']['interactive_budget_seconds'] = 0
    path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(MemoryConfigError):
        load_config(repo)
