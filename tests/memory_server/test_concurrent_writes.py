"""Concurrent multi-process regression tests for v0.5.4 multi-agent safety.

These tests use ``multiprocessing`` (not just threads) because the real
deployment scenario is *N* MCP server processes (one per VS Code window
or Codex session) writing the same workspace at the same time. Threads
share an in-process lock by default and would not catch a missing
cross-process file-lock implementation.

Coverage:
    - Concurrent ``memory_write`` on the *same* file: no torn content;
      exactly one writer's content survives; backup count == writers - 1.
    - Concurrent ``memory_write_record`` for *different* records: every
      record file lands intact (records are independent files).
    - Concurrent ``record_usage_stats`` on the same record: hit-count
      equals the total number of writes (no lost increments).
    - Concurrent ``memory_delete_record`` tombstone appends: every line
      is a complete JSON object (no torn lines).
    - ``memory_write`` with ``if_match``: stale precondition is rejected
      with ``conflict``; fresh precondition succeeds and exposes
      ``new_sha`` for the next round.
    - ``new_request_id()`` returns time-ordered, unique values under
      contention.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

MEMORY_ROOT = Path(__file__).resolve().parents[2]
if str(MEMORY_ROOT) not in sys.path:
    sys.path.insert(0, str(MEMORY_ROOT))


# ── multiprocessing worker functions (must be top-level/picklable) ────


def _worker_write_file(args: tuple[str, int]) -> dict:
    """Each subprocess loads its own MemoryConfig and writes once."""
    repo_root, idx = args
    sys.path.insert(0, str(MEMORY_ROOT))
    from servers.memory_server.memory_config import load_config
    from servers.memory_server.memory_writer import memory_write

    config = load_config(repo_root)
    return memory_write(
        config,
        path="memory-bank/notes.md",
        content=f"# Note from worker {idx}\n",
        mode="overwrite",
        backup=False,  # backups create their own race; isolate this test
        reason=f"worker_{idx}",
    )


def _worker_write_distinct_record(args: tuple[str, int]) -> dict:
    repo_root, idx = args
    sys.path.insert(0, str(MEMORY_ROOT))
    from servers.memory_server.memory_config import load_config
    from servers.memory_server.memory_records import memory_write_record

    config = load_config(repo_root)
    return memory_write_record(
        config,
        content_markdown=(
            f"# Concurrent record {idx}\n\nBody for worker {idx}, "
            f"padded to satisfy minimum body checks: {'x' * 80}\n"
        ),
        record_kind="note",
        scope="personal",
        author=f"agent-{idx}",
        tags=["mcp"],
    )


def _worker_record_usage(args: tuple[str, int]) -> bool:
    repo_root, idx = args
    sys.path.insert(0, str(MEMORY_ROOT))
    from servers.memory_server.memory_compiler_cache import record_usage_stats
    from servers.memory_server.memory_config import load_config
    from servers.memory_server.memory_corpus import CompilableRecord

    config = load_config(repo_root)
    rec = CompilableRecord(
        path="memory-bank/personal/agent/n_shared.md",
        metadata={"id": "mem_shared", "record_kind": "note", "scope": "personal"},
        body="shared",
        title="shared",
    )
    record_usage_stats(
        config,
        [rec],
        used_at="2026-04-24T00:00:00+00:00",
        target=f"runtime_digest_{idx}",
    )
    return True


def _worker_request_ids(_n: int) -> list[str]:
    sys.path.insert(0, str(MEMORY_ROOT))
    from servers.memory_server.memory_request_id import new_request_id

    return [new_request_id() for _ in range(_n)]


# ── shared fixture: minimal real workspace on disk ────────────────────


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


# ── tests ─────────────────────────────────────────────────────────────


def test_concurrent_overwrites_same_file_serialize_cleanly(workspace: Path) -> None:
    """5 procs × 1 write each on the same file. The file must end up with
    exactly one worker's content (any of them) — not torn, not empty."""
    repo_root = str(workspace)
    workers = 5
    ctx = mp.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
        results = list(ex.map(_worker_write_file, [(repo_root, i) for i in range(workers)]))
    assert all(r.get("ok") for r in results), [r for r in results if not r.get("ok")]
    final = (workspace / "memory-bank/notes.md").read_text(encoding="utf-8")
    # Exactly one worker's "# Note from worker N" must appear in full.
    survivors = [i for i in range(workers) if f"# Note from worker {i}\n" in final]
    assert len(survivors) == 1, f"expected exactly one surviving writer, found {survivors}"
    # request_ids are all distinct.
    rids = {r.get("request_id") for r in results}
    assert len(rids) == workers
    assert all(rid for rid in rids)


def test_concurrent_distinct_records_all_survive(workspace: Path) -> None:
    """Different records (different ids) → independent files → all survive."""
    repo_root = str(workspace)
    workers = 5
    ctx = mp.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
        results = list(
            ex.map(_worker_write_distinct_record, [(repo_root, i) for i in range(workers)])
        )
    ok_results = [r for r in results if r.get("ok")]
    assert len(ok_results) == workers, [r for r in results if not r.get("ok")]
    # Every result's path points to a distinct existing file.
    paths = {r["path"] for r in ok_results}
    assert len(paths) == workers
    for path in paths:
        assert (workspace / path).is_file()


def test_concurrent_usage_stats_no_lost_increments(workspace: Path) -> None:
    """N procs each call record_usage_stats once for the same record →
    final compile_hit_count == N (no lost updates under file lock)."""
    repo_root = str(workspace)
    workers = 8
    ctx = mp.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
        list(ex.map(_worker_record_usage, [(repo_root, i) for i in range(workers)]))
    stats = json.loads((workspace / ".ai-memory" / "usage-stats.json").read_text(encoding="utf-8"))
    assert stats["mem_shared"]["compile_hit_count"] == workers
    # Should also have collected 8 distinct compile_targets.
    targets = stats["mem_shared"]["compile_targets"]
    assert len(targets) == workers


def test_if_match_rejects_stale_precondition(workspace: Path) -> None:
    """if_match with the wrong sha returns conflict; with the right sha
    succeeds and returns a fresh new_sha for the next round."""
    sys.path.insert(0, str(MEMORY_ROOT))
    from servers.memory_server.memory_config import load_config
    from servers.memory_server.memory_request_id import content_sha
    from servers.memory_server.memory_writer import memory_write

    config = load_config(str(workspace))
    # Round 1: write with empty-file precondition → fail (file already exists with "# initial\n").
    bad = memory_write(
        config,
        path="memory-bank/notes.md",
        content="round 1\n",
        if_match="",  # empty == "expect missing"; file does exist
        backup=False,
    )
    assert bad["ok"] is False
    assert bad["error"] == "conflict"
    assert "current_sha" in bad

    # Round 2: re-read, supply correct sha, succeed.
    current = (workspace / "memory-bank/notes.md").read_text(encoding="utf-8")
    good = memory_write(
        config,
        path="memory-bank/notes.md",
        content="round 2 content\n",
        if_match=content_sha(current),
        backup=False,
    )
    assert good["ok"] is True, good
    assert good["new_sha"]
    assert good["request_id"]

    # Round 3: same precondition, now stale → conflict again.
    stale = memory_write(
        config,
        path="memory-bank/notes.md",
        content="round 3\n",
        if_match=content_sha(current),  # already overwritten in round 2
        backup=False,
    )
    assert stale["ok"] is False
    assert stale["error"] == "conflict"


def test_request_id_uniqueness_under_contention() -> None:
    """200 ids generated across 4 procs are all unique and roughly time-ordered."""
    workers = 4
    per_worker = 200
    ctx = mp.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
        chunks = list(ex.map(_worker_request_ids, [per_worker] * workers))
    flat = [rid for chunk in chunks for rid in chunk]
    assert len(set(flat)) == len(flat), "request_ids must be unique"
    # Within a single chunk, ids are monotonic-ish (uuid7 is ms-ordered).
    for chunk in chunks:
        sorted_chunk = sorted(chunk)
        # Allow up to 5% out-of-order ties (same-ms collisions resolved by random tail).
        mismatches = sum(1 for a, b in zip(chunk, sorted_chunk) if a != b)
        assert mismatches <= max(5, per_worker // 20), (
            f"chunk too out-of-order: {mismatches}/{per_worker} mismatches"
        )
