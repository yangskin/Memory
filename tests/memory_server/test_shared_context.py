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