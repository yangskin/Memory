"""P2-1 + P2-2: scale baseline & health self-heal (v0.6.0 OOTB hardening)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from servers.memory_server.memory_baseline import (
    detect_regressions,
    load_baseline,
    write_baseline,
)
from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_locks import file_lock, is_lock_sidecar_stale
from servers.memory_server.memory_maintenance import memory_health_check
from servers.memory_server.memory_records import memory_write_record


def _bootstrap(tmp_path: Path) -> object:
    (tmp_path / "memory-bank").mkdir()
    (tmp_path / ".ai-memory").mkdir()
    (tmp_path / ".ai-memory" / "config.json").write_text(
        json.dumps({"allowed_roots": ["memory-bank"]}), encoding="utf-8"
    )
    (tmp_path / "memory-bank" / "seed.md").write_text("# seed\n", encoding="utf-8")
    return load_config(tmp_path)


# ── P2-1: scale baseline ───────────────────────────────────────────────


def test_write_baseline_creates_file(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path)
    result = write_baseline(config)
    assert result["ok"] is True
    assert (tmp_path / ".ai-memory" / "baseline.json").is_file()
    assert "metrics" in result
    assert result["metrics"]["memory_bank_files"] >= 1


def test_write_baseline_counts_packed_logical_records(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path)
    written = memory_write_record(
        config,
        content_markdown="# Packed record\n\nCount the logical record, not a legacy directory.",
        record_kind="note",
        scope="personal",
        author="alice",
        tags=["mcp"],
    )
    assert written["ok"] is True

    result = write_baseline(config)

    assert result["metrics"]["records_count"] == 1


def test_load_baseline_returns_none_when_missing(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path)
    assert load_baseline(config) is None


def test_detect_regressions_no_baseline(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path)
    report = detect_regressions(config)
    assert report["baseline_present"] is False
    assert report["regressions"] == []


def test_detect_regressions_flags_growth(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path)
    write_baseline(config)
    # Triple the file count to exceed factor=2.0.
    for i in range(5):
        (tmp_path / "memory-bank" / f"new_{i}.md").write_text("padding\n" * 100, encoding="utf-8")

    report = detect_regressions(config, factor=2.0)
    assert report["baseline_present"] is True
    metrics_flagged = {r["metric"] for r in report["regressions"]}
    assert "memory_bank_files" in metrics_flagged


def test_health_check_surfaces_regressions(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path)
    write_baseline(config)
    for i in range(10):
        (tmp_path / "memory-bank" / f"big_{i}.md").write_text("x" * 5000, encoding="utf-8")

    result = memory_health_check(config)
    assert result["ok"] is True
    codes = [issue["code"] for issue in result["issues"]]
    assert "scale_regression" in codes


# ── P2-2: self-heal ────────────────────────────────────────────────────


def test_health_self_heals_old_tmp_files(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path)
    orphan = tmp_path / "memory-bank" / "stale.md.tmp"
    orphan.write_text("orphan", encoding="utf-8")
    # Backdate the file to bypass the grace period (60s).
    old = time.time() - 3600
    import os

    os.utime(orphan, (old, old))

    result = memory_health_check(config)
    assert result["ok"] is True
    assert "self_heal" in result
    removed = result["self_heal"]["tmp_removed"]
    assert any("stale.md.tmp" in p for p in removed)
    assert not orphan.exists()


def test_health_does_not_remove_recent_tmp(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path)
    fresh = tmp_path / "memory-bank" / "wip.md.tmp"
    fresh.write_text("in flight", encoding="utf-8")

    result = memory_health_check(config)
    assert result["ok"] is True
    assert fresh.exists()
    assert not any("wip.md.tmp" in p for p in result["self_heal"]["tmp_removed"])


def test_health_does_not_remove_live_lock_sidecar(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path)
    target = tmp_path / "memory-bank" / "seed.md"

    with file_lock(config.repo_root, target):
        locks = list((tmp_path / ".ai-memory" / "locks").glob("*.lock"))
        assert locks
        old = time.time() - (2 * 24 * 60 * 60)
        for lock in locks:
            os.utime(lock, (old, old))
            assert is_lock_sidecar_stale(lock, now=time.time()) is False

        result = memory_health_check(config)

        assert result["ok"] is True
        assert locks[0].exists()
        assert not result["self_heal"]["stale_locks_removed"]


def test_health_removes_old_dead_lock_sidecar(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path)
    locks_dir = tmp_path / ".ai-memory" / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    lock = locks_dir / "dead.lock"
    lock.write_text(
        json.dumps({"pid": 99999999, "host": "definitely-not-this-host", "target": "memory-bank/seed.md"}),
        encoding="utf-8",
    )
    old = time.time() - (2 * 24 * 60 * 60)
    os.utime(lock, (old, old))

    result = memory_health_check(config)

    assert result["ok"] is True
    assert not lock.exists()
    assert any("dead.lock" in p for p in result["self_heal"]["stale_locks_removed"])
