"""Tests for backup rotation, global budget, and dynamic tool descriptions."""

from __future__ import annotations

import json
from pathlib import Path

from servers.memory_server.memory_backup import backup_files, _list_backup_files, _rotate_backups
from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_guard import memory_guard_check, check_total_budget
from servers.memory_server.memory_writer import memory_write
from servers.memory_server.server import _build_tools, _build_file_roles


# ── Backup rotation ─────────────────────────────────────────────────────

def _make_backup_file(backups_dir: Path, name: str, content: str = "x" * 100) -> Path:
    """Helper: create a fake backup file."""
    backups_dir.mkdir(parents=True, exist_ok=True)
    f = backups_dir / name
    f.write_text(content, encoding="utf-8")
    return f


def test_rotate_by_max_files(repo: Path) -> None:
    """When max_batches (=max_files) is exceeded, oldest backup files are removed."""
    config = load_config(repo)
    from dataclasses import replace as dc_replace
    config2 = dc_replace(config, backup_max_batches=2)
    _make_backup_file(config2.backups_dir, "backup-001.md", "aaa")
    _make_backup_file(config2.backups_dir, "backup-002.md", "bbb")
    _make_backup_file(config2.backups_dir, "backup-003.md", "ccc")

    result = _rotate_backups(config2)
    assert result is not None
    assert result["removed_count"] == 1
    remaining = _list_backup_files(config2.backups_dir)
    assert len(remaining) == 2
    names = [f.name for f in remaining]
    assert "backup-001.md" not in names
    assert "backup-002.md" in names
    assert "backup-003.md" in names


def test_rotate_by_total_bytes(repo: Path) -> None:
    """When total backup size exceeds max_total_bytes, oldest removed."""
    config = load_config(repo)
    from dataclasses import replace as dc_replace
    config2 = dc_replace(config, backup_max_total_bytes=250, backup_max_batches=None)
    _make_backup_file(config2.backups_dir, "backup-001.md", "a" * 100)
    _make_backup_file(config2.backups_dir, "backup-002.md", "b" * 100)
    _make_backup_file(config2.backups_dir, "backup-003.md", "c" * 100)

    result = _rotate_backups(config2)
    assert result is not None
    assert result["removed_count"] >= 1
    remaining = _list_backup_files(config2.backups_dir)
    total_size = sum(f.stat().st_size for f in remaining)
    assert total_size <= 250


def test_rotate_not_needed(repo: Path) -> None:
    """No rotation when within limits."""
    config = load_config(repo)
    result = _rotate_backups(config)
    assert result is None


def test_backup_triggers_rotation(repo: Path) -> None:
    """backup_files returns rotation info when rotation occurs."""
    config = load_config(repo)
    from dataclasses import replace as dc_replace
    config2 = dc_replace(config, backup_max_batches=2)
    _make_backup_file(config2.backups_dir, "backup-001.md", "old1")
    _make_backup_file(config2.backups_dir, "backup-002.md", "old2")

    result = backup_files(config2, ["memory-bank/notes.md"])
    assert result["ok"] is True
    # After backup, rotation should keep file count within limit
    remaining = _list_backup_files(config2.backups_dir)
    assert len(remaining) <= 2


def test_backup_file_split_on_size(repo: Path) -> None:
    """When backup file exceeds max_file_bytes, a new file is created."""
    config = load_config(repo)
    from dataclasses import replace as dc_replace
    # Set very small max to force split
    config2 = dc_replace(config, backup_max_file_bytes=50, backup_max_batches=None, backup_max_total_bytes=None)
    backup_files(config2, ["memory-bank/notes.md"], reason="first")
    backup_files(config2, ["memory-bank/notes.md"], reason="second")

    files = _list_backup_files(config2.backups_dir)
    assert len(files) >= 2, f"Expected split but got {len(files)} file(s)"


# ── Global budget ────────────────────────────────────────────────────────

def test_guard_check_returns_total_budget(repo: Path) -> None:
    """memory_guard_check now includes total_budget in response."""
    config = load_config(repo)
    result = memory_guard_check(config)
    assert result["ok"] is True
    assert "total_budget" in result
    tb = result["total_budget"]
    assert "total_chars" in tb
    assert "total_tokens_est" in tb
    assert "max_chars" in tb
    assert "status" in tb
    assert tb["status"] in ("ok", "warn", "exceeded")


def test_check_total_budget_within_limit(repo: Path) -> None:
    """Small extra_chars should pass budget check."""
    config = load_config(repo)
    result = check_total_budget(config, extra_chars=10)
    assert result is None  # None means ok


def test_check_total_budget_exceeded(repo: Path) -> None:
    """Huge extra_chars should trigger budget exceeded."""
    config = load_config(repo)
    result = check_total_budget(config, extra_chars=999_999)
    assert result is not None
    assert result["ok"] is False
    assert result["error"] == "total_budget_exceeded"


def test_write_rejected_by_total_budget(repo: Path) -> None:
    """memory_write should reject if global budget would be exceeded."""
    config = load_config(repo)
    from dataclasses import replace as dc_replace
    # Set an extremely tight budget
    config2 = dc_replace(config, guard_total_max_chars=100)
    huge_content = "x" * 200
    result = memory_write(config2, "memory-bank/notes.md", huge_content, mode="overwrite", backup=False)
    assert result["ok"] is False
    assert "budget" in result["message"].lower()


def test_write_passes_within_budget(repo: Path) -> None:
    """memory_write succeeds when within budget."""
    config = load_config(repo)
    result = memory_write(config, "memory-bank/notes.md", "small update", mode="overwrite", backup=False)
    assert result["ok"] is True


# ── Dynamic tool descriptions ───────────────────────────────────────────

def test_build_file_roles_includes_config_roles(repo: Path) -> None:
    """_build_file_roles extracts role text from guard targets."""
    config = load_config(repo)
    roles_text = _build_file_roles(config)
    assert "hot task context" in roles_text
    assert "notes" in roles_text
    assert "sprint focus" in roles_text


def test_build_tools_dynamic_descriptions(repo: Path) -> None:
    """_build_tools produces descriptions containing dynamic file roles."""
    config = load_config(repo)
    tools = _build_tools(config)
    assert len(tools) == 2
    assert {tool.name for tool in tools} == {"memory_read", "memory_write"}

    read_tool = next(t for t in tools if t.name == "memory_read")
    assert "hot task context" in read_tool.description

    write_tool = next(t for t in tools if t.name == "memory_write")
    assert "notes" in write_tool.description


def test_build_tools_path_hints(repo: Path) -> None:
    """inputSchema path descriptions contain target paths from config."""
    config = load_config(repo)
    tools = _build_tools(config)
    read_tool = next(t for t in tools if t.name == "memory_read")
    path_desc = read_tool.inputSchema["properties"]["path"]["description"]
    assert "memory-bank/long.md" in path_desc
    assert ".ai-context/current-task.md" in path_desc
