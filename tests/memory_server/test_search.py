from __future__ import annotations

from pathlib import Path

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_search import memory_search


def test_memory_search_basic_hit(repo: Path) -> None:
    config = load_config(repo)
    result = memory_search(config, query="boss", scopes=["memory-bank"], top_k=5)
    assert result["ok"] is True
    assert result["results"]
    assert result["results"][0]["path"] == "memory-bank/notes.md"
