from __future__ import annotations

from datetime import UTC, datetime, timedelta

from servers.memory_server.memory_shared_context import get_shared_context
from servers.memory_server.memory_sync_config import SharedMemoryConfig
from servers.memory_server.memory_sync_store import SyncStore


def test_no_cache_and_disabled_remote_degrades_to_local_only(tmp_path) -> None:
    result = get_shared_context(SyncStore(tmp_path / "shared-sync.db"), SharedMemoryConfig(enabled=False), {})
    assert result is None


def test_usable_cache_is_returned_without_remote(tmp_path) -> None:
    store = SyncStore(tmp_path / "shared-sync.db")
    args = {"agent_id": "pytest", "task_id": "task"}
    store.put_cache("context:pytest:task", {"pending_updates": [], "freshness": {}}, (datetime.now(UTC) + timedelta(minutes=5)).isoformat())
    result = get_shared_context(store, SharedMemoryConfig(enabled=False), args)
    assert result is not None
    assert result["source"] == "cache"