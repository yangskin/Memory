from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from servers.memory_server.memory_compiler import memory_compile, memory_get_runtime_digest
from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_governance import (
    memory_archive_record,
    memory_publish_candidate,
    memory_validate_candidate,
)
from servers.memory_server.memory_records import memory_write_record
from servers.memory_server.server import _dispatch_tool


def test_governance_tools_return_not_found_for_missing_record(repo: Path) -> None:
    config = load_config(repo)

    validate = memory_validate_candidate(config, "mem_missing", validated_by="lead")
    publish = memory_publish_candidate(config, "mem_missing", published_by="owner")
    archive = memory_archive_record(config, "mem_missing", reason="missing")

    assert validate["error"] == "not_found"
    assert publish["error"] == "not_found"
    assert archive["error"] == "not_found"


def test_compile_rejects_non_list_filters(repo: Path) -> None:
    config = load_config(repo)

    scopes = memory_compile(
        config,
        target="runtime_digest",
        include_scopes="shared",  # type: ignore[arg-type]
    )
    statuses = memory_compile(
        config,
        target="runtime_digest",
        include_statuses=["validated", 42],  # type: ignore[list-item]
    )
    tags = memory_compile(
        config,
        target="runtime_digest",
        preferred_tags={"mcp": True},  # type: ignore[arg-type]
    )

    assert scopes["ok"] is False
    assert scopes["error"] == "invalid_input"
    assert statuses["ok"] is False
    assert statuses["error"] == "invalid_input"
    assert tags["ok"] is False
    assert tags["error"] == "invalid_input"


def test_get_runtime_digest_negative_max_chars_rejected(repo: Path) -> None:
    config = load_config(repo)

    result = memory_get_runtime_digest(config, max_chars=-1)

    assert result["ok"] is False
    assert result["error"] == "invalid_input"


def test_write_record_uses_configured_tag_schema_without_leaking(repo: Path) -> None:
    custom_config_path = repo / ".ai-memory/custom-tags.json"
    base_data = json.loads((repo / ".ai-memory/config.json").read_text(encoding="utf-8"))
    base_data["tag_schema"] = {"allowed_tags": ["custom_tag"], "version": "custom-v1"}
    custom_config_path.write_text(json.dumps(base_data, ensure_ascii=False, indent=2), encoding="utf-8")
    custom_config = load_config(repo, custom_config_path)
    default_config = replace(load_config(repo), tag_allowed_tags=["mcp"], tag_schema_version="v1")

    custom = memory_write_record(
        custom_config,
        content_markdown="# Custom Tag\n\nThis tag is allowed only by the custom schema.\n",
        tags=["custom_tag"],
    )
    default = memory_write_record(
        default_config,
        content_markdown="# Custom Tag\n\nThis should still be rejected by the default schema.\n",
        tags=["custom_tag"],
    )

    assert custom["ok"] is True
    assert default["ok"] is False
    assert default["error"] == "invalid_input"


def test_get_runtime_digest_truncates_existing_digest(repo: Path) -> None:
    config = load_config(repo)
    memory_write_record(
        config,
        content_markdown="# Long Digest Record\n\n" + ("x" * 200),
        record_kind="note",
        scope="personal",
        status="validated",
        author="alice",
        tags=["mcp"],
        task_id="task_edges",
    )
    memory_compile(config, target="runtime_digest", user="alice", task_id="task_edges")

    result = memory_get_runtime_digest(config, user="alice", task_id="task_edges", max_chars=40)

    assert result["ok"] is True
    assert result["truncated"] is True
    assert len(result["content"]) == 40


def test_dispatch_compile_tool_is_cli_only(repo: Path) -> None:
    config = load_config(repo)

    result = _dispatch_tool(config, "memory_compile", {})

    assert result["ok"] is False
    assert result["error"] == "unknown_tool"


def test_dispatch_update_index_tool_is_cli_only(repo: Path) -> None:
    config = load_config(repo)

    result = _dispatch_tool(config, "memory_update_index", {"paths": "memory-bank/notes.md"})

    assert result["ok"] is False
    assert result["error"] == "unknown_tool"
