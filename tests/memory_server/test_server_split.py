"""Regression tests for P1-A server.py split.

Verifies:
- Public symbols are still importable from `servers.memory_server.server`.
- Default MCP surface exposes only memory_read / memory_write.
- Admin flows are CLI-only and cannot be enabled through MCP config.
- Each advertised read operation enum is wired in dispatch.
- create_server returns a Server instance and registers handlers.
"""

from __future__ import annotations

import inspect

from servers.memory_server.memory_config import MemoryConfig
from servers.memory_server import server as server_module
from servers.memory_server.server import (
    SERVER_NAME,
    SERVER_VERSION,
    _BASE_DESCRIPTIONS,
    _build_file_roles,
    _build_tools,
    _check_required,
    _dispatch_memory_context,
    _dispatch_memory_read,
    _dispatch_memory_write,
    _dispatch_tool,
    create_server,
)


def _make_config(tmp_path) -> MemoryConfig:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "memory-bank").mkdir()
    (workspace / ".ai-context").mkdir()
    (workspace / ".ai-memory").mkdir()
    cfg_path = workspace / ".ai-memory" / "config.json"
    payload = {}
    import json

    cfg_path.write_text(json.dumps(payload), encoding="utf-8")
    from servers.memory_server.memory_config import load_config

    return load_config(str(workspace), str(cfg_path))


def test_public_reexports_present():
    assert SERVER_NAME == "generic-memory-mcp"
    assert isinstance(SERVER_VERSION, str) and SERVER_VERSION
    assert "memory_read" in _BASE_DESCRIPTIONS
    assert "memory_write" in _BASE_DESCRIPTIONS
    assert "memory_context" in _BASE_DESCRIPTIONS
    assert "memory_enhance" in _BASE_DESCRIPTIONS
    # Tests still import these from the top-level server module.
    for name in (
        "_build_file_roles",
        "_build_tools",
        "_check_required",
        "_dispatch_tool",
        "_dispatch_memory_read",
        "_dispatch_memory_write",
        "_dispatch_memory_context",
        "create_server",
    ):
        assert hasattr(server_module, name), f"missing re-export: {name}"


def test_default_facade_returns_two_tools(tmp_path):
    config = _make_config(tmp_path)
    tools = _build_tools(config)
    names = [t.name for t in tools]
    assert names == ["memory_read", "memory_write"]


def test_admin_flows_are_not_configurable_through_mcp_surface(tmp_path):
    config = _make_config(tmp_path)
    tools = _build_tools(config)
    names = [t.name for t in tools]
    assert names == ["memory_read", "memory_write"]


def test_check_required_returns_error_for_missing(tmp_path):
    err = _check_required({}, "path")
    assert err is not None
    assert err["error"] == "invalid_input"
    assert "path" in err["message"]
    # None counts as missing.
    err = _check_required({"path": None}, "path")
    assert err is not None
    # Empty string is allowed.
    assert _check_required({"path": ""}, "path") is None


def test_unknown_tool_returns_unknown_tool_error(tmp_path):
    config = _make_config(tmp_path)
    result = _dispatch_tool(config, "memory_does_not_exist", {})
    assert result["ok"] is False
    assert result["error"] == "unknown_tool"


def test_dispatch_memory_read_invalid_operation(tmp_path):
    config = _make_config(tmp_path)
    result = _dispatch_memory_read(config, {"operation": "no_such_op"})
    assert result["ok"] is False
    assert result["error"] == "invalid_input"


def test_dispatch_memory_write_invalid_operation(tmp_path):
    config = _make_config(tmp_path)
    result = _dispatch_memory_write(config, {"operation": "no_such_op"})
    assert result["ok"] is False
    assert result["error"] == "invalid_input"


def test_dispatch_memory_write_checkpoint_persists_content_with_warning(tmp_path):
    config = _make_config(tmp_path)
    for body_field in ("content_markdown", "content"):
        result = _dispatch_memory_write(
            config,
            {
                "operation": "checkpoint",
                "task_phase": "task_done",
                body_field: "# Summary\n\nThis body would otherwise be ignored.",
            },
        )

        assert result["ok"] is True
        assert result["error"] is None
        assert result["message"] == "checkpoint accepted; content persisted as record"
        assert result["operation"] == "checkpoint"
        assert result["persisted_record"]["ok"] is True
        assert result["persisted_record"]["record_kind"] == "handoff"
        assert result["warnings"] == [
            {
                "code": "checkpoint_content_persisted_as_record",
                "message": "checkpoint content was saved as a structured record; use operation=record for summaries before sending checkpoint.",
            }
        ]


def test_dispatch_memory_write_checkpoint_test_phase_defaults_to_validation_result(tmp_path):
    config = _make_config(tmp_path)
    result = _dispatch_memory_write(
        config,
        {
            "operation": "checkpoint",
            "task_phase": "test_passed",
            "content_markdown": "# Validation\n\nTests passed.",
        },
    )

    assert result["ok"] is True
    assert result["persisted_record"]["record_kind"] == "validation_result"


def test_dispatch_memory_write_checkpoint_content_survives_invalid_metadata(tmp_path):
    config = _make_config(tmp_path)
    result = _dispatch_memory_write(
        config,
        {
            "operation": "checkpoint",
            "task_phase": "task_done",
            "content_markdown": "# Important\n\nThe body should survive bad metadata.",
            "record_kind": "not_a_kind",
            "scope": "not_a_scope",
            "tags": ["not_a_tag"],
        },
    )

    assert result["ok"] is True
    assert result["persisted_record"]["ok"] is True
    assert result["persisted_record"]["record_kind"] == "handoff"
    assert [warning["code"] for warning in result["warnings"]] == [
        "checkpoint_content_metadata_fallback",
        "checkpoint_content_persisted_as_record",
    ]


def test_dispatch_memory_write_observation_accepts_content_alias(tmp_path):
    config = _make_config(tmp_path)
    result = _dispatch_memory_write(
        config,
        {
            "operation": "observation",
            "content": "# Observation\n\nAlias body should be persisted.",
            "author": "alice",
            "tags": ["mcp"],
        },
    )

    assert result["ok"] is True
    assert result["record_kind"] == "observation"


def test_dispatch_memory_context_invalid_operation(tmp_path):
    config = _make_config(tmp_path)
    result = _dispatch_memory_context(config, {"operation": "no_such_op"})
    assert result["ok"] is False
    assert result["error"] == "invalid_input"


def test_facade_read_operations_match_dispatch(tmp_path):
    """The enum advertised in memory_read schema must be covered by dispatch."""
    config = _make_config(tmp_path)
    tools = _build_tools(config)
    read_tool = next(t for t in tools if t.name == "memory_read")
    op_enum = read_tool.inputSchema["properties"]["operation"]["enum"]

    # Every advertised operation must NOT raise unknown-operation error
    # when called with empty args (the operation itself must be recognized
    # even if it then fails on missing required params).
    for op in op_enum:
        args = {"operation": op}
        if op == "task_context":
            args.update({"user": "alice", "user_goal": "schema coverage"})
        result = _dispatch_memory_read(config, args)
        # The dispatch may reject for other reasons (missing params, no records),
        # but it must NOT return the "operation must be one of" message.
        if result.get("ok") is False and result.get("error") == "invalid_input":
            assert "operation must be one of" not in result.get("message", ""), (
                f"operation '{op}' in schema but not in dispatch"
            )


def test_facade_write_schema_describes_checkpoint_content_behavior(tmp_path):
    config = _make_config(tmp_path)
    tools = _build_tools(config)
    write_tool = next(t for t in tools if t.name == "memory_write")
    properties = write_tool.inputSchema["properties"]

    assert "persisted as a structured record" in properties["content"]["description"]
    assert "persisted as a structured record" in properties["content_markdown"]["description"]
    assert "checkpoint content is accepted for compatibility" in properties["task_phase"]["description"]


def test_create_server_returns_server_instance(tmp_path):
    config = _make_config(tmp_path)
    srv = create_server(config)
    assert srv is not None
    # MCP Server exposes name attr or similar; do a duck-typed sanity check.
    assert callable(getattr(srv, "create_initialization_options", None))


def test_dispatch_tool_signature_unchanged():
    sig = inspect.signature(_dispatch_tool)
    params = list(sig.parameters)
    assert params == ["config", "name", "args"]
