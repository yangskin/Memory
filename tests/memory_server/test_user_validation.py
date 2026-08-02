"""P0-1: user id strict validation (v0.6.0 OOTB hardening).

Scope:
- ``is_placeholder_user`` rejects empty / whitespace / literal "unknown"
  variants and names containing path-injection characters.
- ``is_ambiguous_user`` flags common shared admin names (warning, not block).
- ``validate_effective_user`` returns structured error when placeholder
  detected and config does not opt out via ``mcp.allow_unknown_user``.
- Facade entry points (``memory_write`` / ``memory_write_record``) refuse
  to write when validation fails; the failure surfaces as
  ``error="user_not_configured"`` with a ``setup_hint``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_users import (
    is_ambiguous_user,
    is_placeholder_user,
    validate_effective_user,
)
from servers.memory_server.memory_writer import memory_write


# ---------------------------------------------------------------------------
# pure-function level
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["", " ", "\t", "unknown", "Unknown", "UNKNOWN", "a/b", "a\\b", "a:b", "a\nb", "a\rb", "a\0b"],
)
def test_is_placeholder_user_rejects_invalid(name: str) -> None:
    assert is_placeholder_user(name) is True


@pytest.mark.parametrize("name", ["carol", "alice", "bob123", "user.surname", "中文用户", "dev-1"])
def test_is_placeholder_user_accepts_valid(name: str) -> None:
    assert is_placeholder_user(name) is False


@pytest.mark.parametrize("name", ["Administrator", "User", "admin", "root", "guest", "default"])
def test_is_ambiguous_user_flags_common_shared_names(name: str) -> None:
    assert is_ambiguous_user(name) is True
    # ambiguous != placeholder; ambiguous still passes hard validation.
    assert is_placeholder_user(name) is False


@pytest.mark.parametrize("name", ["carol", "alice"])
def test_is_ambiguous_user_passes_personal_names(name: str) -> None:
    assert is_ambiguous_user(name) is False


# ---------------------------------------------------------------------------
# config-level
# ---------------------------------------------------------------------------


def _bootstrap_config(tmp_path: Path, mcp_overrides: dict | None = None) -> object:
    (tmp_path / "memory-bank").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai-memory").mkdir(parents=True, exist_ok=True)
    cfg = {"allowed_roots": ["memory-bank"]}
    if mcp_overrides is not None:
        cfg["mcp"] = mcp_overrides
    (tmp_path / ".ai-memory" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    return load_config(tmp_path)


def test_validate_effective_user_blocks_placeholder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.delenv("USER", raising=False)
    config = _bootstrap_config(tmp_path)

    err = validate_effective_user(config)
    assert err is not None
    assert err["error"] == "user_not_configured"
    assert "setup_hint" in err
    assert "user_config.local.json" in err["setup_hint"]


def test_validate_effective_user_accepts_real_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USERNAME", "alice")
    config = _bootstrap_config(tmp_path)

    assert validate_effective_user(config) is None


def test_validate_effective_user_allows_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.delenv("USER", raising=False)
    config = _bootstrap_config(tmp_path, mcp_overrides={"allow_unknown_user": True})

    assert validate_effective_user(config) is None


def test_validate_effective_user_warns_on_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USERNAME", "Administrator")
    config = _bootstrap_config(tmp_path)

    err = validate_effective_user(config)
    # ambiguous must NOT block; result is None or carries a warning marker.
    assert err is None or err.get("warning") == "user_ambiguous"


# ---------------------------------------------------------------------------
# facade-level
# ---------------------------------------------------------------------------


def test_memory_write_blocked_when_user_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.delenv("USER", raising=False)
    config = _bootstrap_config(tmp_path)

    result = memory_write(config, path="memory-bank/note.md", content="# hi\nbody\n")
    assert result.get("ok") is False
    assert result.get("error") == "user_not_configured"
    assert "setup_hint" in result
    # No file written.
    assert not (tmp_path / "memory-bank" / "note.md").exists()


def test_memory_write_succeeds_with_real_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USERNAME", "alice")
    config = _bootstrap_config(tmp_path)

    result = memory_write(config, path="memory-bank/note.md", content="# hi\nbody\n")
    assert result.get("ok") is True
    assert (tmp_path / "memory-bank" / "note.md").exists()


# ---------------------------------------------------------------------------
# v0.10.1 — MEMORY_MCP_USER env override (CI / subprocess / tests)
# ---------------------------------------------------------------------------


def test_memory_mcp_user_env_overrides_local_and_vscode_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``MEMORY_MCP_USER`` is the highest-priority source so CI / tests /
    detached subprocesses can inject a stable user without touching
    ``.vscode/settings.json``."""
    from servers.memory_server.memory_events import _vscode_user_cache, get_current_user

    _vscode_user_cache.clear()
    memory_root = tmp_path / "MCP" / "Memory"
    memory_root.mkdir(parents=True, exist_ok=True)
    (memory_root / "user_config.local.json").write_text(
        json.dumps({"user_name": "from-local"}), encoding="utf-8"
    )
    settings_dir = tmp_path / ".vscode"
    settings_dir.mkdir(parents=True, exist_ok=True)
    (settings_dir / "settings.json").write_text(
        json.dumps({"memory-mcp.userName": "from-vscode"}), encoding="utf-8"
    )
    monkeypatch.setenv("USERNAME", "from-os")
    monkeypatch.setenv("MEMORY_MCP_USER", "from-env")

    assert get_current_user(tmp_path) == "from-env"


def test_local_user_config_overrides_vscode_and_os(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from servers.memory_server.memory_events import _local_user_cache, _vscode_user_cache, get_current_user

    _local_user_cache.clear()
    _vscode_user_cache.clear()
    memory_root = tmp_path / "MCP" / "Memory"
    memory_root.mkdir(parents=True, exist_ok=True)
    (memory_root / "user_config.local.json").write_text(
        json.dumps({"user_name": "from-local"}), encoding="utf-8"
    )
    settings_dir = tmp_path / ".vscode"
    settings_dir.mkdir(parents=True, exist_ok=True)
    (settings_dir / "settings.json").write_text(
        json.dumps({"memory-mcp.userName": "from-vscode"}), encoding="utf-8"
    )
    monkeypatch.setenv("USERNAME", "from-os")

    assert get_current_user(tmp_path) == "from-local"


def test_memory_mcp_user_env_unblocks_unconfigured_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In CI / subprocess scenarios neither ``.vscode/settings.json`` nor
    OS account name may be reliable; ``MEMORY_MCP_USER`` alone must be
    enough to satisfy ``validate_effective_user``."""
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.setenv("MEMORY_MCP_USER", "ci-runner")
    config = _bootstrap_config(tmp_path)

    assert validate_effective_user(config) is None


def test_memory_mcp_user_env_blank_falls_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whitespace-only ``MEMORY_MCP_USER`` must NOT mask a valid lower-tier
    source (otherwise an empty CI variable would silently break user-id
    resolution)."""
    from servers.memory_server.memory_events import _local_user_cache, _vscode_user_cache, get_current_user

    _local_user_cache.clear()
    _vscode_user_cache.clear()
    memory_root = tmp_path / "MCP" / "Memory"
    memory_root.mkdir(parents=True, exist_ok=True)
    (memory_root / "user_config.local.json").write_text(
        json.dumps({"user_name": "from-local"}), encoding="utf-8"
    )
    monkeypatch.setenv("MEMORY_MCP_USER", "   ")
    monkeypatch.setenv("USERNAME", "from-os")

    assert get_current_user(tmp_path) == "from-local"
