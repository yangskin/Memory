from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from memory_hub.api.main import create_app
from memory_hub.auth.tokens import create_token
from memory_hub.db.models import AccessToken


pytestmark = pytest.mark.skipif(not os.getenv("MEMORY_HUB_DATABASE_URL"), reason="requires PostgreSQL")


def _task_event(
    command_id: str,
    event_type: str,
    *,
    expected_version: int,
    task_version: int,
    assignment_epoch: int,
    payload: dict[str, object],
    expected_assignment_epoch: int | None = None,
    actor_id: str = "agent:lead",
) -> dict[str, object]:
    return {
        "version": "1.0",
        "command_id": command_id,
        "event_type": event_type,
        "task_id": "task-api-1",
        "actor_id": actor_id,
        "expected_version": expected_version,
        "expected_assignment_epoch": expected_assignment_epoch,
        "task_version": task_version,
        "assignment_epoch": assignment_epoch,
        "payload": payload,
        "occurred_at": datetime.now(UTC).isoformat(),
    }


def _event(task_event: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "event_id": str(uuid4()),
        "agent_id": str(task_event["actor_id"]),
        "agent_instance_id": "task-api-agent-1",
        "task_id": "task-api-1",
        "operation": "task_sync",
        "scope": "project_shared",
        "content_markdown": "task graph event",
        "metadata": {"task_event": task_event},
        "occurred_at": datetime.now(UTC).isoformat(),
        "content_hash": "sha256:" + uuid4().hex + uuid4().hex,
    }


def test_task_sync_events_project_a_graph_bundle_and_timeline() -> None:
    app = create_app()
    token_id, token, secret_hash = create_token()
    project_id = f"task-api-{uuid4().hex}"
    with app.state.session_factory() as session:
        session.add(
            AccessToken(
                token_id=token_id,
                token_secret_hash=secret_hash,
                token_prefix=token[:20],
                user_id="task-user",
                project_id=project_id,
                scopes=["events:write", "context:read"],
            )
        )
        session.commit()
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}
    events = [
        _event(_task_event("task-create", "TaskCreated", expected_version=0, task_version=1, assignment_epoch=0, payload={"title": "Hub task", "objective": "Project events", "acceptance": "Graph bundle", "priority": "normal", "depends_on": [], "produced_memory": []})),
        _event(_task_event("task-assign", "TaskAssigned", expected_version=1, task_version=2, assignment_epoch=1, payload={"attempt_id": "attempt-api-1", "assignee": "agent:worker", "assigned_by": "agent:lead", "epoch": 1})),
        _event(_task_event("task-claim", "TaskClaimed", expected_version=2, task_version=3, assignment_epoch=1, expected_assignment_epoch=1, actor_id="agent:worker", payload={"attempt_id": "attempt-api-1"})),
        _event(_task_event("task-submit", "TaskSubmitted", expected_version=3, task_version=4, assignment_epoch=1, expected_assignment_epoch=1, actor_id="agent:worker", payload={"attempt_id": "attempt-api-1", "submission_id": "submission-api-1", "summary": "Implementation", "evidence": ["pytest"]})),
        _event(_task_event("task-review", "TaskReviewed", expected_version=4, task_version=5, assignment_epoch=1, actor_id="agent:reviewer", payload={"review_id": "review-api-1", "submission_id": "submission-api-1", "decision": "approved", "summary": "Approved"})),
    ]

    response = client.post(f"/v1/projects/{project_id}/events/batch", headers=headers, json={"events": events})
    assert response.status_code == 200
    assert len(response.json()["accepted"]) == len(events)

    legacy_event = _event(_task_event("legacy-graph-delta", "TaskCreated", expected_version=0, task_version=1, assignment_epoch=0, payload={"title": "Legacy graph"}))
    legacy_event["operation"] = "record"
    legacy_event["metadata"] = {"graph_delta": {"version": "1.0"}}
    legacy_response = client.post(f"/v1/projects/{project_id}/events/batch", headers=headers, json={"events": [legacy_event]})
    assert legacy_response.status_code == 200
    assert legacy_response.json()["rejected"][0]["code"] == "unsupported_metadata"

    graph = client.get(f"/v1/projects/{project_id}/task-graph?task_id=task-api-1", headers=headers)
    history = client.get(f"/v1/projects/{project_id}/task-events?task_id=task-api-1", headers=headers)

    assert graph.status_code == 200
    assert {node["type"] for node in graph.json()["nodes"]} >= {"task", "agent", "attempt", "submission", "review"}
    assert {edge["relation_type"] for edge in graph.json()["edges"]} >= {"current_attempt", "assigned_to", "has_submission", "has_review"}
    task = next(node for node in graph.json()["nodes"] if node["type"] == "task")
    assert task["metadata"]["state"] == "done"
    assert history.status_code == 200
    assert [item["event_type"] for item in history.json()["events"]] == [
        "TaskCreated",
        "TaskAssigned",
        "TaskClaimed",
        "TaskSubmitted",
        "TaskReviewed",
    ]
    assert history.json()["events"][-1]["task_version"] == 5
    assert history.json()["events"][-1]["assignment_epoch"] == 1
    unrelated_agent = client.get(
        f"/v1/projects/{project_id}/task-graph?agent_id=agent:unrelated",
        headers=headers,
    )
    assert unrelated_agent.status_code == 200
    assert unrelated_agent.json()["nodes"] == []
    assert unrelated_agent.json()["roots"] == {"current": [], "assigned": [], "review": [], "attention": []}
    assert client.get(f"/v1/projects/{project_id}/graph", headers=headers).status_code == 404
    assert client.post(f"/v1/projects/{project_id}/graph/query", headers=headers, json={}).status_code == 404


def test_task_sync_rejects_an_outdated_task_version() -> None:
    app = create_app()
    token_id, token, secret_hash = create_token()
    project_id = f"task-conflict-{uuid4().hex}"
    with app.state.session_factory() as session:
        session.add(
            AccessToken(
                token_id=token_id,
                token_secret_hash=secret_hash,
                token_prefix=token[:20],
                user_id="task-user",
                project_id=project_id,
                scopes=["events:write", "context:read"],
            )
        )
        session.commit()
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}
    created = _event(_task_event("conflict-create", "TaskCreated", expected_version=0, task_version=1, assignment_epoch=0, payload={"title": "Conflict task", "depends_on": [], "produced_memory": []}))
    stale = _event(_task_event("conflict-assign", "TaskAssigned", expected_version=7, task_version=8, assignment_epoch=1, payload={"attempt_id": "attempt-conflict", "assignee": "agent:worker", "assigned_by": "agent:lead", "epoch": 1}))

    assert client.post(f"/v1/projects/{project_id}/events/batch", headers=headers, json={"events": [created]}).status_code == 200
    response = client.post(f"/v1/projects/{project_id}/events/batch", headers=headers, json={"events": [stale]})

    assert response.status_code == 200
    assert response.json()["rejected"][0]["code"] == "version_conflict"


def test_task_sync_rejects_an_executor_event_for_a_different_attempt() -> None:
    app = create_app()
    token_id, token, secret_hash = create_token()
    project_id = f"task-attempt-{uuid4().hex}"
    with app.state.session_factory() as session:
        session.add(
            AccessToken(
                token_id=token_id,
                token_secret_hash=secret_hash,
                token_prefix=token[:20],
                user_id="task-user",
                project_id=project_id,
                scopes=["events:write", "context:read"],
            )
        )
        session.commit()
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}
    created = _event(_task_event("attempt-create", "TaskCreated", expected_version=0, task_version=1, assignment_epoch=0, payload={"title": "Attempt task", "depends_on": [], "produced_memory": []}))
    assigned = _event(_task_event("attempt-assign", "TaskAssigned", expected_version=1, task_version=2, assignment_epoch=1, payload={"attempt_id": "attempt-current", "assignee": "agent:worker", "assigned_by": "agent:lead", "epoch": 1}))
    wrong_claim = _event(_task_event("attempt-claim", "TaskClaimed", expected_version=2, task_version=3, assignment_epoch=1, expected_assignment_epoch=1, actor_id="agent:worker", payload={"attempt_id": "attempt-other"}))

    assert client.post(f"/v1/projects/{project_id}/events/batch", headers=headers, json={"events": [created, assigned]}).status_code == 200
    response = client.post(f"/v1/projects/{project_id}/events/batch", headers=headers, json={"events": [wrong_claim]})

    assert response.status_code == 200
    assert response.json()["rejected"][0]["code"] == "attempt_conflict"


def test_task_sync_rejects_a_changed_replay_of_the_same_command() -> None:
    app = create_app()
    token_id, token, secret_hash = create_token()
    project_id = f"task-command-{uuid4().hex}"
    with app.state.session_factory() as session:
        session.add(
            AccessToken(
                token_id=token_id,
                token_secret_hash=secret_hash,
                token_prefix=token[:20],
                user_id="task-user",
                project_id=project_id,
                scopes=["events:write", "context:read"],
            )
        )
        session.commit()
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}
    created = _event(_task_event("replay-create", "TaskCreated", expected_version=0, task_version=1, assignment_epoch=0, payload={"title": "Replay task", "depends_on": [], "produced_memory": []}))
    changed = _event(_task_event("replay-create", "TaskCreated", expected_version=0, task_version=2, assignment_epoch=0, payload={"title": "Replay task", "depends_on": [], "produced_memory": []}))

    assert client.post(f"/v1/projects/{project_id}/events/batch", headers=headers, json={"events": [created]}).status_code == 200
    response = client.post(f"/v1/projects/{project_id}/events/batch", headers=headers, json={"events": [changed]})

    assert response.status_code == 200
    assert response.json()["rejected"][0]["code"] == "task_command_conflict"


def test_task_sync_rejects_a_nested_actor_that_disagrees_with_event_identity() -> None:
    app = create_app()
    token_id, token, secret_hash = create_token()
    project_id = f"task-actor-{uuid4().hex}"
    with app.state.session_factory() as session:
        session.add(
            AccessToken(
                token_id=token_id,
                token_secret_hash=secret_hash,
                token_prefix=token[:20],
                user_id="task-user",
                project_id=project_id,
                scopes=["events:write", "context:read"],
            )
        )
        session.commit()
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}
    event = _event(_task_event("actor-create", "TaskCreated", expected_version=0, task_version=1, assignment_epoch=0, actor_id="agent:inner", payload={"title": "Actor task", "depends_on": [], "produced_memory": []}))
    event["agent_id"] = "agent:outer"

    response = client.post(f"/v1/projects/{project_id}/events/batch", headers=headers, json={"events": [event]})

    assert response.status_code == 200
    assert response.json()["rejected"][0]["code"] == "invalid_task_actor"