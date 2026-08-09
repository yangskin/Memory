from __future__ import annotations

from types import SimpleNamespace

from servers.memory_server.memory_sync_protocol import build_memory_event


def test_build_memory_event_preserves_task_event_and_excludes_retired_graph_delta(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "servers.memory_server.memory_sync_protocol.load_runtime_identity",
        lambda _root, _args: SimpleNamespace(
            agent_id="copilot",
            agent_instance_id="copilot-1",
            session_id="session-1",
            source_node_id="node-1",
            source_node_name="node",
            workspace_id="workspace-1",
        ),
    )
    task_event = {
        "version": "1.0",
        "command_id": "command-1",
        "event_type": "TaskCreated",
        "task_id": "task-1",
        "actor_id": "agent:test",
        "expected_version": 0,
        "expected_assignment_epoch": None,
        "task_version": 1,
        "assignment_epoch": 0,
        "payload": {"title": "Task"},
        "occurred_at": "2026-08-09T00:00:00+00:00",
    }

    event = build_memory_event(
        {
            "operation": "checkpoint",
            "scope": "project_shared",
            "task_id": "task-1",
            "graph_delta": {"retired": True},
            "task_event": task_event,
            "map_names": ["Main"],
            "plugin_names": ["Memory"],
        },
        {"ok": True},
        repo_root=tmp_path,
    )

    assert "graph_delta" not in event["metadata"]
    assert event["metadata"]["task_event"] == task_event
    assert event["metadata"]["map_names"] == ["Main"]
    assert event["metadata"]["plugin_names"] == ["Memory"]