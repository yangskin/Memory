"""Real HTTP/auth/SQL routes; SQLite type adapters are test-only."""
from datetime import UTC, datetime
import hashlib
import secrets
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import BigInteger, Integer, JSON, create_engine, event
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from memory_hub.api import routes_board, routes_context, routes_enrollment, routes_events, routes_tasks
from memory_hub.auth.rate_limit import TokenRateLimiter
from memory_hub.auth.tokens import create_token
from memory_hub.config import load_settings
from memory_hub.db.models import AccessToken, Base, BriefHead, BriefSnapshot


@pytest.fixture
def service():
    originals = []
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, (ARRAY, JSONB, BigInteger)):
                originals.append((column, column.type))
                column.type = Integer() if isinstance(column.type, BigInteger) else JSON()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    event.listen(engine, "connect", lambda db, _: db.create_function("btrim", 1, lambda value: value.strip() if value is not None else None))
    try:
        Base.metadata.create_all(engine)
        app = FastAPI()
        app.state.session_factory = sessionmaker(engine, expire_on_commit=False)
        app.state.rate_limiter = TokenRateLimiter()
        app.state.settings = load_settings()
        for module in (routes_enrollment, routes_board, routes_context, routes_events, routes_tasks):
            app.include_router(module.router)
        def seed(project="project", user="owner", scopes=None):
            tid, raw, hashed = create_token()
            with app.state.session_factory() as session:
                session.add(AccessToken(token_id=tid, token_secret_hash=hashed, token_prefix="test",
                                        project_id=project, user_id=user, scopes=scopes or ["devices:enroll"]))
                session.commit()
            return {"Authorization": "Bearer " + raw}
        yield TestClient(app), app, seed
    finally:
        engine.dispose()
        for column, original in originals:
            column.type = original


def request_data(device="TEST-PC"):
    tid, raw, hashed = create_token()
    return {"enrollment_id": uuid4().hex, "device_name": device, "token_id": tid,
            "token_secret_hash": hashed, "poll_secret_hash": hashlib.sha256(secrets.token_bytes(32)).hexdigest()}, raw


def test_invitation_scope_project_and_idempotency(service):
    client, app, seed = service
    invite = seed()
    ordinary = seed(scopes=["events:write", "context:read"])
    outsider = seed(project="other")
    payload, token = request_data()
    url = "/v1/projects/project/device-enrollments/auto"
    assert client.post(url, json=payload).status_code == 401
    assert client.post(url, json=payload, headers=ordinary).status_code == 403
    assert client.post(url, json=payload, headers=outsider).status_code == 403
    result = client.post(url, json=payload, headers=invite)
    assert result.status_code == 200 and result.json()["status"] == "approved"
    assert client.post(url, json=payload, headers=invite).json() == result.json()
    assert client.post(url, json={**payload, "device_name": "OTHER"}, headers=invite).status_code == 409
    assert client.post(url, json=payload, headers=seed()).status_code == 409
    with app.state.session_factory() as session:
        device = session.get(AccessToken, payload["token_id"])
        assert device.user_id == "owner" and device.scopes == ["events:write", "context:read"]
        device.revoked_at = datetime.now(UTC)
        session.commit()
    assert client.post(url, json=payload, headers=invite).status_code == 409
    assert client.get("/v1/projects/project/connection", headers={"Authorization": "Bearer " + token}).status_code == 401


def test_new_device_board_task_lifecycle_and_project_brief(service):
    client, app, seed = service
    payload, token = request_data()
    base = "/v1/projects/project"
    invite = seed()
    assert client.post(base + "/device-enrollments/auto", json=payload, headers=invite).status_code == 200
    headers = {"Authorization": "Bearer " + token, "X-Memory-User-ID": "forged"}
    info = client.get(base + "/connection", headers=headers).json()
    assert info["user_id"] == "owner" and all(info["capabilities"].values())
    assert client.get(base + "/device-enrollments", headers=headers).status_code == 403
    assert client.post(base + "/device-enrollments/auto", json=request_data()[0], headers=headers).status_code == 403
    assert client.get("/v1/projects/other/connection", headers=headers).status_code == 403
    post = client.post(base + "/board/post", headers=headers, json={"post_type": "note", "content": "Enrollment test"})
    assert post.status_code == 200, post.text
    item = post.json()["post"]
    assert item["author_user_id"] == "owner"
    assert client.post(base + "/board/query", headers=headers, json={}).json()["items"][0]["post_id"] == item["post_id"]
    assert client.post(base + "/board/reply", headers=headers, json={"reply_to": item["post_id"], "content": "Verified"}).status_code == 200
    assert client.post(base + "/board/resolve", headers=headers, json={"post_id": item["post_id"]}).status_code == 200
    # The very same independent device token can execute the full Task Graph lifecycle.
    commands = [
        ("TaskCreated", {"title": "Test", "objective": "Verify enrollment", "acceptance": "done", "priority": "normal", "depends_on": [], "produced_memory": []}, 0, None),
        ("TaskAssigned", {"attempt_id": "attempt", "assignee": "agent", "assigned_by": "agent", "epoch": 1}, 1, None),
        ("TaskClaimed", {"attempt_id": "attempt"}, 1, 1),
        ("TaskSubmitted", {"attempt_id": "attempt", "submission_id": "submission", "summary": "tested", "evidence": ["pytest"]}, 1, 1),
        ("TaskReviewed", {"review_id": "review", "submission_id": "submission", "decision": "approved", "summary": "verified"}, 1, None),
    ]
    for version, (kind, data, epoch, expected_epoch) in enumerate(commands):
        event = {"version": "1.0", "command_id": uuid4().hex, "event_type": kind, "task_id": "task",
                 "actor_id": "reviewer" if kind == "TaskReviewed" else "agent", "expected_version": version, "task_version": version + 1,
                 "assignment_epoch": epoch, "expected_assignment_epoch": expected_epoch,
                 "payload": data, "occurred_at": datetime.now(UTC).isoformat()}
        envelope = {"schema_version": "1.0", "event_id": str(uuid4()), "agent_id": event["actor_id"], "agent_instance_id": "test",
                    "task_id": "task", "operation": "task_sync", "scope": "project_shared", "content_markdown": "",
                    "metadata": {"task_event": event}, "occurred_at": datetime.now(UTC).isoformat(), "content_hash": "sha256:" + "0" * 64}
        response = client.post(base + "/events/batch", headers=headers, json={"events": [envelope]})
        assert response.status_code == 200 and len(response.json()["accepted"]) == 1, response.text
    graph = client.get(base + "/task-graph?task_id=task", headers=headers).json()
    assert next(node for node in graph["nodes"] if node["type"] == "task")["metadata"]["state"] == "done"
    assert len(client.get(base + "/task-events?task_id=task", headers=headers).json()["events"]) == 5
    # Seed a derived public brief and a different user's private brief.
    with app.state.session_factory() as session:
        for kind, subject, text in [("project_recent", "", "Important shared project memory"), ("user_recent", "forged", "PRIVATE")]:
            bid = uuid4()
            session.add(BriefSnapshot(brief_id=bid, project_id="project", brief_type=kind, subject_user_id=subject,
                        input_seq_to=5, structured_brief={}, rendered_markdown=text, prompt_version="test",
                        generated_at=datetime.now(UTC), source_event_ids=[], status="ready"))
            session.add(BriefHead(project_id="project", brief_type=kind, subject_user_id=subject, current_brief_id=bid))
        session.commit()
    response = client.post(base + "/context", headers=headers,
                           json={"agent_instance_id": "test", "include": ["project_brief", "user_brief"]})
    assert response.status_code == 200, response.text
    assert response.json()["project_brief"]["markdown"] == "Important shared project memory"
    assert "PRIVATE" not in response.text and "user_brief" not in response.json()


def test_client_background_enrollment_uses_real_http_routes(service, tmp_path, monkeypatch):
    import json
    pytest.importorskip("servers.memory_server.memory_hub_enrollment", reason="requires the local MCP source for cross-package integration")
    from servers.memory_server.memory_config import load_config, ReloadableMemoryConfig
    from servers.memory_server.memory_hub_enrollment import ensure_hub_connection
    client, app, seed = service
    invitation = seed()["Authorization"].removeprefix("Bearer ")
    monkeypatch.delenv("MEMORY_HUB_TOKEN", raising=False)
    monkeypatch.setenv("MEMORY_HUB_ENROLLMENT_TOKEN", invitation)
    config = load_config(tmp_path)
    config.config_path.parent.mkdir(parents=True, exist_ok=True)
    config.config_path.write_text(json.dumps({"shared_memory": {"enabled": True,
        "server_url": "https://hub.example.com", "project_id": "project"}}))
    provider = ReloadableMemoryConfig(load_config(tmp_path))
    def send(shared, method, path, payload=None):
        response = client.request(method, "/v1/projects/" + shared.project_id + path,
                                  json=payload, headers={"Authorization": "Bearer " + shared.token})
        assert response.status_code == 200, response.text
        return response.json()
    ready = ensure_hub_connection(provider.get(), send=send)
    assert ready["status"] == "ready" and ready["registered"]
    assert ready["project_brief_available"] is False  # no invented summary on an empty project
    assert provider.get().shared_memory.token != invitation
    assert ensure_hub_connection(provider.get(), send=send)["status"] == "ready"
    # Every client gets the same usable persisted credential; no client brand gate.
    for name in ("Codex", "CodeBuddy", "other-mcp-client"):
        shared = load_config(tmp_path).shared_memory
        posted = client.post("/v1/projects/project/board/post", json={"post_type": "note", "content": name},
                             headers={"Authorization": "Bearer " + shared.token})
        assert posted.status_code == 200


def test_background_registration_then_actual_mcp_tools(service, tmp_path, monkeypatch):
    import asyncio
    import json
    pytest.importorskip("servers.memory_server.memory_hub_enrollment", reason="requires local MCP source")
    from mcp.types import CallToolRequest, CallToolRequestParams
    from servers.memory_server import memory_hub_enrollment as enrollment
    from servers.memory_server import memory_sync_worker as sync
    from servers.memory_server.memory_config import load_config, ReloadableMemoryConfig
    from servers.memory_server.memory_sync_client import MemoryHubClient
    from servers.memory_server.server import create_server

    client, app, seed = service
    monkeypatch.delenv("MEMORY_HUB_TOKEN", raising=False)
    monkeypatch.setenv("MEMORY_HUB_ENROLLMENT_TOKEN", seed()["Authorization"].removeprefix("Bearer "))
    monkeypatch.setenv("MEMORY_MCP_USER", "owner")
    config = load_config(tmp_path)
    config.config_path.parent.mkdir(parents=True, exist_ok=True)
    config.config_path.write_text(json.dumps({"shared_memory": {"enabled": True,
        "server_url": "https://hub.example.com", "project_id": "project"},
        "worker": {"enabled": False}, "reflection": {"enabled": False}}))
    provider = ReloadableMemoryConfig(load_config(tmp_path))
    # Construct the MCP server before enrollment to exercise its config reload.
    server = create_server(provider)

    def transport(shared, method, path, payload=None):
        return client.request(method, path, json=payload,
                              headers={"Authorization": "Bearer " + shared.token})
    def send(shared, method, path, payload=None):
        response = transport(shared, method, "/v1/projects/" + shared.project_id + path, payload)
        assert response.status_code == 200, response.text
        return response.json()
    original = enrollment.ensure_hub_connection
    monkeypatch.setattr(enrollment, "ensure_hub_connection", lambda config: original(config, send=send))
    def request(self, method, path, payload, timeout_seconds):
        response = transport(self.config, method, path, payload)
        return response.status_code, response.json()
    monkeypatch.setattr(MemoryHubClient, "_request", request)
    worker = sync.MemorySyncWorker(provider.get)
    monkeypatch.setitem(sync._WORKERS, tmp_path.resolve(), worker)
    worker.run_once()
    assert worker.connection_status["status"] == "ready"
    assert provider.get().shared_memory.token

    bid = uuid4()
    with app.state.session_factory() as session:
        session.add(BriefSnapshot(brief_id=bid, project_id="project", brief_type="project_recent", subject_user_id="",
                    input_seq_to=0, structured_brief={}, rendered_markdown="Shared project brief", prompt_version="test",
                    generated_at=datetime.now(UTC), source_event_ids=[], status="ready"))
        session.add(BriefHead(project_id="project", brief_type="project_recent", subject_user_id="", current_brief_id=bid))
        session.commit()

    async def call(name, args):
        response = await server.request_handlers[CallToolRequest](CallToolRequest(
            method="tools/call", params=CallToolRequestParams(name=name, arguments=args)))
        return json.loads(response.root.content[0].text)
    async def exercise():
        posted = await call("memory_board_write", {"action": "post", "post_type": "note", "content": "MCP registered device"})
        assert posted["ok"], posted
        assert posted["board_sync"]["queued"], posted
        # Board writes deliberately acknowledge local persistence first. Wait for
        # the real scheduled sender, then prove presence in the remote database.
        from servers.memory_server import server_dispatch
        for thread in list(server_dispatch._board_sync_threads.values()):
            thread.join(timeout=5)
            assert not thread.is_alive()
        remote_posts = send(provider.get().shared_memory, "POST", "/board/query", {})
        assert any(item["post_id"] == posted["post"]["post_id"] for item in remote_posts["items"])
        queried = await call("memory_board_read", {"filter": "unresolved"})
        assert queried["ok"] and queried["board_sync"]["remote"], queried
        assert any(item["post_id"] == posted["post"]["post_id"] for item in queried["items"])
        created = await call("memory_task_sync", {"action": "create", "command_id": "mcp-new-create",
            "expected_version": 0, "task_id": "mcp-new", "actor_id": "agent:mcp", "title": "MCP new device"})
        assert created["ok"], created
        graph = await call("memory_task_sync", {"action": "sync", "task_id": "mcp-new"})
        assert graph["ok"] and graph["source"] == "remote" and graph["authoritative"], graph
        assert any(node["type"] == "task" for node in graph["bundle"]["nodes"]), graph
        brief = await call("memory_read", {"operation": "shared_context", "include": ["project_brief"], "force_refresh": True})
        assert brief["shared_context"]["source"] == "remote", brief
        assert brief["shared_context"]["project_brief"]["markdown"] == "Shared project brief", brief
        assert brief["hub_connection"]["status"] == "ready", brief
    asyncio.run(exercise())
