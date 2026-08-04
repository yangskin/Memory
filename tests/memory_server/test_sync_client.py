from __future__ import annotations

import json

from servers.memory_server.memory_sync_client import MemoryHubClient
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

    def fake_urlopen(request, *, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("servers.memory_server.memory_sync_client.urlopen", fake_urlopen)
    client = MemoryHubClient(
        SharedMemoryConfig(
            enabled=True,
            server_url="https://memory.example.com",
            project_id="project-1",
            token_env="TEST_MEMORY_HUB_TOKEN",
        )
    )
    status, response = client.upload([])
    assert status == 200
    assert response["accepted"] == []
    assert captured == {
        "url": "https://memory.example.com/v1/projects/project-1/events/batch",
        "authorization": "Bearer mem_v1.test.secret",
        "payload": {"events": []},
        "timeout": 5.0,
    }


def test_event_omits_context_token_and_absolute_paths() -> None:
    event = build_memory_event(
        {"content_markdown": "safe", "context_token": "ctx_secret", "scope": "personal"},
        {"id": "mem_1", "path": "/absolute/private/path.md"},
    )
    assert "context_token" not in event
    assert event["source_record_id"] == "mem_1"
    assert event["content_hash"].startswith("sha256:")