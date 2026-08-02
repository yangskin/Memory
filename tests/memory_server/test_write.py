"""Tests for memory_write tool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_writer import memory_write


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """Set up a minimal repo with memory-bank and .ai-context dirs."""
    _write(tmp_path / "memory-bank/notes.md", "# Notes\n\nOriginal content.\n")
    _write(tmp_path / "memory-bank/activeContext.md", "# Active\n## Sprint\n- item A\n")
    _write(tmp_path / ".ai-context/current-task.md", "# Task\n- build MVP\n")
    (tmp_path / ".ai-memory/backups").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai-memory/temp").mkdir(parents=True, exist_ok=True)
    _write(tmp_path / ".ai-memory/events.jsonl", "")

    config_data = {
        "allowed_roots": [".ai-context", "memory-bank"],
        "excluded_dirs": [],
        "max_file_size_bytes": 1048576,
        "skip_binary_files": True,
        "events_file": ".ai-memory/events.jsonl",
        "backups_dir": ".ai-memory/backups",
        "temp_dir": ".ai-memory/temp",
        "guard": {
            "default_max_chars": 12000,
            "default_max_tokens": 3000,
            "targets": [
                {"path": "memory-bank/notes.md", "max_chars": 100, "policy": "warm_context"},
                {"path": "memory-bank/activeContext.md", "max_chars": 8000, "policy": "warm_context"},
                {"path": ".ai-context/current-task.md", "max_chars": 6000, "policy": "hot_task"},
            ],
        },
    }
    _write(tmp_path / ".ai-memory/config.json", json.dumps(config_data, ensure_ascii=False, indent=2))
    return tmp_path


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _load(repo: Path):
    return load_config(str(repo))


# ── Overwrite mode ──────────────────────────────────────────────────────

def test_overwrite_existing_file(repo: Path) -> None:
    config = _load(repo)
    result = memory_write(config, "memory-bank/notes.md", "# Updated\nNew content.\n")
    assert result["ok"] is True
    assert result["mode"] == "overwrite"
    assert result["created"] is False
    # Verify file content (overwrite 模式会注入用户标签尾注)
    text = (repo / "memory-bank/notes.md").read_text(encoding="utf-8")
    assert "# Updated\nNew content." in text
    assert "<!-- last overwritten by" in text
    # Verify backup was created
    assert result.get("backup_batch_id") is not None


def test_overwrite_creates_file_if_missing(repo: Path) -> None:
    config = _load(repo)
    result = memory_write(config, "memory-bank/new_file.md", "# New File\nHello.\n")
    assert result["ok"] is True
    assert result["created"] is True
    assert result["backup_batch_id"] is None  # no backup for new file
    text = (repo / "memory-bank/new_file.md").read_text(encoding="utf-8")
    assert "# New File\nHello." in text
    assert "<!-- last overwritten by" in text


def test_overwrite_empty_content_rejected(repo: Path) -> None:
    config = _load(repo)
    result = memory_write(config, "memory-bank/notes.md", "")
    assert result["ok"] is False
    assert result["error"] == "invalid_input"


# ── Append mode ─────────────────────────────────────────────────────────

def test_append_mode(repo: Path) -> None:
    config = _load(repo)
    result = memory_write(config, "memory-bank/notes.md", "- appended line", mode="append")
    assert result["ok"] is True
    assert result["mode"] == "append"
    text = (repo / "memory-bank/notes.md").read_text(encoding="utf-8")
    assert "Original content." in text
    assert "- appended line" in text


def test_append_empty_content_allowed(repo: Path) -> None:
    """Appending empty string is allowed (no-op append)."""
    config = _load(repo)
    result = memory_write(config, "memory-bank/notes.md", "", mode="append")
    assert result["ok"] is True


# ── Security ────────────────────────────────────────────────────────────

def test_path_outside_allowed_roots_rejected(repo: Path) -> None:
    config = _load(repo)
    result = memory_write(config, "Source/secret.cpp", "hacked")
    assert result["ok"] is False
    assert result["error"] == "path_not_allowed"


def test_invalid_mode_rejected(repo: Path) -> None:
    config = _load(repo)
    result = memory_write(config, "memory-bank/notes.md", "content", mode="delete")
    assert result["ok"] is False
    assert result["error"] == "invalid_input"


# ── Backup control ──────────────────────────────────────────────────────

def test_write_without_backup(repo: Path) -> None:
    config = _load(repo)
    result = memory_write(config, "memory-bank/notes.md", "# No backup\n", backup=False)
    assert result["ok"] is True
    assert result["backup_batch_id"] is None


# ── Guard warning ───────────────────────────────────────────────────────

def test_guard_warning_on_capacity_exceeded(repo: Path) -> None:
    config = _load(repo)
    # notes.md has max_chars=100, write enough to exceed
    big_content = "# Big\n" + "x" * 120 + "\n"
    result = memory_write(config, "memory-bank/notes.md", big_content)
    assert result["ok"] is True
    assert result["guard_warning"] is not None
    assert "max_chars" in result["guard_warning"]


def test_no_guard_warning_when_within_limit(repo: Path) -> None:
    config = _load(repo)
    result = memory_write(config, "memory-bank/activeContext.md", "# Small\nOK.\n")
    assert result["ok"] is True
    assert result["guard_warning"] is None


# ── Audit event ─────────────────────────────────────────────────────────

def test_audit_event_logged(repo: Path) -> None:
    config = _load(repo)
    memory_write(config, "memory-bank/notes.md", "# Audited\n", reason="test write")
    events_text = (repo / ".ai-memory/events.jsonl").read_text(encoding="utf-8")
    lines = [l for l in events_text.strip().splitlines() if l.strip()]
    # Should have at least backup event + write event
    write_events = [json.loads(l) for l in lines if "memory_write" in l]
    assert len(write_events) >= 1
    assert write_events[-1]["payload"]["reason"] == "test write"
    assert write_events[-1]["payload"]["mode"] == "overwrite"


# ── Create if missing = False ───────────────────────────────────────────

def test_create_if_missing_false_rejects_new_file(repo: Path) -> None:
    config = _load(repo)
    result = memory_write(
        config, "memory-bank/nonexistent.md", "content",
        create_if_missing=False,
    )
    assert result["ok"] is False
    assert result["error"] == "not_found"


# ── Newline normalization ───────────────────────────────────────────────

def test_trailing_newline_ensured(repo: Path) -> None:
    config = _load(repo)
    result = memory_write(config, "memory-bank/notes.md", "no trailing newline")
    assert result["ok"] is True
    text = (repo / "memory-bank/notes.md").read_text(encoding="utf-8")
    assert text.endswith("\n")


# ── User-tag injection control ──────────────────────────────────────────

def test_inject_user_tag_default_skips_non_markdown(repo: Path) -> None:
    """JSON / non-Markdown files must NOT receive the HTML user-tag comment."""
    config = _load(repo)
    json_payload = '{"key": "value"}'
    result = memory_write(config, "memory-bank/notes.json", json_payload)
    assert result["ok"] is True
    saved = (repo / "memory-bank/notes.json").read_text(encoding="utf-8")
    assert "<!--" not in saved
    # Round-trips as valid JSON.
    import json as _json
    assert _json.loads(saved) == {"key": "value"}


def test_inject_user_tag_default_marks_markdown(repo: Path) -> None:
    config = _load(repo)
    result = memory_write(config, "memory-bank/notes.md", "# Hello\n")
    assert result["ok"] is True
    saved = (repo / "memory-bank/notes.md").read_text(encoding="utf-8")
    assert "<!-- last overwritten by" in saved


def test_inject_user_tag_can_be_force_disabled(repo: Path) -> None:
    config = _load(repo)
    result = memory_write(
        config,
        "memory-bank/notes.md",
        "# Hello\n",
        inject_user_tag=False,
    )
    assert result["ok"] is True
    saved = (repo / "memory-bank/notes.md").read_text(encoding="utf-8")
    assert "<!--" not in saved
