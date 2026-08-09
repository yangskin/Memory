from __future__ import annotations

import json
from email.message import Message
from io import BytesIO
from urllib.error import HTTPError

from servers.memory_server.memory_sync_client import MemoryHubClient, _SSL_CONTEXT
from servers.memory_server.memory_sync_config import SharedMemoryConfig
from servers.memory_server.memory_sync_protocol import build_memory_event


def test_disabled_client_does_not_make_a_network_request(monkeypatch) -> None:
    client = MemoryHubClient(SharedMemoryConfig(enabled=False))
    status, response = client.upload([])
    assert status == 0
    assert response["error"] == "shared_memory_disabled"


def test_configured_client_uses_https_url_and_environment_token(monkeypatch) -> None:
    monkeypatch.setenv("TEST_MEMORY_HUB_TOKEN", "mem_v1.test.secret")
    captured = {}

    class Response:
        status = 200

        def read(self):
            return b'{"accepted": [], "duplicates": [], "rejected": []}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def fake_urlopen(request, *, timeout, context):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["x-memory-user-id"] = request.get_header("X-memory-user-id")
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        captured["context"] = context
        return Response()

    monkeypatch.setattr("servers.memory_server.memory_sync_client.urlopen", fake_urlopen)
    client = MemoryHubClient(
        SharedMemoryConfig(
            enabled=True,
            server_url="https://memory.example.com",
            project_id="project-1",
            user_id="alice",
            token_env="TEST_MEMORY_HUB_TOKEN",
        )
    )
    status, response = client.upload([])
    assert status == 200
    assert response["accepted"] == []
    assert captured == {
        "url": "https://memory.example.com/v1/projects/project-1/events/batch",
        "authorization": "Bearer mem_v1.test.secret",
        "x-memory-user-id": "alice",
        "payload": {"events": []},
        "timeout": 5.0,
        "context": _SSL_CONTEXT,
    }


def test_http_error_preserves_json_body_and_retry_after(monkeypatch) -> None:
    monkeypatch.setenv("TEST_MEMORY_HUB_TOKEN", "mem_v1.test.secret")
    headers = Message()
    headers["Retry-After"] = "17"

    def fake_urlopen(*_args, **_kwargs):
        raise HTTPError(
            "https://memory.example.com/board/post",
            429,
            "Too Many Requests",
            headers,
            BytesIO(b'{"detail":"rate limit exceeded"}'),
        )

    monkeypatch.setattr("servers.memory_server.memory_sync_client.urlopen", fake_urlopen)
    client = MemoryHubClient(
        SharedMemoryConfig(
            enabled=True,
            server_url="https://memory.example.com",
            project_id="project-1",
            token_env="TEST_MEMORY_HUB_TOKEN",
        )
    )
    status, response = client.post("/board/post", {}, 1.0)
    assert status == 429
    assert response == {
        "detail": "rate limit exceeded",
        "error": "http_429",
        "retry_after_seconds": 17.0,
    }


def test_event_omits_context_token_and_absolute_paths() -> None:
    event = build_memory_event(
        {"content_markdown": "safe", "context_token": "ctx_secret", "scope": "personal"},
        {"id": "mem_1", "path": "/absolute/private/path.md"},
    )
    assert "context_token" not in event
    assert event["source_record_id"] == "mem_1"
    assert event["content_hash"].startswith("sha256:")


def test_event_identity_is_persisted_and_explicit_agent_is_respected(tmp_path) -> None:
    args = {"repo_root": tmp_path, "content_markdown": "safe", "scope": "personal"}
    first = build_memory_event(args, {"id": "mem_1", "path": "record.md"})
    second = build_memory_event(args, {"id": "mem_2", "path": "record-2.md"})
    assert first["source_node_id"] == second["source_node_id"]
    assert first["agent_instance_id"] == second["agent_instance_id"]
    assert first["agent_session_id"] == second["agent_session_id"]
    assert first["runtime_node_id"] == first["source_node_id"]
    assert first["source_node_name"]
    assert first["workspace_id"].startswith("sha256:")
    explicit = build_memory_event({**args, "agent_id": "copilot", "agent_instance_id": "copilot-1"}, {"id": "mem_3", "path": "record-3.md"})
    assert explicit["agent_id"] == "copilot"
    assert explicit["agent_instance_id"] == "copilot-1"


def test_event_identity_recovers_from_corrupt_file_and_bounds_fields(tmp_path) -> None:
    identity_path = tmp_path / ".ai-memory" / "identity.json"
    identity_path.parent.mkdir(parents=True)
    identity_path.write_text("not-json", encoding="utf-8")
    event = build_memory_event(
        {"content_markdown": "safe", "scope": "personal", "agent_id": "a" * 300},
        {"id": "mem_1", "path": "record.md"},
        repo_root=tmp_path,
    )
    assert len(event["agent_id"]) == 256
    assert event["source_node_id"]
    assert json.loads(identity_path.read_text(encoding="utf-8"))["source_node_id"] == event["source_node_id"]
