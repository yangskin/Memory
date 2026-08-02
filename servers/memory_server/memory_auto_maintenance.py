"""P0-3: startup auto-maintenance (v0.6.0 OOTB hardening).

``run_if_due(config)`` is meant to be called once at MCP server boot.
It checks ``.ai-memory/last_maintenance.json`` against a small set of
threshold checks and runs the corresponding maintenance functions
(``memory_health_check``, ``memory_rebuild_index``) only when due.
All steps are idempotent and best-effort: a single failure is logged
to ``events.jsonl`` but never raises.

Design intent (see MemorySystemDesignDocument §15.9.2):

- 普通用户开箱即用：从不需要手工跑 maintenance。
- 失败必须不阻塞主链路：任何 step 异常都写入审计 log，函数本身始终返回。
- 完全可关：``mcp.auto_maintenance.enabled=false`` 跳过。
"""

from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .memory_config import MemoryConfig
from .memory_events import append_event

# Default thresholds tuned for "set-and-forget" teams.
DEFAULT_MIN_INTERVAL_SECONDS: int = 7 * 24 * 60 * 60   # 168 hours
DEFAULT_EVENTS_MAX_BYTES: int = 50 * 1024 * 1024       # 50 MB
DEFAULT_INDEX_STALE_SECONDS: int = 600                  # 10 minutes


@dataclass(frozen=True)
class AutoMaintenanceConfig:
    enabled: bool = True
    min_interval_seconds: int = DEFAULT_MIN_INTERVAL_SECONDS
    events_max_bytes: int = DEFAULT_EVENTS_MAX_BYTES
    index_stale_seconds: int = DEFAULT_INDEX_STALE_SECONDS
    retention_enabled: bool = True


def _resolve_settings(config: MemoryConfig) -> AutoMaintenanceConfig:
    """Pull auto-maintenance settings out of the loaded config object.

    Tolerates missing fields by falling back to defaults so that the
    feature works on legacy ``.ai-memory/config.json`` files too.
    """
    raw = getattr(config, "mcp_auto_maintenance", None) or {}
    if not isinstance(raw, dict):
        raw = {}
    return AutoMaintenanceConfig(
        enabled=bool(raw.get("enabled", True)),
        min_interval_seconds=int(raw.get("min_interval_seconds", DEFAULT_MIN_INTERVAL_SECONDS)),
        events_max_bytes=int(raw.get("events_max_bytes", DEFAULT_EVENTS_MAX_BYTES)),
        index_stale_seconds=int(raw.get("index_stale_seconds", DEFAULT_INDEX_STALE_SECONDS)),
        retention_enabled=bool((raw.get("retention") or {}).get("enabled", True)) if isinstance(raw.get("retention"), dict) else True,
    )


def _state_path(config: MemoryConfig) -> Path:
    return config.repo_root / ".ai-memory" / "last_maintenance.json"


def _read_state(config: MemoryConfig) -> dict[str, Any]:
    path = _state_path(config)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(config: MemoryConfig, state: dict[str, Any]) -> None:
    path = _state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        # Never let state-write failure break the call site.
        pass


def _events_size_bytes(config: MemoryConfig) -> int:
    try:
        return config.events_file.stat().st_size
    except OSError:
        return 0


def _index_is_stale(config: MemoryConfig, threshold_seconds: int) -> bool:
    """Return True when the SQLite index appears older than the newest
    record source by more than ``threshold_seconds``.

    Conservative: when index is missing, declare stale (so first boot
    will rebuild).
    """
    index_path = config.repo_root / ".ai-memory" / "search.db"
    if not index_path.exists():
        return True
    try:
        index_mtime = index_path.stat().st_mtime
    except OSError:
        return True
    memory_bank = config.repo_root / "memory-bank"
    if not memory_bank.exists():
        return False
    newest = 0.0
    try:
        for child in memory_bank.rglob("*.md"):
            try:
                mt = child.stat().st_mtime
            except OSError:
                continue
            if mt > newest:
                newest = mt
    except OSError:
        return False
    return (newest - index_mtime) > threshold_seconds


def _guard_needs_optimization(config: MemoryConfig) -> bool:
    """Return True when any configured guard target or total budget is over.

    This intentionally ignores the maintenance interval: context overflow is
    a correctness issue for agent bootstrapping, so it should be repaired as
    soon as it is detected.
    """
    try:
        from .memory_guard import memory_guard_check

        result = memory_guard_check(config)
    except Exception:
        return False
    stats = result.get("stats") if isinstance(result, dict) else None
    if isinstance(stats, dict):
        if int(stats.get("exceeded", 0) or 0) > 0:
            return True
    items_exceeded = any(
        item.get("status") == "exceeded"
        for item in (result.get("items") or result.get("targets") or [])
    )
    if items_exceeded:
        return True
    total = result.get("total_budget")
    return isinstance(total, dict) and total.get("status") == "exceeded"


def _retention_needed(config: MemoryConfig) -> bool:
    try:
        from .memory_baseline import detect_regressions

        report = detect_regressions(config)
    except Exception:
        return False
    for regression in report.get("regressions", []):
        if regression.get("metric") == "memory_bank_total_bytes":
            return True
    return False


def _decide_actions(
    config: MemoryConfig,
    settings: AutoMaintenanceConfig,
    state: dict[str, Any],
    now: float,
) -> dict[str, bool]:
    """Pure decision function: which maintenance steps are due."""
    last_run = float(state.get("last_run_ts", 0.0))
    interval_due = (now - last_run) >= settings.min_interval_seconds
    events_oversize = _events_size_bytes(config) > settings.events_max_bytes
    index_stale = _index_is_stale(config, settings.index_stale_seconds)
    guard_exceeded = _guard_needs_optimization(config)
    retention = settings.retention_enabled and (interval_due or _retention_needed(config))
    return {
        "health_check": interval_due,
        "rebuild_index": interval_due or index_stale,
        "rotate_events": events_oversize,
        "guard_optimize": guard_exceeded,
        "retention": retention,
        "any": interval_due or events_oversize or index_stale or guard_exceeded or retention,
    }


def run_if_due(config: MemoryConfig, *, now: float | None = None) -> dict[str, Any]:
    """Run due maintenance actions; return a structured report.

    Always returns a dict with ``ok`` (bool) and ``actions`` (list of
    per-step records). Never raises. Safe to call from MCP server
    startup, CLI, or tests.
    """
    settings = _resolve_settings(config)
    if not settings.enabled:
        return {"ok": True, "skipped": True, "reason": "disabled", "actions": []}

    state = _read_state(config)
    current_ts = now if now is not None else time.time()
    decisions = _decide_actions(config, settings, state, current_ts)

    actions: list[dict[str, Any]] = []

    if not decisions["any"]:
        return {
            "ok": True,
            "skipped": True,
            "reason": "not_due",
            "actions": actions,
            "decisions": decisions,
        }

    # Lazy import to avoid circular dependency at module load time.
    from .memory_maintenance import memory_health_check
    from .memory_record_index import memory_rebuild_index
    from .memory_guard_optimizer import optimize_guard_targets
    from .memory_retention import apply_retention

    if decisions["health_check"]:
        actions.append(_safe_run("health_check", lambda: memory_health_check(config)))

    if decisions["rebuild_index"]:
        actions.append(_safe_run("rebuild_index", lambda: memory_rebuild_index(config)))

    if decisions.get("guard_optimize"):
        # 后台维护必须可复现且不依赖外部模型，避免 LLM 输出异常污染关键文档。
        actions.append(_safe_run("guard_optimize", lambda: optimize_guard_targets(config, prefer_llm=False)))

    if decisions.get("retention"):
        actions.append(_safe_run("retention", lambda: apply_retention(config)))

    # NOTE: events rotation is done implicitly by `append_event` itself
    # (`_rotate_events_if_needed`); we surface a no-op record so the
    # report is honest about what was checked.
    if decisions["rotate_events"]:
        actions.append({"step": "rotate_events", "ok": True, "note": "delegated to append_event"})

    state.update(
        {
            "last_run_ts": current_ts,
            "last_run_iso": _iso(current_ts),
            "decisions": decisions,
            "actions_summary": [{"step": a["step"], "ok": a.get("ok", True)} for a in actions],
        }
    )
    _write_state(config, state)

    try:
        from .memory_strategy_hash import current_strategy_hash

        append_event(
            config,
            event_type="auto_maintenance",
            payload={
                "decisions": decisions,
                "actions": state["actions_summary"],
                "scoring_strategy_hash": current_strategy_hash(),
            },
            status="ok",
        )
    except Exception:  # pragma: no cover — audit log must never raise
        pass

    return {"ok": True, "actions": actions, "decisions": decisions}


def _safe_run(step: str, fn) -> dict[str, Any]:
    try:
        result = fn()
        ok = bool(result.get("ok", True)) if isinstance(result, dict) else True
        return {"step": step, "ok": ok, "result": result if isinstance(result, dict) else None}
    except Exception as exc:  # pragma: no cover (covered indirectly)
        return {
            "step": step,
            "ok": False,
            "error": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(limit=3),
        }


def _iso(ts: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
