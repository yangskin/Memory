from __future__ import annotations

from pathlib import Path

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_reader import memory_get


def test_path_traversal_rejected(repo: Path) -> None:
    config = load_config(repo)
    result = memory_get(config, "../outside.md")
    assert result["ok"] is False
    assert result["error"] == "path_not_allowed"


def test_memory_get_line_range(repo: Path) -> None:
    config = load_config(repo)
    result = memory_get(config, "memory-bank/notes.md", start_line=2, end_line=3)
    assert result["ok"] is True
    assert result["content"] == "line2\nline3\n"
    assert result["start_line"] == 2
    assert result["end_line"] == 3
