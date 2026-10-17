from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from servers.memory_server import memory_durable_jobs, memory_llm_runner, memory_reflection_state
from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_durable_jobs import DurableJobQueue
from servers.memory_server.memory_llm import LLMClient, LLMConfig, LLMInputTooLarge, LLMRequestError
from servers.memory_server.memory_record_io import find_record_by_id, write_same_record
from servers.memory_server.memory_records import memory_write_record
from servers.memory_server.memory_reflection import _run_two_pass, collect_task_evidence, reflect_task
from servers.memory_server.memory_reflection_jobs import drain_project_reflection_jobs, enqueue_project_reflection
from servers.memory_server.memory_reflection_state import read_reflection_state, reflection_state_path


def _config(repo, *, quiet=0, maximum=0):
    path = repo / ".ai-memory/config.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["reflection"] = {"enabled": True, "debounce_seconds": quiet, "max_wait_seconds": maximum,
                         "auto_publish": False, "curator_enabled": False}
    raw["llm_defaults"] = {"capabilities": {"project_reflection": {"enabled": True}}}
    path.write_text(json.dumps(raw), encoding="utf-8")
    return load_config(repo)


def _write(config, body="# Evidence\n\nKeep complete evidence.", **kwargs):
    return memory_write_record(config, content_markdown=body, record_kind="decision",
                               task_id=kwargs.pop("task_id", "task-cost"), author="tester",
                               scope=kwargs.pop("scope", "personal"), **kwargs)


@pytest.fixture(autouse=True)
def isolated_llm_config(repo, monkeypatch):
    monkeypatch.setattr(memory_reflection_state, "PLUGIN_ROOT", repo)
    monkeypatch.setattr(memory_llm_runner, "_default_client_factory",
                        lambda _profile: (_ for _ in ()).throw(AssertionError("real LLM forbidden")))


def _fake_client(monkeypatch, calls, *, input_cap=32000, candidates=None):
    def transport(_url, _headers, body, _timeout):
        calls.append(json.loads(body))
        frame = {"summary": "checked", "proposals": candidates or []}
        return 200, json.dumps({"choices": [{"message": {"content": json.dumps(frame)}}],
                                "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

    factory_calls = []

    def factory(_profile):
        factory_calls.append(True)
        return LLMClient(LLMConfig(api_key="test-only", max_input_tokens_per_call=input_cap,
                                  retry_backoff_seconds=0), transport=transport)

    monkeypatch.setattr(memory_llm_runner, "_default_client_factory", factory)
    return factory_calls


def _queue(config):
    return DurableJobQueue(config, "project-reflection")


def test_100_triggers_coalesce_and_maximum_wait_is_bounded(repo, monkeypatch):
    config = _config(repo, quiet=300, maximum=1800)
    clock = [datetime(2026, 10, 12, tzinfo=timezone.utc)]
    monkeypatch.setattr(memory_durable_jobs, "_now_dt", lambda: clock[0])
    first = clock[0]
    ids = []
    for index in range(100):
        clock[0] = first + timedelta(seconds=index * 20)
        result = enqueue_project_reflection(config, task_id="task-cost", user=None, trigger="task_done")
        ids.append(result["job_id"])
    assert len(set(ids)) == 1
    job = _queue(config).read()["jobs"][ids[0]]
    assert datetime.fromisoformat(job["available_at"]) == first + timedelta(seconds=1800)
    clock[0] = first + timedelta(seconds=1799)
    assert _queue(config).claim(worker_id="early", lease_seconds=30) is None
    clock[0] += timedelta(seconds=1)
    assert _queue(config).claim(worker_id="ready", lease_seconds=30) is not None


def test_quiet_window_survives_queue_recreation(repo, monkeypatch):
    config = _config(repo, quiet=300, maximum=1800)
    clock = [datetime(2026, 10, 12, tzinfo=timezone.utc)]
    monkeypatch.setattr(memory_durable_jobs, "_now_dt", lambda: clock[0])
    enqueue_project_reflection(config, task_id="task-cost", user=None, trigger="test_failed")
    clock[0] += timedelta(seconds=299)
    assert _queue(load_config(repo)).claim(worker_id="early", lease_seconds=30) is None
    clock[0] += timedelta(seconds=1)
    assert _queue(load_config(repo)).claim(worker_id="ready", lease_seconds=30) is not None


def test_running_task_has_one_successor_and_other_tasks_can_run(repo):
    config = _config(repo)
    enqueue_project_reflection(config, task_id="task-cost", user=None, trigger="task_done")
    running = _queue(config).claim(worker_id="first", lease_seconds=120)
    for _ in range(10):
        enqueue_project_reflection(config, task_id="task-cost", user=None, trigger="test_failed")
    assert len(_queue(config).read()["queue"]) == 1
    root = Path(__file__).resolve().parents[2]
    child = subprocess.run([sys.executable, "-c",
                            "from pathlib import Path; from servers.memory_server.memory_config import load_config; "
                            "from servers.memory_server.memory_durable_jobs import DurableJobQueue; "
                            f"q=DurableJobQueue(load_config(Path({str(repo)!r})), 'project-reflection'); "
                            "assert q.claim(worker_id='second-process', lease_seconds=120) is None"],
                           cwd=root, capture_output=True, text=True, timeout=30)
    assert child.returncode == 0, child.stderr
    enqueue_project_reflection(config, task_id="other-task", user=None, trigger="task_done")
    other = _queue(config).claim(worker_id="other", lease_seconds=120)
    assert other["payload"]["task_id"] == "other-task"
    _queue(config).succeed(running["job_id"], running["lease_token"], result={"ok": True})
    assert _queue(config).claim(worker_id="next", lease_seconds=120)["payload"]["task_id"] == "task-cost"


def test_unchanged_evidence_skips_llm_after_restart_and_prune(repo, monkeypatch):
    config = _config(repo)
    source = _write(config)
    calls = []
    _fake_client(monkeypatch, calls)
    assert reflect_task(config, task_id="task-cost")["ok"]
    assert len(calls) == 1
    for _ in range(10):
        assert reflect_task(load_config(repo), task_id="task-cost")["skip_reason"] == "no_new_evidence"
    _queue(config).prune(keep_completed=0)
    state = read_reflection_state(reflection_state_path(config, "task-cost"))
    assert source["id"] in state["processed"]["record_versions"]
    root = Path(__file__).resolve().parents[2]
    child = subprocess.run([sys.executable, "-c",
                            "from pathlib import Path; from servers.memory_server.memory_config import load_config; "
                            "from servers.memory_server.memory_reflection import reflect_task; "
                            f"r=reflect_task(load_config(Path({str(repo)!r})), task_id='task-cost'); "
                            "assert r.get('skip_reason') == 'no_new_evidence', r"],
                           cwd=root, capture_output=True, text=True, timeout=30)
    assert child.returncode == 0, child.stderr
    assert len(calls) == 1


def test_same_id_content_change_and_new_record_invalidate_watermark(repo, monkeypatch):
    config = _config(repo)
    source = _write(config)
    calls = []
    _fake_client(monkeypatch, calls)
    reflect_task(config, task_id="task-cost")
    abs_path, rel_path, metadata, body = find_record_by_id(config, source["id"])
    stamp = abs_path.stat()
    assert write_same_record(config, abs_path=abs_path, rel_path=rel_path, metadata=metadata,
                             body=body.replace("complete", "updated!"))["ok"]
    os.utime(abs_path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert reflect_task(config, task_id="task-cost")["ok"]
    _write(config, "# Correction\n\nReconsider the previous conclusion.", supersedes=[source["id"]])
    assert reflect_task(config, task_id="task-cost")["ok"]
    assert len(calls) == 3


def test_auto_summary_dedup_requires_all_sources_and_keeps_corrections(repo):
    config = _config(repo)
    first = _write(config)
    second = _write(config, task_id="outside-task")
    duplicate = _write(config, "# Shared summary\n\nSame fact.", scope="project_shared",
                       provenance="auto_team_settlement", derived_from_record_ids=[first["id"]])
    partial = _write(config, "# Multi-source\n\nAdditional fact.", scope="project_shared",
                     provenance="auto_team_settlement", derived_from_record_ids=[first["id"], second["id"]])
    correction = _write(config, "# Correction\n\nThe previous claim is wrong.",
                        derived_from_record_ids=[first["id"]], conflicts_with=[first["id"]])
    result = collect_task_evidence(config, task_id="task-cost")
    ids = {item["id"] for item in result["evidence"]}
    assert duplicate["id"] not in ids
    assert {first["id"], partial["id"], correction["id"]} <= ids
    assert next(item for item in result["evidence"] if item["id"] == correction["id"])["conflicts_with"] == [first["id"]]


def test_collection_overflow_does_not_truncate_or_advance_watermark(repo, monkeypatch):
    config = _config(repo)
    config.reflection["max_evidence_records"] = 1
    sources = [_write(config), _write(config, "# Second\n\nAlso important.")]
    result = reflect_task(config, task_id="task-cost")
    assert not result["ok"] and result["retryable"] is False
    assert set(result["pending_record_ids"]) == {source["id"] for source in sources}
    state = read_reflection_state(reflection_state_path(config, "task-cost"))
    assert "processed" not in state
    assert reflect_task(config, task_id="task-cost")["suppressed"]
    config.reflection["max_evidence_records"] = 256
    config = replace(config, config_hash="effective-capacity-change")
    calls = []
    _fake_client(monkeypatch, calls)
    assert reflect_task(config, task_id="task-cost")["ok"]
    assert len(calls) == 1


def test_input_limit_is_not_retried_until_evidence_or_llm_config_changes(repo, monkeypatch):
    config = _config(repo)
    _write(config)
    calls = []
    factory_calls = _fake_client(monkeypatch, calls, input_cap=10)
    result = reflect_task(config, task_id="task-cost")
    assert result["llm_status"] == "input_too_large" and not result["retryable"]
    for _ in range(5):
        assert reflect_task(load_config(repo), task_id="task-cost")["suppressed"]
    assert len(factory_calls) == 1 and not calls
    monkeypatch.setenv("MEMORY_LLM_MODEL", "changed-model")
    assert not reflect_task(config, task_id="task-cost").get("suppressed")
    assert len(factory_calls) == 2
    _write(config, "# New evidence\n\nRelevant change.")
    assert not reflect_task(config, task_id="task-cost").get("suppressed")
    assert len(factory_calls) == 3 and not calls


def test_deterministic_queue_failure_dies_after_one_attempt(repo, monkeypatch):
    config = _config(repo)
    _write(config)
    _fake_client(monkeypatch, [], input_cap=10)
    queued = enqueue_project_reflection(config, task_id="task-cost", user=None, trigger="task_done")
    result = drain_project_reflection_jobs(config, max_jobs=4)
    assert result["processed"] == 1
    job = _queue(config).read()["jobs"][queued["job_id"]]
    assert job["status"] == "dead" and job["attempts"] == 1 and not job["retryable"]


def test_empty_candidates_skip_critic_and_critic_gets_complete_referenced_evidence():
    calls = []
    frame = {"proposals": []}

    class Client:
        config = LLMConfig(api_key="test-only")

        def complete_text(self, prompt, **kwargs):
            calls.append(prompt)
            return json.dumps(frame)

    profile = memory_llm_runner.CapabilityProfile(enabled=True, max_tokens=1024)
    evidence = [{"id": "first", "record_kind": "decision", "body": "x" * 5000 + "critical-tail"},
                {"id": "unrelated", "record_kind": "note", "body": "not referenced"}]
    result = _run_two_pass(Client(), profile, task_id="task-cost", evidence=evidence)
    assert result["critic_skipped"] and len(calls) == 1
    frame["proposals"] = [{"supporting_record_ids": ["first"]}]
    _run_two_pass(Client(), profile, task_id="task-cost", evidence=evidence)
    critic = json.loads(calls[-1])
    assert [item["id"] for item in critic["evidence"]] == ["first"]
    assert critic["evidence"][0]["body"].endswith("critical-tail")


@pytest.mark.parametrize("status,retryable", [(401, False), (400, False), (429, True), (500, True), (501, False)])
def test_runner_preserves_retry_classification(repo, status, retryable):
    config = _config(repo)

    def fail(_client, _profile):
        raise LLMRequestError("upstream failure", status=status)

    result = memory_llm_runner.run_llm_capability(config, "project_reflection", fail,
        client_factory=lambda _profile: LLMClient(LLMConfig(api_key="test-only")))
    assert result.meta["retryable"] is retryable


def test_request_events_include_task_job_stage_tokens_and_retry_reason(repo, monkeypatch):
    config = _config(repo)
    _write(config)
    source = collect_task_evidence(config, task_id="task-cost")["evidence"][0]
    frame = {"proposals": [{"kind": "decision", "title": "Keep evidence", "content_markdown": "Keep all evidence.",
                            "confidence": 0.9, "importance": 0.9, "supporting_record_ids": [source["id"]]}]}
    attempt = []
    events = []

    def transport(*_args):
        attempt.append(True)
        if len(attempt) == 1:
            return 429, "{}"
        return 200, json.dumps({"choices": [{"message": {"content": json.dumps(frame)}}],
                                "usage": {"prompt_tokens": 123, "completion_tokens": 45}})

    monkeypatch.setattr(memory_llm_runner, "_default_client_factory", lambda _profile:
                        LLMClient(LLMConfig(api_key="test-only", retry_backoff_seconds=0), transport=transport))
    monkeypatch.setattr("servers.memory_server.memory_events.append_event",
                        lambda _config, kind, payload, **_kwargs: events.append((kind, payload)))
    result = reflect_task(config, task_id="task-cost", job_id="job-cost", job_attempt=2)
    assert result["ok"]
    requests = [payload for kind, payload in events if kind == "llm_request_completed"]
    assert len(requests) == 3
    assert {item["stage"] for item in requests} == {"extract", "critic"}
    assert all(item["task_id"] == "task-cost" and item["job_id"] == "job-cost" and item["job_attempt"] == 2 for item in requests)
    assert requests[0]["retry_reason"] == "http_429" and requests[0]["input_tokens"] is None
    assert requests[1]["input_tokens"] == 123 and requests[1]["output_tokens"] == 45
    assert "test-only" not in json.dumps(events)


def test_direct_concurrent_reflection_is_single_flight(repo, monkeypatch):
    config = _config(repo)
    _write(config)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def transport(*_args):
        calls.append(True)
        entered.set()
        assert release.wait(5)
        return 200, json.dumps({"choices": [{"message": {"content": '{"proposals":[]}'}}]})

    monkeypatch.setattr(memory_llm_runner, "_default_client_factory", lambda _profile:
                        LLMClient(LLMConfig(api_key="test-only"), transport=transport))
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(reflect_task, config, task_id="task-cost")
        assert entered.wait(5)
        try:
            assert reflect_task(config, task_id="task-cost")["error"] == "reflection_busy"
        finally:
            release.set()
        assert future.result()["ok"]
    assert len(calls) == 1


def test_unreadable_evidence_and_corrupt_state_never_mark_success(repo, monkeypatch):
    config = _config(repo)
    _write(config)
    original = collect_task_evidence(config, task_id="task-cost")
    monkeypatch.setattr("servers.memory_server.memory_reflection.iter_parsed_records",
                        lambda _config: ([], {"skipped_read_errors": 1}))
    assert reflect_task(config, task_id="task-cost")["error"] == "evidence_read_incomplete"
    path = reflection_state_path(config, "task-cost")
    assert not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("broken state", encoding="utf-8")
    assert reflect_task(config, task_id="task-cost")["error"] == "reflection_state_unavailable"
    assert original["record_versions"]


def test_new_evidence_during_run_queues_one_followup_without_checkpoint(repo, monkeypatch):
    config = _config(repo)
    first = _write(config)
    calls = []
    new_ids = []

    def transport(*_args):
        calls.append(True)
        if len(calls) == 1:
            new_ids.append(_write(config, "# New while running\n\nDo not lose this.")["id"])
        return 200, json.dumps({"choices": [{"message": {"content": '{"proposals":[]}'}}]})

    monkeypatch.setattr(memory_llm_runner, "_default_client_factory", lambda _profile:
                        LLMClient(LLMConfig(api_key="test-only"), transport=transport))
    result = reflect_task(config, task_id="task-cost")
    assert result["follow_up"]["queued"]
    state = read_reflection_state(reflection_state_path(config, "task-cost"))
    assert set(state["processed"]["record_versions"]) == {first["id"]}
    assert len(_queue(config).read()["queue"]) == 1
    assert drain_project_reflection_jobs(config)["processed"] == 1
    state = read_reflection_state(reflection_state_path(config, "task-cost"))
    assert new_ids[0] in state["processed"]["record_versions"]
    assert len(calls) == 2


def test_valid_config_rejects_contradictory_debounce_window(repo):
    config = _config(repo)
    path = repo / ".ai-memory/config.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["reflection"].update(debounce_seconds=300, max_wait_seconds=10)
    path.write_text(json.dumps(raw), encoding="utf-8")
    from servers.memory_server.memory_config import MemoryConfigError

    with pytest.raises(MemoryConfigError, match="max_wait_seconds"):
        load_config(repo)
    assert config.reflection["max_wait_seconds"] == 0


def test_trigger_replay_coalesces_continuous_writes_and_skips_unchanged_rounds():
    from scripts.replay_reflection_triggers import replay

    start = datetime(2026, 10, 12, tzinfo=timezone(timedelta(hours=8)))
    events = []
    for index in range(100):
        stamp = (start + timedelta(seconds=index * 20)).isoformat()
        events.extend([
            {"ts": stamp, "event_type": "memory_write_record", "payload": {"task_id": "task-cost"}},
            {"ts": stamp, "event_type": "project_reflection_queued", "payload": {"task_id": "task-cost"}},
        ])
    events.append({"ts": (start + timedelta(hours=2)).isoformat(), "event_type": "project_reflection_queued",
                   "payload": {"task_id": "task-cost"}})
    result = replay(events, day="2026-10-12")
    assert result["trigger_events"] == 101
    assert result["debounced_rounds"] == 3
    assert result["changed_evidence_rounds"] == 2
    assert result["no_new_evidence_rounds"] == 1
    assert result["two_pass_call_ceiling_for_changed_rounds"] == 4


def _candidate(source_id):
    return {"kind": "decision", "title": "Recover complete evidence", "content_markdown": "Keep the complete result until delivery.",
            "confidence": 0.99, "importance": 0.9, "supporting_record_ids": [source_id]}


def test_crash_after_cache_before_queue_success_replays_across_processes(repo, monkeypatch):
    from servers.memory_server.memory_reflection_jobs import _prior_support

    config = _config(repo)
    source = _write(config)
    queued = enqueue_project_reflection(config, task_id="task-cost", user=None, trigger="task_done")
    frame = json.dumps({"proposals": [_candidate(source["id"])]})
    response = json.dumps({"choices": [{"message": {"content": frame}}]})
    root = Path(__file__).resolve().parents[2]
    child_code = (
        "import os; from pathlib import Path; from servers.memory_server import memory_llm_runner as runner; "
        "from servers.memory_server.memory_config import load_config; "
        "from servers.memory_server.memory_llm import LLMClient,LLMConfig; "
        "from servers.memory_server.memory_durable_jobs import DurableJobQueue; "
        "from servers.memory_server.memory_reflection_jobs import drain_project_reflection_jobs; "
        f"runner._default_client_factory=lambda _profile: LLMClient(LLMConfig(api_key='test-only'), transport=lambda *args:(200,{response!r})); "
        "DurableJobQueue.succeed=lambda *args,**kwargs:os._exit(23); "
        f"drain_project_reflection_jobs(load_config(Path({str(repo)!r})))"
    )
    child = subprocess.run([sys.executable, "-c", child_code], cwd=root, capture_output=True, text=True, timeout=30)
    assert child.returncode == 23, child.stderr
    cached = read_reflection_state(reflection_state_path(config, "task-cost"))
    assert cached["pending_results"][queued["job_id"]]["result"]["proposals"]
    assert not cached["processed"]["delivered"]
    job = _queue(config).read()["jobs"][queued["job_id"]]
    clock = datetime.fromisoformat(job["lease_expires_at"]) + timedelta(seconds=1)
    monkeypatch.setattr(memory_durable_jobs, "_now_dt", lambda: clock)
    result = drain_project_reflection_jobs(config)
    replay = result["jobs"][0]["reflection"]
    assert replay["result_replayed"] and replay["proposals"]
    state = _queue(config).read()
    assert state["jobs"][queued["job_id"]]["status"] == "done"
    assert _prior_support(state)[replay["proposals"][0]["fingerprint"]] == {"task-cost"}
    settled = read_reflection_state(reflection_state_path(config, "task-cost"))
    assert settled["processed"]["delivered"] and not settled["pending_results"]
    assert reflect_task(config, task_id="task-cost")["skip_reason"] == "no_new_evidence"


@pytest.mark.parametrize("failure", ["lease_lost", "succeed_write_failed"])
def test_lease_or_queue_write_failure_preserves_results_without_llm_repeat(repo, monkeypatch, failure):
    from contextlib import contextmanager

    config = _config(repo)
    source = _write(config)
    calls = []
    _fake_client(monkeypatch, calls, candidates=[_candidate(source["id"])])
    queued = enqueue_project_reflection(config, task_id="task-cost", user=None, trigger="task_done")
    clock = [datetime.now(timezone.utc)]
    monkeypatch.setattr(memory_durable_jobs, "_now_dt", lambda: clock[0])
    guard = DurableJobQueue.lease_guard
    succeed = DurableJobQueue.succeed

    @contextmanager
    def lost_guard(*_args, **_kwargs):
        yield {"lost": True, "errors": []}

    def write_failed(*_args, **_kwargs):
        raise OSError("simulated durable commit failure")

    if failure == "lease_lost":
        monkeypatch.setattr(DurableJobQueue, "lease_guard", lost_guard)
        first = drain_project_reflection_jobs(config)
        assert first["jobs"][0]["reflection"]["error"] == "lease_lost"
    else:
        monkeypatch.setattr(DurableJobQueue, "succeed", write_failed)
        with pytest.raises(OSError, match="durable commit"):
            drain_project_reflection_jobs(config)
    clock[0] += timedelta(seconds=121)
    monkeypatch.setattr(DurableJobQueue, "lease_guard", guard)
    monkeypatch.setattr(DurableJobQueue, "succeed", succeed)
    recovered = drain_project_reflection_jobs(config)
    assert recovered["jobs"][0]["ok"]
    assert recovered["jobs"][0]["reflection"]["result_replayed"]
    assert recovered["jobs"][0]["reflection"]["proposals"]
    assert _queue(config).read()["jobs"][queued["job_id"]]["status"] == "done"
    assert len(calls) == 2


def test_pending_old_result_is_not_overwritten_by_new_evidence(repo, monkeypatch):
    config = _config(repo)
    source = _write(config)
    calls = []
    _fake_client(monkeypatch, calls, candidates=[_candidate(source["id"])])
    original = reflect_task(config, task_id="task-cost", job_id="uncommitted-job")
    _write(config, "# New evidence\n\nA later conclusion.")
    newer = reflect_task(config, task_id="task-cost", job_id="newer-job")
    assert newer["evidence_hash"] != original["evidence_hash"]
    replay = reflect_task(config, task_id="task-cost", job_id="uncommitted-job")
    assert replay["result_replayed"] and replay["evidence_hash"] == original["evidence_hash"]
    assert replay["proposals"] == original["proposals"]
    assert len(calls) == 4


def test_published_result_keeps_key_document_intent_recoverable(repo, monkeypatch):
    from servers.memory_server import memory_reflection_jobs
    from servers.memory_server.memory_key_document_jobs import read_key_document_rebuild_jobs

    config = _config(repo)
    path = repo / ".ai-memory/config.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["reflection"]["auto_publish"] = True
    raw["key_documents"] = {"auto_rebuild": {"enabled": True, "targets": ["progress"]}}
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = load_config(repo)
    source = memory_write_record(config, content_markdown="# Validated\n\nA supported test passed.",
                                 record_kind="validation_result", status="published", scope="personal",
                                 task_id="task-cost", author="tester")
    candidate = {**_candidate(source["id"]), "validation_evidence_ids": [source["id"]]}
    calls = []
    _fake_client(monkeypatch, calls, candidates=[candidate])
    queued = enqueue_project_reflection(config, task_id="task-cost", user=None, trigger="task_done")
    original_enqueue = memory_reflection_jobs.enqueue_key_document_rebuild
    monkeypatch.setattr(memory_reflection_jobs, "enqueue_key_document_rebuild",
                        lambda *_args, **_kwargs: {"ok": False, "error": "simulated_queue_failure"})
    clock = [datetime.now(timezone.utc)]
    monkeypatch.setattr(memory_durable_jobs, "_now_dt", lambda: clock[0])
    first = drain_project_reflection_jobs(config)
    assert first["jobs"][0]["reflection"]["error"] == "reflection_delivery_deferred"
    assert _queue(config).read()["jobs"][queued["job_id"]]["status"] == "pending"
    clock[0] += timedelta(seconds=3)
    monkeypatch.setattr(memory_reflection_jobs, "enqueue_key_document_rebuild", original_enqueue)
    original_succeed = DurableJobQueue.succeed

    def crash_before_commit(*_args, **_kwargs):
        assert read_key_document_rebuild_jobs(config)["queue"]
        raise OSError("crash after derived intent before result commit")

    monkeypatch.setattr(DurableJobQueue, "succeed", crash_before_commit)
    with pytest.raises(OSError, match="after derived intent"):
        drain_project_reflection_jobs(config)
    clock[0] += timedelta(seconds=121)
    monkeypatch.setattr(DurableJobQueue, "succeed", original_succeed)
    recovered = drain_project_reflection_jobs(config)
    assert recovered["jobs"][0]["reflection"]["result_replayed"]
    assert recovered["jobs"][0]["key_document_rebuild"]["coalesced"]
    assert len(read_key_document_rebuild_jobs(config)["queue"]) == 1
    assert _queue(config).read()["jobs"][queued["job_id"]]["status"] == "done"
    assert len(calls) == 2


@pytest.mark.parametrize("damage", ["pack_closing_marker", "record_front_matter", "record_metadata", "utf8"])
def test_damaged_record_with_valid_sibling_cannot_advance_watermark(repo, monkeypatch, damage):
    from servers.memory_server.memory_frontmatter import PACK_HEADER, render_record_pack_entry
    from servers.memory_server.memory_record_io import iter_parsed_records
    from servers.memory_server.memory_records import render_record_markdown

    config = _config(repo)
    source = _write(config)
    fixtures_dir = repo / "memory-bank" / "fault-fixtures"
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    ordinary = fixtures_dir / "plain-guide.md"
    ordinary.write_text("# Ordinary Markdown\n\nNot a memory record.\n", encoding="utf-8")
    broken = fixtures_dir / "broken-record.md"
    metadata = {"id": "damaged-evidence", "record_kind": "decision", "task_id": "task-cost"}
    markdown = render_record_markdown(metadata, "# Evidence\n\nDo not lose this.")
    if damage == "pack_closing_marker":
        packed = PACK_HEADER + "\n" + render_record_pack_entry("damaged-evidence", markdown)
        broken.write_text(packed.replace("<!-- /memory-record-pack-entry id=damaged-evidence -->", ""), encoding="utf-8")
    elif damage == "record_front_matter":
        broken.write_text("---\nid: damaged-evidence\nrecord_kind: decision\ntask_id: task-cost\n", encoding="utf-8")
    elif damage == "record_metadata":
        broken.write_text(render_record_markdown({"id": "damaged-evidence"}, "# Missing kind"), encoding="utf-8")
    else:
        broken.write_bytes(b"---\nid: damaged-evidence\nrecord_kind: decision\n---\n\xff")
    records, stats = iter_parsed_records(config)
    assert any(record.metadata["id"] == source["id"] for record in records)
    assert stats["skipped_read_errors" if damage == "utf8" else "skipped_parse_errors"] == 1
    result = reflect_task(config, task_id="task-cost")
    assert result["error"] == "evidence_read_incomplete"
    assert not reflection_state_path(config, "task-cost").exists()
    broken.write_text(markdown, encoding="utf-8")
    calls = []
    _fake_client(monkeypatch, calls)
    assert reflect_task(config, task_id="task-cost")["ok"]
    assert len(calls) == 1


def test_http_200_malformed_json_can_retry_and_then_settle_same_evidence(repo, monkeypatch):
    config = _config(repo)
    _write(config)
    attempts = []
    events = []
    monkeypatch.setattr("servers.memory_server.memory_events.append_event",
                        lambda _config, kind, payload, **_kwargs: events.append((kind, payload)))

    def transport(*_args):
        attempts.append(True)
        if len(attempts) == 1:
            return 200, "not-json"
        return 200, json.dumps({"choices": [{"message": {"content": '{"proposals":[]}'}}]})

    monkeypatch.setattr(memory_llm_runner, "_default_client_factory", lambda _profile:
                        LLMClient(LLMConfig(api_key="test-only", retry_backoff_seconds=0), transport=transport))
    queued = enqueue_project_reflection(config, task_id="task-cost", user=None, trigger="task_done")
    clock = [datetime.now(timezone.utc)]
    monkeypatch.setattr(memory_durable_jobs, "_now_dt", lambda: clock[0])
    first = drain_project_reflection_jobs(config)
    assert first["jobs"][0]["reflection"]["retryable"]
    assert _queue(config).read()["jobs"][queued["job_id"]]["status"] == "pending"
    assert not reflection_state_path(config, "task-cost").exists()
    clock[0] += timedelta(seconds=3)
    second = drain_project_reflection_jobs(config)
    assert second["jobs"][0]["ok"]
    assert len(attempts) == 2
    assert reflect_task(config, task_id="task-cost")["skip_reason"] == "no_new_evidence"
    requests = [payload for kind, payload in events if kind == "llm_request_completed"]
    assert requests[0]["status"] == "invalid_response"
    assert requests[0]["retry_reason"] == "invalid_json_response"
    assert requests[0]["input_tokens"] is None and requests[0]["output_tokens"] is None
    assert requests[1]["status"] == "ok"


def test_ack_failure_preserves_recoverable_result_without_extra_llm(repo, monkeypatch):
    from servers.memory_server import memory_reflection_jobs
    from servers.memory_server.memory_reflection_state import acknowledge_reflection_delivery

    config = _config(repo)
    source = _write(config)
    calls = []
    _fake_client(monkeypatch, calls, candidates=[_candidate(source["id"])])
    queued = enqueue_project_reflection(config, task_id="task-cost", user=None, trigger="task_done")
    monkeypatch.setattr(memory_reflection_jobs, "acknowledge_reflection_delivery",
                        lambda *_args, **_kwargs: {"ok": False, "error": "simulated_ack_failure"})
    first = drain_project_reflection_jobs(config)
    assert not first["jobs"][0]["ok"]
    assert _queue(config).read()["jobs"][queued["job_id"]]["result"]["proposals"]
    assert read_reflection_state(reflection_state_path(config, "task-cost"))["pending_results"]
    monkeypatch.setattr(memory_reflection_jobs, "acknowledge_reflection_delivery", acknowledge_reflection_delivery)
    enqueue_project_reflection(config, task_id="task-cost", user=None, trigger="task_done")
    second = drain_project_reflection_jobs(config)
    assert second["jobs"][0]["ok"] and second["jobs"][0]["reflection"]["result_replayed"]
    assert not read_reflection_state(reflection_state_path(config, "task-cost"))["pending_results"]
    assert len(calls) == 2
