from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .memory_config import MemoryConfig
from .memory_events import get_current_user
from .memory_locks import file_lock


_STATE_REL = Path(".ai-memory") / "key_documents_auto_rebuild.json"
_ALL_TARGETS = ["activeContext", "teamContext", "progress", "techContext", "systemPatterns"]
_VALID_TARGETS = set(_ALL_TARGETS)

_PHASE_TARGETS: dict[str, list[str]] = {
    "exploration": ["activeContext"],
    "plan_confirmed": ["activeContext", "teamContext", "progress"],
    "implementation": ["activeContext"],
    "test_failed": ["activeContext", "teamContext", "techContext"],
    "test_passed": ["activeContext", "teamContext", "progress"],
    "stable_pattern_found": ["techContext", "systemPatterns"],
    "task_done": list(_ALL_TARGETS),
}

_KIND_TARGETS: dict[str, list[str]] = {
    "decision": ["activeContext", "teamContext", "progress"],
    "incident": ["activeContext", "teamContext", "techContext"],
    "procedure": ["techContext", "systemPatterns"],
    "rule_candidate": ["systemPatterns"],
    "system_rule": ["systemPatterns"],
    "observation": ["activeContext"],
    "note": ["activeContext"],
}

_PHASE_LEVELS: dict[str, str] = {
    "exploration": "shu",
    "plan_confirmed": "fa",
    "implementation": "shu",
    "test_failed": "shu",
    "test_passed": "fa",
    "stable_pattern_found": "dao",
    "task_done": "fa",
}


def _state_path(config: MemoryConfig) -> Path:
    return config.repo_root / _STATE_REL


def _read_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_text_limited(path: Path, limit: int) -> str:
    try:
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _clean_targets(values: Any, *, fallback: list[str]) -> list[str]:
    if not isinstance(values, list):
        return list(fallback)
    cleaned = [str(item).strip() for item in values if str(item).strip() in _VALID_TARGETS]
    return list(dict.fromkeys(cleaned)) or list(fallback)


def _phase_from(write_result: dict[str, Any], phase: str | None) -> str | None:
    if phase:
        return str(phase).strip() or None
    for key in ("task_phase", "phase"):
        raw = write_result.get(key)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return None


def _deterministic_targets(settings: Any, write_result: dict[str, Any], phase: str | None) -> list[str]:
    allowed = _clean_targets(getattr(settings, "targets", None), fallback=_ALL_TARGETS)
    candidates: list[str] = []
    if phase and phase in _PHASE_TARGETS:
        candidates.extend(_PHASE_TARGETS[phase])
    kind = str(write_result.get("record_kind") or "").strip()
    if kind and kind in _KIND_TARGETS:
        candidates.extend(_KIND_TARGETS[kind])
    if not candidates:
        candidates = list(allowed)
    selected = [target for target in dict.fromkeys(candidates) if target in allowed]
    return selected or list(allowed)


def _deterministic_layer(write_result: dict[str, Any], phase: str | None) -> str:
    explicit = str(write_result.get("cognitive_level") or "").strip()
    if explicit in {"dao", "fa", "shu"}:
        return explicit
    if phase and phase in _PHASE_LEVELS:
        return _PHASE_LEVELS[phase]
    kind = str(write_result.get("record_kind") or "").strip()
    if kind in {"procedure", "system_rule", "rule_candidate"}:
        return "dao"
    if kind == "decision":
        return "fa"
    return "shu"


def _user_for_rebuild(config: MemoryConfig, write_result: dict[str, Any]) -> str | None:
    for key in ("author", "user"):
        raw = write_result.get(key)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    current = get_current_user(config.repo_root)
    if current and current != "unknown":
        return current
    return None


def _extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if "\n" in stripped:
            stripped = stripped.split("\n", 1)[1]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        payload = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _llm_gate_decision(
    config: MemoryConfig,
    *,
    settings: Any,
    operation: str,
    write_result: dict[str, Any],
    phase: str | None,
    deterministic_targets: list[str],
    deterministic_layer: str,
) -> dict[str, Any]:
    llm_gate = str(getattr(settings, "llm_gate", "when_available") or "when_available").strip().lower()
    if llm_gate == "off":
        return {
            "used": False,
            "status": "off",
            "should_rebuild": True,
            "targets": deterministic_targets,
            "layer": deterministic_layer,
        }

    from .memory_llm import extract_text
    from .memory_llm_runner import run_llm_capability

    current_task_path = str(write_result.get("current_task_path") or "").strip()
    current_task = ""
    if current_task_path:
        current_task = _read_text_limited(config.repo_root / current_task_path, 2000)
    payload = {
        "operation": operation,
        "phase": phase,
        "write_result": {
            key: write_result.get(key)
            for key in ("id", "path", "record_kind", "scope", "status", "task_id", "branch", "tags")
            if write_result.get(key) is not None
        },
        "deterministic_targets": deterministic_targets,
        "deterministic_layer": deterministic_layer,
        "current_task_excerpt": current_task,
    }
    messages = [
        {
            "role": "system",
            "content": (
                "You are a gate for agent memory settling. Return only compact JSON. "
                "Decide whether the current memory batch is worth settling into derived key documents. "
                "Allowed targets: activeContext, teamContext, progress, techContext, systemPatterns. "
                "Allowed layer values: dao, fa, shu. Never propose raw edits."
            ),
        },
        {
            "role": "user",
            "content": (
                "Input JSON:\n"
                f"{json.dumps(payload, ensure_ascii=False)}\n\n"
                "Return JSON schema: {\"should_rebuild\": boolean, "
                "\"targets\": [string], \"layer\": \"dao|fa|shu\", \"phase\": string, \"reason\": string}."
            ),
        },
    ]

    def _invoke(client, profile):
        response = client.chat(
            messages,
            max_tokens=profile.max_tokens,
            temperature=0,
            thinking=False,
        )
        text = extract_text(response)
        parsed = _extract_json_object(text)
        if parsed is None:
            return {
                "should_rebuild": True,
                "targets": deterministic_targets,
                "layer": deterministic_layer,
                "phase": phase,
                "reason": "llm_output_unparseable",
                "raw_preview": text[:240],
            }
        return parsed

    result = run_llm_capability(
        config,
        "auto_memory_gate",
        _invoke,
        force_enabled=llm_gate in {"when_available", "always"},
    )
    if not result.ok:
        return {
            "used": False,
            "status": result.status,
            "error": result.error,
            "should_rebuild": llm_gate != "always",
            "targets": deterministic_targets,
            "layer": deterministic_layer,
            "fallback_used": True,
        }

    value = result.value if isinstance(result.value, dict) else {}
    targets = _clean_targets(value.get("targets"), fallback=deterministic_targets)
    layer = str(value.get("layer") or deterministic_layer).strip()
    if layer not in {"dao", "fa", "shu"}:
        layer = deterministic_layer
    return {
        "used": True,
        "status": result.status,
        "should_rebuild": bool(value.get("should_rebuild", True)),
        "targets": targets,
        "layer": layer,
        "phase": str(value.get("phase") or phase or ""),
        "reason": str(value.get("reason") or ""),
        "meta": result.meta,
    }


def maybe_auto_rebuild_key_documents(
    config: MemoryConfig,
    *,
    operation: str,
    write_result: dict[str, Any],
    phase: str | None = None,
) -> dict[str, Any]:
    """Optionally settle derived key documents after write/checkpoint events.

    The MCP server cannot observe human chat turns directly, so the default
    trigger counts successful structured memory writes. Agents can also report
    task phases through ``operation='checkpoint'``.
    """

    settings = getattr(config, "key_documents_auto_rebuild", None)
    if settings is None or not settings.enabled:
        return {
            "enabled": False,
            "triggered": False,
            "reason": "disabled",
        }
    if getattr(config, "key_documents_mode", "auto") != "auto":
        return {
            "enabled": True,
            "triggered": False,
            "reason": "key_documents_mode_not_auto",
            "mode": getattr(config, "key_documents_mode", None),
        }
    if not bool(write_result.get("ok")):
        return {
            "enabled": True,
            "triggered": False,
            "reason": "write_not_ok",
        }

    effective_phase = _phase_from(write_result, phase)
    phase_triggers = set(getattr(settings, "phase_triggers", None) or [])
    phase_forces = operation == "checkpoint" and effective_phase in phase_triggers

    count_operations = set(getattr(settings, "count_operations", None) or [])
    if operation not in count_operations and not phase_forces:
        return {
            "enabled": True,
            "triggered": False,
            "reason": "operation_not_counted",
            "operation": operation,
            "count_operations": sorted(count_operations),
            "phase": effective_phase,
        }

    threshold = max(1, int(settings.after_successful_writes or 5))
    deterministic_targets = _deterministic_targets(settings, write_result, effective_phase)
    deterministic_layer = _deterministic_layer(write_result, effective_phase)
    path = _state_path(config)
    with file_lock(config.repo_root, path):
        state = _read_state(path)
        pending = int(state.get("successful_writes_since_rebuild") or 0)
        if not phase_forces:
            pending += 1
        if pending < threshold and not phase_forces:
            state["successful_writes_since_rebuild"] = pending
            state["threshold"] = threshold
            _write_state(path, state)
            return {
                "enabled": True,
                "triggered": False,
                "pending_successful_writes": pending,
                "threshold": threshold,
            }

        gate = _llm_gate_decision(
            config,
            settings=settings,
            operation=operation,
            write_result=write_result,
            phase=effective_phase,
            deterministic_targets=deterministic_targets,
            deterministic_layer=deterministic_layer,
        )
        if not gate.get("should_rebuild", True):
            state["successful_writes_since_rebuild"] = 0
            state["threshold"] = threshold
            state["last_result"] = {
                "ok": True,
                "skipped_by_gate": True,
                "gate": {
                    "used": gate.get("used"),
                    "status": gate.get("status"),
                    "reason": gate.get("reason"),
                },
            }
            _write_state(path, state)
            return {
                "enabled": True,
                "triggered": False,
                "reason": "llm_gate_skipped",
                "pending_successful_writes": pending,
                "threshold": threshold,
                "phase": effective_phase,
                "selected_layer": gate.get("layer") or deterministic_layer,
                "selected_targets": gate.get("targets") or deterministic_targets,
                "gate": gate,
            }

        targets = _clean_targets(gate.get("targets"), fallback=deterministic_targets)
        user = _user_for_rebuild(config, write_result)
        if bool(getattr(settings, "async_enabled", True)):
            from .memory_key_document_jobs import enqueue_key_document_rebuild

            rebuild = enqueue_key_document_rebuild(
                config,
                targets=targets,
                user=user,
                renderer=str(settings.renderer or "deterministic"),
                guard_prefer_llm=bool(getattr(settings, "guard_prefer_llm", False)),
                phase=effective_phase,
                layer=str(gate.get("layer") or deterministic_layer),
                trigger=operation,
                reason=str(gate.get("reason") or "auto_rebuild_triggered"),
            )
            state["successful_writes_since_rebuild"] = 0 if rebuild.get("ok") else pending
        else:
            from .memory_key_documents import rebuild_key_documents

            rebuild = rebuild_key_documents(
                config,
                targets=targets,
                user=user,
                renderer=str(settings.renderer or "deterministic"),
                guard_prefer_llm=bool(getattr(settings, "guard_prefer_llm", False)),
            )
            state["successful_writes_since_rebuild"] = 0 if rebuild.get("ok") else pending
        state["threshold"] = threshold
        state["last_result"] = {
            "ok": bool(rebuild.get("ok")),
            "error": rebuild.get("error"),
            "renderer": rebuild.get("renderer"),
            "request_id": rebuild.get("request_id"),
            "job_id": rebuild.get("job_id"),
            "queued": bool(rebuild.get("queued")),
            "phase": effective_phase,
            "targets": targets,
            "layer": gate.get("layer") or deterministic_layer,
        }
        _write_state(path, state)

    return {
        "enabled": True,
        "triggered": True,
        "pending_successful_writes": pending,
        "threshold": threshold,
        "phase": effective_phase,
        "selected_layer": gate.get("layer") or deterministic_layer,
        "selected_targets": targets,
        "gate": gate,
        "mode": "async" if bool(getattr(settings, "async_enabled", True)) else "sync",
        "rebuild": rebuild,
    }
