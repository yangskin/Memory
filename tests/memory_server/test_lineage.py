from __future__ import annotations

from pathlib import Path

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_lineage import (
    memory_link_artifact,
    memory_list_conflicts,
    memory_record_observation,
    memory_trace_lineage,
)
from servers.memory_server.memory_record_index import memory_rebuild_index, memory_search_records
from servers.memory_server.memory_record_io import find_record_by_id, write_same_record
from servers.memory_server.memory_records import memory_write_record, parse_record_markdown, render_record_markdown
from servers.memory_server.server import _build_tools, _dispatch_tool


def test_record_observation_creates_schema_v2_evidence_record(repo: Path) -> None:
    config = load_config(repo)

    result = memory_record_observation(
        config,
        content_markdown="# Editor Observation\n\nClipboard texture replacement touched the widget path.\n",
        author="alice",
        tags=["mcp"],
        task_id="task_widgets",
        occurred_at="2026-04-23T09:00:00+00:00",
        asset_paths=["/Game/UI/WBP_Test"],
        plugin_names=["AssetCustoms"],
        system_area="memory",
    )

    assert result["ok"] is True
    metadata, body = parse_record_markdown((repo / result["path"]).read_text(encoding="utf-8"))
    assert metadata["schema_version"] == "2.0"
    assert metadata["record_kind"] == "observation"
    assert metadata["scope"] == "session"
    assert metadata["status"] == "raw"
    assert metadata["occurred_at"] == "2026-04-23T09:00:00+00:00"
    assert metadata["memory_tier"] == "hot"
    assert metadata["cognitive_level"] == "shu"
    assert metadata["asset_paths"] == ["/Game/UI/WBP_Test"]
    assert metadata["plugin_names"] == ["AssetCustoms"]
    assert metadata["system_area"] == "memory"
    assert body.startswith("# Editor Observation")


def test_link_artifact_upgrades_record_and_refreshes_index(repo: Path) -> None:
    config = load_config(repo)
    record = memory_write_record(
        config,
        content_markdown="# Existing Note\n\nAttach facets after the fact.\n",
        record_kind="note",
        scope="personal",
        status="validated",
        author="alice",
        tags=["mcp"],
    )
    memory_rebuild_index(config)

    result = memory_link_artifact(
        config,
        record["id"],
        asset_paths=["/Game/Maps/TestMap"],
        module_names=["MemoryServer"],
        system_area="memory",
    )
    search = memory_search_records(config, query="MemoryServer")

    assert result["ok"] is True
    metadata, _body = parse_record_markdown((repo / record["path"]).read_text(encoding="utf-8"))
    assert metadata["schema_version"] == "2.0"
    assert metadata["asset_paths"] == ["/Game/Maps/TestMap"]
    assert metadata["module_names"] == ["MemoryServer"]
    assert metadata["system_area"] == "memory"
    assert search["results"][0]["id"] == record["id"]


def test_link_artifact_rejects_empty_update(repo: Path) -> None:
    config = load_config(repo)
    record = memory_write_record(config, content_markdown="# Empty Link\n\nNo facets.\n", tags=["mcp"])

    result = memory_link_artifact(config, record["id"])

    assert result["ok"] is False
    assert result["error"] == "invalid_input"


def test_trace_lineage_follows_record_edges(repo: Path) -> None:
    config = load_config(repo)
    source = memory_write_record(
        config,
        content_markdown="# Source Evidence\n\nBase evidence.\n",
        record_kind="observation",
        scope="session",
        author="alice",
        tags=["mcp"],
    )
    rule = memory_write_record(
        config,
        content_markdown="# Derived Rule\n\nRule derived from source evidence.\n",
        record_kind="decision",
        scope="project_shared",
        author="alice",
        tags=["mcp"],
        derived_from_record_ids=[source["id"]],
        conflicts_with=["mem_missing"],
    )

    result = memory_trace_lineage(config, rule["id"])

    assert result["ok"] is True
    node_ids = {node["id"] for node in result["nodes"]}
    assert rule["id"] in node_ids
    assert source["id"] in node_ids
    assert {"from": rule["id"], "to": source["id"], "type": "derived_from_record_ids"} in result["edges"]
    assert {"from": rule["id"], "to": "mem_missing", "type": "conflicts_with"} in result["missing"]


def test_list_conflicts_reports_open_and_missing_edges(repo: Path) -> None:
    config = load_config(repo)
    first = memory_write_record(
        config,
        content_markdown="# First Rule\n\nFirst side of the conflict.\n",
        record_kind="decision",
        scope="project_shared",
        status="validated",
        author="alice",
        tags=["mcp"],
    )
    second = memory_write_record(
        config,
        content_markdown="# Second Rule\n\nSecond side of the conflict.\n",
        record_kind="decision",
        scope="project_shared",
        status="validated",
        author="bob",
        tags=["mcp"],
        conflicts_with=[first["id"], "mem_missing"],
    )
    # Reciprocal declaration should not create a duplicate conflict pair.
    memory_link_artifact(config, first["id"], system_area="memory")
    found = find_record_by_id(config, first["id"])
    assert not isinstance(found, dict)
    abs_path, rel_path, metadata, body = found
    metadata["conflicts_with"] = [second["id"]]
    update = write_same_record(config, abs_path=abs_path, rel_path=rel_path, metadata=metadata, body=body)
    assert update["ok"] is True

    result = memory_list_conflicts(config)

    assert result["ok"] is True
    assert result["stats"]["conflicts"] == 1
    assert result["stats"]["missing"] == 1
    assert result["conflicts"][0]["source"]["id"] in {first["id"], second["id"]}
    assert result["conflicts"][0]["target"]["id"] in {first["id"], second["id"]}
    assert result["missing"][0]["target_id"] == "mem_missing"


def test_list_conflicts_excludes_resolved_by_default(repo: Path) -> None:
    config = load_config(repo)
    archived = memory_write_record(
        config,
        content_markdown="# Archived Side\n\nResolved side.\n",
        record_kind="archive_record",
        scope="archive",
        status="archived",
        author="alice",
        tags=["mcp"],
    )
    open_record = memory_write_record(
        config,
        content_markdown="# Open Side\n\nConflicts with archived side.\n",
        record_kind="decision",
        scope="project_shared",
        status="validated",
        author="bob",
        tags=["mcp"],
        conflicts_with=[archived["id"]],
    )

    default = memory_list_conflicts(config)
    with_resolved = memory_list_conflicts(config, include_resolved=True)

    assert default["ok"] is True
    assert default["conflicts"] == []
    assert with_resolved["ok"] is True
    assert with_resolved["conflicts"][0]["resolved"] is True
    assert with_resolved["conflicts"][0]["source"]["id"] == open_record["id"]


def test_lineage_tools_are_internal_not_mcp_surface(repo: Path) -> None:
    config = load_config(repo)
    tool_names = {tool.name for tool in _build_tools(config)}
    observation = memory_record_observation(
        config,
        content_markdown="# Dispatch Observation\n\nObservation through internal API.\n",
        tags=["mcp"],
        module_names=["MemoryServer"],
    )
    linked = memory_link_artifact(
        config,
        observation["id"],
        plugin_names=["ProjectMemoryMCP"],
    )
    traced = memory_trace_lineage(config, observation["id"])
    rejected = _dispatch_tool(config, "memory_trace_lineage", {"record_id": observation["id"]})

    assert tool_names == {"memory_read", "memory_write", "memory_board_read", "memory_board_write", "memory_task_sync"}
    assert observation["ok"] is True
    assert linked["ok"] is True
    assert traced["ok"] is True
    assert rejected["ok"] is False
    assert rejected["error"] == "unknown_tool"


def test_dispatch_context_lists_conflicts(repo: Path) -> None:
    config = load_config(repo)
    first = memory_write_record(
        config,
        content_markdown="# Facade Conflict A\n\nA side.\n",
        record_kind="decision",
        scope="project_shared",
        status="validated",
        author="alice",
        tags=["mcp"],
    )
    second = memory_write_record(
        config,
        content_markdown="# Facade Conflict B\n\nB side.\n",
        record_kind="decision",
        scope="project_shared",
        status="validated",
        author="bob",
        tags=["mcp"],
        conflicts_with=[first["id"]],
    )

    result = memory_list_conflicts(config)

    assert result["ok"] is True
    assert result["stats"]["conflicts"] == 1
    assert {result["conflicts"][0]["source"]["id"], result["conflicts"][0]["target"]["id"]} == {
        first["id"],
        second["id"],
    }
