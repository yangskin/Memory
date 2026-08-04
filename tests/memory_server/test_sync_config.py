from __future__ import annotations

import json

from servers.memory_server import memory_config
from servers.memory_server.memory_config import load_config
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


def test_local_user_config_supplies_private_hub_connection(repo, monkeypatch, tmp_path) -> None:
    local_config = tmp_path / "user_config.local.json"
    local_config.write_text(
        json.dumps(
            {
                "user_name": "alice",
                "shared_memory": {
                    "enabled": True,
                    "server_url": "https://memory.example.com",
                    "project_id": "project-1",
                    "token": "local-test-token",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(memory_config, "_local_user_config_path", lambda _root: local_config)

    config = load_config(repo)

    assert config.shared_memory.active
    assert config.shared_memory.token == "local-test-token"
    assert config.shared_memory.user_id == "alice"