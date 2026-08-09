from __future__ import annotations

import json
from pathlib import Path

from servers.memory_server.memory_compiler import get_record_last_used_at, memory_compile
from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_maintenance import (
    memory_delete_record,
    memory_health_check,
    memory_migrate_records,
)
from servers.memory_server.memory_record_index import memory_rebuild_index, memory_search_records, memory_update_index
from servers.memory_server.memory_records import memory_write_record, parse_record_markdown
from servers.memory_server.server import _build_tools, _dispatch_tool


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_compile_updates_last_used_at_and_writes_cache_manifest(repo: Path) -> None:
    config = load_config(repo)
    record = memory_write_record(
        config,
        content_markdown="# Runtime Used\n\nCompiling should mark this as used.\n",
        record_kind="note",
        scope="personal",
        status="validated",
        author="alice",
        tags=["mcp"],
        task_id="task_runtime",
    )

    result = memory_compile(config, target="runtime_digest", user="alice", task_id="task_runtime")

    record_path = repo / record["path"]
    metadata, _body = parse_record_markdown(record_path.read_text(encoding="utf-8"))
    record_mtime_before = record_path.stat().st_mtime
    assert result["ok"] is True
    # Compiler must NOT mutate source records: last_used_at lives in usage-stats.json now.
    assert metadata.get("last_used_at") is None
    assert get_record_last_used_at(config, record["id"]) is not None
    # Re-compile should not touch source mtime either.
    memory_compile(config, target="runtime_digest", user="alice", task_id="task_runtime")
    assert record_path.stat().st_mtime == record_mtime_before
    cache_path = repo / ".ai-memory/compile-cache/runtime_digest-task_runtime.json"
    assert cache_path.is_file()
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cache["included_record_ids"] == [record["id"]]


def test_runtime_digest_includes_legacy_memory_section(repo: Path) -> None:
    config = load_config(repo)
    result = memory_compile(config, target="runtime_digest", user="alice", include_scopes=["shared"])

    assert result["ok"] is True
    assert "## Legacy Memory Files" in result["content"]
    assert "memory-bank/activeContext.md" in result["content"]
    assert "Boss Notes" not in result["content"]


def test_health_check_reports_invalid_record_and_stale_index(repo: Path) -> None:
    _write(repo / "memory-bank/candidates/bad.md", "---\nid: bad\nstatus: candidate\n---\n# Missing kind\n")
    config = load_config(repo)

    result = memory_health_check(config)

    assert result["ok"] is True
    assert result["status"] == "warn"
    assert any(issue["code"] == "missing_required_metadata" for issue in result["issues"])
    assert any(issue["code"] == "missing_search_db" for issue in result["issues"])


def test_health_check_reports_invalid_front_matter(repo: Path) -> None:
    _write(repo / "memory-bank/candidates/broken.md", "---\nid: broken\n# no closing front matter\n")
    config = load_config(repo)

    result = memory_health_check(config)

    assert result["ok"] is True
    assert any(issue["code"] == "invalid_record_format" for issue in result["issues"])


def test_migrate_records_updates_schema_version(repo: Path) -> None:
    config = load_config(repo)
    record = memory_write_record(
        config,
        content_markdown="# Old Schema\n\nNeeds migration.\n",
        record_kind="note",
        scope="personal",
        status="validated",
        author="alice",
        tags=["mcp"],
    )
    path = repo / record["path"]
    text = path.read_text(encoding="utf-8").replace('schema_version: "1.0"', 'schema_version: "0.9"')
    path.write_text(text, encoding="utf-8")

    result = memory_migrate_records(config, target_schema_version="1.0")

    metadata, _body = parse_record_markdown(path.read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert result["migrated_records"] == 1
    assert metadata["schema_version"] == "1.0"
    assert metadata["schema_migrated_from"] == "0.9"


def test_update_index_indexes_single_new_record_without_full_rebuild(repo: Path) -> None:
    config = load_config(repo)
    memory_rebuild_index(config)
    record = memory_write_record(
        config,
        content_markdown="# Incremental Index\n\nSingle record should be indexed.\n",
        record_kind="note",
        scope="personal",
        status="validated",
        author="alice",
        tags=["mcp"],
    )

    result = memory_update_index(config, paths=[record["path"]])
    search = memory_search_records(config, query="Incremental")

    assert result["ok"] is True
    assert result["indexed_records"] == 1
    assert search["results"][0]["id"] == record["id"]


def test_update_index_rejects_invalid_paths_argument(repo: Path) -> None:
    config = load_config(repo)

    result = memory_update_index(config, paths="memory-bank/not-a-list.md")  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["error"] == "invalid_input"


def test_delete_record_requires_archived_status(repo: Path) -> None:
    config = load_config(repo)
    record = memory_write_record(
        config,
        content_markdown="# Keep Me\n\nNon-archived records cannot be deleted.\n",
        record_kind="note",
        scope="personal",
        status="validated",
        author="alice",
        tags=["mcp"],
    )

    result = memory_delete_record(config, record["id"])

    assert result["ok"] is False
    assert result["error"] == "invalid_state"
    assert (repo / record["path"]).is_file()


def test_delete_archived_record_writes_tombstone(repo: Path) -> None:
    config = load_config(repo)
    record = memory_write_record(
        config,
        content_markdown="# Delete Me\n\nArchived records can be tombstoned.\n",
        record_kind="archive_record",
        scope="archive",
        status="archived",
        author="alice",
        tags=["mcp"],
    )

    result = memory_delete_record(config, record["id"], reason="cleanup")

    assert result["ok"] is True
    assert not (repo / record["path"]).exists()
    tombstone = repo / ".ai-memory/tombstones.jsonl"
    assert tombstone.is_file()
    assert record["id"] in tombstone.read_text(encoding="utf-8")


def test_runtime_maintenance_tools_are_internal_cli_only(repo: Path) -> None:
    config = load_config(repo)
    record = memory_write_record(
        config,
        content_markdown="# Dispatch Maintenance\n\nMaintenance tools are CLI/internal only.\n",
        record_kind="archive_record",
        scope="archive",
        status="archived",
        author="alice",
        tags=["mcp"],
    )

    rejected = _dispatch_tool(config, "memory_health_check", {})
    health = memory_health_check(config)
    migrate = memory_migrate_records(config, target_schema_version="1.0")
    update = memory_update_index(config, paths=[record["path"]])
    delete = memory_delete_record(config, record["id"], reason="dispatch")

    assert rejected["ok"] is False
    assert rejected["error"] == "unknown_tool"
    assert health["ok"] is True
    assert migrate["ok"] is True
    assert update["ok"] is True
    assert delete["ok"] is True


def test_build_tools_excludes_runtime_maintenance_tools_from_mcp(repo: Path) -> None:
    config = load_config(repo)
    tool_names = {tool.name for tool in _build_tools(config)}

    assert tool_names == {"memory_read", "memory_write", "memory_board_read", "memory_board_write", "memory_task_sync"}
    assert "memory_health_check" not in tool_names
    assert "memory_migrate_records" not in tool_names
    assert "memory_update_index" not in tool_names
    assert "memory_delete_record" not in tool_names
