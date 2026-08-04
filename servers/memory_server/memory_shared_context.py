"""Read shared context with cache-first, offline-safe behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from typing import Any

from .memory_shared_cache import cache_state
from .memory_sync_client import MemoryHubClient
from .memory_sync_config import SharedMemoryConfig
from .memory_sync_store import SyncStore


def cache_key(args: dict[str, Any]) -> str:
    return "context:" + ":".join((str(args.get("agent_id") or "memory-mcp"), str(args.get("task_id") or "")))


def _compact_injected_context(payload: dict[str, Any], max_tokens: int) -> dict[str, Any]:
    """Bound automatic task-context injection without changing active queries."""
    budget = max_tokens * 4
    compact = dict(payload)
    for key in ("same_task_agents", "my_other_agents", "other_tasks", "project_activity", "pending_updates"):
        if isinstance(compact.get(key), list):
            compact[key] = compact[key][:5]
    for key in ("user_brief", "project_brief"):
        brief = compact.get(key)
        if isinstance(brief, dict) and isinstance(brief.get("markdown"), str):
            brief = dict(brief)
            brief["markdown"] = brief["markdown"][: max(160, budget // 4)]
            compact[key] = brief
    while len(json.dumps(compact, ensure_ascii=False)) > budget and compact.get("project_activity"):
        compact["project_activity"] = compact["project_activity"][:-1]
    return compact


def get_shared_context(store: SyncStore, config: SharedMemoryConfig, args: dict[str, Any], *, force_refresh: bool = False, active: bool = False) -> dict[str, Any] | None:
    key = cache_key(args)
    store.put_state("default_context_args", {"agent_id": str(args.get("agent_id") or "memory-mcp"), "task_id": args.get("task_id"), "include": args.get("include"), "max_age_minutes": args.get("max_age_minutes"), "max_items": args.get("max_items")})
    cached = store.get_cache(key)
    if cached and not force_refresh:
        payload = __import__("json").loads(cached["payload_json"])
        state = cache_state(cached["fetched_at"], config.fresh_cache_seconds, config.usable_cache_seconds)
        if state == "fresh":
            payload = _compact_injected_context(payload, config.max_injected_tokens) if not active else payload
            return {"status": state, "source": "cache", **payload}
    if not config.active or not config.read_enabled:
        return ({"status": "stale", "source": "cache", **__import__("json").loads(cached["payload_json"])} if cached else None)
    disabled = store.get_state("remote_auth_disabled")
    token_hash = hashlib.sha256(str(config.token).encode("utf-8")).hexdigest()
    if disabled and disabled.get("token_hash") == token_hash:
        return ({"status": "stale", "source": "cache", **__import__("json").loads(cached["payload_json"])} if cached else None)
    if disabled:
        store.delete_state("remote_auth_disabled")
    request = {"agent_instance_id": str(args.get("agent_id") or "memory-mcp"), "task_id": args.get("task_id"), "include": args.get("include") or ["user_brief", "project_brief", "same_task_agents", "my_other_agents", "other_tasks", "project_activity"], "max_age_minutes": int(args.get("max_age_minutes") or config.recent_window_hours * 60), "max_items": int(args.get("max_items") or config.max_items)}
    timeout = (config.active_query_timeout_ms if active else config.task_context_timeout_ms) / 1000
    status, payload = MemoryHubClient(config).context(request, timeout)
    if status == 200:
        store.put_cache(key, payload, (datetime.now(UTC) + timedelta(seconds=config.usable_cache_seconds)).isoformat(), payload.get("freshness", {}).get("latest_event_seq"))
        payload = _compact_injected_context(payload, config.max_injected_tokens) if not active else payload
        return {"status": "fresh", "source": "remote", **payload}
    return ({"status": "stale", "source": "cache", **__import__("json").loads(cached["payload_json"])} if cached else None)