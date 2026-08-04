from __future__ import annotations

from servers.memory_server.memory_sync_client import MemoryHubClient
from servers.memory_server.memory_sync_config import SharedMemoryConfig
from servers.memory_server.memory_sync_protocol import build_memory_event


def test_disabled_client_does_not_make_a_network_request(monkeypatch) -> None:
    client = MemoryHubClient(SharedMemoryConfig(enabled=False))
    status, response = client.upload([])
    assert status == 0
    assert response["error"] == "shared_memory_disabled"


def test_event_omits_context_token_and_absolute_paths() -> None:
    event = build_memory_event(
        {"content_markdown": "safe", "context_token": "ctx_secret", "scope": "personal"},
        {"id": "mem_1", "path": "/absolute/private/path.md"},
    )
    assert "context_token" not in event
    assert event["source_record_id"] == "mem_1"
    assert event["content_hash"].startswith("sha256:")