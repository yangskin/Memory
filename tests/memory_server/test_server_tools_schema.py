from __future__ import annotations

import json
from pathlib import Path

from servers.memory_server.memory_config import load_config
from servers.memory_server.server_tools import _build_tools


def _make_config(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "memory-bank").mkdir()
    (workspace / ".ai-context").mkdir()
    config_dir = workspace / ".ai-memory"
    config_dir.mkdir()
    config_path = config_dir / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "tag_schema": {
                    "allowed_tags": ["custom_tag", "mcp"],
                    "version": "custom-v1",
                }
            }
        ),
        encoding="utf-8",
    )
    return load_config(str(workspace), str(config_path))


def _tool_schema(config, name: str) -> dict:
    tools = {tool.name: tool for tool in _build_tools(config)}
    return tools[name].inputSchema


def test_facade_schema_exposes_llm_metadata_opt_ins(tmp_path: Path) -> None:
    config = _make_config(tmp_path)

    read_schema = _tool_schema(config, "memory_read")
    write_schema = _tool_schema(config, "memory_write")

    assert read_schema["properties"]["llm_suggest_metadata"]["type"] == "boolean"
    assert write_schema["properties"]["llm_normalize_tags"]["type"] == "boolean"


def test_memory_write_tags_schema_uses_configured_controlled_vocabulary(tmp_path: Path) -> None:
    config = _make_config(tmp_path)

    write_schema = _tool_schema(config, "memory_write")
    tags_schema = write_schema["properties"]["tags"]

    assert tags_schema["items"]["enum"] == ["custom_tag", "mcp"]
    assert "Omit tags when unsure" in tags_schema["description"]
