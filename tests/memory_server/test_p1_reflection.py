from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_durable_jobs import DurableJobQueue
from servers.memory_server.memory_llm_runner import LLMRunResult
from servers.memory_server.memory_record_io import iter_parsed_records
from servers.memory_server.memory_records import memory_write_record
from servers.memory_server.memory_reflection import (
    _run_two_pass,
    collect_reflection_targets,
    collect_task_evidence,
    publish_reflection_proposal,
    proposal_fingerprint,
    reflect_task,
    validate_reflection_frame,
)
from servers.memory_server.memory_reflection_jobs import (
    curate_project_reflections,
    drain_project_reflection_jobs,
    enqueue_project_reflection,
)
from servers.memory_server.server_dispatch import _dispatch_tool


def _reflection_config(repo: Path, *, auto_targets: list[str] | None = None):
    path = repo / ".ai-memory" / "config.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["reflection"] = {
        "enabled": True,
        "trigger_phases": ["task_done", "test_failed"],
        "min_confidence": 0.8,
        "publish_min_confidence": 0.95,
        "publish_repeated_tasks": 2,
        "publish_with_validation_evidence": True,
        "auto_publish": True,
        "curator_enabled": True,
    }
    if auto_targets is not None:
        raw["key_documents"] = {
            "auto_rebuild": {
                "enabled": True,
                "targets": auto_targets,
            }
        }
    path.write_text(json.dumps(raw), encoding="utf-8")
    return load_config(repo)


def _write_evidence(config, *, task_id: str, kind: str, body: str):
    return memory_write_record(
        config,
        content_markdown=body,
        record_kind=kind,
        scope="project_shared",
        status="published",
        author="Codex",
        task_id=task_id,
        system_area="memory-mcp",
    )


def test_evidence_collection_excludes_secrets_and_reflection_outputs(repo: Path) -> None:
    config = _reflection_config(repo)
    good = _write_evidence(config, task_id="task-r", kind="decision", body="# Durable\n\nUse leases for workers.")
    secret = _write_evidence(config, task_id="task-r", kind="note", body="# Secret\n\napi_key=do-not-learn")
    derived = memory_write_record(
        config,
        content_markdown="# Derived\n\nDo not reflect a reflection.",
        record_kind="decision",
        scope="project_shared",
        status="distilled",
        author="memory-reflector",
        task_id="task-r",
        provenance="background_reflection",
        replaceable=True,
        authoritative=False,
    )

    result = collect_task_evidence(config, task_id="task-r")
    ids = {item["id"] for item in result["evidence"]}
    assert good["id"] in ids
    assert secret["id"] not in ids
    assert derived["id"] not in ids
    assert {item["reason"] for item in result["excluded"]} == {"secret_signal", "derived_or_snapshot"}


def test_reflection_targets_derive_title_from_parsed_record_body(repo: Path) -> None:
    config = _reflection_config(repo)
    existing = memory_write_record(
        config,
        content_markdown="# Existing reflection\n\nA replaceable project-level conclusion.",
        record_kind="decision",
        scope="project_shared",
        status="distilled",
        author="memory-reflector",
        task_id="task-existing",
        system_area="memory-mcp",
        provenance="background_reflection",
        replaceable=True,
        authoritative=False,
    )

    assert existing["ok"] is True
    targets = collect_reflection_targets(config, evidence=[{"system_area": "memory-mcp"}])
    target = next(item for item in targets if item["id"] == existing["id"])
    assert target["title"] == "Existing reflection"


def test_reflection_validator_rejects_unsupported_and_secret_bearing_proposals() -> None:
    evidence = [{"id": "e1", "record_kind": "decision", "body": "lease worker"}]
    frame = {
        "summary": "test",
        "proposals": [
            {
                "kind": "procedure",
                "title": "Supported",
                "content_markdown": "Renew a lease while long work runs.",
                "confidence": 0.9,
                "importance": 0.9,
                "supporting_record_ids": ["e1"],
                "validation_evidence_ids": ["e1"],
            },
            {
                "kind": "decision",
                "title": "Unsupported",
                "content_markdown": "No citation.",
                "confidence": 0.99,
                "importance": 0.9,
                "supporting_record_ids": ["missing"],
            },
            {
                "kind": "system_rule",
                "title": "Leak",
                "content_markdown": "api_key=should-never-persist",
                "confidence": 0.99,
                "importance": 1.0,
                "supporting_record_ids": ["e1"],
            },
        ],
    }
    result = validate_reflection_frame(frame, evidence=evidence, max_candidates=8, min_confidence=0.8)
    assert len(result["proposals"]) == 1
    assert result["proposals"][0]["validation_evidence_ids"] == []
    assert {item["reason"] for item in result["rejected"]} == {"missing_evidence", "secret_signal"}


def test_validation_evidence_must_be_published_and_support_the_same_proposal() -> None:
    evidence = [
        {"id": "decision", "record_kind": "decision", "status": "published", "body": "decision"},
        {"id": "validation", "record_kind": "validation_result", "status": "published", "body": "test"},
        {"id": "draft-validation", "record_kind": "validation_result", "status": "raw", "body": "draft"},
    ]
    frame = {
        "proposals": [
            {
                "kind": "procedure",
                "title": "No unrelated validation",
                "content_markdown": "Only directly supporting validation may unlock publication.",
                "confidence": 0.99,
                "importance": 0.9,
                "supporting_record_ids": ["decision", "draft-validation"],
                "validation_evidence_ids": ["validation", "draft-validation"],
            }
        ]
    }
    result = validate_reflection_frame(frame, evidence=evidence, max_candidates=8, min_confidence=0.8)
    assert result["proposals"][0]["validation_evidence_ids"] == []


def test_two_pass_reflection_uses_extractor_then_adversarial_critic() -> None:
    class Client:
        def __init__(self):
            self.calls = []
            self.config = type("Config", (), {"model": "fake-model"})()

        def complete_text(self, prompt, *, system, max_tokens):
            self.calls.append((prompt, system, max_tokens))
            return json.dumps({"summary": "ok", "proposals": []})

    client = Client()
    profile = type("Profile", (), {"max_tokens": 256})()
    result = _run_two_pass(
        client,
        profile,
        task_id="task-two-pass",
        evidence=[{"id": "e1", "record_kind": "decision", "body": "evidence"}],
    )
    assert result["model"] == "fake-model"
    assert len(client.calls) == 2
    assert "background reflection layer" in client.calls[0][1]
    assert "adversarial critic" in client.calls[1][1]


def test_two_pass_reflection_bounds_candidates_by_effective_client_token_cap() -> None:
    class Client:
        def __init__(self):
            self.calls = []
            self.config = type(
                "Config",
                (),
                {"model": "fake-model", "max_output_tokens_per_call": 1024},
            )()

        def complete_text(self, prompt, *, system, max_tokens):
            self.calls.append((prompt, system, max_tokens))
            return json.dumps({"summary": "ok", "proposals": []})

    client = Client()
    profile = type("Profile", (), {"max_tokens": 2048})()
    _run_two_pass(
        client,
        profile,
        task_id="task-budget",
        evidence=[{"id": "e1", "record_kind": "decision", "body": "evidence"}],
        max_candidates=8,
    )

    assert [call[2] for call in client.calls] == [1024, 1024]
    assert "Return at most 1 proposals" in client.calls[0][0]
    critic_payload = json.loads(client.calls[1][0])
    assert critic_payload["output_constraints"] == {
        "max_proposals": 1,
        "summary_max_chars": 96,
        "title_max_chars": 64,
        "content_markdown_max_chars": 160,
        "max_output_tokens": 1024,
    }


def test_validated_high_confidence_reflection_publishes_replaceable_project_memory(repo: Path, monkeypatch) -> None:
    config = _reflection_config(repo)
    source = _write_evidence(
        config,
        task_id="task-publish",
        kind="validation_result",
        body="# Crash recovery validated\n\nLease expiry replay passed fault injection.",
    )
    frame = {
        "summary": "validated",
        "proposals": [
            {
                "kind": "procedure",
                "title": "Recover leased background jobs",
                "content_markdown": "Reclaim expired leases on startup and retry from durable intent.",
                "confidence": 0.99,
                "importance": 0.95,
                "system_area": "memory-mcp",
                "supporting_record_ids": [source["id"]],
                "validation_evidence_ids": [source["id"]],
                "contradicts_record_ids": [],
            }
        ],
    }
    monkeypatch.setattr(
        "servers.memory_server.memory_reflection.run_llm_capability",
        lambda *_args, **_kwargs: LLMRunResult(
            ok=True,
            status="ok",
            capability="project_reflection",
            value={"frame": frame, "model": "fake-model"},
        ),
    )

    result = reflect_task(config, task_id="task-publish")
    assert result["ok"] is True
    assert result["proposals"][0]["publish_gate"] == "validation"
    assert result["published"][0]["ok"] is True
    records, _stats = iter_parsed_records(config)
    published = [record for record in records if record.metadata.get("provenance") == "background_reflection"]
    assert len(published) == 1
    assert published[0].metadata["scope"] == "project_shared"
    assert published[0].metadata["status"] == "distilled"
    assert published[0].metadata["authoritative"] == "false"
    assert published[0].metadata["replaceable"] == "true"


def test_explicit_contradiction_blocks_automatic_publication(repo: Path, monkeypatch) -> None:
    config = _reflection_config(repo)
    validation = _write_evidence(
        config,
        task_id="task-conflict",
        kind="validation_result",
        body="# Validation\n\nA test passed.",
    )
    contradiction = _write_evidence(
        config,
        task_id="task-conflict",
        kind="decision",
        body="# Contradiction\n\nThe rule is disputed.",
    )
    frame = {
        "proposals": [
            {
                "kind": "system_rule",
                "title": "Disputed rule",
                "content_markdown": "This must remain a candidate while contradictory evidence exists.",
                "confidence": 0.99,
                "importance": 0.99,
                "supporting_record_ids": [validation["id"]],
                "validation_evidence_ids": [validation["id"]],
                "contradicts_record_ids": [contradiction["id"]],
            }
        ]
    }
    monkeypatch.setattr(
        "servers.memory_server.memory_reflection.run_llm_capability",
        lambda *_args, **_kwargs: LLMRunResult(
            ok=True,
            status="ok",
            capability="project_reflection",
            value={"frame": frame, "model": "fake-model"},
        ),
    )
    result = reflect_task(config, task_id="task-conflict")
    assert result["proposals"][0]["publish_gate"] == "conflict"
    assert result["proposals"][0]["publish_eligible"] is False
    assert result["published"] == []


def test_curator_requires_two_distinct_tasks_and_deduplicates_publication(repo: Path) -> None:
    config = _reflection_config(repo)
    first = _write_evidence(config, task_id="task-a", kind="decision", body="# Lease\n\nUse durable leases.")
    second = _write_evidence(config, task_id="task-b", kind="decision", body="# Lease\n\nUse durable leases.")
    base = {
        "kind": "system_rule",
        "title": "Background work must be replayable",
        "content_markdown": "Persist intent before execution and reclaim expired leases after restart.",
        "confidence": 0.99,
        "importance": 0.99,
        "system_area": "memory-mcp",
        "validation_evidence_ids": [],
        "contradicts_record_ids": [],
    }
    fingerprint = proposal_fingerprint(base)
    queue = DurableJobQueue(
        config,
        "project-reflection",
        state_rel=Path(".ai-memory/jobs/project-reflection.json"),
    )
    for task_id, source in (("task-a", first), ("task-b", second)):
        queued = queue.enqueue(kind="project_reflection", payload={"task_id": task_id})
        claimed = queue.claim(worker_id="test", lease_seconds=30)
        proposal = {**base, "fingerprint": fingerprint, "supporting_record_ids": [source["id"]]}
        queue.succeed(
            queued["job_id"],
            claimed["lease_token"],
            result={"ok": True, "task_id": task_id, "model": "fake", "proposals": [proposal]},
        )

    first_curate = curate_project_reflections(config)
    second_curate = curate_project_reflections(config)
    assert first_curate["published"][0]["result"]["ok"] is True
    assert second_curate["published"][0]["result"]["duplicate"] is True


def test_publication_revalidates_fingerprint_secrets_and_support(repo: Path) -> None:
    config = _reflection_config(repo)
    source = _write_evidence(config, task_id="task-direct", kind="decision", body="# Source\n\nSupported.")
    base = {
        "kind": "decision",
        "title": "Safe proposal",
        "content_markdown": "Persist only checked proposals.",
        "confidence": 0.99,
        "importance": 0.9,
        "supporting_record_ids": [source["id"]],
    }
    mismatch = publish_reflection_proposal(
        config,
        proposal={**base, "fingerprint": "tampered"},
        task_id="task-direct",
        model="fake",
    )
    secret = publish_reflection_proposal(
        config,
        proposal={**base, "content_markdown": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"},
        task_id="task-direct",
        model="fake",
    )
    missing = publish_reflection_proposal(
        config,
        proposal={**base, "supporting_record_ids": ["missing-record"]},
        task_id="task-direct",
        model="fake",
    )
    conflict = publish_reflection_proposal(
        config,
        proposal={**base, "contradicts_record_ids": [source["id"]]},
        task_id="task-direct",
        model="fake",
    )
    assert mismatch["error"] == "invalid_proposal"
    assert secret["error"] == "secret_signal"
    assert missing["error"] == "missing_evidence"
    assert conflict["error"] == "unresolved_conflict"


def test_concurrent_publication_is_fenced_to_one_record(repo: Path, monkeypatch) -> None:
    from servers.memory_server import memory_reflection as module

    config = _reflection_config(repo)
    source = _write_evidence(config, task_id="task-concurrent", kind="decision", body="# Source\n\nSupported.")
    proposal = {
        "kind": "procedure",
        "title": "Fence publication",
        "content_markdown": "Serialize duplicate checks with the record write.",
        "confidence": 0.99,
        "importance": 0.95,
        "supporting_record_ids": [source["id"]],
    }
    original_write = module.memory_write_record

    def delayed_write(*args, **kwargs):
        time.sleep(0.05)
        return original_write(*args, **kwargs)

    monkeypatch.setattr(module, "memory_write_record", delayed_write)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: publish_reflection_proposal(
                    config,
                    proposal=proposal,
                    task_id="task-concurrent",
                    model="fake",
                ),
                range(4),
            )
        )
    assert sum(bool(item.get("duplicate")) for item in results) == 3
    assert sum(bool(item.get("ok") and not item.get("duplicate")) for item in results) == 1


def test_reflection_queue_survives_event_log_failure(repo: Path, monkeypatch) -> None:
    config = _reflection_config(repo)
    monkeypatch.setattr(
        "servers.memory_server.memory_reflection_jobs.append_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("event disk unavailable")),
    )
    result = enqueue_project_reflection(
        config,
        task_id="task-event-failure",
        user="Codex",
        trigger="task_done",
    )
    assert result["ok"] is True
    assert result["queued"] is True
    assert result["warnings"][0]["code"] == "event_log_deferred"


def test_reflection_does_not_enqueue_shared_key_documents_when_only_active_context_is_allowed(
    repo: Path,
    monkeypatch,
) -> None:
    config = _reflection_config(repo, auto_targets=["activeContext"])
    enqueue = enqueue_project_reflection(
        config,
        task_id="task-active-only",
        user="Codex",
        trigger="task_done",
    )
    assert enqueue["ok"] is True
    monkeypatch.setattr(
        "servers.memory_server.memory_reflection_jobs.reflect_task",
        lambda *_args, **_kwargs: {
            "ok": True,
            "task_id": "task-active-only",
            "published": [{"ok": True, "duplicate": False}],
            "proposals": [],
        },
    )
    calls: list[dict] = []
    monkeypatch.setattr(
        "servers.memory_server.memory_reflection_jobs.enqueue_key_document_rebuild",
        lambda *_args, **kwargs: calls.append(kwargs) or {"ok": True, "queued": True},
    )

    result = drain_project_reflection_jobs(config, max_jobs=1, worker_id="test")

    assert result["ok"] is True
    assert calls == []
    key_docs = result["jobs"][0]["key_document_rebuild"]
    assert key_docs["skipped"] is True
    assert key_docs["targets"] == []


def test_reflection_enqueues_only_shared_targets_allowed_by_auto_rebuild(
    repo: Path,
    monkeypatch,
) -> None:
    config = _reflection_config(repo, auto_targets=["activeContext", "progress"])
    enqueue = enqueue_project_reflection(
        config,
        task_id="task-progress-only",
        user="Codex",
        trigger="task_done",
    )
    assert enqueue["ok"] is True
    monkeypatch.setattr(
        "servers.memory_server.memory_reflection_jobs.reflect_task",
        lambda *_args, **_kwargs: {
            "ok": True,
            "task_id": "task-progress-only",
            "published": [{"ok": True, "duplicate": False}],
            "proposals": [],
        },
    )
    calls: list[dict] = []
    monkeypatch.setattr(
        "servers.memory_server.memory_reflection_jobs.enqueue_key_document_rebuild",
        lambda *_args, **kwargs: calls.append(kwargs) or {"ok": True, "queued": True},
    )

    result = drain_project_reflection_jobs(config, max_jobs=1, worker_id="test")

    assert result["ok"] is True
    assert len(calls) == 1
    assert calls[0]["targets"] == ["progress"]
    assert "systemPatterns" not in calls[0]["targets"]


def test_corrupt_background_queue_never_fails_checkpoint(repo: Path) -> None:
    config = _reflection_config(repo)
    queue_path = repo / ".ai-memory" / "jobs" / "project-reflection.json"
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.write_text("not-json", encoding="utf-8")

    result = _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "checkpoint",
            "task_phase": "task_done",
            "task_id": "task-corrupt-queue",
            "user": "Codex",
        },
    )
    assert result["ok"] is True
    assert result["background_reflection"]["ok"] is False
    assert result["background_reflection"]["queued"] is False
