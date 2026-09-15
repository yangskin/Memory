from __future__ import annotations

from datetime import UTC, datetime, timedelta

from servers.memory_server.memory_shared_context import _compact_injected_context, get_shared_context
from servers.memory_server.memory_sync_config import SharedMemoryConfig
from servers.memory_server.memory_sync_store import SyncStore


def test_no_cache_and_disabled_remote_degrades_to_local_only(tmp_path) -> None:
    result = get_shared_context(SyncStore(tmp_path / "shared-sync.db"), SharedMemoryConfig(enabled=False), {})
    assert result is None


def test_usable_cache_is_returned_without_remote(tmp_path) -> None:
    store = SyncStore(tmp_path / "shared-sync.db")
    args = {"agent_id": "pytest", "task_id": "task"}
    store.put_cache(
        "context:pytest:task",
        {"pending_updates": [{"content_markdown": "x" * 20_000}], "freshness": {}},
        (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    )
    result = get_shared_context(
        store,
        SharedMemoryConfig(enabled=False, max_injected_tokens=100),
        args,
    )
    assert result is not None
    assert result["source"] == "cache"
    assert len(str(result)) < 2_000


def test_injected_context_budget_prioritizes_source_status_and_briefs() -> None:
    result = _compact_injected_context(
        {
            "pending_updates": [{"content_markdown": "update" * 10_000}],
            "project_activity": [{"summary": "activity" * 10_000}],
            "status": "fresh",
            "source": "remote",
            "freshness": {"latest_event_seq": 42},
            "user_brief": {"markdown": "user brief" * 1_000},
            "project_brief": {"markdown": "project brief" * 1_000},
        },
        max_tokens=100,
    )

    assert result["status"] == "fresh"
    assert result["source"] == "remote"
    assert "freshness" in result
    assert "user_brief" in result
    assert "project_brief" in result

def test_passive_injection_with_cold_cache_does_not_contact_hub(tmp_path, monkeypatch):
    from servers.memory_server.memory_sync_client import MemoryHubClient
    config=SharedMemoryConfig(enabled=True,server_url='https://hub.example.invalid',project_id='fixture',local_token='test-token')
    monkeypatch.setattr(MemoryHubClient,'context',lambda *a,**k: (_ for _ in ()).throw(AssertionError('foreground must not query Hub')))
    result=get_shared_context(SyncStore(tmp_path/'shared-sync.db'),config,{'task_id':'fixture'},passive=True)
    assert result['status']=='pending' and result['refresh']=='background'
    assert result['advisory'] is True


def test_passive_stale_snapshot_cannot_claim_remote_freshness(tmp_path, monkeypatch):
    from servers.memory_server.memory_sync_client import MemoryHubClient
    config=SharedMemoryConfig(enabled=True,server_url='https://hub.example.invalid',project_id='fixture',local_token='test-token')
    store=SyncStore(tmp_path/'shared-sync.db');args={'agent_id':'fixture','task_id':'fixture'}
    store.put_cache('context:fixture:fixture',{'status':'fresh','source':'remote','user_brief':{'markdown':'cached fact'}},None)
    with store._connect() as conn:
        conn.execute('UPDATE shared_cache SET fetched_at=?',((datetime.now(UTC)-timedelta(hours=1)).isoformat(),))
    monkeypatch.setattr(MemoryHubClient,'context',lambda *a,**k: (_ for _ in ()).throw(AssertionError('foreground must not query Hub')))
    result=get_shared_context(store,config,args,passive=True)
    assert result['status']=='stale' and result['source']=='cache'
    assert result['user_brief']['markdown']=='cached fact'


def test_active_and_background_refresh_still_query_hub(tmp_path,monkeypatch):
    from servers.memory_server.memory_sync_client import MemoryHubClient
    config=SharedMemoryConfig(enabled=True,server_url='https://hub.example.invalid',project_id='fixture',local_token='test-token')
    calls=[]
    def remote(*args):
        calls.append(True)
        return 200,{'user_brief':{'markdown':'new fact'},'freshness':{}}
    monkeypatch.setattr(MemoryHubClient,'context',remote)
    store=SyncStore(tmp_path/'shared-sync.db')
    result=get_shared_context(store,config,{'task_id':'active'},active=True,passive=True)
    assert result['source']=='remote' and result['user_brief']['markdown']=='new fact'
    result=get_shared_context(store,config,{'task_id':'background'},force_refresh=True,passive=True)
    assert result['source']=='remote' and len(calls)==2
