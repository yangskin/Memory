from __future__ import annotations

from pathlib import Path

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_records import memory_write_record, parse_record_markdown
from servers.memory_server.memory_record_io import iter_parsed_records
from servers.memory_server.server import _dispatch_tool


def test_write_record_creates_candidate_markdown_with_front_matter(repo: Path) -> None:
    config = load_config(repo)

    result = memory_write_record(
        config,
        content_markdown="# Export Size Rule\n\nKeep `max_texture_size` during export.\n",
        record_kind="rule_candidate",
        scope="personal",
        author="yangskin",
        tags=["asset_pipeline", "texture", "validation"],
        confidence=0.82,
        source_refs=["evt_1023"],
        task_id="task_sp_sync",
        branch="feature/sp-roundtrip",
    )

    assert result["ok"] is True
    assert result["path"].startswith("memory-bank/candidates/")
    assert result["path"].endswith(".md")

    record_path = repo / result["path"]
    metadata, body = parse_record_markdown(record_path.read_text(encoding="utf-8"))
    assert metadata["schema_version"] == "1.0"
    assert metadata["id"] == result["id"]
    assert metadata["record_kind"] == "rule_candidate"
    assert metadata["scope"] == "personal"
    assert metadata["status"] == "candidate"
    assert metadata["author"] == "yangskin"
    assert metadata["tags"] == ["asset_pipeline", "texture", "validation"]
    assert metadata["confidence"] == 0.82
    assert metadata["source_refs"] == ["evt_1023"]
    assert metadata["task_id"] == "task_sp_sync"
    assert metadata["branch"] == "feature/sp-roundtrip"
    assert body.startswith("# Export Size Rule")


def test_write_record_accepts_schema_v2_phase3_metadata(repo: Path) -> None:
    config = load_config(repo)

    result = memory_write_record(
        config,
        content_markdown="# Widget Incident\n\nTexture replacement regressed in the editor widget path.\n",
        record_kind="incident",
        scope="task_or_branch",
        author="alice",
        tags=["mcp"],
        occurred_at="2026-04-23T08:30:00+00:00",
        memory_tier="hot",
        cognitive_level="shu",
        derived_from_record_ids=["mem_source"],
        conflicts_with=["mem_conflict"],
        related_artifact_ids=["asset:/Game/UI/WBP_Test"],
        importance_score=0.74,
        asset_paths=["/Game/UI/WBP_Test"],
        plugin_names=["AssetCustoms"],
        module_names=["ToolTest"],
        class_names=["UToolTestWidget"],
        blueprint_paths=["/Game/UI/WBP_Test.WBP_Test"],
        system_area="memory",
    )

    assert result["ok"] is True
    assert result["path"].startswith("memory-bank/people/")

    metadata, body = parse_record_markdown((repo / result["path"]).read_text(encoding="utf-8"))
    assert metadata["schema_version"] == "2.0"
    assert metadata["record_kind"] == "incident"
    assert metadata["scope"] == "task_or_branch"
    assert metadata["memory_tier"] == "hot"
    assert metadata["cognitive_level"] == "shu"
    assert metadata["derived_from_record_ids"] == ["mem_source"]
    assert metadata["conflicts_with"] == ["mem_conflict"]
    assert metadata["related_artifact_ids"] == ["asset:/Game/UI/WBP_Test"]
    assert metadata["importance_score"] == 0.74
    assert metadata["asset_paths"] == ["/Game/UI/WBP_Test"]
    assert metadata["plugin_names"] == ["AssetCustoms"]
    assert metadata["module_names"] == ["ToolTest"]
    assert metadata["class_names"] == ["UToolTestWidget"]
    assert metadata["blueprint_paths"] == ["/Game/UI/WBP_Test.WBP_Test"]
    assert metadata["system_area"] == "memory"
    assert body.startswith("# Widget Incident")


def test_write_record_rejects_v2_fields_with_schema_v1(repo: Path) -> None:
    config = load_config(repo)

    result = memory_write_record(
        config,
        content_markdown="# Bad Version\n",
        schema_version="1.0",
        record_kind="note",
        tags=["mcp"],
        memory_tier="hot",
    )

    assert result["ok"] is False
    assert result["error"] == "invalid_input"
    assert "schema_version 2.0" in result["message"]


def test_write_record_rejects_unknown_memory_tier(repo: Path) -> None:
    config = load_config(repo)

    result = memory_write_record(
        config,
        content_markdown="# Bad Tier\n",
        record_kind="note",
        tags=["mcp"],
        memory_tier="lukewarm",
    )

    assert result["ok"] is False
    assert result["error"] == "invalid_input"
    assert "memory_tier" in result["message"]


def test_write_record_rejects_unknown_record_kind(repo: Path) -> None:
    config = load_config(repo)

    result = memory_write_record(
        config,
        content_markdown="# Bad\n",
        record_kind="random_thought",
        tags=["mcp"],
    )

    assert result["ok"] is False
    assert result["error"] == "invalid_input"
    assert "record_kind" in result["message"]


def test_write_record_rejects_uncontrolled_tag(repo: Path) -> None:
    config = load_config(repo)

    result = memory_write_record(
        config,
        content_markdown="# Bad\n",
        record_kind="note",
        tags=["invented_tag"],
    )

    assert result["ok"] is False
    assert result["error"] == "invalid_input"
    assert "tags" in result["message"]
    assert result["invalid_field"] == "tags"
    assert result["rejected_tags"] == ["invented_tag"]
    assert "mcp" in result["allowed_tags"]
    assert result["tag_schema_version"] == config.tag_schema_version


def test_legacy_write_record_tool_is_cli_only(repo: Path) -> None:
    config = load_config(repo)

    rejected = _dispatch_tool(
        config,
        "memory_write_record",
        {
            "content_markdown": "# Handoff\n\nContinue record-layer work.\n",
            "record_kind": "handoff",
            "scope": "personal",
            "tags": ["handoff_ready", "mcp"],
        },
    )
    result = memory_write_record(
        config,
        content_markdown="# Handoff\n\nContinue record-layer work.\n",
        record_kind="handoff",
        scope="personal",
        tags=["handoff_ready", "mcp"],
    )

    assert rejected["ok"] is False
    assert rejected["error"] == "unknown_tool"
    assert result["ok"] is True
    assert result["path"].startswith("memory-bank/people/")
    assert (repo / result["path"]).is_file()


def test_record_packing_coalesces_small_records_by_date(repo: Path) -> None:
    config = load_config(repo)
    config = config.__class__(
        **{
            **config.__dict__,
            "record_packing_max_record_chars": 2000,
            "record_packing_max_pack_chars": 12000,
        }
    )

    first = memory_write_record(
        config,
        content_markdown="# First Packed\n\nSmall body.\n",
        record_kind="note",
        scope="personal",
        author="alice",
        tags=["mcp"],
    )
    second = memory_write_record(
        config,
        content_markdown="# Second Packed\n\nAnother small body.\n",
        record_kind="note",
        scope="personal",
        author="alice",
        tags=["mcp"],
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert first["packed"] is True
    assert second["packed"] is True
    assert first["path"] == second["path"]
    assert "/packs/" in first["path"]

    packed_text = (repo / first["path"]).read_text(encoding="utf-8")
    assert first["id"] in packed_text
    assert second["id"] in packed_text

    records, stats = iter_parsed_records(config)
    assert stats["skipped_non_records"] == 0
    assert {record.metadata["id"] for record in records} >= {first["id"], second["id"]}


def test_shared_record_packing_partitions_by_task_to_reduce_merge_hotspots(repo: Path) -> None:
    config = load_config(repo)
    config = config.__class__(
        **{
            **config.__dict__,
            "record_packing_max_record_chars": 2000,
            "record_packing_max_pack_chars": 12000,
        }
    )

    first = memory_write_record(
        config,
        content_markdown="# Shared First\n\nShared task A body.\n",
        record_kind="handoff",
        scope="project_shared",
        author="alice",
        tags=["mcp"],
        task_id="task_shared_a",
    )
    second = memory_write_record(
        config,
        content_markdown="# Shared Second\n\nShared task B body.\n",
        record_kind="handoff",
        scope="project_shared",
        author="alice",
        tags=["mcp"],
        task_id="task_shared_b",
    )
    same_task = memory_write_record(
        config,
        content_markdown="# Shared Third\n\nSame task can still pack together.\n",
        record_kind="handoff",
        scope="project_shared",
        author="alice",
        tags=["mcp"],
        task_id="task_shared_a",
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert same_task["ok"] is True
    assert first["path"] != second["path"]
    assert first["path"] == same_task["path"]
    assert first["path"].startswith("memory-bank/shared/packs/alice/task_shared_a/")
    assert second["path"].startswith("memory-bank/shared/packs/alice/task_shared_b/")

    records, _stats = iter_parsed_records(config)
    assert {record.metadata["id"] for record in records} >= {first["id"], second["id"], same_task["id"]}


def test_personal_record_packing_partitions_by_task_without_per_run_fragments(repo: Path) -> None:
    config = load_config(repo)
    config = config.__class__(
        **{
            **config.__dict__,
            "record_packing_max_record_chars": 2000,
            "record_packing_max_pack_chars": 12000,
        }
    )

    first = memory_write_record(
        config,
        content_markdown="# Personal First\n\nTask A body.\n",
        record_kind="note",
        scope="personal",
        author="alice",
        tags=["mcp"],
        task_id="task_personal_a",
    )
    second = memory_write_record(
        config,
        content_markdown="# Personal Second\n\nTask B body.\n",
        record_kind="note",
        scope="personal",
        author="alice",
        tags=["mcp"],
        task_id="task_personal_b",
    )
    same_task = memory_write_record(
        config,
        content_markdown="# Personal Third\n\nSame task still packs together.\n",
        record_kind="note",
        scope="personal",
        author="alice",
        tags=["mcp"],
        task_id="task_personal_a",
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert same_task["ok"] is True
    assert first["path"] != second["path"]
    assert first["path"] == same_task["path"]
    assert first["path"].startswith("memory-bank/people/alice/packs/task_personal_a/")
    assert second["path"].startswith("memory-bank/people/alice/packs/task_personal_b/")

    records, _stats = iter_parsed_records(config)
    assert {record.metadata["id"] for record in records} >= {first["id"], second["id"], same_task["id"]}


def test_personal_record_packing_uses_branch_fallback_and_sanitized_task_bucket(repo: Path) -> None:
    config = load_config(repo)
    config = config.__class__(
        **{
            **config.__dict__,
            "record_packing_max_record_chars": 2000,
            "record_packing_max_pack_chars": 12000,
        }
    )

    branch_first = memory_write_record(
        config,
        content_markdown="# Branch First\n\nBranch-only body.\n",
        record_kind="note",
        scope="personal",
        author="alice",
        tags=["mcp"],
        branch="feature/memory/pack",
    )
    branch_same = memory_write_record(
        config,
        content_markdown="# Branch Second\n\nSame branch still packs together.\n",
        record_kind="note",
        scope="personal",
        author="alice",
        tags=["mcp"],
        branch="feature/memory/pack",
    )
    task_preferred = memory_write_record(
        config,
        content_markdown="# Task Preferred\n\nTask id wins over branch.\n",
        record_kind="note",
        scope="personal",
        author="alice",
        tags=["mcp"],
        task_id="task_preferred",
        branch="feature/ignored",
    )
    sanitized = memory_write_record(
        config,
        content_markdown="# Sanitized\n\nUnsafe task id should not escape the pack root.\n",
        record_kind="note",
        scope="personal",
        author="alice",
        tags=["mcp"],
        task_id="../../Unsafe Task/With:Separators",
    )

    assert branch_first["ok"] is True
    assert branch_same["ok"] is True
    assert task_preferred["ok"] is True
    assert sanitized["ok"] is True
    assert branch_first["path"] == branch_same["path"]
    assert branch_first["path"].startswith("memory-bank/people/alice/packs/feature-memory-pack/")
    assert task_preferred["path"].startswith("memory-bank/people/alice/packs/task_preferred/")
    assert "feature-ignored" not in task_preferred["path"]
    assert sanitized["path"].startswith("memory-bank/people/alice/packs/Unsafe-Task-With-Separators/")
    assert ".." not in sanitized["path"]
    assert "\\" not in sanitized["path"]
    assert (repo / sanitized["path"]).is_file()

    records, _stats = iter_parsed_records(config)
    assert {record.metadata["id"] for record in records} >= {
        branch_first["id"],
        branch_same["id"],
        task_preferred["id"],
        sanitized["id"],
    }


def test_record_packing_rolls_when_pack_reaches_size_limit(repo: Path) -> None:
    config = load_config(repo)
    config = config.__class__(
        **{
            **config.__dict__,
            "record_packing_max_record_chars": 2000,
            "record_packing_max_pack_chars": 1400,
        }
    )

    first = memory_write_record(
        config,
        content_markdown="# First Packed\n\n" + ("a" * 320) + "\n",
        record_kind="note",
        scope="personal",
        author="alice",
        tags=["mcp"],
    )
    second = memory_write_record(
        config,
        content_markdown="# Second Packed\n\n" + ("b" * 320) + "\n",
        record_kind="note",
        scope="personal",
        author="alice",
        tags=["mcp"],
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert first["path"].endswith("-001.md")
    assert second["path"].endswith("-002.md")
