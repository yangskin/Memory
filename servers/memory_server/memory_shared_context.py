"""Read shared context with cache-first, offline-safe behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .memory_shared_cache import cache_state
from .memory_sync_client import MemoryHubClient
from .memory_sync_config import SharedMemoryConfig
from .memory_sync_store import SyncStore


def cache_key(args: dict[str, Any]) -> str:
    return "context:" + ":".join((str(args.get("agent_id") or "memory-mcp"), str(args.get("task_id") or "")))


def get_shared_context(store: SyncStore, config: SharedMemoryConfig, args: dict[str, Any], *, force_refresh: bool = False, active: bool = False) -> dict[str, Any] | None:
    key = cache_key(args)
    cached = store.get_cache(key)
    if cached and not force_refresh:
        payload = __import__("json").loads(cached["payload_json"])
        state = cache_state(cached["fetched_at"], config.fresh_cache_seconds, config.usable_cache_seconds)
        if state != "stale":
            return {"status": state, "source": "cache", **payload}
    if not config.active or not config.read_enabled:
        return ({"status": "stale", "source": "cache", **__import__("json").loads(cached["payload_json"])} if cached else None)
    request = {"agent_instance_id": str(args.get("agent_id") or "memory-mcp"), "task_id": args.get("task_id"), "include": args.get("include") or ["user_brief", "project_brief", "same_task_agents", "my_other_agents", "other_tasks", "project_activity"], "max_age_minutes": int(args.get("max_age_minutes") or config.recent_window_hours * 60), "max_items": int(args.get("max_items") or config.max_items)}
    timeout = (config.active_query_timeout_ms if active else config.task_context_timeout_ms) / 1000
    status, payload = MemoryHubClient(config).context(request, timeout)
    if status == 200:
        store.put_cache(key, payload, (datetime.now(UTC) + timedelta(seconds=config.usable_cache_seconds)).isoformat(), payload.get("freshness", {}).get("latest_event_seq"))
        return {"status": "fresh", "source": "remote", **payload}
    return ({"status": "stale", "source": "cache", **__import__("json").loads(cached["payload_json"])} if cached else None)