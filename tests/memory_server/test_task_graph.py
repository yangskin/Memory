from __future__ import annotations

from types import SimpleNamespace

from servers.memory_server.memory_task_graph import build_task_graph_delta
from servers.memory_server.memory_task_graph_jobs import (
    drain_task_graph_settlement_jobs,
    enqueue_task_graph_settlement,
)


def _disable_record_index(monkeypatch) -> None:
    monkeypatch.setattr(
        "servers.memory_server.memory_task_graph.record_paths_for_exact_task",
        lambda *_args, **_kwargs: {"ok": False, "error": "index_missing"},
    )


def test_build_task_graph_delta_is_bounded_and_observed(monkeypatch) -> None:
    _disable_record_index(monkeypatch)
    records = [
        SimpleNamespace(
            metadata={
                "id": "rec-1",
                "task_id": "task-7",
                "scope": "project_shared",
                "system_area": "sync",
                "module_names": ["outbox"],
                "active_files": ["servers/memory_server/memory_sync_store.py"],
            }
        ),
        SimpleNamespace(
            metadata={
                "id": "rec-2",
                "task_id": "task-7",
                "scope": "shared",
                "module_names": ["outbox"],
            }
        ),
        SimpleNamespace(metadata={"id": "other", "task_id": "task-8", "scope": "project_shared", "module_names": ["ignored"]}),
    ]
    monkeypatch.setattr(
        "servers.memory_server.memory_task_graph.iter_parsed_records",
        lambda _config, **_kwargs: (records, {"parsed": len(records)}),
    )

    result = build_task_graph_delta(SimpleNamespace(), task_id="task-7")

    assert result["ok"] is True
    delta = result["graph_delta"]
    assert delta["version"] == "1.0"
    assert delta["delta_id"].startswith("sha256:")
    assert {node["key"] for node in delta["nodes"]} == {
        "task-7",
        "sync",
        "outbox",
        "servers/memory_server/memory_sync_store.py",
    }
    outbox_edge = next(edge for edge in delta["edges"] if edge["target"]["key"] == "outbox")
    assert outbox_edge == {
        "source": {"type": "task", "key": "task-7"},
        "target": {"type": "module", "key": "outbox"},
        "relation": "affects",
        "origin": "observed",
        "confidence": 1.0,
        "evidence_ids": ["rec-1", "rec-2"],
    }


def test_build_task_graph_delta_rejects_missing_task_id() -> None:
    result = build_task_graph_delta(SimpleNamespace(), task_id="")

    assert result["ok"] is False
    assert result["error"] == "invalid_input"


def test_build_task_graph_delta_excludes_private_and_evidence_free_records(monkeypatch) -> None:
    _disable_record_index(monkeypatch)
    records = [
        SimpleNamespace(metadata={"id": "private", "task_id": "task-7", "scope": "personal", "module_names": ["secret"]}),
        SimpleNamespace(metadata={"task_id": "task-7", "scope": "project_shared", "module_names": ["no-evidence"]}),
        SimpleNamespace(metadata={"id": "shared", "task_id": "task-7", "scope": "project_shared", "module_names": ["public"]}),
    ]
    monkeypatch.setattr(
        "servers.memory_server.memory_task_graph.iter_parsed_records",
        lambda _config, **_kwargs: (records, {"parsed": len(records)}),
    )

    delta = build_task_graph_delta(SimpleNamespace(), task_id="task-7")["graph_delta"]

    assert {node["key"] for node in delta["nodes"]} == {"task-7", "public"}
    assert delta["edges"][0]["evidence_ids"] == ["shared"]


def test_build_task_graph_delta_always_retains_task_node_at_limit(monkeypatch) -> None:
    _disable_record_index(monkeypatch)
    records = [
        SimpleNamespace(
            metadata={
                "id": f"rec-{index}",
                "task_id": "zz-task",
                "scope": "project_shared",
                "active_files": [f"a{index:02}.py"],
            }
        )
        for index in range(35)
    ]
    monkeypatch.setattr(
        "servers.memory_server.memory_task_graph.iter_parsed_records",
        lambda _config, **_kwargs: (records, {"parsed": len(records)}),
    )

    delta = build_task_graph_delta(SimpleNamespace(), task_id="zz-task")["graph_delta"]

    assert delta["nodes"][0] == {"type": "task", "key": "zz-task", "name": "zz-task"}
    assert len(delta["nodes"]) == 30
    assert len(delta["edges"]) == 29


def test_build_task_graph_delta_caps_evidence_per_edge(monkeypatch) -> None:
    _disable_record_index(monkeypatch)
    records = [
        SimpleNamespace(
            metadata={
                "id": f"rec-{index:03}",
                "task_id": "task-7",
                "scope": "project_shared",
                "module_names": ["core"],
            }
        )
        for index in range(100)
    ]
    monkeypatch.setattr(
        "servers.memory_server.memory_task_graph.iter_parsed_records",
        lambda _config, **_kwargs: (records, {"parsed": len(records)}),
    )

    delta = build_task_graph_delta(SimpleNamespace(), task_id="task-7")["graph_delta"]

    assert delta["edges"][0]["evidence_ids"] == [f"rec-{index:03}" for index in range(16)]


def test_build_task_graph_delta_id_changes_with_evidence(monkeypatch) -> None:
    _disable_record_index(monkeypatch)
    records = [
        SimpleNamespace(
            metadata={
                "id": "rec-1",
                "task_id": "task-7",
                "scope": "project_shared",
                "module_names": ["core"],
            }
        )
    ]
    monkeypatch.setattr(
        "servers.memory_server.memory_task_graph.iter_parsed_records",
        lambda _config, **_kwargs: (records, {"parsed": len(records)}),
    )
    first = build_task_graph_delta(SimpleNamespace(), task_id="task-7")["graph_delta"]
    records[0].metadata["id"] = "rec-2"

    second = build_task_graph_delta(SimpleNamespace(), task_id="task-7")["graph_delta"]

    assert first["delta_id"] != second["delta_id"]


def test_build_task_graph_delta_uses_exact_task_index_paths(monkeypatch) -> None:
    monkeypatch.setattr(
        "servers.memory_server.memory_task_graph.record_paths_for_exact_task",
        lambda *_args, **_kwargs: {"ok": True, "paths": ["memory-bank/records/task-7.md"]},
    )
    captured = {}

    def _records(_config, *, include_rel_paths=None):
        captured["paths"] = include_rel_paths
        return [], {"scanned_files": 0}

    monkeypatch.setattr("servers.memory_server.memory_task_graph.iter_parsed_records", _records)

    result = build_task_graph_delta(SimpleNamespace(), task_id="task-7")

    assert result["ok"] is True
    assert captured["paths"] == {"memory-bank/records/task-7.md"}


def test_task_graph_jobs_coalesce_and_finish_without_shared_hub(tmp_path, monkeypatch) -> None:
    config = SimpleNamespace(
        repo_root=tmp_path,
        config_hash="test-config",
        mcp_fsync_strict=False,
        worker={"max_attempts": 2, "lease_seconds": 30, "retry_base_seconds": 0, "history_limit": 20},
        shared_memory=SimpleNamespace(enabled=False),
    )
    first = enqueue_task_graph_settlement(
        config, task_id="task-7", user="alice", branch="main", trigger="task_done"
    )
    second = enqueue_task_graph_settlement(
        config, task_id="task-7", user="alice", branch="main", trigger="task_done"
    )
    monkeypatch.setattr(
        "servers.memory_server.memory_task_graph_jobs.build_task_graph_delta",
        lambda _config, task_id: {
            "ok": True,
            "graph_delta": {"version": "1.0", "delta_id": "sha256:test", "task_id": task_id, "nodes": [], "edges": []},
        },
    )

    drained = drain_task_graph_settlement_jobs(config, worker_id="test-worker")

    assert first["queued"] is True
    assert second["coalesced"] is True
    assert drained["processed"] == 1
    assert drained["jobs"][0]["ok"] is True