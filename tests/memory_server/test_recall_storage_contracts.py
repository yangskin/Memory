"""记录打包与 SQLite 投影不能改变召回、权限或 LLM 引用边界。"""
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_frontmatter import PACK_HEADER, render_record_markdown, render_record_pack_entry
from servers.memory_server.memory_record_index import memory_rebuild_index
from servers.memory_server.memory_retrieval import memory_retrieve_context
from servers.memory_server.memory_pack_migration import plan_coalescence, apply_coalescence
from servers.memory_server.memory_task_brief import build_task_brief
from servers.memory_server.server import _dispatch_tool
from servers.memory_server.memory_llm import LLMRequestError

CASES = [
    ('CrystalValve 压力阈值', 'CrystalValve 压力阈值必须小于安全上限。'),
    ('图集纹理 Mipmap 渗色', '图集纹理 Mipmap 渗色需要边缘扩展两个像素。'),
    ('AudioBus Submix 路由', 'AudioBus Submix 路由先检查主音轨绑定。'),
    ('CacheGate EqualMtime hash', 'CacheGate EqualMtime 外部改写需要 hash 校验。'),
    ('Git ReplicaJournal 合并', 'Git ReplicaJournal 合并按记录 ID 去重。'),
    ('InputMapping Gamepad 输入', 'InputMapping Gamepad 输入必须激活映射上下文。'),
    ('ArchiveVault 迁移回滚', 'ArchiveVault 迁移回滚保存原始字节并核对摘要。'),
    ('EmitterBounds 粒子裁剪', 'EmitterBounds 粒子裁剪检查固定包围盒范围。'),
]


def seed(repo, *, include_expired=False):
    now = datetime.now(timezone.utc).isoformat()
    for index, (query, body) in enumerate(CASES + [(f'Unrelated{n}', f'Unrelated{n} shopping list') for n in range(24)]):
        meta = dict(schema_version='2.0', id=f'mem_fixture_{index}', record_kind='decision',
                    scope='personal', author='alice', status='validated', created_at=now,
                    updated_at=now, tags=['high_value'], supersedes=[], source_refs=[], confidence=None)
        # 历史 pack 与新周日志混用；所有查询涉及的旧记录能够参加冷归档迁移。
        meta['created_at'] = meta['updated_at'] = '2020-01-01T00:00:00Z'
        relative = f'memory-bank/archive/record-packs/alice/202001/source-{index}.md'
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(PACK_HEADER+'\n\n'+render_record_pack_entry(meta['id'], render_record_markdown(meta, '# '+query+'\n\n'+body)), encoding='utf-8')
    # 同一文件内混合作者，不能靠文件路径或包头代替记录级 ACL。
    path = repo / 'memory-bank/personal/packs/journal/mixed.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = [PACK_HEADER+'\n']
    for index, (query, body) in enumerate(CASES):
        meta.update(id=f'mem_private_{index}', author='bob', created_at=now, updated_at=now)
        parts.append(render_record_pack_entry(meta['id'], render_record_markdown(meta, '# '+query+'\n\nPRIVATE_CANARY '+body, sparse=True)))
    path.write_text('\n'.join(parts), encoding='utf-8')
    if not include_expired:
        return
    # 过期事实只留作历史依据，不进入当前任务的有效经验。
    meta.update(id='mem_expired_0', author='alice', valid_to='2021-01-01T00:00:00Z')
    expired = repo / 'memory-bank/personal/packs/expired.md'
    expired.write_text(PACK_HEADER+'\n\n'+render_record_pack_entry(meta['id'], render_record_markdown(meta,
        '# CrystalValve 压力阈值\n\nEXPIRED_CANARY 旧压力阈值不能作为当前结论。')), encoding='utf-8')


def answers(config):
    result = []
    for index, (query, _) in enumerate(CASES):
        found = memory_retrieve_context(config, query=query, user='alice', top_k=5, max_chars=20000)
        assert found['ok'], found
        ids = [item['id'] for item in found['context_items']]
        assert ids[0] == f'mem_fixture_{index}', (query, ids)
        assert not any('mem_private_' in item for item in ids)
        result.append(ids)
    return result


def test_exact_recall_top1_and_top5_equal_to_markdown_before_after_migration(repo, monkeypatch):
    config = load_config(repo)
    seed(repo)
    assert memory_rebuild_index(config)['ok']
    sql = answers(config)
    import servers.memory_server.memory_retrieval as retrieval
    original = retrieval.prefilter_record_paths
    def path_only(*args, **kwargs):
        result = original(*args, **kwargs)
        result.pop('records', None)
        return result
    with monkeypatch.context() as patch:
        patch.setattr(retrieval, 'prefilter_record_paths', path_only)
        assert answers(config) == sql
    plan = plan_coalescence(config)
    assert plan['ok'] and plan['target_files'] < plan['source_files']
    assert apply_coalescence(config, plan['plan_id'], retire_legacy_paths=True)['ok']
    assert answers(config) == sql


@pytest.mark.parametrize('mode', ['disabled', 'success', 'timeout', 'malformed', 'unknown_citation'])
def test_brief_keeps_same_authorized_evidence_with_and_without_llm(repo, mode):
    config = load_config(repo)
    seed(repo, include_expired=True)
    config = replace(config, llm_defaults={'capabilities': {'generate_task_brief': {'enabled': True}}})
    goal = 'CrystalValve 压力阈值'
    task = _dispatch_tool(config, 'memory_read', dict(operation='task_context', agent_id='codex',
        user='alice', user_goal=goal, client_session_id='recall-contract', include_task_brief=False))
    assert task['ok'], task
    baseline = build_task_brief(config, task_context=task, current_task=task['current_task'], user_goal=goal, use_llm=False, refresh=True)
    class Client:
        config = type('Config', (), {'model': 'contract-test'})()
        def chat(self, messages, **kwargs):
            assert 'PRIVATE_CANARY' not in json.dumps(messages)
            assert 'EXPIRED_CANARY' not in json.dumps(messages)
            if mode == 'timeout':
                raise LLMRequestError('request timeout')
            content = 'invalid response'
            if mode in {'success', 'unknown_citation'}:
                citation = 'mem_fixture_0' if mode == 'success' else 'mem_unknown_999'
                content = json.dumps(dict(intent_summary='检查压力阈值', done_when='核对真源', focus=[], risks=[],
                    assumptions=[], open_questions=[], used_record_ids=[citation]), ensure_ascii=False)
            return {'choices': [{'message': {'content': content}}]}
        def usage_snapshot(self):
            return {}
    result = build_task_brief(config, task_context=task, current_task=task['current_task'], user_goal=goal,
                              use_llm=mode != 'disabled', client_factory=lambda _: Client(), refresh=True)
    assert result['ok'], result
    assert result['provenance']['record_ids'] == baseline['provenance']['record_ids']
    assert 'mem_fixture_0' in result['provenance']['record_ids']
    assert 'mem_unknown_999' not in result['provenance']['record_ids']
    assert 'PRIVATE_CANARY' not in result['brief_markdown']
    assert 'EXPIRED_CANARY' not in result['brief_markdown']
    assert 'mem_expired_0' not in result['provenance']['record_ids']
    assert result['generation']['mode'] == ('llm' if mode == 'success' else 'deterministic')
    from servers.memory_server.memory_response_budget import finalize_mcp_response
    delivered = finalize_mcp_response({'ok': True, 'context_token': 'ctx_delivery',
        'task_brief': result, 'diagnostics': {str(i): 'extra' * 5000 for i in range(30)}})
    assert 'mem_fixture_0' in delivered['task_brief']['brief_markdown']
    assert delivered['task_brief']['generation']['mode'] == result['generation']['mode']

    if mode in {'timeout', 'malformed', 'unknown_citation'}:
        assert result['generation']['fallback_used']
