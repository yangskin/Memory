from __future__ import annotations

from servers.memory_server.memory_sync_config import parse_shared_memory_config


def test_shared_memory_is_inactive_without_configuration(monkeypatch) -> None:
    monkeypatch.delenv("MEMORY_HUB_TOKEN", raising=False)
    config = parse_shared_memory_config({})
    assert not config.active


def test_shared_memory_activates_only_with_complete_configuration(monkeypatch) -> None:
    config = parse_shared_memory_config(
        {
            "enabled": True,
            "server_url": "https://memory.example.com/",
            "project_id": "project-1",
        }
    )
    assert not config.active
    monkeypatch.setenv("MEMORY_HUB_TOKEN", "mem_v1.test.secret")
    assert config.active
    assert config.server_url == "https://memory.example.com"