from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from servers.memory_server.memory_compactor import compact_memory, recover_compaction_transactions
from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_durable_jobs import DurableJobQueue
from servers.memory_server.memory_key_document_jobs import (
    drain_key_document_rebuild_jobs,
    enqueue_key_document_rebuild,
    read_key_document_rebuild_jobs,
)
from servers.memory_server.memory_reader import memory_get
from servers.memory_server.memory_records import memory_write_record
from servers.memory_server.memory_request_id import content_sha
from servers.memory_server.memory_worker import MemoryBackgroundWorker


def test_durable_queue_reclaims_expired_lease_retries_and_dead_letters(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = load_config(tmp_path)
    queue = DurableJobQueue(config, "test-jobs")
    clock = [datetime(2026, 8, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr("servers.memory_server.memory_durable_jobs._now_dt", lambda: clock[0])

    queued = queue.enqueue(kind="test", payload={"value": 1}, max_attempts=3)
    first = queue.claim(worker_id="worker-a", lease_seconds=1)
    assert first and first["job_id"] == queued["job_id"]

    clock[0] += timedelta(seconds=2)
    recovered = queue.read()
    assert recovered["reclaimed"] == [queued["job_id"]]
    second = queue.claim(worker_id="worker-b", lease_seconds=1)
    assert second and second["attempts"] == 2
    queue.fail(second["job_id"], second["lease_token"], error="transient", retry_base_seconds=0)

    third = queue.claim(worker_id="worker-b", lease_seconds=1)
    assert third and third["attempts"] == 3
    queue.fail(third["job_id"], third["lease_token"], error="permanent", retry_base_seconds=0)
    final = queue.read()
    assert final["jobs"][queued["job_id"]]["status"] == "dead"
    assert final["dead_letter"] == [queued["job_id"]]


def test_job_claimed_by_exited_process_is_recovered_from_disk(tmp_path: Path) -> None:
    script = """
import sys
from pathlib import Path
from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_durable_jobs import DurableJobQueue
config = load_config(Path(sys.argv[1]))
queue = DurableJobQueue(config, 'process-exit')
queued = queue.enqueue(kind='crash', payload={'durable': True})
claimed = queue.claim(worker_id='child-process', lease_seconds=1)
assert claimed and claimed['job_id'] == queued['job_id']
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert child.returncode == 0, child.stderr
    time.sleep(1.1)
    queue = DurableJobQueue(load_config(tmp_path), "process-exit")
    recovered = queue.read()
    assert len(recovered["reclaimed"]) == 1
    job_id = recovered["reclaimed"][0]
    assert recovered["jobs"][job_id]["status"] == "pending"


def test_durable_queue_recovers_last_committed_state_and_reports_double_corruption(tmp_path: Path) -> None:
    config = load_config(tmp_path)
    queue = DurableJobQueue(config, "recovery")
    first = queue.enqueue(kind="one", payload={"sequence": 1})
    queue.enqueue(kind="two", payload={"sequence": 2})
    queue.path.write_text("corrupt", encoding="utf-8")

    recovered = queue.read()
    assert recovered["ok"] is True
    assert recovered["recovered"] is True
    assert first["job_id"] in recovered["jobs"]

    queue.path.write_text("corrupt-live", encoding="utf-8")
    queue.backup_path.write_text("corrupt-backup", encoding="utf-8")
    failed = queue.read()
    assert failed["ok"] is False
    assert failed["error"] == "queue_corrupt"


def test_durable_queue_recovers_backup_when_live_state_is_structurally_poisoned(tmp_path: Path) -> None:
    config = load_config(tmp_path)
    queue = DurableJobQueue(config, "structural-recovery")
    first = queue.enqueue(kind="one", payload={"sequence": 1})
    queue.enqueue(kind="two", payload={"sequence": 2})
    state = json.loads(queue.path.read_text(encoding="utf-8"))
    poisoned_id = state["queue"][-1]
    state["jobs"][poisoned_id]["payload"] = ["not", "an", "object"]
    queue.path.write_text(json.dumps(state), encoding="utf-8")

    recovered = queue.read()
    assert recovered["ok"] is True
    assert recovered["recovered"] is True
    assert first["job_id"] in recovered["jobs"]


def test_durable_queue_rejects_non_json_payload_without_mutating_state(tmp_path: Path) -> None:
    config = load_config(tmp_path)
    queue = DurableJobQueue(config, "json-safe")
    with pytest.raises(ValueError, match="JSON serializable"):
        queue.enqueue(kind="bad", payload={"value": object()})
    state = queue.read()
    assert state["ok"] is True
    assert state["jobs"] == {}
    assert state["queue"] == []


def test_lease_guard_heartbeats_long_running_job(tmp_path: Path) -> None:
    config = load_config(tmp_path)
    queue = DurableJobQueue(config, "heartbeat")
    queue.enqueue(kind="slow", payload={})
    claimed = queue.claim(worker_id="owner", lease_seconds=1)
    assert claimed is not None

    with queue.lease_guard(claimed["job_id"], claimed["lease_token"], lease_seconds=1):
        time.sleep(1.2)
        assert queue.claim(worker_id="competitor", lease_seconds=1) is None

    finished = queue.succeed(claimed["job_id"], claimed["lease_token"], result={"ok": True})
    assert finished["ok"] is True


def test_lost_lease_requeues_key_document_job_instead_of_reporting_success(repo: Path, monkeypatch) -> None:
    config = load_config(repo)
    queued = enqueue_key_document_rebuild(
        config,
        targets=["progress"],
        user=None,
        renderer="deterministic",
        guard_prefer_llm=False,
    )
    assert queued["ok"] is True
    monkeypatch.setattr(DurableJobQueue, "heartbeat", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        "servers.memory_server.memory_key_document_jobs.rebuild_key_documents",
        lambda *_args, **_kwargs: {"ok": True, "written": {"progress": "memory-bank/progress.md"}, "errors": {}},
    )

    drained = drain_key_document_rebuild_jobs(config, max_jobs=1, worker_id="lease-loser")
    assert drained["ok"] is True
    assert drained["jobs"][0]["ok"] is False
    assert drained["jobs"][0]["result"]["error"] == "lease_lost"
    state = read_key_document_rebuild_jobs(config)
    assert state["jobs"][queued["job_id"]]["status"] == "pending"


def test_prepared_compaction_is_replayed_after_restart(repo: Path) -> None:
    config = load_config(repo)
    target = repo / ".ai-context" / "latest-error.md"
    source = target.read_text(encoding="utf-8")
    candidate = "# Recovered compact\n\n- transaction resumed\n"
    journal = repo / ".ai-memory" / "transactions" / "compact-test.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(
        json.dumps(
            {
                "transaction_id": "compact-test",
                "operation": "memory_compact",
                "state": "prepared",
                "path": ".ai-context/latest-error.md",
                "source_sha": content_sha(source),
                "candidate_sha": content_sha(candidate),
                "candidate_content": candidate,
            }
        ),
        encoding="utf-8",
    )

    recovered = recover_compaction_transactions(config)
    assert recovered["ok"] is True
    assert recovered["recovered"] == 1
    assert target.read_text(encoding="utf-8") == candidate
    assert json.loads(journal.read_text(encoding="utf-8"))["state"] == "committed"


def test_compaction_compare_and_swap_refuses_concurrent_source_change(repo: Path, monkeypatch) -> None:
    from servers.memory_server import memory_compactor as module

    config = load_config(repo)
    target = repo / ".ai-context" / "latest-error.md"
    original_backup = module.backup_files

    def backup_then_external_edit(*args, **kwargs):
        result = original_backup(*args, **kwargs)
        target.write_text("# External edit\n\nMust win the race.\n", encoding="utf-8")
        return result

    monkeypatch.setattr(module, "backup_files", backup_then_external_edit)
    result = compact_memory(
        config,
        path=".ai-context/latest-error.md",
        policy="error_summary",
        dry_run=False,
    )
    assert result["ok"] is False
    assert result["error"] == "source_changed"
    assert target.read_text(encoding="utf-8").startswith("# External edit")


def test_missing_compaction_target_is_sealed_as_conflict(repo: Path) -> None:
    config = load_config(repo)
    candidate = "# Candidate\n"
    journal = repo / ".ai-memory" / "transactions" / "compact-missing.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(
        json.dumps(
            {
                "transaction_id": "compact-missing",
                "operation": "memory_compact",
                "state": "prepared",
                "path": ".ai-context/missing.md",
                "source_sha": content_sha("# Old\n"),
                "candidate_sha": content_sha(candidate),
                "candidate_content": candidate,
            }
        ),
        encoding="utf-8",
    )
    result = recover_compaction_transactions(config)
    persisted = json.loads(journal.read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert result["conflicts"] == 1
    assert persisted["state"] == "conflict"
    assert "candidate_content" not in persisted


def test_corrupt_compaction_journal_surfaces_recovery_failure(repo: Path) -> None:
    config = load_config(repo)
    journal = repo / ".ai-memory" / "transactions" / "compact-corrupt.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("not-json", encoding="utf-8")
    result = recover_compaction_transactions(config)
    assert result["ok"] is False
    assert result["error"] == "recovery_incomplete"


def test_compaction_event_failure_does_not_invalidate_committed_write(repo: Path, monkeypatch) -> None:
    config = load_config(repo)
    target = repo / ".ai-context" / "latest-error.md"
    monkeypatch.setattr(
        "servers.memory_server.memory_compactor.append_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("audit disk unavailable")),
    )
    result = compact_memory(
        config,
        path=".ai-context/latest-error.md",
        policy="error_summary",
        dry_run=False,
        backup=False,
        archive_original=False,
    )
    assert result["ok"] is True
    assert target.read_text(encoding="utf-8").startswith("# Latest Error Summary")
    assert result["warnings"][0]["code"] == "event_log_deferred"


def test_record_pack_append_is_atomic_and_event_failure_is_non_blocking(repo: Path, monkeypatch) -> None:
    config = load_config(repo)
    first = memory_write_record(config, content_markdown="# First\n\nDurable.\n", task_id="task-pack-atomic")
    assert first["ok"] is True
    target = repo / first["path"]
    original = target.read_bytes()

    monkeypatch.setattr(
        "servers.memory_server.memory_record_io._atomic_write_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("replace interrupted")),
    )
    failed = memory_write_record(config, content_markdown="# Second\n\nMust not tear.\n", task_id="task-pack-atomic")
    assert failed["ok"] is False
    assert target.read_bytes() == original

    monkeypatch.undo()
    monkeypatch.setattr(
        "servers.memory_server.memory_records.append_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("event log unavailable")),
    )
    written = memory_write_record(config, content_markdown="# Third\n\nPrimary survives.\n", task_id="task-pack-atomic")
    assert written["ok"] is True
    assert any(item["code"] == "event_log_deferred" for item in written["warnings"])


def test_failed_periodic_worker_step_is_retried_without_waiting_full_interval(repo: Path, monkeypatch) -> None:
    config = load_config(repo)
    worker = MemoryBackgroundWorker(lambda: config)
    worker._last_encoding_audit = float("inf")
    worker._last_curator = float("inf")
    calls = []

    def index_step(_config):
        calls.append(len(calls))
        return {"ok": len(calls) > 1, "error": "transient" if len(calls) == 1 else None}

    monkeypatch.setattr("servers.memory_server.memory_worker.ensure_index_fresh", index_step)
    worker.run_once(config)
    worker.run_once(config)
    assert len(calls) == 2


def test_worker_instance_can_restart_after_clean_stop(repo: Path, monkeypatch) -> None:
    config = load_config(repo)
    config.worker["startup_grace_seconds"] = 0
    config.worker["poll_seconds"] = 0.05
    worker = MemoryBackgroundWorker(lambda: config)
    calls = []
    monkeypatch.setattr(worker, "run_once", lambda *_args, **_kwargs: calls.append(time.monotonic()) or {"ok": True})

    worker.start()
    deadline = time.monotonic() + 1.0
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    worker.stop(timeout=1.0)
    first_count = len(calls)
    worker.start()
    deadline = time.monotonic() + 1.0
    while len(calls) == first_count and time.monotonic() < deadline:
        time.sleep(0.01)
    worker.stop(timeout=1.0)
    assert first_count >= 1
    assert len(calls) > first_count


def test_worker_waits_for_startup_grace_before_recovering_jobs(repo: Path, monkeypatch) -> None:
    config = load_config(repo)
    config.worker["startup_grace_seconds"] = 0.2
    config.worker["poll_seconds"] = 0.05
    worker = MemoryBackgroundWorker(lambda: config)
    calls = []
    monkeypatch.setattr(worker, "run_once", lambda *_args, **_kwargs: calls.append(time.monotonic()) or {"ok": True})

    started = time.monotonic()
    worker.start()
    time.sleep(0.08)
    assert calls == []
    deadline = time.monotonic() + 1.0
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    worker.stop(timeout=1.0)
    assert calls
    assert calls[0] - started >= 0.18


def test_background_step_failure_does_not_break_basic_memory_reads(repo: Path, monkeypatch) -> None:
    config = load_config(repo)
    worker = MemoryBackgroundWorker(lambda: config)
    worker._last_index_check = float("inf")
    worker._last_encoding_audit = float("inf")
    worker._last_curator = float("inf")
    monkeypatch.setattr(
        "servers.memory_server.memory_worker.drain_project_reflection_jobs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("LLM chain down")),
    )

    status = worker.run_once(config)
    direct_read = memory_get(config, path="memory-bank/notes.md")
    assert status["ok"] is False
    assert "LLM chain down" in status["steps"]["reflection"]["error"]
    assert direct_read["ok"] is True
