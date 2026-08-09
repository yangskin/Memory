"""Shared-feed endpoint: strictly project-visible data only.

These are PostgreSQL-backed integration tests; they are skipped unless
``MEMORY_HUB_DATABASE_URL`` is set (see the existing integration suite).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from memory_hub.api.main import create_app
from memory_hub.auth.tokens import create_token
from memory_hub.db.models import AccessToken, BriefHead, BriefSnapshot, MemoryEvent


pytestmark = pytest.mark.skipif(not os.getenv("MEMORY_HUB_DATABASE_URL"), reason="requires PostgreSQL")


def _event(event_id: str, *, scope: str, content: str, user_id: str = "alice") -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "event_id": event_id,
        "agent_id": "pytest",
        "agent_instance_id": "pytest-1",
        "task_id": "task-1",
        "operation": "record",
        "record_kind": "handoff",
        "scope": scope,
        "content_markdown": content,
        "metadata": {"active_files": ["src/example.py"]},
        "occurred_at": datetime.now(UTC).isoformat(),
        "content_hash": "sha256:" + "a" * 64,
    }


def _seed_token(app, *, user_id: str = "alice", scopes: list[str] | None = None, project_id: str = "project-a") -> str:
    token_id, raw_token, secret_hash = create_token()
    with app.state.session_factory() as session:
        session.add(AccessToken(
            token_id=token_id,
            token_secret_hash=secret_hash,
            token_prefix=raw_token[:20],
            user_id=user_id,
            project_id=project_id,
            scopes=scopes or ["events:write", "context:read"],
        ))
        session.commit()
    return raw_token


def _seed_brief(app, *, project_id: str = "project-a") -> None:
    with app.state.session_factory() as session:
        snapshot = BriefSnapshot(
            brief_id=uuid4(),
            project_id=project_id,
            brief_type="project_recent",
            subject_user_id="",
            input_seq_to=0,
            structured_brief={
                "schema_version": "1.0",
                "as_of": datetime.now(UTC).isoformat(),
                "summary": "shared summary",
                "workstreams": [],
                "cross_cutting_changes": [{"title": "cross-cutting note", "source_event_ids": []}],
                "possible_overlaps": [],
                "project_blockers": [],
                "build_and_test_status": [],
                "recent_decisions": [],
                "source_event_ids": [],
            },
            rendered_markdown="shared summary",
            model="fake",
            prompt_version="v1",
            generated_at=datetime.now(UTC),
            source_event_ids=[],
            status="completed",
        )
        session.add(snapshot)
        session.flush()
        session.add(BriefHead(project_id=project_id, brief_type="project_recent", subject_user_id="", current_brief_id=snapshot.brief_id))
        session.commit()


def test_shared_feed_returns_only_project_visible_events() -> None:
    app = create_app()
    project_id = f"project-{uuid4().hex}"
    raw_token = _seed_token(app, project_id=project_id)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {raw_token}", "X-Memory-User-ID": "alice"}
    payload = {"events": [
        _event(str(uuid4()), scope="shared", content="shared note"),
        _event(str(uuid4()), scope="org_shared", content="org wide note"),
        _event(str(uuid4()), scope="personal", content="alice private note"),
        _event(str(uuid4()), scope="user_private", content="alice secret note"),
    ]}
    assert client.post(f"/v1/projects/{project_id}/events/batch", headers=headers, json=payload).status_code == 200

    response = client.post("/v1/shared-feed", headers={"Authorization": f"Bearer {raw_token}"}, json={"max_age_minutes": 10080, "max_items": 50})
    assert response.status_code == 200
    body = response.json()
    assert body["project_id"] == project_id
    scopes = [event["scope"] for event in body["events"]]
    assert set(scopes) == {"shared", "org_shared"}
    contents = [event["content_markdown"] for event in body["events"]]
    assert "shared note" in contents
    assert "org wide note" in contents
    assert "alice private note" not in contents
    assert "alice secret note" not in contents


def test_shared_feed_excludes_other_users_private_events() -> None:
    app = create_app()
    project_id = f"project-{uuid4().hex}"
    raw_token = _seed_token(app, user_id="bob", project_id=project_id)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {raw_token}", "X-Memory-User-ID": "bob"}
    payload = {"events": [
        _event(str(uuid4()), scope="shared", content="shared again"),
        _event(str(uuid4()), scope="personal", content="bob private note"),
    ]}
    assert client.post(f"/v1/projects/{project_id}/events/batch", headers=headers, json=payload).status_code == 200

    response = client.post("/v1/shared-feed", headers={"Authorization": f"Bearer {raw_token}"}, json={"max_items": 50})
    assert response.status_code == 200
    events = response.json()["events"]
    assert all(event["scope"] in {"shared", "project_shared", "org_shared"} for event in events)
    assert all(event["content_markdown"] != "bob private note" for event in events)


def test_shared_feed_returns_latest_shared_events_when_current_window_is_empty() -> None:
    app = create_app()
    project_id = f"project-{uuid4().hex}"
    raw_token = _seed_token(app, project_id=project_id)
    old_time = datetime.now(UTC) - timedelta(hours=13)
    with app.state.session_factory() as session:
        session.add_all([
            MemoryEvent(event_id=uuid4(), project_id=project_id, user_id="alice", agent_id="pytest", agent_instance_id="pytest-1", operation="record", scope="project_shared", content_markdown="retained shared note", metadata_json={}, occurred_at=old_time, content_hash="sha256:" + "1" * 64),
            MemoryEvent(event_id=uuid4(), project_id=project_id, user_id="alice", agent_id="pytest", agent_instance_id="pytest-1", operation="record", scope="personal", content_markdown="private old note", metadata_json={}, occurred_at=old_time, content_hash="sha256:" + "2" * 64),
        ])
        session.commit()

    response = TestClient(app).post(
        "/v1/shared-feed",
        headers={"Authorization": f"Bearer {raw_token}"},
        json={"max_age_minutes": 60, "include_content": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["events_from_history"] is True
    assert [event["content_markdown"] for event in body["events"]] == ["retained shared note"]


def test_shared_feed_ignores_empty_shared_checkpoints() -> None:
    app = create_app()
    project_id = f"project-{uuid4().hex}"
    raw_token = _seed_token(app, project_id=project_id)
    now = datetime.now(UTC)
    with app.state.session_factory() as session:
        session.add_all([
            MemoryEvent(event_id=uuid4(), project_id=project_id, user_id="alice", agent_id="pytest", agent_instance_id="pytest-1", operation="record", scope="project_shared", content_markdown="retained shared note", metadata_json={}, occurred_at=now - timedelta(hours=13), content_hash="sha256:" + "3" * 64),
            MemoryEvent(event_id=uuid4(), project_id=project_id, user_id="alice", agent_id="pytest", agent_instance_id="pytest-1", operation="checkpoint", scope="project_shared", content_markdown="", metadata_json={}, occurred_at=now, content_hash="sha256:" + "4" * 64),
        ])
        session.commit()

    response = TestClient(app).post(
        "/v1/shared-feed",
        headers={"Authorization": f"Bearer {raw_token}"},
        json={"max_age_minutes": 60, "include_content": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["events_from_history"] is True
    assert [event["content_markdown"] for event in body["events"]] == ["retained shared note"]


def test_shared_feed_includes_project_brief() -> None:
    app = create_app()
    project_id = f"project-{uuid4().hex}"
    raw_token = _seed_token(app, scopes=["context:read"], project_id=project_id)
    _seed_brief(app, project_id=project_id)
    client = TestClient(app)

    compact_response = client.post("/v1/shared-feed", headers={"Authorization": f"Bearer {raw_token}"}, json={})
    assert compact_response.status_code == 200
    assert "structured" not in compact_response.json()["brief"]

    response = client.post(
        "/v1/shared-feed",
        headers={"Authorization": f"Bearer {raw_token}"},
        json={"include_brief_details": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["project_id"] == project_id
    assert body["brief"] is not None
    assert body["brief"]["structured"]["summary"] == "shared summary"
    assert body["brief"]["markdown"] == "shared summary"
    assert body["freshness"]["project_brief_covers_through_seq"] == 0


def test_shared_feed_requires_auth() -> None:
    app = create_app()
    client = TestClient(app)
    assert client.post("/v1/shared-feed", json={}).status_code == 401


def test_shared_feed_requires_context_read_scope() -> None:
    app = create_app()
    project_id = f"project-{uuid4().hex}"
    raw_token = _seed_token(app, scopes=["events:write"], project_id=project_id)
    client = TestClient(app)
    response = client.post("/v1/shared-feed", headers={"Authorization": f"Bearer {raw_token}"}, json={})
    assert response.status_code == 403


def test_shared_feed_ignores_forged_project_in_body() -> None:
    app = create_app()
    project_id = f"project-{uuid4().hex}"
    raw_token = _seed_token(app, scopes=["context:read"], project_id=project_id)
    client = TestClient(app)
    response = client.post("/v1/shared-feed", headers={"Authorization": f"Bearer {raw_token}"}, json={"project_id": "project-b"})
    assert response.status_code == 422  # extra="forbid" rejects forged fields
