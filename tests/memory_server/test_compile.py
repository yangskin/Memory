from __future__ import annotations

from pathlib import Path

from servers.memory_server.memory_compiler import memory_compile, memory_get_runtime_digest
from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_records import memory_write_record
from servers.memory_server.server import _dispatch_tool, _build_tools


def test_compile_runtime_digest_filters_records_and_writes_deterministic_view(repo: Path) -> None:
    config = load_config(repo)
    included = memory_write_record(
        config,
        content_markdown="# Runtime Rule\n\nUse deterministic compile output.\n",
        record_kind="note",
        scope="personal",
        status="validated",
        author="alice",
        tags=["mcp", "high_value"],
        task_id="task_compile",
        branch="feature/memory-compile",
    )
    memory_write_record(
        config,
        content_markdown="# Draft Candidate\n\nDo not include candidate records by default.\n",
        record_kind="rule_candidate",
        scope="personal",
        author="alice",
        tags=["mcp"],
        task_id="task_compile",
        branch="feature/memory-compile",
    )
    memory_write_record(
        config,
        content_markdown="# Other User\n\nShould not be in Alice personal digest.\n",
        record_kind="note",
        scope="personal",
        status="validated",
        author="bob",
        tags=["mcp"],
        task_id="task_compile",
        branch="feature/memory-compile",
    )

    first = memory_compile(
        config,
        target="runtime_digest",
        user="alice",
        task_id="task_compile",
        branch="feature/memory-compile",
        preferred_tags=["mcp"],
    )
    second = memory_compile(
        config,
        target="runtime_digest",
        user="alice",
        task_id="task_compile",
        branch="feature/memory-compile",
        preferred_tags=["mcp"],
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert first["path"] == "memory-bank/compiled/runtime/task/task_compile.md"
    assert first["included_record_ids"] == [included["id"]]
    assert first["content"] == second["content"]

    compiled_text = (repo / first["path"]).read_text(encoding="utf-8")
    assert compiled_text == first["content"]
    assert "# Runtime Digest" in compiled_text
    assert "Runtime Rule" in compiled_text
    assert "Draft Candidate" not in compiled_text
    assert "Other User" not in compiled_text


def test_compile_runtime_digest_can_include_shared_published_records(repo: Path) -> None:
    config = load_config(repo)
    shared = memory_write_record(
        config,
        content_markdown="# Shared Rule\n\nPublished system memory should be visible.\n",
        record_kind="system_rule",
        scope="shared",
        author="lead",
        tags=["mcp"],
        task_id="task_compile",
    )

    result = memory_compile(
        config,
        target="runtime_digest",
        user="alice",
        task_id="task_compile",
        include_scopes=["shared"],
        include_statuses=["published"],
    )

    assert result["ok"] is True
    assert result["included_record_ids"] == [shared["id"]]
    assert "Published system memory" in result["content"]


def test_compile_defaults_to_compact_body_mode(repo: Path) -> None:
    config = load_config(repo)
    written = memory_write_record(
        config,
        content_markdown=(
            "# Compact Record\n\n"
            "Intro paragraph should be ignored when a higher value section exists.\n\n"
            "## Decision\n\n"
            "Keep only the key decision for runtime context.\n\n"
            "## Long Note\n\n"
            "This verbose implementation detail should remain in the source record but not the compact digest. "
            "It repeats a lot of low-value text that is useful for traceability but wasteful in prompts.\n"
        ),
        record_kind="note",
        scope="personal",
        status="validated",
        author="alice",
        tags=["mcp", "high_value"],
        task_id="task_compile",
    )

    compact = memory_compile(config, target="runtime_digest", user="alice", task_id="task_compile")
    full = memory_compile(config, target="runtime_digest", user="alice", task_id="task_compile", body_mode="full")

    assert compact["ok"] is True
    assert compact["body_mode"] == "compact"
    assert full["ok"] is True
    assert full["body_mode"] == "full"
    assert len(compact["content"]) < len(full["content"])
    assert f"- id: `{written['id']}`" in compact["content"]
    assert f"- source: `{written['path']}`" in compact["content"]
    assert "- status: `validated`" in compact["content"]
    assert "Keep only the key decision" in compact["content"]
    assert "verbose implementation detail" not in compact["content"]
    assert "- tags:" not in compact["content"]
    assert "- author:" not in compact["content"]
    assert "- tags: `mcp, high_value`" in full["content"]
    assert "verbose implementation detail" in full["content"]


def test_get_runtime_digest_reads_existing_compiled_output(repo: Path) -> None:
    config = load_config(repo)
    memory_write_record(
        config,
        content_markdown="# Runtime Note\n\nRead me through runtime digest getter.\n",
        record_kind="note",
        scope="personal",
        status="validated",
        author="alice",
        tags=["mcp"],
        task_id="task_compile",
    )
    compiled = memory_compile(config, target="runtime_digest", user="alice", task_id="task_compile")

    result = memory_get_runtime_digest(config, user="alice", task_id="task_compile")

    assert result["ok"] is True
    assert result["path"] == compiled["path"]
    assert result["content"] == compiled["content"]


def test_compile_task_handoff_writes_task_handoff_view(repo: Path) -> None:
    config = load_config(repo)
    written = memory_write_record(
        config,
        content_markdown="# Handoff Ready\n\nNext action is to validate publish flow.\n",
        record_kind="handoff",
        scope="personal",
        status="validated",
        author="alice",
        tags=["handoff_ready", "mcp"],
        task_id="task_compile",
    )

    result = memory_compile(config, target="task_handoff", user="alice", task_id="task_compile")

    assert result["ok"] is True
    assert result["path"] == "memory-bank/compiled/runtime/task/task_compile-handoff.md"
    assert result["included_record_ids"] == [written["id"]]
    assert "# Task Handoff" in result["content"]
    assert "Next action is to validate publish flow." in result["content"]


def test_compile_rejects_unknown_target(repo: Path) -> None:
    config = load_config(repo)

    result = memory_compile(config, target="weekly_digest")

    assert result["ok"] is False
    assert result["error"] == "invalid_input"
    assert "target" in result["message"]


def test_compile_rejects_unknown_body_mode(repo: Path) -> None:
    config = load_config(repo)

    result = memory_compile(config, target="runtime_digest", body_mode="summary")

    assert result["ok"] is False
    assert result["error"] == "invalid_input"
    assert "body_mode" in result["message"]


def test_dispatch_compile_and_get_runtime_digest(repo: Path) -> None:
    config = load_config(repo)
    memory_write_record(
        config,
        content_markdown="# Dispatch Compile\n\nDispatch should expose compiler tools.\n",
        record_kind="note",
        scope="personal",
        status="validated",
        author="alice",
        tags=["mcp"],
        task_id="task_compile",
    )

    compiled = memory_compile(config, target="runtime_digest", user="alice", task_id="task_compile")
    fetched = memory_get_runtime_digest(config, user="alice", task_id="task_compile")

    assert compiled["ok"] is True
    assert fetched["ok"] is True
    assert fetched["content"] == compiled["content"]


def test_build_tools_keeps_compile_cli_only(repo: Path) -> None:
    config = load_config(repo)
    tool_names = {tool.name for tool in _build_tools(config)}

    assert tool_names == {"memory_read", "memory_write", "memory_board_read", "memory_board_write"}
    assert "memory_compile" not in tool_names
    assert "memory_get_runtime_digest" not in tool_names
