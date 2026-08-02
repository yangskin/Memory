"""P1-1: bootstrap helpers (v0.6.0 OOTB hardening).

Pure-Python helpers shared by ``bootstrap.ps1`` and unit tests so the
risky parts of OOTB deployment (JSON merging, user-id persistence) are
covered by automated tests rather than living only in shell.

All functions are atomic, idempotent and never destroy existing
unrelated keys.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .memory_users import is_placeholder_user, validate_effective_user
from .memory_config import load_config
from .memory_events import USER_CONFIG_LOCAL_FILENAME


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _posix(path: Path) -> str:
    return str(path).replace("\\", "/")


def _infer_memory_root_from_venv_python(python_exe: str | None) -> Path | None:
    if not python_exe:
        return None
    python_path = Path(python_exe)
    try:
        if python_path.parent.name.lower() == "scripts" and python_path.parent.parent.name.lower() == ".venv":
            return python_path.parent.parent.parent
    except IndexError:
        return None
    return None


def write_user_setting(repo_root: Path, user_name: str) -> dict[str, Any]:
    """Persist ``memory-mcp.userName`` into ``.vscode/settings.json``.

    Returns ``{ok, path, user, created}``. Refuses placeholder names.
    """
    if is_placeholder_user(user_name):
        return {"ok": False, "error": "invalid_user", "user": user_name}

    settings_path = repo_root / ".vscode" / "settings.json"
    existing = _read_json(settings_path)
    created = "memory-mcp.userName" not in existing
    existing["memory-mcp.userName"] = user_name
    _write_json(settings_path, existing)
    return {"ok": True, "path": str(settings_path), "user": user_name, "created": created}


def write_local_user_config(memory_root: Path, user_name: str) -> dict[str, Any]:
    """Persist the stable user id into ``MCP/Memory/user_config.local.json``.

    This is the preferred agent-neutral identity config. It intentionally
    lives beside ``llm_config.local.json`` and must remain gitignored.
    """
    if is_placeholder_user(user_name):
        return {"ok": False, "error": "invalid_user", "user": user_name}

    config_path = memory_root / USER_CONFIG_LOCAL_FILENAME
    existing = _read_json(config_path)
    created = "user_name" not in existing
    existing["user_name"] = user_name
    _write_json(config_path, existing)
    return {"ok": True, "path": str(config_path), "user": user_name, "created": created}


def merge_mcp_json(
    repo_root: Path,
    *,
    server_name: str = "memory-mcp",
    python_exe: str | None = None,
    memory_root: Path | str | None = None,
    args: list[str] | None = None,
) -> dict[str, Any]:
    """Idempotent merge of a memory-mcp entry into ``.vscode/mcp.json``.

    Preserves any existing ``servers`` map and any non-server top-level
    keys (e.g. ``inputs``).
    """
    mcp_path = repo_root / ".vscode" / "mcp.json"
    existing = _read_json(mcp_path)
    servers = existing.get("servers")
    if not isinstance(servers, dict):
        servers = {}

    resolved_memory_root = Path(memory_root) if memory_root is not None else _infer_memory_root_from_venv_python(python_exe)
    entry: dict[str, Any] = {
        "command": python_exe or "python",
        "args": args or ["-m", "servers.memory_server", "--root", "${workspaceFolder}"],
        "type": "stdio",
    }
    if resolved_memory_root is not None:
        entry["env"] = {
            "PYTHONPATH": _posix(resolved_memory_root),
            "PYTHONUTF8": "1",
        }
    servers[server_name] = entry
    existing["servers"] = servers
    _write_json(mcp_path, existing)
    return {"ok": True, "path": str(mcp_path), "server": server_name}


def health_green_light(repo_root: Path) -> dict[str, Any]:
    """Best-effort one-shot validation: load config, validate user.

    Returns ``{ok, checks}``; never raises. ``ok`` is False when
    validation surfaces a structured error (e.g. ``user_not_configured``).
    """
    checks: list[dict[str, Any]] = []
    try:
        config = load_config(repo_root)
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "error": "config_load_failed", "message": str(exc)}

    user_check = validate_effective_user(config)
    if user_check is None:
        # ``None`` is the OK sentinel from validate_effective_user.
        checks.append({"step": "validate_user", "ok": True})
    else:
        checks.append({"step": "validate_user", **user_check})

    return {
        "ok": all(c.get("ok") is True for c in checks),
        "checks": checks,
        "repo_root": str(config.repo_root),
    }
