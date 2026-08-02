"""Write-robustness regression tests for v0.5.5.

Covers:
- ``_atomic_write_text`` calls ``os.fsync`` on the data fd.
- ``_atomic_write_text`` calls ``os.fsync`` on the parent dir on POSIX
  (best-effort; smoke-tested via call counter).
- ``mcp.fsync_strict`` switch: data-fsync OSError propagates and the
  partially-written tmp file is cleaned up; the target file is left
  untouched.
- ``mcp.fsync_strict=False`` (default): same OSError is swallowed and
  the rename still completes.
- ``memory_write`` tmp file lands next to the target (same dir as
  ``resolved``), not in ``config.temp_dir``, so cross-volume failures
  cannot happen.
- ``memory_events.append_event`` calls ``os.fsync`` on the events
  handle; in strict mode an OSError is raised back to the caller.
- ``record_usage_stats`` rewrite is atomic: a forced fsync failure in
  strict mode leaves the *previous* JSON intact (no half-written file).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

MEMORY_ROOT = Path(__file__).resolve().parents[2]
if str(MEMORY_ROOT) not in sys.path:
    sys.path.insert(0, str(MEMORY_ROOT))

from servers.memory_server.memory_compiler_cache import record_usage_stats  # noqa: E402
from servers.memory_server.memory_config import load_config  # noqa: E402
from servers.memory_server.memory_corpus import CompilableRecord  # noqa: E402
from servers.memory_server.memory_events import append_event  # noqa: E402
from servers.memory_server.memory_record_io import _atomic_write_text  # noqa: E402
from servers.memory_server.memory_writer import memory_write  # noqa: E402


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "memory-bank").mkdir()
    (tmp_path / "memory-bank/notes.md").write_text("# initial\n", encoding="utf-8")
    (tmp_path / ".ai-context").mkdir()
    (tmp_path / ".ai-context/current-task.md").write_text(
        "# Task\n## Task\n- t\n## Goal / Done Definition\n- g\n"
        "## Current status\n- s\n## Relevant files / assets\n- x\n"
        "## Constraints\n- c\n## Latest attempts\n- a\n## Next planned step\n- n\n",
        encoding="utf-8",
    )
    (tmp_path / ".ai-memory").mkdir()
    return tmp_path


def _enable_strict(workspace: Path) -> None:
    """Turn on mcp.fsync_strict via the on-disk config file."""
    cfg_path = workspace / ".ai-memory" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps({"mcp": {"fsync_strict": True}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ── _atomic_write_text durability ─────────────────────────────────────


def test_atomic_write_calls_fsync_on_data_fd(tmp_path: Path) -> None:
    target = tmp_path / "out.md"
    fsync_calls: list[int] = []
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        fsync_calls.append(fd)
        real_fsync(fd)

    with patch("servers.memory_server.memory_record_io.os.fsync", side_effect=spy):
        _atomic_write_text(target, "hello\n")
    assert target.read_text(encoding="utf-8") == "hello\n"
    # At least the data fd must be fsync-ed; on POSIX the parent dir is too.
    assert len(fsync_calls) >= 1


def test_atomic_write_strict_mode_propagates_data_fsync_error(tmp_path: Path) -> None:
    target = tmp_path / "out.md"
    target.write_text("original\n", encoding="utf-8")

    def boom(_fd: int) -> None:
        raise OSError("simulated EIO")

    with patch("servers.memory_server.memory_record_io.os.fsync", side_effect=boom):
        with pytest.raises(OSError, match="simulated EIO"):
            _atomic_write_text(target, "new\n", fsync_strict=True)
    # Original content is preserved (the rename never ran).
    assert target.read_text(encoding="utf-8") == "original\n"
    # No leftover tmp files.
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".out.md.")]
    assert leftovers == []


def test_atomic_write_default_mode_swallows_fsync_error(tmp_path: Path) -> None:
    target = tmp_path / "out.md"

    def boom(_fd: int) -> None:
        raise OSError("simulated EIO")

    with patch("servers.memory_server.memory_record_io.os.fsync", side_effect=boom):
        _atomic_write_text(target, "kept\n", fsync_strict=False)
    # Despite fsync failing, the rename completed and content is on disk.
    assert target.read_text(encoding="utf-8") == "kept\n"


# ── memory_write integration ──────────────────────────────────────────


def test_memory_write_tmp_lives_next_to_target(workspace: Path) -> None:
    """tmp file must be a sibling of `resolved`, never in temp_dir."""
    config = load_config(str(workspace))
    target = workspace / "memory-bank" / "notes.md"
    seen_tmp: list[Path] = []
    real_open = os.open

    def spy_open(path: str, flags: int, *args, **kwargs):
        # Record any tmp file created with O_EXCL inside _atomic_write_text.
        if (flags & os.O_EXCL) and Path(path).name.startswith(".notes.md."):
            seen_tmp.append(Path(path))
        return real_open(path, flags, *args, **kwargs)

    with patch("servers.memory_server.memory_record_io.os.open", side_effect=spy_open):
        result = memory_write(
            config,
            path="memory-bank/notes.md",
            content="locked-in\n",
            backup=False,
        )
    assert result["ok"] is True
    assert seen_tmp, "expected at least one tmp file from _atomic_write_text"
    for tmp in seen_tmp:
        assert tmp.parent == target.parent, (
            f"tmp must be sibling of target, got {tmp} vs {target.parent}"
        )


def test_memory_write_strict_fsync_failure_keeps_original(workspace: Path) -> None:
    _enable_strict(workspace)
    config = load_config(str(workspace))
    assert config.mcp_fsync_strict is True
    original = (workspace / "memory-bank" / "notes.md").read_text(encoding="utf-8")

    def boom(_fd: int) -> None:
        raise OSError("disk full")

    with patch("servers.memory_server.memory_record_io.os.fsync", side_effect=boom):
        result = memory_write(
            config,
            path="memory-bank/notes.md",
            content="should-not-land\n",
            backup=False,
        )
    assert result["ok"] is False
    assert result["error"] == "write_failed"
    # File contents unchanged; no half-written state.
    assert (workspace / "memory-bank" / "notes.md").read_text(encoding="utf-8") == original


# ── events.jsonl durability ───────────────────────────────────────────


def test_append_event_calls_fsync(workspace: Path) -> None:
    config = load_config(str(workspace))
    fsync_called: list[int] = []
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        fsync_called.append(fd)
        real_fsync(fd)

    with patch("servers.memory_server.memory_events.os.fsync", side_effect=spy):
        append_event(config, "test_event", {"k": "v"})
    assert fsync_called, "append_event must fsync the events file"
    # And the line landed.
    content = config.events_file.read_text(encoding="utf-8")
    assert '"event_type": "test_event"' in content


def test_append_event_strict_mode_propagates_fsync_error(workspace: Path) -> None:
    _enable_strict(workspace)
    config = load_config(str(workspace))

    def boom(_fd: int) -> None:
        raise OSError("simulated")

    with patch("servers.memory_server.memory_events.os.fsync", side_effect=boom):
        with pytest.raises(OSError, match="simulated"):
            append_event(config, "test_event", {"k": "v"})


# ── record_usage_stats atomicity ──────────────────────────────────────


def test_record_usage_stats_strict_failure_preserves_previous_json(workspace: Path) -> None:
    _enable_strict(workspace)
    config = load_config(str(workspace))
    stats_path = workspace / ".ai-memory" / "usage-stats.json"
    # Seed previous state.
    stats_path.write_text(
        json.dumps({"mem_keep": {"compile_hit_count": 7}}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    rec = CompilableRecord(
        path="memory-bank/personal/agent/n_other.md",
        metadata={"id": "mem_other", "record_kind": "note", "scope": "personal"},
        body="x",
        title="x",
    )

    def boom(_fd: int) -> None:
        raise OSError("simulated")

    # record_usage_stats swallows OSError around the write itself, but in
    # strict mode the atomic helper raises — the OSError is caught by
    # record_usage_stats's own try/except so the previous JSON stays.
    with patch("servers.memory_server.memory_record_io.os.fsync", side_effect=boom):
        record_usage_stats(config, [rec], used_at="2026-04-25T00:00:00+00:00", target="t")

    # Previous JSON is fully intact (atomic write never replaced it).
    after = json.loads(stats_path.read_text(encoding="utf-8"))
    assert after == {"mem_keep": {"compile_hit_count": 7}}
