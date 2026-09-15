"""远端 Board 慢/失败时本地简报不能等待，缓存状态不得冒充远端新鲜结果。"""
import json
import threading
import time
from dataclasses import replace

import pytest

from servers.memory_server import memory_board_refresh as refresh
from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_sync_config import SharedMemoryConfig
from servers.memory_server.server import _dispatch_tool


def configured(repo):
    return replace(load_config(repo), shared_memory=SharedMemoryConfig(enabled=True,
        server_url='https://hub.example.invalid', project_id='fixture', token_env='MEMORY_TEST_UNUSED_TOKEN',local_token='test-token'))


def completed():
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with refresh._LOCK:
            if not refresh._RUNNING:
                return
        time.sleep(0.01)
    pytest.fail('background Board refresh did not finish')


def test_slow_board_does_not_block_task_injection_and_duplicate_reads_share_request(repo, monkeypatch):
    from servers.memory_server import server_dispatch as dispatch
    config=configured(repo);release=threading.Event();calls=[];state={}
    def slow(config,payload):
        calls.append(True);release.wait(3)
        return {'ok':True,'remote':{'items':[]}}
    monkeypatch.setattr(dispatch,'remote_board_query',slow)
    monkeypatch.setattr(dispatch,'_schedule_board_sync',lambda *a:None)
    try:
        start=time.monotonic()
        assert dispatch._load_open_board_items_for_task(config,task_id='fixture',max_items=5,max_tokens=500,freshness=state)==[]
        assert time.monotonic()-start < 1
        assert state['freshness']=='not_verified' and state['refresh']=='scheduled'
        second={}
        dispatch._load_open_board_items_for_task(config,task_id='fixture',max_items=5,max_tokens=500,freshness=second)
        assert second['refresh']=='in_progress' and len(calls)==1
    finally:
        release.set();completed()


def test_successful_empty_snapshot_replaces_previous_remote_items(repo):
    config=configured(repo);payload={'task_id':'fixture','filter':'unresolved','max_items':5}
    refresh.cached_board_query(config,payload,query=lambda *a:{'ok':True,'remote':{'items':[{'post_id':'old','content':'old question'}]}})
    completed()
    items,state=refresh.cached_board_query(config,payload,query=lambda *a:pytest.fail('fresh cache must not contact remote'))
    assert items[0]['post_id']=='old' and state['freshness']=='fresh'
    expired=replace(config,shared_memory=replace(config.shared_memory,fresh_cache_seconds=0))
    refresh.cached_board_query(expired,payload,query=lambda *a:{'ok':True,'remote':{'items':[]}});completed()
    items,state=refresh.cached_board_query(config,payload,query=lambda *a:pytest.fail('fresh empty cache must remain valid'))
    assert items==[] and state['freshness']=='fresh'


def test_board_cache_isolated_when_credentials_change(repo):
    first=configured(repo);payload={'task_id':'fixture'}
    second=replace(first,shared_memory=replace(first.shared_memory,local_token='different-test-token'))
    assert refresh._cache_path(first,payload)!=refresh._cache_path(second,payload)
    third=replace(first,shared_memory=replace(first.shared_memory,server_url='https://another.example.invalid'))
    assert refresh._cache_path(first,payload)!=refresh._cache_path(third,payload)


def test_remote_failure_is_reported_without_private_exception_details(repo):
    config=configured(repo);payload={'task_id':'failure'}
    def fail(*args):raise RuntimeError('private exception detail')
    refresh.cached_board_query(config,payload,query=fail);completed()
    _,state=refresh.cached_board_query(config,payload,query=fail);completed()
    assert state['last_refresh_error']=='RuntimeError'
    assert 'private exception detail' not in json.dumps(state)


def test_saturation_does_not_create_more_remote_requests(repo,monkeypatch):
    monkeypatch.setattr(refresh,'_RUNNING',{'first','second'})
    items,state=refresh.cached_board_query(configured(repo),{'task_id':'third'},query=lambda *a:pytest.fail('must not start'))
    assert items is None and state['refresh']=='busy'


def test_task_context_keeps_explicit_board_cache_status_in_compact_response(repo):
    result=_dispatch_tool(load_config(repo),'memory_read',{'operation':'task_context','user':'alice','agent_id':'pytest',
        'user_goal':'check local pressure facts','include_task_brief':False})
    assert result['ok'] and result['board_context']['advisory'] is True
    assert result['board_context']['freshness']=='local_only'


def test_output_budget_keeps_board_freshness_when_returning_advisory_items():
    from servers.memory_server.memory_response_budget import finalize_mcp_response
    payload={'ok':True,'context_token':'ctx_fixture','task_id':'task_fixture',
        'task_brief':{'brief_markdown':'当前经验','provenance':{'record_ids':['fact']}},
        **{f'diagnostic_{i}':'诊断'*5000 for i in range(30)},
        'board_context':{'freshness':'stale','source':'cache','advisory':True},
        'open_board_items':[{'post_id':'advisory','content':'待确认问题'}]}
    result=finalize_mcp_response(payload)
    assert result['open_board_items'][0]['post_id']=='advisory'
    assert result['board_context']['freshness']=='stale'
