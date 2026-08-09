"""End-to-end MCP-protocol tests for the memory server.

Exercises the *real* `mcp.server.Server` instance produced by `create_server`
by dispatching `ListToolsRequest` and `CallToolRequest` through the SDK's
registered request-handler chain. This validates:

- `create_server(config)` wires up `list_tools` and `call_tool` correctly.
- Default tools (`memory_read`, `memory_write`) are reachable
  via the actual MCP request envelope.
- Tool responses are JSON-serialised into a single `TextContent` block whose
  body parses back to the same dict the dispatch layer produced.
- Admin/sync flows are CLI-only and are never advertised by MCP.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from mcp.types import (
    CallToolRequest,
    CallToolRequestParams,
    ListToolsRequest,
)

from servers.memory_server.memory_config import load_config
from servers.memory_server.server import create_server


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _list_tools(server) -> list[Any]:
    handler = server.request_handlers[ListToolsRequest]
    req = ListToolsRequest(method="tools/list")
    server_result = await handler(req)
    # ServerResult wraps the actual ListToolsResult under .root
    return server_result.root.tools


async def _call_tool(server, name: str, arguments: dict) -> dict:
    handler = server.request_handlers[CallToolRequest]
    req = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=name, arguments=arguments),
    )
    server_result = await handler(req)
    payload = server_result.root  # CallToolResult
    assert payload.content, "MCP response missing content"
    text_block = payload.content[0]
    assert getattr(text_block, "type", None) == "text"
    return json.loads(text_block.text)


def test_mcp_list_tools_default_facade(repo: Path) -> None:
    """Default config exposes general memory and dedicated Board tools."""
    config = load_config(repo)
    server = create_server(config)
    tools = _run(_list_tools(server))
    names = sorted(t.name for t in tools)
    assert names == ["memory_board_read", "memory_board_write", "memory_read", "memory_task_sync", "memory_write"]


def test_mcp_list_tools_excludes_admin_flows(repo: Path) -> None:
    """Admin flows are CLI-only and never appear in MCP tools/list."""
    config = load_config(repo)
    server = create_server(config)
    tools = _run(_list_tools(server))
    names = [t.name for t in tools]
    assert names == ["memory_read", "memory_write", "memory_board_read", "memory_board_write", "memory_task_sync"]


def test_mcp_call_memory_read_get(repo: Path) -> None:
    """memory_read{operation=get} round-trip via MCP envelope returns file content."""
    config = load_config(repo)
    server = create_server(config)
    result = _run(_call_tool(server, "memory_read", {
        "operation": "get",
        "path": "memory-bank/notes.md",
    }))
    assert result["ok"] is True, result
    assert "Boss Notes" in result["content"]


def test_mcp_call_memory_write_record(repo: Path) -> None:
    """memory_write via MCP writes structured memory, not files."""
    config = load_config(repo)
    server = create_server(config)
    result = _run(_call_tool(server, "memory_write", {
        "content_markdown": "# MCP Record\n\nTwo-tool write path.\n",
        "record_kind": "note",
        "scope": "personal",
        "author": "alice",
    }))
    assert result["ok"] is True, result
    assert result["record_kind"] == "note"


def test_mcp_call_dedicated_board_tools(repo: Path) -> None:
    config = load_config(repo)
    server = create_server(config)
    posted = _run(_call_tool(server, "memory_board_write", {
        "action": "post",
        "post_type": "request",
        "content": "MCP protocol board test",
        "task_id": "mcp-board",
    }))
    assert posted["ok"] is True, posted

    queried = _run(_call_tool(server, "memory_board_read", {
        "filter": "unresolved",
        "task_id": "mcp-board",
    }))
    assert queried["ok"] is True, queried
    assert any(item["post_id"] == posted["post"]["post_id"] for item in queried["items"])


def test_mcp_call_memory_task_sync(repo: Path) -> None:
    config = load_config(repo)
    server = create_server(config)
    created = _run(_call_tool(server, "memory_task_sync", {
        "action": "create",
        "command_id": "mcp-task-create",
        "expected_version": 0,
        "task_id": "mcp-task",
        "actor_id": "agent:pytest",
        "title": "MCP task sync",
    }))
    bundle = _run(_call_tool(server, "memory_task_sync", {
        "action": "sync",
        "task_id": "mcp-task",
    }))

    assert created["ok"] is True, created
    assert bundle["ok"] is True, bundle
    assert bundle["bundle"]["nodes"][0]["id"] == "task:mcp-task"


def test_mcp_call_memory_read_task_context(repo: Path) -> None:
    """memory_read{operation=task_context} is the MCP task bootstrap path."""
    config = load_config(repo)
    server = create_server(config)
    result = _run(_call_tool(server, "memory_read", {
        "operation": "task_context",
        "user": "alice",
        "agent_id": "pytest",
        "user_goal": "verify two-tool task context",
    }))
    assert result["ok"] is True, result
    assert result["context_token"].startswith("ctx_")
    assert result["active_context"]["ok"] is True


def test_mcp_call_unknown_tool_returns_error(repo: Path) -> None:
    """Unknown tool name yields an `unknown_tool` error envelope, not an exception."""
    config = load_config(repo)
    server = create_server(config)
    result = _run(_call_tool(server, "no_such_tool", {}))
    assert result["ok"] is False
    assert result["error"] == "unknown_tool"
    assert "unknown tool" in result["message"].lower()


def test_mcp_call_legacy_tool_is_rejected(repo: Path) -> None:
    """Legacy MCP tools are rejected because they are outside the public agent surface."""
    config = load_config(repo)
    server = create_server(config)
    result = _run(_call_tool(server, "memory_get", {"path": "memory-bank/notes.md"}))
    assert result["ok"] is False
    assert result["error"] == "unknown_tool"
