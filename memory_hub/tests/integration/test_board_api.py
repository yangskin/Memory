from __future__ import annotations

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from memory_hub.api.main import create_app
from memory_hub.auth.tokens import create_token
from memory_hub.db.models import AccessToken


pytestmark = pytest.mark.skipif(not os.getenv("MEMORY_HUB_DATABASE_URL"), reason="requires PostgreSQL")


def _seed_token(app, *, user_id: str = "alice", scopes: list[str] | None = None, project_id: str = "project-a") -> str:
    token_id, raw_token, secret_hash = create_token()
    with app.state.session_factory() as session:
        session.add(
            AccessToken(
                token_id=token_id,
                token_secret_hash=secret_hash,
                token_prefix=raw_token[:20],
                user_id=user_id,
                project_id=project_id,
                scopes=scopes or ["events:write", "context:read"],
            )
        )
        session.commit()
    return raw_token


def test_board_post_query_reply_resolve_roundtrip() -> None:
    app = create_app()
    project_id = f"project-{uuid4().hex}"
    raw_token = _seed_token(app, project_id=project_id)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {raw_token}", "X-Memory-User-ID": "alice"}

    post_resp = client.post(
        f"/v1/projects/{project_id}/board/post",
        headers=headers,
        json={
            "post_type": "question",
            "content": "请确认网络接口修改影响",
            "task_id": "network",
            "author_agent_id": "pytest",
            "author_agent_instance_id": "pytest-1",
            "runtime_node_id": "node-1",
            "source_node_name": "test-host",
            "workspace_id": "sha256:" + "c" * 64,
            "agent_session_id": "session-1",
            "transport_id": "memory-mcp",
        },
    )
    assert post_resp.status_code == 200
    root = post_resp.json()["post"]
    assert root["post_type"] == "question"
    assert root["author_user_id"] == "alice"
    assert root["runtime_node_id"] == "node-1"
    assert root["source_node_name"] == "test-host"
    assert root["workspace_id"] == "sha256:" + "c" * 64
    assert root["agent_session_id"] == "session-1"
    assert root["transport_id"] == "memory-mcp"

    reply_resp = client.post(
        f"/v1/projects/{project_id}/board/reply",
        headers=headers,
        json={
            "thread_id": root["thread_id"],
            "reply_to": root["post_id"],
            "content": "已确认影响在复制排序阶段",
            "task_id": "network",
            "author_agent_id": "pytest",
            "author_agent_instance_id": "pytest-1",
            "runtime_node_id": "node-1",
            "source_node_name": "test-host",
            "workspace_id": "sha256:" + "c" * 64,
            "agent_session_id": "session-1",
            "transport_id": "memory-mcp",
        },
    )
    assert reply_resp.status_code == 200
    reply = reply_resp.json()["post"]
    assert reply["post_type"] == "reply"
    assert reply["thread_id"] == root["thread_id"]
    assert reply["runtime_node_id"] == "node-1"
    assert reply["agent_session_id"] == "session-1"

    unresolved = client.post(
        f"/v1/projects/{project_id}/board/query",
        headers={"Authorization": f"Bearer {raw_token}"},
        json={"filter": "unresolved", "task_id": "network", "max_items": 20},
    )
    assert unresolved.status_code == 200
    ids = {item["post_id"] for item in unresolved.json()["items"]}
    assert root["post_id"] in ids
    assert reply["post_id"] in ids

    resolve = client.post(
        f"/v1/projects/{project_id}/board/resolve",
        headers=headers,
        json={"post_id": root["post_id"]},
    )
    assert resolve.status_code == 200
    assert resolve.json()["post"]["status"] == "resolved"

    resolved = client.post(
        f"/v1/projects/{project_id}/board/query",
        headers={"Authorization": f"Bearer {raw_token}"},
        json={"status": "resolved", "thread_id": root["thread_id"], "max_items": 20},
    )
    assert resolved.status_code == 200
    assert any(item["post_id"] == root["post_id"] for item in resolved.json()["items"])


def test_board_route_enforces_scope_and_secret_filter() -> None:
    app = create_app()
    project_id = f"project-{uuid4().hex}"
    writer = _seed_token(app, scopes=["events:write"], project_id=project_id)
    reader = _seed_token(app, user_id="bob", scopes=["context:read"], project_id=project_id)
    client = TestClient(app)

    no_read = client.post(
        f"/v1/projects/{project_id}/board/query",
        headers={"Authorization": f"Bearer {writer}"},
        json={},
    )
    assert no_read.status_code == 403

    no_write = client.post(
        f"/v1/projects/{project_id}/board/post",
        headers={"Authorization": f"Bearer {reader}"},
        json={"post_type": "note", "content": "reader cannot write"},
    )
    assert no_write.status_code == 403

    secret = client.post(
        f"/v1/projects/{project_id}/board/post",
        headers={"Authorization": f"Bearer {writer}", "X-Memory-User-ID": "alice"},
        json={"post_type": "warning", "content": "postgres://u:p@host/db"},
    )
    assert secret.status_code == 422


def test_board_project_scope_is_enforced() -> None:
    app = create_app()
    project_a = f"project-a-{uuid4().hex}"
    project_b = f"project-b-{uuid4().hex}"
    token = _seed_token(app, project_id=project_a)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}", "X-Memory-User-ID": "alice"}

    forbidden = client.post(
        f"/v1/projects/{project_b}/board/post",
        headers=headers,
        json={"post_type": "note", "content": "cross project"},
    )
    assert forbidden.status_code == 403


def test_board_post_retry_with_same_client_id_is_idempotent() -> None:
    app = create_app()
    project_id = f"project-{uuid4().hex}"
    raw_token = _seed_token(app, project_id=project_id)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {raw_token}", "X-Memory-User-ID": "alice"}
    post_id = str(uuid4())
    payload = {
        "post_id": post_id,
        "post_type": "handoff",
        "content": "offline-first retry",
        "thread_id": "",
        "references_json": [],
    }

    first = client.post(f"/v1/projects/{project_id}/board/post", headers=headers, json=payload)
    second = client.post(f"/v1/projects/{project_id}/board/post", headers=headers, json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["post"]["post_id"] == post_id
    assert second.json()["post"]["post_id"] == post_id

    queried = client.post(
        f"/v1/projects/{project_id}/board/query",
        headers={"Authorization": f"Bearer {raw_token}"},
        json={"thread_id": post_id},
    )
    assert queried.status_code == 200
    assert queried.json()["total"] == 1


def test_shared_board_returns_project_board_with_explicit_details() -> None:
    app = create_app()
    project_id = f"project-{uuid4().hex}"
    raw_token = _seed_token(app, project_id=project_id)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {raw_token}", "X-Memory-User-ID": "alice"}

    first = client.post(
        f"/v1/projects/{project_id}/board/post",
        headers=headers,
        json={"post_type": "question", "content": "question one"},
    )
    root = first.json()["post"]
    client.post(
        f"/v1/projects/{project_id}/board/reply",
        headers=headers,
        json={"thread_id": root["thread_id"], "reply_to": root["post_id"], "content": "reply one"},
    )
    client.post(
        f"/v1/projects/{project_id}/board/resolve",
        headers=headers,
        json={"post_id": root["post_id"]},
    )

    board = client.post(
        "/v1/shared-board",
        headers={"Authorization": f"Bearer {raw_token}"},
        json={},
    )
    assert board.status_code == 200
    body = board.json()
    assert body["project_id"] == project_id
    assert body["total"] == 2
    ids = {item["post_id"] for item in body["items"]}
    assert root["post_id"] in ids
    statuses = {item["post_type"] for item in body["items"]}
    assert {"question", "reply"} <= statuses
    assert all("references_json" not in item for item in body["items"])

    detailed = client.post(
        "/v1/shared-board",
        headers={"Authorization": f"Bearer {raw_token}"},
        json={"include_content": True, "include_references": True},
    )
    assert detailed.status_code == 200
    assert all("references_json" in item for item in detailed.json()["items"])


def test_shared_board_requires_context_read_scope() -> None:
    app = create_app()
    project_id = f"project-{uuid4().hex}"
    writer = _seed_token(app, scopes=["events:write"], project_id=project_id)
    client = TestClient(app)
    resp = client.post("/v1/shared-board", headers={"Authorization": f"Bearer {writer}"}, json={})
    assert resp.status_code == 403
