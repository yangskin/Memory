from __future__ import annotations

import sqlite3
from pathlib import Path

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_record_index import build_search_text, memory_rebuild_index, memory_search_records
from servers.memory_server.memory_records import memory_write_record
from servers.memory_server.server import _dispatch_tool


def test_rebuild_index_indexes_front_matter_records(repo: Path) -> None:
    config = load_config(repo)
    memory_write_record(
        config,
        content_markdown="# Roundtrip Export\n\nPreserve texture size during Substance roundtrip.\n",
        record_kind="rule_candidate",
        tags=["asset_pipeline", "texture"],
    )
    memory_write_record(
        config,
        content_markdown="# Build Note\n\nThe memory server runs without LLM dependencies.\n",
        record_kind="note",
        tags=["mcp", "build"],
    )

    result = memory_rebuild_index(config)

    assert result["ok"] is True
    assert result["indexed_records"] == 2
    assert (repo / ".ai-memory/search.db").is_file()


def test_rebuild_index_deduplicates_identical_record_ids(repo: Path) -> None:
    config = load_config(repo)
    written = memory_write_record(
        config,
        content_markdown="# Exact Duplicate\n\nArchive migration kept an identical copy.\n",
        record_kind="note",
        tags=["mcp"],
    )
    source = repo / written["path"]
    duplicate = repo / "memory-bank/archive/record-packs/exact-duplicate.md"
    duplicate.parent.mkdir(parents=True, exist_ok=True)
    duplicate.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    result = memory_rebuild_index(config)

    assert result["ok"] is True
    assert result["indexed_records"] == 1
    assert result["stats"]["duplicate_record_ids"] == 1
    assert result["stats"]["deduplicated_records"] == 1
    assert result["duplicate_records"][0]["id"] == written["id"]
    assert len(result["duplicate_records"][0]["paths"]) == 2


def test_rebuild_index_rejects_conflicting_record_ids_before_replacing_index(repo: Path) -> None:
    config = load_config(repo)
    stable = memory_write_record(
        config,
        content_markdown="# Stable Record\n\nThis row must survive a rejected rebuild.\n",
        record_kind="note",
        tags=["mcp"],
    )
    assert memory_rebuild_index(config)["ok"] is True

    conflicting = memory_write_record(
        config,
        content_markdown="# Original Record\n\nOriginal body.\n",
        record_kind="note",
        tags=["mcp"],
    )
    source = repo / conflicting["path"]
    duplicate = repo / "memory-bank/archive/record-packs/conflicting-duplicate.md"
    duplicate.parent.mkdir(parents=True, exist_ok=True)
    duplicate.write_text(
        source.read_text(encoding="utf-8").replace("Original body.", "Conflicting body."),
        encoding="utf-8",
    )

    result = memory_rebuild_index(config)

    assert result["ok"] is False
    assert result["error"] == "duplicate_record_id"
    assert any(item["id"] == conflicting["id"] for item in result["conflicts"])
    with sqlite3.connect(repo / ".ai-memory/search.db") as conn:
        indexed_ids = {row[0] for row in conn.execute("SELECT id FROM memory_records")}
    assert stable["id"] in indexed_ids


def test_search_records_query_plan_uses_sqlite_fts_index(repo: Path) -> None:
    config = load_config(repo)
    memory_write_record(
        config,
        content_markdown="# Indexed Search\n\nSQLite FTS should handle record lookups.\n",
        record_kind="note",
        tags=["mcp"],
    )
    memory_rebuild_index(config)

    db_path = repo / ".ai-memory/search.db"
    query_text = build_search_text(title="SQLite FTS", body="", tags=[], metadata_values=[])
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_records_fts)").fetchall()}
        # Mirror the production query: every token quoted as a phrase, no
        # column qualifier so FTS5 searches all indexed columns.
        match_expr = " ".join('"' + token + '"' for token in query_text.split() if token)
        plan_rows = conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT r.id
            FROM memory_records_fts
            JOIN memory_records AS r ON r.id = memory_records_fts.id
            WHERE memory_records_fts MATCH ?
            """,
            (match_expr,),
        ).fetchall()

    plan_text = " ".join(str(row) for row in plan_rows).lower()
    assert "search_text" in columns
    assert "virtual table index" in plan_text


def test_search_records_uses_rebuilt_index(repo: Path) -> None:
    config = load_config(repo)
    written = memory_write_record(
        config,
        content_markdown="# Roundtrip Export\n\nPreserve texture size during Substance roundtrip.\n",
        record_kind="rule_candidate",
        tags=["asset_pipeline", "texture"],
        task_id="task_sp_sync",
    )
    memory_rebuild_index(config)

    result = memory_search_records(config, query="roundtrip", top_k=5)

    assert result["ok"] is True
    assert result["results"]
    assert result["results"][0]["id"] == written["id"]
    assert result["results"][0]["path"] == written["path"]
    assert result["results"][0]["record_kind"] == "rule_candidate"
    assert result["results"][0]["task_id"] == "task_sp_sync"


def test_record_index_handles_packed_records(repo: Path) -> None:
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
        content_markdown="# Packed Alpha\n\nNeedle alpha content.\n",
        record_kind="note",
        scope="personal",
        author="alice",
        tags=["mcp"],
    )
    second = memory_write_record(
        config,
        content_markdown="# Packed Beta\n\nNeedle beta content.\n",
        record_kind="note",
        scope="personal",
        author="alice",
        tags=["mcp"],
    )

    assert first["path"] == second["path"]
    rebuild = memory_rebuild_index(config)
    assert rebuild["ok"] is True
    assert rebuild["indexed_records"] >= 2

    search = memory_search_records(config, "Needle", top_k=10)
    assert search["ok"] is True
    ids = {item["id"] for item in search["results"]}
    assert {first["id"], second["id"]} <= ids


def test_record_index_rebuild_and_search_are_internal_cli_only(repo: Path) -> None:
    config = load_config(repo)
    memory_write_record(
        config,
        content_markdown="# Handoff\n\nContinue record indexing work.\n",
        record_kind="handoff",
        tags=["handoff_ready", "mcp"],
    )

    rejected = _dispatch_tool(config, "memory_rebuild_index", {})
    rebuild = memory_rebuild_index(config)
    search = memory_search_records(config, query="indexing")

    assert rejected["ok"] is False
    assert rejected["error"] == "unknown_tool"
    assert rebuild["ok"] is True
    assert search["ok"] is True
    assert search["stats"]["total_hits"] == 1


def test_record_search_supports_chinese_ngram_queries(repo: Path) -> None:
    config = load_config(repo)
    written = memory_write_record(
        config,
        content_markdown="# 导出链路尺寸约束\n\n导出链路必须保留最大贴图尺寸，避免回传后材质异常。\n",
        record_kind="rule_candidate",
        tags=["asset_pipeline", "texture", "validation"],
    )
    memory_rebuild_index(config)

    result = memory_search_records(config, query="尺寸约束")

    assert result["ok"] is True
    assert result["results"]
    assert result["results"][0]["id"] == written["id"]
    assert result["results"][0]["title"] == "导出链路尺寸约束"


def test_record_search_matches_metadata_without_new_dependencies(repo: Path) -> None:
    config = load_config(repo)
    written = memory_write_record(
        config,
        content_markdown="# 普通交接\n\n继续补运行时摘要。\n",
        record_kind="handoff",
        tags=["handoff_ready", "mcp"],
        task_id="task_runtime_digest",
        branch="feature/memory-compile",
    )
    memory_rebuild_index(config)

    result = memory_search_records(config, query="task_runtime_digest")

    assert result["ok"] is True
    assert result["results"]
    assert result["results"][0]["id"] == written["id"]


def test_record_search_indexes_schema_v2_facets(repo: Path) -> None:
    config = load_config(repo)
    written = memory_write_record(
        config,
        content_markdown="# Module Decision\n\nKeep memory facets searchable without vector dependencies.\n",
        record_kind="decision",
        scope="project_shared",
        tags=["mcp"],
        memory_tier="warm",
        cognitive_level="fa",
        importance_score=0.91,
        module_names=["MemoryServer"],
        plugin_names=["ProjectMemoryMCP"],
        system_area="memory",
    )
    memory_rebuild_index(config)

    result = memory_search_records(config, query="MemoryServer")

    assert result["ok"] is True
    assert result["results"]
    hit = result["results"][0]
    assert hit["id"] == written["id"]
    assert hit["schema_version"] == "2.0"
    assert hit["scope"] == "project_shared"
    assert hit["memory_tier"] == "warm"
    assert hit["cognitive_level"] == "fa"
    assert hit["importance_score"] == 0.91
    assert hit["system_area"] == "memory"
    assert "MemoryServer" in hit["facets"]


def test_build_search_text_generates_cjk_ngrams() -> None:
    text = build_search_text(
        title="导出链路尺寸约束",
        body="保留最大贴图尺寸。",
        tags=["asset_pipeline"],
        metadata_values=["rule_candidate", "task_sp_sync"],
    )

    assert "尺寸" in text
    assert "尺寸约" in text
    assert "约束" in text
    assert "asset_pipeline" in text
    assert "task_sp_sync" in text


def test_rebuild_index_migrates_legacy_fts_schema(repo: Path) -> None:
    config = load_config(repo)
    db_path = repo / ".ai-memory/search.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE VIRTUAL TABLE memory_records_fts USING fts5(
                id UNINDEXED,
                path UNINDEXED,
                title,
                body,
                tags
            )
            """
        )

    memory_write_record(
        config,
        content_markdown="# 中文检索\n\n记录层需要支持中文短词搜索。\n",
        record_kind="note",
        tags=["mcp"],
    )

    result = memory_rebuild_index(config)

    assert result["ok"] is True
    assert memory_search_records(config, query="中文短词")["results"]


def test_search_records_handles_fts5_reserved_characters(repo: Path) -> None:
    """Queries containing FTS5 syntax (hyphens, OR, quotes) must not raise."""
    config = load_config(repo)
    memory_write_record(
        config,
        content_markdown="# Roundtrip Export\n\nPreserve texture-size during round-trip OR fallback.\n",
        record_kind="rule_candidate",
        tags=["asset_pipeline", "texture"],
    )
    memory_rebuild_index(config)

    # Each of these used to break the previous "search_text : <raw>" query.
    for query in ["texture-size", "round-trip", "OR fallback", '"quoted phrase"']:
        result = memory_search_records(config, query=query, top_k=5)
        assert result["ok"] is True, f"query failed: {query!r} -> {result}"


def test_search_records_empty_after_normalization_returns_no_hits(repo: Path) -> None:
    config = load_config(repo)
    memory_write_record(
        config,
        content_markdown="# Note\n\nPlain content.\n",
        record_kind="note",
        tags=["mcp"],
    )
    memory_rebuild_index(config)

    result = memory_search_records(config, query="!!!---!!!", top_k=5)
    assert result["ok"] is True
    assert result["results"] == []
