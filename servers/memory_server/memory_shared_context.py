"""Read shared context with cache-first, offline-safe behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from typing import Any

from .memory_shared_cache import cache_state
from .memory_response_budget import _bounded_value
from .memory_sync_client import MemoryHubClient
from .memory_sync_config import SharedMemoryConfig
from .memory_sync_store import SyncStore


def cache_key(args: dict[str, Any]) -> str:
    return "context:" + ":".join((str(args.get("agent_id") or "memory-mcp"), str(args.get("task_id") or "")))


def _compact_injected_context(payload: dict[str, Any], max_tokens: int) -> dict[str, Any]:
    """Bound shared context from active and cached reads."""
    budget = max(256, max_tokens * 4)
    compact = dict(payload)
    list_keys = ("same_task_agents", "my_other_agents", "other_tasks", "project_activity", "pending_updates")
    for key in list_keys:
        if isinstance(compact.get(key), list):
            compact[key] = _bounded_value(
                compact[key][:5],
                max_dict_items=30,
                max_list_items=5,
                max_string_chars=max(160, budget // 5),
                max_depth=8,
            )
    for key in ("user_brief", "project_brief"):
        brief = compact.get(key)
        if isinstance(brief, dict) and isinstance(brief.get("markdown"), str):
            brief = dict(brief)
            brief["markdown"] = brief["markdown"][: max(160, budget // 4)]
            compact[key] = brief
    for key in reversed(list_keys):
        while len(json.dumps(compact, ensure_ascii=False)) > budget and compact.get(key):
            compact[key] = compact[key][:-1]
    if len(json.dumps(compact, ensure_ascii=False)) > budget:
        priority_keys = ("status", "source", "freshness", "user_brief", "project_brief")
        ordered = {key: compact[key] for key in priority_keys if key in compact}
        ordered.update((key, value) for key, value in compact.items() if key not in ordered)
        compact = _bounded_value(
            ordered,
            max_dict_items=30,
            max_list_items=3,
            max_string_chars=max(80, budget // 8),
            max_depth=6,
        )
    return compact


def get_shared_context(store: SyncStore, config: SharedMemoryConfig, args: dict[str, Any], *, force_refresh: bool = False, active: bool = False) -> dict[str, Any] | None:
    key = cache_key(args)
    store.put_state("default_context_args", {"agent_id": str(args.get("agent_id") or "memory-mcp"), "task_id": args.get("task_id"), "include": args.get("include"), "max_age_minutes": args.get("max_age_minutes"), "max_items": args.get("max_items")})
    cached = store.get_cache(key)
    if cached and not force_refresh:
        payload = __import__("json").loads(cached["payload_json"])
        state = cache_state(cached["fetched_at"], config.fresh_cache_seconds, config.usable_cache_seconds)
        if state == "fresh":
            payload = _compact_injected_context(payload, config.max_injected_tokens)
            return {"status": state, "source": "cache", **payload}
    if not config.active or not config.read_enabled:
        return ({"status": "stale", "source": "cache", **_compact_injected_context(__import__("json").loads(cached["payload_json"]), config.max_injected_tokens)} if cached else None)
    disabled = store.get_state("remote_auth_disabled")
    token_hash = hashlib.sha256(str(config.token).encode("utf-8")).hexdigest()
    if disabled and disabled.get("token_hash") == token_hash:
        return ({"status": "stale", "source": "cache", **_compact_injected_context(__import__("json").loads(cached["payload_json"]), config.max_injected_tokens)} if cached else None)
    if disabled:
        store.delete_state("remote_auth_disabled")
    request = {"agent_instance_id": str(args.get("agent_id") or "memory-mcp"), "task_id": args.get("task_id"), "include": list(dict.fromkeys(args.get("include") or ["user_brief", "project_brief", "same_task_agents", "my_other_agents", "other_tasks", "project_activity"]))[:6], "max_age_minutes": min(int(args.get("max_age_minutes") or config.recent_window_hours * 60), 10080), "max_items": min(int(args.get("max_items") or config.max_items), 20)}
    timeout = (config.active_query_timeout_ms if active else config.task_context_timeout_ms) / 1000
    status, payload = MemoryHubClient(config).context(request, timeout)
    if status == 200:
        store.put_cache(key, payload, (datetime.now(UTC) + timedelta(seconds=config.usable_cache_seconds)).isoformat(), payload.get("freshness", {}).get("latest_event_seq"))
        payload = _compact_injected_context(payload, config.max_injected_tokens)
        return {"status": "fresh", "source": "remote", **payload}
    return ({"status": "stale", "source": "cache", **_compact_injected_context(__import__("json").loads(cached["payload_json"]), config.max_injected_tokens)} if cached else None)


def get_project_graph(config: SharedMemoryConfig, args: dict[str, Any]) -> dict[str, Any] | None:
    if not config.active or not config.read_enabled:
        return None
    request = {
        "task_id": args.get("task_id"),
        "files": args.get("active_files") or args.get("files") or [],
        "classes": args.get("class_names") or args.get("classes") or [],
        "modules": args.get("module_names") or args.get("modules") or [],
        "assets": args.get("asset_paths") or args.get("assets") or [],
        "blueprints": args.get("blueprint_paths") or args.get("blueprints") or [],
        "maps": args.get("map_names") or args.get("maps") or [],
        "plugins": args.get("plugin_names") or args.get("plugins") or [],
        "system_areas": args.get("system_areas") or ([args["system_area"]] if args.get("system_area") else []),
        "depth": min(max(int(args.get("depth") or 1), 0), 2),
        "max_nodes": min(max(int(args.get("max_nodes") or args.get("max_items") or 50), 1), 200),
        "max_edges": min(max(int(args.get("max_edges") or 100), 1), 400),
        "include_metadata": False,
        "include_source_event_ids": False,
    }
    status, payload = MemoryHubClient(config).graph(request, config.active_query_timeout_ms / 1000)
    if status == 200:
        return payload
    return {"status": "unavailable", "error": payload.get("error", "remote_unavailable")}