from __future__ import annotations

from pathlib import Path

from servers.memory_server.memory_backup import _list_backup_files
from servers.memory_server.memory_compactor import compact_memory
from servers.memory_server.memory_config import load_config


def test_memory_compact_dry_run(repo: Path) -> None:
    config = load_config(repo)
    result = compact_memory(config, path=".ai-context/current-task.md", policy="hot_task", dry_run=True)
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert "candidate_content" in result
    compacted = result["candidate_content"]
    assert "- attempt 5" in compacted
    assert "- attempt 1" not in compacted
    assert "- attempt 2" not in compacted


def test_memory_compact_apply_backup_atomic_log(repo: Path) -> None:
    config = load_config(repo)
    result = compact_memory(config, path=".ai-context/latest-error.md", policy="error_summary", dry_run=False)
    assert result["ok"] is True
    assert result["applied"] is True

    rewritten = (repo / ".ai-context/latest-error.md").read_text(encoding="utf-8")
    assert rewritten.startswith("# Latest Error Summary")

    backups = _list_backup_files(repo / ".ai-memory/backups")
    assert backups, "backup file should exist for compact apply"
    content = backups[0].read_text(encoding="utf-8")
    assert "latest-error.md" in content

    events = (repo / ".ai-memory/events.jsonl").read_text(encoding="utf-8")
    assert "memory_compact" in events

    temp_files = [item for item in (repo / ".ai-memory/temp").iterdir() if item.is_file()]
    assert not temp_files
