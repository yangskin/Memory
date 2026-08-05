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