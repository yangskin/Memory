"""P1-4: config diagnostics (v0.6.0 OOTB hardening).

Reports the *effective* value and *source* of key memory-mcp config
fields so users can answer "why is this setting what it is?" without
reading source.

Source labels:
- ``default`` — dataclass default; no override observed
- ``file``    — set in ``.ai-memory/config.json``
- ``env``     — overridden by environment variable
- ``local``   — derived from ``MCP/Memory/user_config.local.json`` (user id)
- ``vscode``  — derived from ``.vscode/settings.json`` (e.g. user id)

Pure read-only function; never raises.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .memory_config import MemoryConfig
from .memory_events import get_current_user_source


def _read_raw_config(repo_root: Path) -> dict[str, Any]:
    cfg_path = repo_root / ".ai-memory" / "config.json"
    if not cfg_path.is_file():
        return {}
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def config_diagnose(config: MemoryConfig) -> dict[str, Any]:
    """Return a dict of ``{field: {value, source, override_hint}}``."""
    raw = _read_raw_config(config.repo_root)
    mcp_raw = raw.get("mcp") if isinstance(raw.get("mcp"), dict) else {}

    fields: dict[str, dict[str, Any]] = {}

    def report(field: str, value: Any, source: str, hint: str | None = None) -> None:
        entry: dict[str, Any] = {"value": value, "source": source}
        if hint:
            entry["override_hint"] = hint
        fields[field] = entry

    # mcp.allow_unknown_user
    if "allow_unknown_user" in mcp_raw:
        report("mcp.allow_unknown_user", config.mcp_allow_unknown_user, "file",
               "set mcp.allow_unknown_user in .ai-memory/config.json")
    else:
        report("mcp.allow_unknown_user", config.mcp_allow_unknown_user, "default")

    # mcp.shared_overwrite_policy
    if "shared_overwrite_policy" in mcp_raw:
        report("mcp.shared_overwrite_policy", config.mcp_shared_overwrite_policy, "file",
               "values: 'reject' | 'downgrade'")
    else:
        report("mcp.shared_overwrite_policy", config.mcp_shared_overwrite_policy, "default")

    # mcp.auto_maintenance.*
    am_raw = mcp_raw.get("auto_maintenance") if isinstance(mcp_raw.get("auto_maintenance"), dict) else {}
    am_value = config.mcp_auto_maintenance or {}
    report(
        "mcp.auto_maintenance.enabled",
        am_value.get("enabled", True),
        "file" if "enabled" in am_raw else "default",
    )

    # mcp.fsync_strict (env override common)
    fsync_env = os.environ.get("MEMORY_MCP_FSYNC_STRICT")
    if fsync_env is not None:
        report("mcp.fsync_strict", getattr(config, "mcp_fsync_strict", False), "env",
               "unset MEMORY_MCP_FSYNC_STRICT to revert to file/default")
    elif "fsync_strict" in mcp_raw:
        report("mcp.fsync_strict", getattr(config, "mcp_fsync_strict", False), "file")
    else:
        report("mcp.fsync_strict", getattr(config, "mcp_fsync_strict", False), "default")

    # Multi-user policy is always on.  Legacy configs may still contain
    # multi_user.enabled, but the value is ignored and must not reintroduce a
    # single-user execution path.
    mu_raw = raw.get("multi_user") if isinstance(raw.get("multi_user"), dict) else {}
    if "enabled" in mu_raw:
        report(
            "multi_user.enabled_ignored",
            mu_raw.get("enabled"),
            "file",
            "legacy field ignored; multi-user policy is always on",
        )
    report("multi_user.mode", "always_on", "default")

    # Effective user id
    user_source = get_current_user_source(config.repo_root)
    source = user_source.get("source") or "default"
    source_detail = user_source.get("source_detail")
    hint_by_source = {
        "env": "unset MEMORY_MCP_USER to fall through, or edit MCP/Memory/user_config.local.json",
        "local": f"edit {source_detail or 'MCP/Memory/user_config.local.json'}",
        "vscode": "legacy source; prefer MCP/Memory/user_config.local.json",
        "default": "set MEMORY_MCP_USER or MCP/Memory/user_config.local.json",
    }
    report("user.effective", user_source.get("user") or "unknown", source, hint_by_source.get(source))
    if source_detail:
        fields["user.effective"]["source_detail"] = source_detail

    return {
        "ok": True,
        "repo_root": str(config.repo_root),
        "fields": fields,
        "llm_capabilities": _diagnose_llm_capabilities(config, raw),
    }


# ---------------------------------------------------------------------------
# \u00a715.2-D: LLM capability diagnostic
# ---------------------------------------------------------------------------


def _diagnose_llm_capabilities(config: MemoryConfig, raw: dict[str, Any]) -> dict[str, Any]:
    """Return the effective ``enabled / timeout / max_tokens`` for every
    LLM capability registered in :data:`DEFAULT_CAPABILITY_PROFILES`,
    along with the source (default / file / file.capabilities.<name> / env)
    that produced each value.

    This mirrors the precedence walk inside
    :func:`memory_llm_runner.resolve_capability_profile` so users can
    answer "why is this capability disabled?" without reading code.
    """

    try:
        from .memory_llm_runner import (
            DEFAULT_CAPABILITY_PROFILES,
            resolve_capability_profile,
        )
    except Exception as exc:  # pragma: no cover \u2014 defensive
        return {"error": f"runner_unavailable: {exc}"}

    block = raw.get("llm_defaults") if isinstance(raw.get("llm_defaults"), dict) else {}
    cap_block = block.get("capabilities") if isinstance(block.get("capabilities"), dict) else {}

    out: dict[str, Any] = {}
    # Map UI field names to the raw key aliases we accept in
    # ``llm_defaults`` / ``llm_defaults.capabilities.<cap>``.
    _ALIASES = {
        "enabled": ("enabled",),
        "timeout_ms": ("timeout_ms", "timeout"),
        "max_tokens": ("max_tokens", "tokens"),
        "fallback": ("fallback",),
    }
    for cap_name, base in DEFAULT_CAPABILITY_PROFILES.items():
        resolved = resolve_capability_profile(config, cap_name)
        cap_overrides = cap_block.get(cap_name) if isinstance(cap_block.get(cap_name), dict) else {}

        def _source(field: str) -> str:
            for alias in _ALIASES.get(field, (field,)):
                if alias in cap_overrides:
                    return "file"
                if alias in block:
                    return "file"
            return "default"

        out[cap_name] = {
            "enabled": {"value": resolved.enabled, "source": _source("enabled")},
            "timeout_ms": {
                "value": int(resolved.timeout * 1000) if resolved.timeout else None,
                "source": _source("timeout_ms"),
            },
            "max_tokens": {
                "value": resolved.max_tokens,
                "source": _source("max_tokens"),
            },
            "fallback": {
                "value": "deterministic" if cap_name in {
                    "rebuild_key_document", "query_rewrite", "snapshot_narrative",
                } else "in_band_error",
                "source": "default",
            },
            "description": base.description,
        }
    return out
