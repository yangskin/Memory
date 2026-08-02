from __future__ import annotations

import json
from pathlib import Path

import pytest

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_events import _vscode_user_cache
from servers.memory_server.memory_guard import memory_guard_check
from servers.memory_server.memory_reader import memory_get
from servers.memory_server.memory_search import memory_search
from servers.memory_server.memory_writer import memory_write
from servers.memory_server.server import _dispatch_tool


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture()
def multi_user_repo(tmp_path: Path) -> Path:
    _write(tmp_path / "memory-bank/activeContext.md", "# Legacy Active\n- shared seed\n")
    _write(tmp_path / "memory-bank/progress.md", "# Progress\n- baseline\n")
    _write(tmp_path / "memory-bank/notes.md", "# Notes\n")
    _write(tmp_path / ".ai-context/current-task.md", "# Task\n")
    _write(tmp_path / ".ai-memory/events.jsonl", "")
    (tmp_path / ".ai-memory/backups").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai-memory/temp").mkdir(parents=True, exist_ok=True)

    config_data = {
        "allowed_roots": [".ai-context", "memory-bank"],
        "excluded_dirs": [],
        "max_file_size_bytes": 1048576,
        "skip_binary_files": True,
        "events_file": ".ai-memory/events.jsonl",
        "backups_dir": ".ai-memory/backups",
        "temp_dir": ".ai-memory/temp",
        "mcp": {"shared_overwrite_policy": "downgrade"},
        "multi_user": {
            "user_scoped_paths": ["memory-bank/activeContext.md"],
            "shared_paths_policy": {
                "memory-bank/progress.md": "append_only",
            },
        },
        "guard": {
            "default_max_chars": 12000,
            "default_max_tokens": 3000,
            "total_max_chars": 60000,
            "total_max_tokens": 15000,
            "targets": [
                {
                    "path": "memory-bank/activeContext.md",
                    "max_chars": 8000,
                    "policy": "warm_context",
                    "role": "current sprint focus",
                    "write_policy": "user_scoped",
                },
                {
                    "path": "memory-bank/progress.md",
                    "max_chars": 12000,
                    "policy": "warm_context",
                    "role": "shared progress",
                    "write_policy": "append_only",
                },
                {
                    "path": ".ai-context/current-task.md",
                    "max_chars": 6000,
                    "policy": "hot_task",
                    "role": "hot task",
                },
            ],
        },
    }
    _write(tmp_path / ".ai-memory/config.json", json.dumps(config_data, ensure_ascii=False, indent=2))
    return tmp_path


def _set_env_user(monkeypatch: pytest.MonkeyPatch, user: str) -> None:
    _vscode_user_cache.clear()
    monkeypatch.setenv("USERNAME", user)
    monkeypatch.delenv("USER", raising=False)


def test_multi_user_policy_is_always_on_for_generated_config(tmp_path: Path) -> None:
    config = load_config(tmp_path)

    assert config.multi_user is not None
    assert config.multi_user.user_scoped_paths == ["memory-bank/activeContext.md"]
    assert config.multi_user.shared_paths_policy
    assert config.multi_user.shared_paths_policy["memory-bank/progress.md"] == "append_only"


def test_legacy_multi_user_enabled_false_is_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path / "memory-bank/activeContext.md", "# Legacy Active\n- seed\n")
    _write(tmp_path / ".ai-memory/events.jsonl", "")
    _write(
        tmp_path / ".ai-memory/config.json",
        json.dumps(
            {
                "allowed_roots": [".ai-context", "memory-bank"],
                "multi_user": {"enabled": False},
                "guard": {"targets": [{"path": "memory-bank/activeContext.md", "max_chars": 8000}]},
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    _set_env_user(monkeypatch, "alice")
    config = load_config(tmp_path)

    result = memory_write(config, "memory-bank/activeContext.md", "# Alice\n", backup=False)

    assert result["ok"] is True
    assert result["path"] == "memory-bank/activeContext/alice.md"


def test_default_multi_user_redirects_legacy_config_without_write_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path / "memory-bank/activeContext.md", "# Legacy Active\n- seed\n")
    _write(tmp_path / "memory-bank/progress.md", "# Progress\n- seed\n")
    _write(tmp_path / ".ai-memory/events.jsonl", "")
    _write(
        tmp_path / ".ai-memory/config.json",
        json.dumps(
            {
                "allowed_roots": [".ai-context", "memory-bank"],
                "mcp": {"shared_overwrite_policy": "downgrade"},
                "guard": {
                    "targets": [
                        {"path": "memory-bank/activeContext.md", "max_chars": 8000},
                        {"path": "memory-bank/progress.md", "max_chars": 12000},
                    ]
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    _set_env_user(monkeypatch, "alice")
    config = load_config(tmp_path)

    active_result = memory_write(config, "memory-bank/activeContext.md", "# Alice\n", backup=False)
    progress_result = memory_write(
        config,
        "memory-bank/progress.md",
        "- alice progress\n",
        mode="overwrite",
        backup=False,
    )

    assert active_result["ok"] is True
    assert active_result["path"] == "memory-bank/activeContext/alice.md"
    assert progress_result["ok"] is True
    assert progress_result["mode"] == "append"
    assert progress_result["policy_override"] == "append_only"


def test_guard_uses_default_multi_user_policy_for_legacy_config_without_write_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path / "memory-bank/activeContext.md", "# Legacy Active\n- seed\n")
    _write(tmp_path / "memory-bank/progress.md", "# Progress\n- seed\n")
    _write(tmp_path / ".ai-memory/events.jsonl", "")
    _write(
        tmp_path / ".ai-memory/config.json",
        json.dumps(
            {
                "allowed_roots": [".ai-context", "memory-bank"],
                "guard": {
                    "total_max_chars": 1000,
                    "targets": [
                        {"path": "memory-bank/activeContext.md", "max_chars": 500},
                        {"path": "memory-bank/progress.md", "max_chars": 500},
                    ],
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    _set_env_user(monkeypatch, "alice")
    config = load_config(tmp_path)

    memory_write(config, "memory-bank/activeContext.md", "# Alice\n", backup=False)
    result = memory_guard_check(config)
    paths = {item["path"] for item in result["targets"]}

    assert result["ok"] is True
    assert "memory-bank/activeContext/alice.md" in paths
    assert "memory-bank/activeContext.md" in paths


def test_user_scoped_writes_create_independent_active_context_files(
    multi_user_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config(multi_user_repo)

    _set_env_user(monkeypatch, "alice")
    alice_result = memory_write(config, "memory-bank/activeContext.md", "# Alice\n- task A\n", backup=False)
    _set_env_user(monkeypatch, "bob")
    bob_result = memory_write(config, "memory-bank/activeContext.md", "# Bob\n- task B\n", backup=False)

    assert alice_result["ok"] is True
    assert bob_result["ok"] is True
    assert alice_result["path"] == "memory-bank/activeContext/alice.md"
    assert bob_result["path"] == "memory-bank/activeContext/bob.md"

    alice_text = (multi_user_repo / "memory-bank/activeContext/alice.md").read_text(encoding="utf-8")
    bob_text = (multi_user_repo / "memory-bank/activeContext/bob.md").read_text(encoding="utf-8")
    legacy_text = (multi_user_repo / "memory-bank/activeContext.md").read_text(encoding="utf-8")

    assert "# Alice" in alice_text
    assert "# Bob" not in alice_text
    assert "# Bob" in bob_text
    assert "# Alice" not in bob_text
    assert legacy_text == "# Legacy Active\n- shared seed\n"


def test_user_scoped_reads_return_current_users_file(
    multi_user_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config(multi_user_repo)

    _set_env_user(monkeypatch, "alice")
    memory_write(config, "memory-bank/activeContext.md", "# Alice\n- task A\n", backup=False)
    _set_env_user(monkeypatch, "bob")
    memory_write(config, "memory-bank/activeContext.md", "# Bob\n- task B\n", backup=False)

    _set_env_user(monkeypatch, "alice")
    alice_read = memory_get(config, "memory-bank/activeContext.md")
    _set_env_user(monkeypatch, "bob")
    bob_read = memory_get(config, "memory-bank/activeContext.md")

    assert alice_read["ok"] is True
    assert bob_read["ok"] is True
    assert alice_read["path"] == "memory-bank/activeContext/alice.md"
    assert bob_read["path"] == "memory-bank/activeContext/bob.md"
    assert "# Alice" in alice_read["content"]
    assert "# Bob" not in alice_read["content"]
    assert "# Bob" in bob_read["content"]
    assert "# Alice" not in bob_read["content"]


def test_first_user_scoped_read_migrates_legacy_active_context(
    multi_user_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config(multi_user_repo)

    _set_env_user(monkeypatch, "alice")
    result = memory_get(config, "memory-bank/activeContext.md")

    assert result["ok"] is True
    assert result["path"] == "memory-bank/activeContext/alice.md"
    assert "# Legacy Active" in result["content"]
    assert (multi_user_repo / "memory-bank/activeContext/alice.md").is_file()
    assert (multi_user_repo / "memory-bank/activeContext.md").is_file()


def test_user_scoped_write_requires_known_user(
    multi_user_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config(multi_user_repo)
    _vscode_user_cache.clear()
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.delenv("USER", raising=False)

    result = memory_write(config, "memory-bank/activeContext.md", "# Nobody\n", backup=False)

    assert result["ok"] is False
    # P0-1 (v0.6.0): unified to user_not_configured for both user-scoped
    # writes and any other facade entry; carries setup_hint.
    assert result["error"] == "user_not_configured"
    assert "setup_hint" in result


def test_vscode_user_setting_overrides_environment_for_user_scoped_paths(
    multi_user_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(
        multi_user_repo / ".vscode/settings.json",
        json.dumps({"memory-mcp.userName": "vscode_user"}),
    )
    _set_env_user(monkeypatch, "env_user")
    _vscode_user_cache.clear()
    config = load_config(multi_user_repo)

    result = memory_write(config, "memory-bank/activeContext.md", "# VS Code User\n", backup=False)

    assert result["ok"] is True
    assert result["path"] == "memory-bank/activeContext/vscode_user.md"
    assert (multi_user_repo / "memory-bank/activeContext/vscode_user.md").is_file()
    assert not (multi_user_repo / "memory-bank/activeContext/env_user.md").exists()


def test_shared_append_only_path_downgrades_overwrite_in_multi_user_mode(
    multi_user_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env_user(monkeypatch, "alice")
    config = load_config(multi_user_repo)

    result = memory_write(config, "memory-bank/progress.md", "- alice update\n", mode="overwrite", backup=False)

    assert result["ok"] is True
    assert result["mode"] == "append"
    assert result["original_mode"] == "overwrite"
    assert result["policy_override"] == "append_only"
    text = (multi_user_repo / "memory-bank/progress.md").read_text(encoding="utf-8")
    assert "# Progress" in text
    assert "- baseline" in text
    assert "- alice update" in text
    assert "<!-- written by alice" in text


def test_guard_reports_each_user_scoped_file_separately(
    multi_user_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config(multi_user_repo)
    _set_env_user(monkeypatch, "alice")
    memory_write(config, "memory-bank/activeContext.md", "# Alice\n", backup=False)
    _set_env_user(monkeypatch, "bob")
    memory_write(config, "memory-bank/activeContext.md", "# Bob\n", backup=False)

    result = memory_guard_check(config)
    paths = {item["path"] for item in result["targets"]}

    assert result["ok"] is True
    assert "memory-bank/activeContext/alice.md" in paths
    assert "memory-bank/activeContext/bob.md" in paths


def test_read_interface_and_file_writer_use_same_user_scoped_redirection(
    multi_user_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env_user(monkeypatch, "alice")
    config = load_config(multi_user_repo)

    write_result = memory_write(config, "memory-bank/activeContext.md", "# Alice Dispatch\n", backup=False)
    read_result = _dispatch_tool(
        config,
        "memory_read",
        {"operation": "get", "path": "memory-bank/activeContext.md"},
    )

    assert write_result["ok"] is True
    assert read_result["ok"] is True
    assert write_result["path"] == "memory-bank/activeContext/alice.md"
    assert read_result["path"] == "memory-bank/activeContext/alice.md"
    assert "# Alice Dispatch" in read_result["content"]


def test_search_scans_user_scoped_active_context_files(
    multi_user_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config(multi_user_repo)
    _set_env_user(monkeypatch, "alice")
    memory_write(config, "memory-bank/activeContext.md", "# Alice\n- unique_alice_marker\n", backup=False)
    _set_env_user(monkeypatch, "bob")
    memory_write(config, "memory-bank/activeContext.md", "# Bob\n- unique_bob_marker\n", backup=False)

    result = memory_search(config, query="unique_bob_marker", scopes=["memory-bank"])

    assert result["ok"] is True
    assert result["results"]
    assert result["results"][0]["path"] == "memory-bank/activeContext/bob.md"


def test_search_include_paths_can_target_one_user_scoped_file(
    multi_user_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config(multi_user_repo)
    _set_env_user(monkeypatch, "alice")
    memory_write(config, "memory-bank/activeContext.md", "# Alice\n- shared_marker\n", backup=False)
    _set_env_user(monkeypatch, "bob")
    memory_write(config, "memory-bank/activeContext.md", "# Bob\n- shared_marker\n", backup=False)

    result = memory_search(
        config,
        query="shared_marker",
        scopes=["memory-bank"],
        include_paths=["memory-bank/activeContext/alice.md"],
    )

    assert result["ok"] is True
    assert result["stats"]["matched_files"] == 1
    assert result["results"][0]["path"] == "memory-bank/activeContext/alice.md"


def test_user_scoped_active_context_auto_archives_and_compacts(
    multi_user_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env_user(monkeypatch, "alice")
    config = load_config(multi_user_repo)
    bulky = "\n".join(
        [
            "# Active",
            "## Current sprint",
            "- alpha focus",
            "## Blockers",
            "- alpha blocker",
            "## Recent decisions",
            "- alpha decision",
            "## This week priorities",
            "- alpha priority",
            "## Long history",
            *[f"- historical detail {idx} {'x' * 80}" for idx in range(160)],
            "",
        ]
    )

    result = memory_write(config, "memory-bank/activeContext.md", bulky, backup=False)

    assert result["ok"] is True
    auto = result.get("active_context_auto_compaction")
    assert auto and auto["ok"] is True
    assert auto["action"] == "archived_and_compacted"
    assert auto["before"]["chars"] > auto["after"]["chars"]
    assert result["after"]["chars"] == auto["after"]["chars"]

    live = (multi_user_repo / "memory-bank/activeContext/alice.md").read_text(encoding="utf-8")
    archive_path = multi_user_repo / auto["archive_path"]
    archive_text = archive_path.read_text(encoding="utf-8")

    assert "# Warm Context Compact" in live
    assert "alpha focus" in live
    assert "historical detail 159" in archive_text
    assert "archived-by: memory-mcp active-context auto-archive" in archive_text


def test_user_scoped_active_context_auto_archive_can_be_disabled(
    multi_user_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_path = multi_user_repo / ".ai-memory/config.json"
    payload = json.loads(cfg_path.read_text(encoding="utf-8"))
    payload.setdefault("key_documents", {})["active_context_auto_archive"] = {"enabled": False}
    cfg_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _set_env_user(monkeypatch, "alice")
    config = load_config(multi_user_repo)

    result = memory_write(config, "memory-bank/activeContext.md", "# Active\n" + ("x" * 9000), backup=False)

    assert result["ok"] is True
    assert result.get("active_context_auto_compaction") is None
    assert result["guard_warning"] is not None
    assert not (multi_user_repo / "memory-bank/archive/activeContext/alice").exists()


def test_user_scoped_active_context_auto_archive_only_current_user(
    multi_user_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config(multi_user_repo)
    _set_env_user(monkeypatch, "bob")
    bob = memory_write(config, "memory-bank/activeContext.md", "# Bob\n- small\n", backup=False)
    _set_env_user(monkeypatch, "alice")
    alice = memory_write(config, "memory-bank/activeContext.md", "# Alice\n" + ("x\n" * 9000), backup=False)

    assert bob["ok"] is True
    assert alice["ok"] is True
    assert alice.get("active_context_auto_compaction", {}).get("ok") is True
    bob_text = (multi_user_repo / "memory-bank/activeContext/bob.md").read_text(encoding="utf-8")
    assert "# Bob" in bob_text
    assert "Warm Context Compact" not in bob_text
    assert (multi_user_repo / "memory-bank/archive/activeContext/alice").is_dir()
    assert not (multi_user_repo / "memory-bank/archive/activeContext/bob").exists()


def test_user_scoped_migration_includes_attribution_banner(
    multi_user_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the legacy single file is auto-migrated to a per-user file, the
    new file must carry a banner stating that authorship is unverified."""
    _vscode_user_cache.clear()
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.setenv("USERNAME", "alice")
    config = load_config(multi_user_repo)

    # First read by alice triggers the migration.
    memory_get(config, "memory-bank/activeContext.md")

    migrated = (multi_user_repo / "memory-bank/activeContext/alice.md").read_text(encoding="utf-8")
    assert "migrated-from-shared" in migrated
    assert "alice" in migrated
    # The original legacy content must still be present after the banner.
    assert "shared seed" in migrated
