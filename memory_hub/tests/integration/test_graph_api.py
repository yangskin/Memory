from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from memory_hub.api.main import create_app
from memory_hub.auth.tokens import create_token
from memory_hub.db.models import AccessToken, GraphEdge, GraphNode, MemoryEvent
from memory_hub.graph.projector import project_events, rebuild_project


pytestmark = pytest.mark.skipif(not os.getenv("MEMORY_HUB_DATABASE_URL"), reason="requires PostgreSQL")


def _event(project_id: str, scope: str, file_name: str) -> MemoryEvent:
    event_id = uuid4()
    return MemoryEvent(event_id=event_id, project_id=project_id, user_id="alice", agent_id="pytest", agent_instance_id="pytest-1", task_id="task-graph", operation="record", record_kind="note", scope=scope, content_markdown="graph event", metadata_json={"active_files": [file_name], "module_names": ["graph-module"]}, occurred_at=datetime.now(UTC), content_hash="sha256:" + event_id.hex.ljust(64, "0"))


def _sealed_delta(task_id: str) -> dict[str, object]:
    body: dict[str, object] = {
        "version": "1.0",
        "task_id": task_id,
        "nodes": [
            {"type": "task", "key": task_id, "name": task_id},
            {"type": "module", "key": "explicit-module", "name": "explicit-module"},
        ],
        "edges": [{
            "source": {"type": "task", "key": task_id},
            "target": {"type": "module", "key": "explicit-module"},
            "relation": "implements",
            "origin": "observed",
            "confidence": 1.0,
            "evidence_ids": ["record-1"],
        }],
    }
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return {**body, "delta_id": "sha256:" + hashlib.sha256(encoded).hexdigest()}


def test_projector_and_graph_query_are_idempotent_and_private_safe() -> None:
    app = create_app()
    project_id = f"graph-{uuid4().hex}"
    token_id, raw_token, secret_hash = create_token()
    with app.state.session_factory() as session:
        session.add(AccessToken(token_id=token_id, token_secret_hash=secret_hash, token_prefix=raw_token[:20], user_id="alice", project_id=project_id, scopes=["context:read"]))
        session.add_all([_event(project_id, "project_shared", "visible.py"), _event(project_id, "project_shared", "visible.py"), _event(project_id, "personal", "private.py")])
        session.commit()
        first_watermark = project_events(session, project_id)
        second_watermark = project_events(session, project_id)
        assert second_watermark == first_watermark
        assert session.query(GraphNode).filter_by(project_id=project_id, node_type="file").count() == 1
        edge = session.query(GraphEdge).filter_by(project_id=project_id, relation_type="affects").first()
        assert edge is not None
        assert len(edge.source_event_ids) == 2

    client = TestClient(app)
    response = client.post(f"/v1/projects/{project_id}/graph/query", headers={"Authorization": f"Bearer {raw_token}"}, json={"task_id": "task-graph", "depth": 1})
    assert response.status_code == 200
    body = response.json()
    assert body["freshness"]["stale"] is False
    assert "visible.py" in {node["key"] for node in body["nodes"]}
    assert "private.py" not in {node["key"] for node in body["nodes"]}
    assert "agent" not in {node["type"] for node in body["nodes"]}
    assert all("metadata" not in node for node in body["nodes"])
    assert all("source_event_ids" not in edge for edge in body["edges"])
    assert all("evidence_ids" not in edge for edge in body["edges"])
    assert all(edge["origin"] == "shared_metadata" for edge in body["edges"])

    detailed = client.post(
        f"/v1/projects/{project_id}/graph/query",
        headers={"Authorization": f"Bearer {raw_token}"},
        json={"task_id": "task-graph", "depth": 1, "include_metadata": True, "include_source_event_ids": True, "include_evidence_ids": True},
    )
    assert detailed.status_code == 200
    assert all("metadata" in node for node in detailed.json()["nodes"])
    assert all("source_event_ids" in edge for edge in detailed.json()["edges"])
    assert all("evidence_ids" in edge for edge in detailed.json()["edges"])


def test_graph_api_marks_client_delta_edges_and_rebuild_removes_legacy_projection() -> None:
    app = create_app()
    project_id = f"graph-{uuid4().hex}"
    token_id, raw_token, secret_hash = create_token()
    task_id = "task-explicit"
    event_id = uuid4()
    with app.state.session_factory() as session:
        session.add(AccessToken(token_id=token_id, token_secret_hash=secret_hash, token_prefix=raw_token[:20], user_id="alice", project_id=project_id, scopes=["context:read"]))
        session.add(MemoryEvent(event_id=event_id, project_id=project_id, user_id="alice", agent_id="pytest", agent_instance_id="pytest-1", task_id=task_id, operation="checkpoint", scope="project_shared", content_markdown="", metadata_json={"graph_delta": _sealed_delta(task_id)}, occurred_at=datetime.now(UTC), content_hash="sha256:" + event_id.hex.ljust(64, "0")))
        session.commit()
        project_events(session, project_id)

        task_node = session.query(GraphNode).filter_by(project_id=project_id, node_type="task", node_key=task_id).one()
        legacy_node = GraphNode(id=uuid4(), project_id=project_id, node_type="agent", node_key="legacy-agent", name="legacy-agent", metadata_json={})
        session.add(legacy_node)
        session.flush()
        session.add(GraphEdge(id=uuid4(), project_id=project_id, source_node_id=legacy_node.id, target_node_id=task_node.id, relation_type="performed", confidence=1.0, source_event_ids=[str(event_id)], evidence_ids=[]))
        session.commit()

        rebuild_project(session, project_id)
        assert session.query(GraphNode).filter_by(project_id=project_id, node_type="agent").count() == 0
        assert session.query(GraphEdge).filter_by(project_id=project_id, relation_type="performed").count() == 0

    response = TestClient(app).get(f"/v1/projects/{project_id}/graph", headers={"Authorization": f"Bearer {raw_token}"})
    assert response.status_code == 200
    assert {edge["origin"] for edge in response.json()["edges"]} == {"client_delta"}