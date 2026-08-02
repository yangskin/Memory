"""P1-3: shared-append auto-compact (v0.6.0 OOTB hardening)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_shared_compactor import (
    _looks_like_archive_banner,
    auto_compact_shared_file,
    needs_compaction,
)


def _bootstrap(tmp_path: Path) -> object:
    (tmp_path / "memory-bank").mkdir()
    (tmp_path / ".ai-memory").mkdir()
    (tmp_path / ".ai-memory" / "config.json").write_text(
        json.dumps({"allowed_roots": ["memory-bank"]}), encoding="utf-8"
    )
    return load_config(tmp_path)


def _make_long_file(tmp_path: Path, lines: int) -> Path:
    (tmp_path / "memory-bank").mkdir(exist_ok=True)
    target = tmp_path / "memory-bank" / "progress.md"
    target.write_text("\n".join(f"- entry {i}" for i in range(lines)) + "\n", encoding="utf-8")
    return target


def test_needs_compaction_below_threshold(tmp_path: Path) -> None:
    target = _make_long_file(tmp_path, 100)
    assert needs_compaction(target, threshold_lines=200) is False


def test_needs_compaction_above_threshold(tmp_path: Path) -> None:
    target = _make_long_file(tmp_path, 300)
    assert needs_compaction(target, threshold_lines=200) is True


def test_auto_compact_skips_when_below_threshold(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path)
    target = _make_long_file(tmp_path, 50)
    result = auto_compact_shared_file(config, target, threshold_lines=2000)
    assert result["ok"] is True
    assert result["action"] == "skipped"
    # Original file untouched.
    assert (tmp_path / "memory-bank" / "progress.md").read_text(encoding="utf-8").count("\n") == 50


def test_auto_compact_archives_when_above_threshold(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path)
    target = _make_long_file(tmp_path, 500)

    result = auto_compact_shared_file(
        config, target, threshold_lines=200, keep_lines=50,
        now=datetime(2026, 4, 25, tzinfo=timezone.utc),
    )

    assert result["ok"] is True
    assert result["action"] == "archived"
    assert result["lines_kept"] == 50
    assert result["lines_archived"] == 450

    archive_path = Path(result["archive_path"])
    assert archive_path.is_file()
    assert "progress" in archive_path.name
    archive_text = archive_path.read_text(encoding="utf-8")
    assert "entry 0" in archive_text
    assert "entry 449" in archive_text
    assert "entry 499" not in archive_text  # last 50 stay in live file

    live_text = target.read_text(encoding="utf-8")
    assert _looks_like_archive_banner(live_text)
    assert "entry 499" in live_text
    assert "entry 0" not in live_text


def test_auto_compact_appends_to_existing_weekly_archive(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path)
    moment = datetime(2026, 4, 25, tzinfo=timezone.utc)

    target = _make_long_file(tmp_path, 500)
    auto_compact_shared_file(config, target, threshold_lines=200, keep_lines=50, now=moment)

    # Add more lines, recompact in the same week.
    with target.open("a", encoding="utf-8") as fh:
        for i in range(500, 800):
            fh.write(f"- entry {i}\n")
    second = auto_compact_shared_file(
        config, target, threshold_lines=200, keep_lines=50, now=moment
    )
    assert second["action"] == "archived"

    archive_path = Path(second["archive_path"])
    text = archive_path.read_text(encoding="utf-8")
    # Second-pass entries appended to first-pass archive.
    assert text.count("# Archive of") == 1
    assert "entry 0" in text
    assert "entry 700" in text


def test_auto_compact_returns_error_for_missing_file(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path)
    result = auto_compact_shared_file(config, tmp_path / "memory-bank" / "ghost.md")
    assert result["ok"] is False
    assert result["error"] == "not_found"
