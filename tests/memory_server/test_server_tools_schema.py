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


def test_facade_schema_exposes_board_operation(tmp_path: Path) -> None:
    config = _make_config(tmp_path)

    read_schema = _tool_schema(config, "memory_read")
    write_schema = _tool_schema(config, "memory_write")

    assert "board" in read_schema["properties"]["operation"]["enum"]
    assert "board" in write_schema["properties"]["operation"]["enum"]
    assert read_schema["properties"]["action"]["enum"] == ["query", "post", "reply", "resolve"]
    assert write_schema["properties"]["action"]["enum"] == ["query", "post", "reply", "resolve"]


def test_dedicated_board_tools_are_discoverable_with_narrow_schemas(tmp_path: Path) -> None:
    tools = {tool.name: tool for tool in _build_tools(_make_config(tmp_path))}

    assert {"memory_board_read", "memory_board_write"} <= set(tools)
    read_schema = tools["memory_board_read"].inputSchema
    write_schema = tools["memory_board_write"].inputSchema
    assert "operation" not in read_schema["properties"]
    assert read_schema["properties"]["filter"]["default"] == "unresolved"
    assert write_schema["properties"]["action"]["enum"] == ["post", "reply", "resolve"]
    assert read_schema["additionalProperties"] is False
    assert write_schema["additionalProperties"] is False


def test_facade_schema_exposes_project_graph_operation(tmp_path: Path) -> None:
    read_schema = _tool_schema(_make_config(tmp_path), "memory_read")
    assert "project_graph" in read_schema["properties"]["operation"]["enum"]
    assert read_schema["properties"]["depth"] == {"type": "integer", "minimum": 0, "maximum": 2, "default": 1}
    assert read_schema["properties"]["max_nodes"] == {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}
    assert read_schema["properties"]["max_edges"] == {"type": "integer", "minimum": 1, "maximum": 400, "default": 100}
    assert read_schema["properties"]["max_chars"]["maximum"] == 32000
    assert read_schema["properties"]["max_tokens"]["maximum"] == 8000
    assert read_schema["properties"]["max_items"]["maximum"] == 50
