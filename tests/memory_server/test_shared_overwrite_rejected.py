"""P0-2: shared file overwrite strict-reject (v0.6.0 OOTB hardening).

Scope:
- ``memory_write`` MUST return a structured ``shared_overwrite_forbidden``
  error when the target hits an ``append_only`` shared-paths policy and
  the caller asked for ``mode="overwrite"``.
- The error result must:
  - set ``ok=False``
  - include a ``suggested_operation`` hint (``record`` for structured
    intent, or ``append`` for raw text)
  - NOT write to disk, NOT take a backup, NOT acquire a lock
  - propagate the original ``request_id`` if the caller provided one
- Legacy "silent downgrade" remains available behind
  ``mcp.shared_overwrite_policy="downgrade"`` for back-compat.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_writer import memory_write


def _bootstrap(tmp_path: Path, *, overwrite_policy: str | None = None) -> object:
    (tmp_path / "memory-bank").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai-memory").mkdir(parents=True, exist_ok=True)
    (tmp_path / "memory-bank" / "progress.md").write_text(
        "# Progress\n- seed\n", encoding="utf-8"
    )
    cfg: dict = {"allowed_roots": ["memory-bank"]}
    if overwrite_policy is not None:
        cfg.setdefault("mcp", {})["shared_overwrite_policy"] = overwrite_policy
    (tmp_path / ".ai-memory" / "config.json").write_text(
        json.dumps(cfg), encoding="utf-8"
    )
    return load_config(tmp_path)


def test_overwrite_on_append_only_path_is_rejected_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USERNAME", "alice")
    config = _bootstrap(tmp_path)
    original = (tmp_path / "memory-bank/progress.md").read_text(encoding="utf-8")

    result = memory_write(
        config,
        "memory-bank/progress.md",
        "# REPLACED\n",
        mode="overwrite",
        backup=False,
    )

    assert result["ok"] is False
    assert result["error"] == "shared_overwrite_forbidden"
    # Hint must steer caller to a safe alternative.
    assert result["suggested_operation"] in {"record", "append"}
    # Disk untouched.
    assert (tmp_path / "memory-bank/progress.md").read_text(encoding="utf-8") == original
    # No backup directory side-effects.
    backups_dir = tmp_path / ".ai-memory" / "backups"
    if backups_dir.exists():
        # Must be empty (no batch created for this rejected write).
        assert not any(backups_dir.iterdir())


def test_rejection_carries_request_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USERNAME", "alice")
    config = _bootstrap(tmp_path)

    result = memory_write(
        config,
        "memory-bank/progress.md",
        "# REPLACED\n",
        mode="overwrite",
        backup=False,
        request_id="rid-test-123",
    )

    assert result["ok"] is False
    assert result["error"] == "shared_overwrite_forbidden"
    assert result["request_id"] == "rid-test-123"


def test_append_on_append_only_path_still_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USERNAME", "alice")
    config = _bootstrap(tmp_path)

    result = memory_write(
        config,
        "memory-bank/progress.md",
        "- new line\n",
        mode="append",
        backup=False,
    )

    assert result["ok"] is True
    text = (tmp_path / "memory-bank/progress.md").read_text(encoding="utf-8")
    assert "- seed" in text
    assert "- new line" in text


def test_legacy_downgrade_policy_still_works_when_opted_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USERNAME", "alice")
    config = _bootstrap(tmp_path, overwrite_policy="downgrade")

    result = memory_write(
        config,
        "memory-bank/progress.md",
        "- legacy update\n",
        mode="overwrite",
        backup=False,
    )

    assert result["ok"] is True
    assert result["mode"] == "append"
    assert result.get("policy_override") == "append_only"
    text = (tmp_path / "memory-bank/progress.md").read_text(encoding="utf-8")
    assert "- seed" in text
    assert "- legacy update" in text
