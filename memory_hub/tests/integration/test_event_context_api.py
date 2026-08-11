from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from memory_hub.api.main import create_app
from memory_hub.auth.tokens import create_token
from memory_hub.db.models import AccessToken, ContextUsageDaily, MemoryEvent


pytestmark = pytest.mark.skipif(not os.getenv("MEMORY_HUB_DATABASE_URL"), reason="requires PostgreSQL")


def _event(event_id: str) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "event_id": event_id,
        "agent_id": "pytest",
        "agent_instance_id": "pytest-1",
        "source_node_id": "node-1",
        "runtime_node_id": "node-1",
        "source_node_name": "test-host",
        "workspace_id": "sha256:" + "b" * 64,
        "agent_session_id": "session-1",
        "transport_id": "memory-mcp",
        "task_id": "task-1",
        "operation": "record",
        "record_kind": "handoff",
        "scope": "personal",
        "content_markdown": "completed local implementation",
        "metadata": {"active_files": ["src/example.py"]},
        "occurred_at": datetime.now(UTC).isoformat(),
        "content_hash": "sha256:" + "a" * 64,
    }


def test_authenticated_event_ingest_is_idempotent_and_project_scoped() -> None:
    app = create_app()
    token_id, raw_token, secret_hash = create_token()
    with app.state.session_factory() as session:
        session.add(AccessToken(token_id=token_id, token_secret_hash=secret_hash, token_prefix=raw_token[:20], user_id="pytest-user", project_id="project-a", scopes=["events:write", "context:read", "identity:delegate"]))
        session.commit()
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {raw_token}", "X-Memory-User-ID": "alice"}
    event_id = uuid4()
    payload = {"events": [_event(str(event_id))]}
    response = client.post("/v1/projects/project-a/events/batch", headers=headers, json=payload)
    assert response.status_code == 200
    assert len(response.json()["accepted"]) == 1
    with app.state.session_factory() as session:
        stored = session.query(MemoryEvent).filter_by(project_id="project-a", event_id=event_id).one()
        assert stored.user_id == "alice"
        assert stored.source_node_id == "node-1"
        assert stored.runtime_node_id == "node-1"
        assert stored.source_node_name == "test-host"
        assert stored.workspace_id == "sha256:" + "b" * 64
        assert stored.agent_session_id == "session-1"
        assert stored.transport_id == "memory-mcp"
    forged = {"events": [{**_event(str(uuid4())), "user_id": "forged-user"}]}
    assert client.post("/v1/projects/project-a/events/batch", headers=headers, json=forged).status_code == 422
    duplicate = client.post("/v1/projects/project-a/events/batch", headers=headers, json=payload)
    assert duplicate.status_code == 200
    assert len(duplicate.json()["duplicates"]) == 1
    forbidden = client.post("/v1/projects/project-b/events/batch", headers=headers, json=payload)
    assert forbidden.status_code == 403

    context = client.post("/v1/projects/project-a/context", headers=headers, json={"agent_instance_id": "other-agent", "task_id": "task-1", "include": ["my_other_agents", "same_task_agents"], "max_items": 10})
    assert context.status_code == 200
    assert context.json()["my_other_agents"][0]["task_id"] == "task-1"
    assert context.json()["my_other_agents"][0]["runtime_node_id"] == "node-1"
    assert context.json()["my_other_agents"][0]["workspace_id"] == "sha256:" + "b" * 64
    bob_context = client.post("/v1/projects/project-a/context", headers={**headers, "X-Memory-User-ID": "bob"}, json={"agent_instance_id": "bob-agent", "include": ["project_activity"], "max_items": 10})
    assert bob_context.status_code == 200
    assert bob_context.json()["project_activity"] == []


def test_token_without_identity_delegation_cannot_impersonate_another_user() -> None:
    app = create_app()
    token_id, raw_token, secret_hash = create_token()
    project_id = f"project-{uuid4().hex}"
    with app.state.session_factory() as session:
        session.add(AccessToken(token_id=token_id, token_secret_hash=secret_hash, token_prefix=raw_token[:20], user_id="alice", project_id=project_id, scopes=["events:write", "context:read"]))
        session.commit()

    event_id = uuid4()
    response = TestClient(app).post(
        f"/v1/projects/{project_id}/events/batch",
        headers={"Authorization": f"Bearer {raw_token}", "X-Memory-User-ID": "bob"},
        json={"events": [_event(str(event_id))]},
    )

    assert response.status_code == 200
    with app.state.session_factory() as session:
        assert session.query(MemoryEvent).filter_by(event_id=event_id).one().user_id == "alice"


def test_tokens_private_metadata_and_secret_redaction_are_enforced() -> None:
    app = create_app()
    owner_id, owner_token, owner_hash = create_token()
    reader_id, reader_token, reader_hash = create_token()
    scoped_id, scoped_token, scoped_hash = create_token()
    expired_id, expired_token, expired_hash = create_token()
    project_id = f"project-{uuid4().hex}"
    with app.state.session_factory() as session:
        session.add_all([
            AccessToken(token_id=owner_id, token_secret_hash=owner_hash, token_prefix=owner_token[:20], user_id="alice", project_id=project_id, scopes=["events:write", "context:read"]),
            AccessToken(token_id=reader_id, token_secret_hash=reader_hash, token_prefix=reader_token[:20], user_id="bob", project_id=project_id, scopes=["context:read"]),
            AccessToken(token_id=scoped_id, token_secret_hash=scoped_hash, token_prefix=scoped_token[:20], user_id="scoped", project_id=project_id, scopes=["context:read"]),
            AccessToken(token_id=expired_id, token_secret_hash=expired_hash, token_prefix=expired_token[:20], user_id="expired", project_id=project_id, scopes=["context:read"], expires_at=datetime.now(UTC) - timedelta(seconds=1)),
        ])
        session.commit()
    client = TestClient(app)
    event = _event(str(uuid4()))
    event["metadata"] = {"active_files": ["safe.py"], "secret_notes": "must never be returned"}
    event["content_markdown"] = "AKIAIOSFODNN7EXAMPLE"
    response = client.post(f"/v1/projects/{project_id}/events/batch", headers={"Authorization": f"Bearer {owner_token}", "X-Memory-User-ID": "alice"}, json={"events": [event]})
    assert response.status_code == 200
    assert response.json()["warnings"]
    with app.state.session_factory() as session:
        stored = session.query(MemoryEvent).filter_by(project_id=project_id).one()
        assert stored.content_markdown is None
        session.get(AccessToken, reader_id).revoked_at = datetime.now(UTC)
        session.commit()
    revoked = client.post(f"/v1/projects/{project_id}/context", headers={"Authorization": f"Bearer {reader_token}"}, json={"agent_instance_id": "bob-agent"})
    assert revoked.status_code == 401
    expired = client.post(f"/v1/projects/{project_id}/context", headers={"Authorization": f"Bearer {expired_token}"}, json={"agent_instance_id": "expired-agent"})
    assert expired.status_code == 401
    no_write = client.post(f"/v1/projects/{project_id}/events/batch", headers={"Authorization": f"Bearer {scoped_token}"}, json={"events": [event]})
    assert no_write.status_code == 403

    visible_id, visible_token, visible_hash = create_token()
    with app.state.session_factory() as session:
        session.add(AccessToken(token_id=visible_id, token_secret_hash=visible_hash, token_prefix=visible_token[:20], user_id="bob", project_id=project_id, scopes=["context:read"]))
        session.commit()
    context = client.post(f"/v1/projects/{project_id}/context", headers={"Authorization": f"Bearer {visible_token}", "X-Memory-User-ID": "bob"}, json={"agent_instance_id": "bob-agent", "include": ["project_activity"]})
    assert context.status_code == 200
    assert context.json()["project_activity"] == []


def test_context_usage_counts_requests_and_returned_items() -> None:
    app = create_app()
    token_id, raw_token, secret_hash = create_token()
    project_id = f"usage-{uuid4().hex}"
    with app.state.session_factory() as session:
        session.add(AccessToken(token_id=token_id, token_secret_hash=secret_hash, token_prefix=raw_token[:20], user_id="usage-user", project_id=project_id, scopes=["events:write", "context:read"]))
        session.commit()
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {raw_token}"}
    event = _event(str(uuid4()))
    event["scope"] = "project_shared"
    assert client.post(f"/v1/projects/{project_id}/events/batch", headers=headers, json={"events": [event]}).status_code == 200
    response = client.post(f"/v1/projects/{project_id}/context", headers=headers, json={"agent_instance_id": "usage-reader", "include": ["project_activity", "project_brief"]})
    assert response.status_code == 200

    usage = client.get(f"/v1/projects/{project_id}/context/usage?days=1", headers=headers)
    assert usage.status_code == 200
    row = usage.json()["days"][0]
    assert row["request_count"] == 1
    assert row["returned_event_count"] >= 1
    assert row["include_requests"]["project_activity"] == 1
    with app.state.session_factory() as session:
        assert session.query(ContextUsageDaily).filter_by(project_id=project_id).count() == 1