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
from memory_hub.db.models import AccessToken, BriefHead, BriefSnapshot, GraphEdge, GraphNode, MemoryEvent
from memory_hub.graph.projector import project_events, rebuild_project


pytestmark = pytest.mark.skipif(not os.getenv("MEMORY_HUB_DATABASE_URL"), reason="requires PostgreSQL")


def _event(project_id: str, scope: str, module_name: str) -> MemoryEvent:
    event_id = uuid4()
    return MemoryEvent(event_id=event_id, project_id=project_id, user_id="alice", agent_id="pytest", agent_instance_id="pytest-1", task_id="task-graph", operation="record", record_kind="note", scope=scope, content_markdown="graph event", metadata_json={"graph_delta": _sealed_delta("task-graph", module_name=module_name)}, occurred_at=datetime.now(UTC), content_hash="sha256:" + event_id.hex.ljust(64, "0"))


def _sealed_delta(task_id: str, *, module_name: str = "explicit-module") -> dict[str, object]:
    body: dict[str, object] = {
        "version": "1.0",
        "task_id": task_id,
        "nodes": [
            {"type": "class", "key": "ExplicitWorker", "name": "ExplicitWorker"},
            {"type": "module", "key": module_name, "name": module_name},
        ],
        "edges": [{
            "source": {"type": "class", "key": "ExplicitWorker"},
            "target": {"type": "module", "key": module_name},
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
        session.add_all([_event(project_id, "project_shared", "visible-module"), _event(project_id, "project_shared", "visible-module"), _event(project_id, "personal", "private-module")])
        session.commit()
        first_watermark = project_events(session, project_id)
        second_watermark = project_events(session, project_id)
        assert second_watermark == first_watermark
        assert session.query(GraphNode).filter_by(project_id=project_id, node_type="task").count() == 0
        edge = session.query(GraphEdge).filter_by(project_id=project_id, relation_type="implements").first()
        assert edge is not None
        assert len(edge.source_event_ids) == 2

    client = TestClient(app)
    response = client.post(f"/v1/projects/{project_id}/graph/query", headers={"Authorization": f"Bearer {raw_token}"}, json={"task_id": "task-graph", "depth": 1})
    assert response.status_code == 200
    body = response.json()
    assert body["freshness"]["stale"] is False
    assert "visible-module" in {node["key"] for node in body["nodes"]}
    assert "private-module" not in {node["key"] for node in body["nodes"]}
    assert "agent" not in {node["type"] for node in body["nodes"]}
    assert "task" not in {node["type"] for node in body["nodes"]}
    assert all("metadata" not in node for node in body["nodes"])
    assert all("source_event_ids" not in edge for edge in body["edges"])
    assert all("evidence_ids" not in edge for edge in body["edges"])
    assert all(edge["origin"] == "client_delta" for edge in body["edges"])

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

        semantic_node = session.query(GraphNode).filter_by(project_id=project_id, node_type="class", node_key="ExplicitWorker").one()
        legacy_node = GraphNode(id=uuid4(), project_id=project_id, node_type="agent", node_key="legacy-agent", name="legacy-agent", metadata_json={})
        session.add(legacy_node)
        session.flush()
        session.add(GraphEdge(id=uuid4(), project_id=project_id, source_node_id=legacy_node.id, target_node_id=semantic_node.id, relation_type="performed", confidence=1.0, source_event_ids=[str(event_id)], evidence_ids=[]))
        session.commit()

        rebuild_project(session, project_id)
        assert session.query(GraphNode).filter_by(project_id=project_id, node_type="agent").count() == 0
        assert session.query(GraphNode).filter_by(project_id=project_id, node_type="task").count() == 0
        assert session.query(GraphEdge).filter_by(project_id=project_id, relation_type="performed").count() == 0

    response = TestClient(app).get(f"/v1/projects/{project_id}/graph", headers={"Authorization": f"Bearer {raw_token}"})
    assert response.status_code == 200
    assert {edge["origin"] for edge in response.json()["edges"]} == {"client_delta"}
    assert "task" not in {node["type"] for node in response.json()["nodes"]}


def test_graph_api_marks_server_semantic_edges() -> None:
    app = create_app()
    project_id = f"graph-{uuid4().hex}"
    token_id, raw_token, secret_hash = create_token()
    event_id = uuid4()
    with app.state.session_factory() as session:
        session.add(AccessToken(token_id=token_id, token_secret_hash=secret_hash, token_prefix=raw_token[:20], user_id="alice", project_id=project_id, scopes=["context:read"]))
        event = MemoryEvent(event_id=event_id, project_id=project_id, user_id="alice", agent_id="pytest", agent_instance_id="pytest-1", operation="record", scope="project_shared", content_markdown="Checkout validates payments.", metadata_json={"class_names": ["CheckoutVerifier"], "module_names": ["payments"]}, occurred_at=datetime.now(UTC), content_hash="sha256:" + event_id.hex.ljust(64, "0"))
        session.add(event)
        session.flush()
        source_key = f"event:{event_id}"
        snapshot = BriefSnapshot(brief_id=uuid4(), project_id=project_id, brief_type="project_graph", subject_user_id="", input_seq_to=event.server_seq, structured_brief={"schema_version": "1.0", "nodes": [{"type": "source", "key": source_key, "name": "record: Checkout"}, {"type": "class", "key": "CheckoutVerifier", "name": "CheckoutVerifier"}, {"type": "module", "key": "payments", "name": "payments"}], "edges": [{"source": {"type": "source", "key": source_key}, "target": {"type": "class", "key": "CheckoutVerifier"}, "relation": "documents", "confidence": 1.0, "evidence_ids": [str(event_id)]}, {"source": {"type": "source", "key": source_key}, "target": {"type": "module", "key": "payments"}, "relation": "documents", "confidence": 1.0, "evidence_ids": [str(event_id)]}, {"source": {"type": "class", "key": "CheckoutVerifier"}, "target": {"type": "module", "key": "payments"}, "relation": "validates", "confidence": 0.9, "evidence_ids": [str(event_id)]}], "source_event_ids": [str(event_id)]}, rendered_markdown="2 source links and 1 semantic relation.", prompt_version="v1", generated_at=datetime.now(UTC), source_event_ids=[str(event_id)], status="completed")
        session.add(snapshot)
        session.add(BriefHead(project_id=project_id, brief_type="project_graph", subject_user_id="", current_brief_id=snapshot.brief_id))
        session.commit()
        rebuild_project(session, project_id)

    response = TestClient(app).get(f"/v1/projects/{project_id}/graph", headers={"Authorization": f"Bearer {raw_token}"})
    assert response.status_code == 200
    body = response.json()
    assert {edge["origin"] for edge in body["edges"]} == {"server_provenance", "server_semantic"}
    assert {node["key"] for node in body["nodes"]} == {source_key, "CheckoutVerifier", "payments"}