from __future__ import annotations

from pathlib import Path

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_guard import memory_guard_check


def test_memory_guard_check_exceeded(repo: Path) -> None:
    config = load_config(repo)
    result = memory_guard_check(config)
    assert result["ok"] is True
    long_target = next(item for item in result["targets"] if item["path"].endswith("memory-bank/long.md"))
    assert long_target["status"] == "exceeded"
