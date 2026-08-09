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


def test_shared_memory_bounds_the_authoritative_task_command_timeout() -> None:
    config = parse_shared_memory_config({"task_command_timeout_seconds": 0})

    assert config.task_command_timeout_seconds == 0.1


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
    monkeypatch.setattr(memory_config, "_local_shared_memory_config_path", lambda _root: tmp_path / "shared_memory.local.json")

    config = load_config(repo)

    assert config.shared_memory.active
    assert config.shared_memory.token == "local-test-token"
    assert config.shared_memory.user_id == "alice"


def test_dedicated_shared_memory_file_takes_priority(repo, monkeypatch, tmp_path) -> None:
    local_config = tmp_path / "user_config.local.json"
    local_config.write_text(json.dumps({"user_name": "bob"}), encoding="utf-8")
    shared_config = tmp_path / "shared_memory.local.json"
    shared_config.write_text(
        json.dumps(
            {
                "enabled": True,
                "server_url": "https://hub.example.com",
                "project_id": "project-2",
                "user_id": "bob",
                "token": "dedicated-token",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(memory_config, "_local_user_config_path", lambda _root: local_config)
    monkeypatch.setattr(memory_config, "_local_shared_memory_config_path", lambda _root: shared_config)

    config = load_config(repo)

    assert config.shared_memory.active
    assert config.shared_memory.server_url == "https://hub.example.com"
    assert config.shared_memory.project_id == "project-2"
    assert config.shared_memory.token == "dedicated-token"
    assert config.shared_memory.user_id == "bob"


def test_dedicated_shared_memory_file_backfills_user_id_from_identity(repo, monkeypatch, tmp_path) -> None:
    local_config = tmp_path / "user_config.local.json"
    local_config.write_text(json.dumps({"user_name": "carol"}), encoding="utf-8")
    shared_config = tmp_path / "shared_memory.local.json"
    shared_config.write_text(
        json.dumps(
            {
                "enabled": True,
                "server_url": "https://hub.example.com",
                "project_id": "project-3",
                "token": "dedicated-token",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(memory_config, "_local_user_config_path", lambda _root: local_config)
    monkeypatch.setattr(memory_config, "_local_shared_memory_config_path", lambda _root: shared_config)

    config = load_config(repo)

    assert config.shared_memory.active
    assert config.shared_memory.user_id == "carol"


def test_user_id_always_reused_from_local_identity(repo, monkeypatch, tmp_path) -> None:
    """Hub user_id 始终复用 user 配置中的身份；连接文件中显式配置的 user_id 被覆盖。"""
    local_config = tmp_path / "user_config.local.json"
    local_config.write_text(json.dumps({"user_name": "dave"}), encoding="utf-8")
    shared_config = tmp_path / "shared_memory.local.json"
    shared_config.write_text(
        json.dumps(
            {
                "enabled": True,
                "server_url": "https://hub.example.com",
                "project_id": "project-4",
                "user_id": "stale-user-id",
                "token": "dedicated-token",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(memory_config, "_local_user_config_path", lambda _root: local_config)
    monkeypatch.setattr(memory_config, "_local_shared_memory_config_path", lambda _root: shared_config)

    config = load_config(repo)

    assert config.shared_memory.active
    assert config.shared_memory.project_id == "project-4"
    assert config.shared_memory.user_id == "dave"


def test_shared_memory_user_id_kept_when_identity_missing(repo, monkeypatch, tmp_path) -> None:
    """user 配置缺失时，回退保留连接文件中显式配置的 user_id（向后兼容）。"""
    shared_config = tmp_path / "shared_memory.local.json"
    shared_config.write_text(
        json.dumps(
            {
                "enabled": True,
                "server_url": "https://hub.example.com",
                "project_id": "project-5",
                "user_id": "legacy-user-id",
                "token": "dedicated-token",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(memory_config, "_local_user_config_path", lambda _root: tmp_path / "missing_user_config.local.json")
    monkeypatch.setattr(memory_config, "_local_shared_memory_config_path", lambda _root: shared_config)

    config = load_config(repo)

    assert config.shared_memory.active
    assert config.shared_memory.user_id == "legacy-user-id"