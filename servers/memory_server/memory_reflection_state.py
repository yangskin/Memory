from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .memory_config import MemoryConfig
from .memory_llm import LOCAL_CONFIG_FILENAME, PLUGIN_ROOT
from .memory_locks import file_lock
from .memory_record_io import _atomic_write_text
from .memory_request_id import content_sha
from .memory_result import error_result, ok_result

REFLECTION_VERSION = "reflection-v3-durable-result-gate"


def reflection_state_path(config: MemoryConfig, task_id: str) -> Path:
    return config.repo_root / ".ai-memory" / "reflection-state" / f"{content_sha(task_id)}.json"


def read_reflection_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1}
    state = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    if not isinstance(state, dict) or state.get("version") != 1:
        raise ValueError("reflection evidence state has an unsupported schema")
    for key in ("processed", "blocked"):
        entry = state.get(key)
        if entry is not None and (
            not isinstance(entry, dict)
            or not isinstance(entry.get("record_versions"), dict)
            or not isinstance(entry.get("signature"), str)
        ):
            raise ValueError(f"reflection evidence state has invalid {key} data")
    pending = state.get("pending_results", {})
    if not isinstance(pending, dict):
        raise ValueError("reflection pending results must be an object")
    entries = list(pending.values()) + ([state["processed"]] if state.get("processed") else [])
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("signature"), str):
            raise ValueError("reflection pending result entry is invalid")
        result = entry.get("result")
        if result is not None and (not isinstance(result, dict) or not result.get("ok")):
            raise ValueError("reflection cached success result is invalid")
        if entry in pending.values() and result is None:
            raise ValueError("reflection pending result has no recoverable payload")
    return state


def acknowledge_reflection_delivery(config: MemoryConfig, *, task_id: str,
                                    evidence_hash: str | None) -> dict[str, Any]:
    if not evidence_hash:
        return ok_result("no reflection evidence delivery to acknowledge", skipped=True)
    path = reflection_state_path(config, task_id)
    signature = content_sha(f"{REFLECTION_VERSION}:{evidence_hash}")
    try:
        with file_lock(config.repo_root, path):
            state = read_reflection_state(path)
            processed = state.get("processed") or {}
            if processed.get("signature") == signature:
                processed["delivered"] = True
            state["pending_results"] = {
                job_id: entry for job_id, entry in state.get("pending_results", {}).items()
                if entry.get("signature") != signature
            }
            save_reflection_state(config, path, state)
        return ok_result("reflection result delivery acknowledged")
    except (OSError, UnicodeError, ValueError) as exc:
        return error_result("reflection_delivery_ack_failed", f"reflection delivery acknowledgement failed: {type(exc).__name__}")


def reflection_config_revision(config: MemoryConfig) -> str:
    local_path = PLUGIN_ROOT / LOCAL_CONFIG_FILENAME
    local_digest = content_sha(local_path.read_text(encoding="utf-8")) if local_path.exists() else "missing"
    environment = {
        key: value for key, value in os.environ.items()
        if key.startswith("MEMORY_LLM_") or key in {"DEEPSEEK_API_KEY", "OPENAI_API_KEY"}
    }
    return content_sha(json.dumps({
        "project_config": config.config_hash,
        "llm_config": local_digest,
        "environment_revision": content_sha(json.dumps(environment, sort_keys=True)),
        "reflection_version": REFLECTION_VERSION,
    }, sort_keys=True))


def save_reflection_state(config: MemoryConfig, path: Path, state: dict[str, Any]) -> None:
    state["version"] = 1
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_text(path, json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2),
                       fsync_strict=config.mcp_fsync_strict)
