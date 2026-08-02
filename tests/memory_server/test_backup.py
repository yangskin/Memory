from __future__ import annotations

from pathlib import Path

from servers.memory_server.memory_backup import backup_files, _list_backup_files
from servers.memory_server.memory_config import load_config


def test_memory_backup_appends_to_single_file(repo: Path) -> None:
    config = load_config(repo)
    result = backup_files(
        config,
        paths=["memory-bank/notes.md"],
        reason="manual backup test",
        tag="unit",
    )
    assert result["ok"] is True
    assert result["batch_id"]
    assert result["backups"]

    # Should have created exactly one backup file
    backup_files_list = _list_backup_files(config.backups_dir)
    assert len(backup_files_list) == 1
    content = backup_files_list[0].read_text(encoding="utf-8")
    assert "<<<BACKUP|" in content
    assert "<<<END_BACKUP>>>" in content
    assert "Boss Notes" in content  # original content from notes.md

    events = (repo / ".ai-memory/events.jsonl").read_text(encoding="utf-8")
    assert "memory_backup" in events


def test_memory_backup_multiple_records_same_file(repo: Path) -> None:
    config = load_config(repo)
    backup_files(config, paths=["memory-bank/notes.md"], reason="first")
    backup_files(config, paths=["memory-bank/activeContext.md"], reason="second")

    files = _list_backup_files(config.backups_dir)
    assert len(files) == 1  # both should go into the same file
    content = files[0].read_text(encoding="utf-8")
    assert content.count("<<<BACKUP|") == 2
    assert content.count("<<<END_BACKUP>>>") == 2
