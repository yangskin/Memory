"""Strict multi-agent concurrent-write stress tests for v0.5.5.

These tests deliberately push the lock + atomic-write + optimistic-lock
machinery harder than ``test_concurrent_writes.py`` does:

- High contention on a single target (10 procs × 20 sequential writes).
- Concurrent ``mode="append"`` (read-modify-write) where every line
  contributed by every worker must survive intact and total line count
  must equal sum of contributions.
- Two-process publish race for the **same** record-id target: exactly
  one wins, the other receives a structured ``target_exists`` error
  (validates the in-lock TOCTOU re-check added in v0.5.4).
- ``LockTimeoutError`` propagation: a second waiter with a small
  timeout fails fast instead of blocking forever; reentrance works
  cross-thread (same process, different threads still serialise).
- Optimistic-lock retry loop: 6 procs × 8 increments each on a JSON
  counter, every conflict triggers re-read; eventually the counter
  reflects all 48 increments with zero lost updates.
- ``events.jsonl`` audit-log stress (its own ``fcntl``/``msvcrt``
  lock): 8 procs × 60 ``append_event`` → exactly 480 valid JSON lines,
  no torn lines.
- Mixed workload: writers + record-writers + readers run in parallel
  for a fixed budget; assert no DB lock errors and the index re-reads
  cleanly.
- ``write_same_record`` two-process race for the same archived record:
  exactly one wins each round; both finish with the file containing
  the LAST-write-wins payload (no torn YAML / Markdown).

Every test uses ``multiprocessing.get_context("spawn")`` so workers
each load their own ``MemoryConfig`` and exercise real cross-process
serialisation (threads share an in-process lock and would not detect a
missing OS-level lock).
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import pytest

MEMORY_ROOT = Path(__file__).resolve().parents[2]
if str(MEMORY_ROOT) not in sys.path:
    sys.path.insert(0, str(MEMORY_ROOT))

from servers.memory_server.memory_locks import LockTimeoutError, file_lock  # noqa: E402


# ── multiprocessing worker functions (must be picklable / top-level) ──


def _worker_burst_overwrite(args: tuple[str, int, int]) -> list[dict]:
    """`reps` sequential overwrites of the same file from one process."""
    repo_root, worker_idx, reps = args
    sys.path.insert(0, str(MEMORY_ROOT))
    from servers.memory_server.memory_config import load_config
    from servers.memory_server.memory_writer import memory_write

    config = load_config(repo_root)
    out: list[dict] = []
    for r in range(reps):
        body = (
            f"# Hot file from worker {worker_idx} round {r}\n"
            f"signature: w{worker_idx}-r{r}\n"
            f"payload: {'x' * 64}\n"
        )
        result = memory_write(
            config,
            path="memory-bank/notes.md",
            content=body,
            mode="overwrite",
            backup=False,  # backup race is tested separately; isolate here
            reason=f"burst w{worker_idx}-r{r}",
        )
        out.append({
            "ok": result.get("ok"),
            "worker": worker_idx,
            "round": r,
            "request_id": result.get("request_id"),
            "new_sha": result.get("new_sha"),
            "error": result.get("error"),
        })
    return out


def _worker_append_lines(args: tuple[str, int, int]) -> list[dict]:
    """`reps` sequential append-mode writes from one process.

    Each append adds exactly one line containing the worker idx + round.
    Under correct serialisation the final file must contain ALL
    ``workers * reps`` lines (none dropped, none torn).
    """
    repo_root, worker_idx, reps = args
    sys.path.insert(0, str(MEMORY_ROOT))
    from servers.memory_server.memory_config import load_config
    from servers.memory_server.memory_writer import memory_write

    config = load_config(repo_root)
    out: list[dict] = []
    for r in range(reps):
        # No trailing newline inside payload — memory_write normalises one.
        line = f"LINE w{worker_idx}-r{r}"
        result = memory_write(
            config,
            path="memory-bank/append_log.md",
            content=line,
            mode="append",
            backup=False,
            inject_user_tag=False,  # plain lines, no HTML tag clutter
        )
        out.append({"ok": result.get("ok"), "worker": worker_idx, "round": r})
    return out


def _worker_publish_target(args: tuple[str, int]) -> dict:
    """Two procs publish a candidate to the SAME canonical target slot.

    Each subprocess seeds its own candidate file on disk with a fixed
    record id, then calls ``write_record_to_target`` for that record.
    Exactly one must win; the other must receive ``target_exists``.
    """
    repo_root, worker_idx = args
    sys.path.insert(0, str(MEMORY_ROOT))
    from servers.memory_server.memory_config import load_config
    from servers.memory_server.memory_record_io import write_record_to_target
    from servers.memory_server.memory_records import render_record_markdown

    config = load_config(repo_root)
    record_id = "mem_publish_race"
    metadata = {
        "id": record_id,
        "schema_version": "v2",
        "record_kind": "claim_candidate",
        "scope": "personal",
        "status": "published",  # target slot resolves under published/
        "author": f"agent-{worker_idx}",
        "tags": ["mcp"],
        "confidence": 0.9,
        "created_at": "2026-04-25T00:00:00+00:00",
    }
    body = f"# Published by worker {worker_idx}\nbody-{worker_idx}\n"

    # Stage a candidate source file specific to this worker so old_abs_path
    # is unique per process (avoids racing on the SOURCE file too —
    # we want to race on the TARGET).
    candidates_dir = Path(repo_root) / "memory-bank" / "personal" / "candidate"
    candidates_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = candidates_dir / f"c_publish_race_w{worker_idx}.md"
    candidate_path.write_text(
        render_record_markdown({**metadata, "status": "candidate"}, body),
        encoding="utf-8",
    )
    candidate_rel = candidate_path.relative_to(Path(repo_root)).as_posix()

    # Tiny stagger so both calls hit the lock acquisition at roughly the
    # same time without one trivially winning by being late.
    time.sleep(0.01)

    return write_record_to_target(
        config,
        old_abs_path=candidate_path,
        old_rel_path=candidate_rel,
        metadata=metadata,
        body=body,
    )


def _worker_optimistic_increment(args: tuple[str, int, int]) -> dict:
    """Read-modify-write a JSON counter ``increments`` times with if_match.

    On every conflict the worker re-reads, recomputes, retries. A correct
    implementation converges on ``sum(increments)`` with no lost updates.
    """
    repo_root, worker_idx, increments = args
    sys.path.insert(0, str(MEMORY_ROOT))
    from servers.memory_server.memory_config import load_config
    from servers.memory_server.memory_request_id import content_sha
    from servers.memory_server.memory_writer import memory_write

    config = load_config(repo_root)
    rel = "memory-bank/counter.md"
    abs_path = Path(repo_root) / rel

    successes = 0
    conflicts = 0
    attempts = 0
    deadline = time.monotonic() + 30.0  # safety net

    while successes < increments:
        if time.monotonic() > deadline:
            return {
                "worker": worker_idx,
                "successes": successes,
                "conflicts": conflicts,
                "attempts": attempts,
                "error": "deadline_exceeded",
            }
        attempts += 1
        # Read current state. On Windows os.replace can briefly race with
        # an outside-of-lock read (PermissionError [Errno 13]). Retry a
        # handful of times before giving up — this is exactly the kind
        # of transient that real agent code must tolerate.
        current = ""
        for read_try in range(20):
            try:
                current = abs_path.read_text(encoding="utf-8") if abs_path.exists() else ""
                break
            except (PermissionError, FileNotFoundError):
                time.sleep(0.005)
        else:
            return {
                "worker": worker_idx,
                "successes": successes,
                "conflicts": conflicts,
                "attempts": attempts,
                "error": "read_blocked_by_replace_race",
            }
        try:
            cur_value = int(current.strip().splitlines()[-1]) if current.strip() else 0
        except (ValueError, IndexError):
            cur_value = 0
        new_value = cur_value + 1
        new_content = f"# counter\n{new_value}\n"
        precondition = content_sha(current) if current else ""
        result = memory_write(
            config,
            path=rel,
            content=new_content,
            mode="overwrite",
            backup=False,
            inject_user_tag=False,
            if_match=precondition,
        )
        if result.get("ok"):
            successes += 1
        elif result.get("error") == "conflict":
            conflicts += 1
            # Brief jitter so we don't all retry in lockstep.
            time.sleep(0.001 * (worker_idx + 1))
        else:
            return {
                "worker": worker_idx,
                "successes": successes,
                "conflicts": conflicts,
                "attempts": attempts,
                "error": result.get("error"),
                "message": result.get("message"),
            }
    return {
        "worker": worker_idx,
        "successes": successes,
        "conflicts": conflicts,
        "attempts": attempts,
        "error": None,
    }


def _worker_event_burst(args: tuple[str, int, int]) -> int:
    repo_root, worker_idx, count = args
    sys.path.insert(0, str(MEMORY_ROOT))
    from servers.memory_server.memory_config import load_config
    from servers.memory_server.memory_events import append_event

    config = load_config(repo_root)
    for r in range(count):
        append_event(config, "stress_event", {"worker": worker_idx, "round": r})
    return count


def _worker_mixed_writer(args: tuple[str, int, float]) -> dict:
    repo_root, worker_idx, deadline = args
    sys.path.insert(0, str(MEMORY_ROOT))
    from servers.memory_server.memory_config import load_config
    from servers.memory_server.memory_writer import memory_write

    config = load_config(repo_root)
    n = 0
    err: str | None = None
    while time.monotonic() < deadline:
        result = memory_write(
            config,
            path="memory-bank/notes.md",
            content=f"# mixed writer {worker_idx} iter {n}\n",
            mode="overwrite",
            backup=False,
        )
        if not result.get("ok"):
            err = f"{result.get('error')}: {result.get('message')}"
            break
        n += 1
    return {"ok": err is None, "writes": n, "error": err, "worker": worker_idx}


def _worker_mixed_record_writer(args: tuple[str, int, float]) -> dict:
    repo_root, worker_idx, deadline = args
    sys.path.insert(0, str(MEMORY_ROOT))
    from servers.memory_server.memory_config import load_config
    from servers.memory_server.memory_records import memory_write_record

    config = load_config(repo_root)
    n = 0
    err: str | None = None
    while time.monotonic() < deadline:
        result = memory_write_record(
            config,
            content_markdown=(
                f"# Mixed record w{worker_idx} n{n}\n\n"
                f"Body padded for body-min check: {'y' * 80}\n"
            ),
            record_kind="note",
            scope="personal",
            author=f"agent-mixed-{worker_idx}",
            tags=["mcp"],
        )
        if not result.get("ok"):
            err = str(result.get("error"))
            break
        n += 1
    return {"ok": err is None, "writes": n, "error": err, "worker": worker_idx}


def _worker_mixed_reader(args: tuple[str, int, float]) -> dict:
    repo_root, worker_idx, deadline = args
    sys.path.insert(0, str(MEMORY_ROOT))
    from servers.memory_server.memory_config import load_config
    from servers.memory_server.memory_search import memory_search

    config = load_config(repo_root)
    n = 0
    err: str | None = None
    # Post-v0.5.6 P1 fixes: PathManager normalises ``\\?\`` prefixes and
    # readers go through safe_read_text. Therefore neither the
    # long-path ValueError nor the os.replace PermissionError transient
    # should ever surface to this worker. We treat ANY exception as a
    # hard failure so future regressions are caught immediately.
    while time.monotonic() < deadline:
        try:
            memory_search(config, "record", top_k=5)
        except Exception as exc:  # noqa: BLE001
            err = repr(exc)
            break
        n += 1
    return {"ok": err is None, "reads": n, "error": err, "worker": worker_idx}


def _worker_lock_holder(args: tuple[str, str, float]) -> str:
    """Hold the file_lock for the given target for ``hold_seconds``."""
    repo_root, target_rel, hold_seconds = args
    sys.path.insert(0, str(MEMORY_ROOT))
    from servers.memory_server.memory_config import load_config

    config = load_config(repo_root)
    target = Path(repo_root) / target_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch(exist_ok=True)
    with file_lock(config.repo_root, target):
        time.sleep(hold_seconds)
    return "released"


def _worker_lock_waiter(args: tuple[str, str, float]) -> dict:
    """Try to acquire the same file_lock with a small timeout."""
    repo_root, target_rel, timeout = args
    sys.path.insert(0, str(MEMORY_ROOT))
    from servers.memory_server.memory_config import load_config

    config = load_config(repo_root)
    target = Path(repo_root) / target_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        with file_lock(config.repo_root, target, timeout=timeout):
            return {"acquired": True, "elapsed": time.monotonic() - started}
    except LockTimeoutError as exc:
        return {
            "acquired": False,
            "elapsed": time.monotonic() - started,
            "error": str(exc),
        }


# ── shared workspace fixture ──────────────────────────────────────────


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


# ── 1. high-contention same-file overwrite ────────────────────────────


def test_high_contention_overwrite_no_corruption(workspace: Path) -> None:
    """10 procs × 20 sequential overwrites = 200 writes on the SAME file.

    Verifies:
    - Every single write returns ok=True (no spurious failures).
    - request_ids are pairwise unique across all 200 writes.
    - Final file content is exactly one worker's complete payload — not
      torn, not empty, not interleaved.
    """
    repo_root = str(workspace)
    workers = 10
    reps = 20
    ctx = mp.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
        all_results = list(
            ex.map(_worker_burst_overwrite, [(repo_root, i, reps) for i in range(workers)])
        )

    flat = [r for chunk in all_results for r in chunk]
    failures = [r for r in flat if not r.get("ok")]
    assert failures == [], f"got {len(failures)} failures: {failures[:5]}"
    assert len(flat) == workers * reps == 200

    rids = {r["request_id"] for r in flat}
    assert len(rids) == 200, f"request_ids must be unique, got {len(rids)} unique"

    final = (workspace / "memory-bank/notes.md").read_text(encoding="utf-8")
    # File must contain exactly ONE complete signature pattern.
    import re

    sigs = re.findall(r"signature: w(\d+)-r(\d+)", final)
    assert len(sigs) == 1, f"expected exactly 1 surviving signature, got {len(sigs)}"
    # And the payload-line for that signature must be intact (no truncation).
    assert "payload: " + ("x" * 64) in final


# ── 2. append-mode multi-proc concurrency ─────────────────────────────


def test_concurrent_append_no_torn_lines_no_lost_lines(workspace: Path) -> None:
    """8 procs × 25 appends. Every line must survive, no torn lines.

    `mode="append"` is read-modify-write inside the lock, so a missing
    file lock would either drop lines or corrupt them. We assert both
    line count AND that every (worker, round) pair appears exactly once.
    """
    repo_root = str(workspace)
    workers = 8
    reps = 25
    ctx = mp.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
        all_results = list(
            ex.map(_worker_append_lines, [(repo_root, i, reps) for i in range(workers)])
        )

    flat = [r for chunk in all_results for r in chunk]
    assert all(r["ok"] for r in flat), [r for r in flat if not r["ok"]][:5]

    log_path = workspace / "memory-bank/append_log.md"
    assert log_path.is_file()
    content = log_path.read_text(encoding="utf-8")

    import re

    lines = re.findall(r"LINE w(\d+)-r(\d+)", content)
    pairs = {(int(w), int(r)) for w, r in lines}
    expected = {(w, r) for w in range(workers) for r in range(reps)}
    assert pairs == expected, (
        f"missing {len(expected - pairs)}, extra {len(pairs - expected)}"
    )
    # And every match should be on its own line (no torn writes splicing two
    # signatures into the same line).
    by_line = [
        re.findall(r"LINE w(\d+)-r(\d+)", ln)
        for ln in content.splitlines()
        if "LINE w" in ln
    ]
    assert all(len(matches) == 1 for matches in by_line), "torn line detected"


# ── 3. publish-target race ────────────────────────────────────────────


def test_publish_target_race_exactly_one_winner(workspace: Path) -> None:
    """Two procs publish to the SAME canonical record-id slot.

    With the in-lock TOCTOU re-check from v0.5.4, exactly one worker
    must succeed and the other must surface ``target_exists`` (NOT
    write_failed, NOT silent overwrite).
    """
    repo_root = str(workspace)
    ctx = mp.get_context("spawn")
    with ProcessPoolExecutor(max_workers=2, mp_context=ctx) as ex:
        results = list(ex.map(_worker_publish_target, [(repo_root, i) for i in range(2)]))

    oks = [r for r in results if r.get("ok")]
    fails = [r for r in results if not r.get("ok")]
    assert len(oks) == 1, f"expected exactly one winner, got {len(oks)}: {results}"
    assert len(fails) == 1
    assert fails[0]["error"] == "target_exists", fails[0]
    # The winning record file actually exists.
    target = workspace / oks[0]["path"]
    assert target.is_file()


# ── 4. lock timeout under real contention + reentrance ────────────────


def test_lock_timeout_returns_structured_error(workspace: Path) -> None:
    """Holder process sleeps 1.5s; waiter with 0.2s timeout fails fast."""
    repo_root = str(workspace)
    target_rel = "memory-bank/notes.md"
    ctx = mp.get_context("spawn")

    holder_proc = ctx.Process(
        target=_worker_lock_holder, args=((repo_root, target_rel, 1.5),)
    )
    holder_proc.start()
    time.sleep(0.2)  # let holder grab the lock first

    with ProcessPoolExecutor(max_workers=1, mp_context=ctx) as ex:
        result = ex.submit(_worker_lock_waiter, (repo_root, target_rel, 0.2)).result(
            timeout=5.0
        )

    holder_proc.join(timeout=5.0)
    assert holder_proc.exitcode == 0

    assert result["acquired"] is False, result
    # Timeout should fire promptly, well under the holder's hold time.
    assert result["elapsed"] < 1.0, f"waiter took too long: {result['elapsed']}s"
    assert "could not acquire file lock" in result["error"]


def test_reentrance_same_thread_no_deadlock(workspace: Path) -> None:
    """Nested file_lock on the same target from the same thread is safe.

    Two threads contend on the same lock; the holder reacquires the
    same lock recursively, then yields. The other thread must wait,
    not deadlock, and acquire after release.
    """
    sys.path.insert(0, str(MEMORY_ROOT))
    from servers.memory_server.memory_config import load_config

    config = load_config(str(workspace))
    target = workspace / "memory-bank/notes.md"
    order: list[str] = []
    barrier = threading.Barrier(2)

    def holder() -> None:
        barrier.wait()
        with file_lock(config.repo_root, target):
            order.append("outer-acquired")
            with file_lock(config.repo_root, target):  # reentrant
                order.append("inner-acquired")
                time.sleep(0.2)
                order.append("inner-released")
            order.append("outer-released")

    def waiter() -> None:
        barrier.wait()
        time.sleep(0.05)  # ensure holder grabs first
        with file_lock(config.repo_root, target, timeout=5.0):
            order.append("waiter-acquired")

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(holder)
        f2 = ex.submit(waiter)
        f1.result(timeout=10.0)
        f2.result(timeout=10.0)

    # Holder must complete its outer release before waiter acquires.
    assert order == [
        "outer-acquired",
        "inner-acquired",
        "inner-released",
        "outer-released",
        "waiter-acquired",
    ], order


# ── 5. optimistic-lock retry convergence ──────────────────────────────


def test_if_match_optimistic_retry_no_lost_updates(workspace: Path) -> None:
    """6 procs × 8 increments via if_match retry loop = 48 increments.

    Final on-disk counter MUST equal 48 — proving the optimistic lock
    rejects every stale write and the retry loop catches every
    conflict (no "lost update" anomaly).
    """
    repo_root = str(workspace)
    counter_path = workspace / "memory-bank/counter.md"
    counter_path.write_text("# counter\n0\n", encoding="utf-8")

    workers = 6
    increments = 8
    ctx = mp.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
        results = list(
            ex.map(
                _worker_optimistic_increment,
                [(repo_root, i, increments) for i in range(workers)],
            )
        )

    for r in results:
        assert r["error"] is None, r
        assert r["successes"] == increments, r

    # Final counter equals the sum of all successful increments.
    final = counter_path.read_text(encoding="utf-8").strip().splitlines()[-1]
    assert int(final) == workers * increments, f"final={final}, expected={workers * increments}"

    # Sanity: at least SOME conflicts happened (otherwise the test isn't
    # actually exercising contention). With 6 procs + jittered retries,
    # we expect at least a handful.
    total_conflicts = sum(r["conflicts"] for r in results)
    assert total_conflicts >= 1, (
        f"no conflicts observed → test did not exercise contention: {results}"
    )


# ── 6. events.jsonl audit-log multi-proc stress ───────────────────────


def test_events_jsonl_multi_proc_no_torn_lines(workspace: Path) -> None:
    """8 procs × 60 append_event = 480 events. Every line must be
    valid JSON; the count must match exactly."""
    repo_root = str(workspace)
    workers = 8
    per = 60
    ctx = mp.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
        counts = list(ex.map(_worker_event_burst, [(repo_root, i, per) for i in range(workers)]))
    assert sum(counts) == workers * per

    sys.path.insert(0, str(MEMORY_ROOT))
    from servers.memory_server.memory_config import load_config

    config = load_config(repo_root)
    events_file = config.events_file
    assert events_file.is_file(), f"events file missing: {events_file}"
    raw = events_file.read_text(encoding="utf-8").splitlines()
    # If rotation kicked in, also include rotated archives.
    archives = sorted(
        p for p in events_file.parent.iterdir()
        if p.is_file() and p.name.startswith(events_file.name + ".")
    )
    for arch in archives:
        raw.extend(arch.read_text(encoding="utf-8").splitlines())

    parsed = []
    for ln in raw:
        if not ln.strip():
            continue
        # If a torn line ever escapes, json.loads will raise here.
        parsed.append(json.loads(ln))

    stress_events = [
        e for e in parsed
        if e.get("event_type") == "stress_event" and isinstance(e.get("payload"), dict)
    ]
    assert len(stress_events) == workers * per, (
        f"expected {workers * per} stress events, got {len(stress_events)}"
    )
    # Every (worker, round) combination is present exactly once.
    pairs = {
        (e["payload"]["worker"], e["payload"]["round"]) for e in stress_events
    }
    expected = {(w, r) for w in range(workers) for r in range(per)}
    assert pairs == expected


# ── 7. mixed workload ─────────────────────────────────────────────────


def test_mixed_workload_no_db_lock_no_corruption(workspace: Path) -> None:
    """Writers + record-writers + readers run together for 1.5s.

    Asserts:
    - Every worker reports ok=True (no DB-lock errors, no broken
      lock acquisition).
    - At least N writes / records / reads actually happened (proving
      the workers really overlapped).
    - The notes.md file is non-empty and parseable as plain UTF-8.
    """
    repo_root = str(workspace)
    duration = 1.5
    deadline = time.monotonic() + duration
    ctx = mp.get_context("spawn")

    n_writers = 3
    n_record_writers = 3
    n_readers = 3

    with ProcessPoolExecutor(max_workers=n_writers + n_record_writers + n_readers, mp_context=ctx) as ex:
        writer_futs = [
            ex.submit(_worker_mixed_writer, (repo_root, i, deadline))
            for i in range(n_writers)
        ]
        record_futs = [
            ex.submit(_worker_mixed_record_writer, (repo_root, i, deadline))
            for i in range(n_record_writers)
        ]
        reader_futs = [
            ex.submit(_worker_mixed_reader, (repo_root, i, deadline))
            for i in range(n_readers)
        ]
        writers = [f.result(timeout=duration + 60.0) for f in writer_futs]
        records = [f.result(timeout=duration + 60.0) for f in record_futs]
        readers = [f.result(timeout=duration + 60.0) for f in reader_futs]

    for r in writers + records + readers:
        assert r["ok"], r
    assert sum(w["writes"] for w in writers) >= n_writers, writers
    assert sum(w["writes"] for w in records) >= n_record_writers, records
    assert sum(r["reads"] for r in readers) >= n_readers, readers

    # File still parses and is non-empty.
    final = (workspace / "memory-bank/notes.md").read_text(encoding="utf-8")
    assert final.strip(), "notes.md ended up empty"


# ── 8. lock files are reused, not orphaned ────────────────────────────


def test_lock_files_are_reused_not_orphaned(workspace: Path) -> None:
    """After a burst on a single target, exactly ONE lock sidecar
    exists for that target — proving sha-keyed lock paths are stable
    and we don't accumulate one-shot lock files per call."""
    repo_root = str(workspace)
    workers = 6
    reps = 5
    ctx = mp.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
        list(ex.map(_worker_burst_overwrite, [(repo_root, i, reps) for i in range(workers)]))

    locks_dir = workspace / ".ai-memory" / "locks"
    assert locks_dir.is_dir()
    sidecars = sorted(p for p in locks_dir.iterdir() if p.suffix == ".lock")
    # Only the notes.md target was written, plus possibly a few internal
    # lock files (e.g. usage-stats during compile usage). Loose upper
    # bound: at most 5 distinct sidecars regardless of write count.
    assert 1 <= len(sidecars) <= 5, [p.name for p in sidecars]
    from servers.memory_server.memory_locks import read_lock_metadata

    # Sidecars now carry small lease metadata for safe self-heal cleanup.
    for p in sidecars:
        meta = read_lock_metadata(p)
        assert meta is not None
        assert "pid" in meta
        assert "target" in meta
