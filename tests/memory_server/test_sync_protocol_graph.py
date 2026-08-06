from __future__ import annotations

from types import SimpleNamespace

from servers.memory_server.memory_sync_protocol import build_memory_event


def test_build_memory_event_preserves_graph_and_all_entity_fields(tmp_path, monkeypatch) -> None:
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
    delta = {"version": "1.0", "delta_id": "sha256:test", "task_id": "task-1", "nodes": [], "edges": []}

    event = build_memory_event(
        {
            "operation": "checkpoint",
            "scope": "project_shared",
            "task_id": "task-1",
            "graph_delta": delta,
            "map_names": ["Main"],
            "plugin_names": ["Memory"],
        },
        {"ok": True},
        repo_root=tmp_path,
    )

    assert event["metadata"]["graph_delta"] == delta
    assert event["metadata"]["map_names"] == ["Main"]
    assert event["metadata"]["plugin_names"] == ["Memory"]