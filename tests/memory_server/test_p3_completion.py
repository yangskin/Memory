from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from servers.memory_server.memory_compiler import memory_compare_snapshots, memory_compile
from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_record_index import memory_rebuild_index
from servers.memory_server import memory_retrieval as memory_retrieval_module
from servers.memory_server.memory_records import memory_write_record
from servers.memory_server.memory_retrieval import memory_get_important_memories, memory_retrieve_context
from servers.memory_server.server import _dispatch_tool


def test_p3_snapshot_targets_generate_chain_and_compare(repo: Path) -> None:
    config = load_config(repo)
    first = memory_write_record(
        config,
        content_markdown="# Earlier Decision\n\nTexture import still used the old path.\n",
        record_kind="decision",
        scope="project_shared",
        status="validated",
        author="alice",
        tags=["mcp"],
        occurred_at="2026-04-22T10:00:00+00:00",
        cognitive_level="fa",
        system_area="memory",
    )
    second = memory_write_record(
        config,
        content_markdown="# Later Decision\n\nTexture import uses the new path.\n",
        record_kind="decision",
        scope="project_shared",
        status="validated",
        author="alice",
        tags=["mcp"],
        occurred_at="2026-04-23T10:00:00+00:00",
        cognitive_level="fa",
        system_area="memory",
    )

    day1 = memory_compile(config, target="daily_snapshot", as_of="2026-04-22")
    day2 = memory_compile(config, target="daily_snapshot", as_of="2026-04-23")
    weekly = memory_compile(config, target="weekly_snapshot", as_of="2026-04-23")
    monthly = memory_compile(config, target="monthly_snapshot", as_of="2026-04-23")
    compared = memory_compare_snapshots(config, path=day1["path"], other_path=day2["path"])

    assert day1["ok"] is True
    assert day2["ok"] is True
    assert weekly["ok"] is True
    assert monthly["ok"] is True
    assert day1["path"] == "memory-bank/compiled/snapshots/daily/2026-04-22.md"
    assert first["id"] in day1["included_record_ids"]
    assert second["id"] not in day1["included_record_ids"]
    assert second["id"] in day2["included_record_ids"]
    assert day1["snapshot_id"] in weekly["derived_from_snapshot_ids"]
    assert day2["snapshot_id"] in weekly["derived_from_snapshot_ids"]
    assert weekly["snapshot_id"] in monthly["derived_from_snapshot_ids"]
    assert compared["ok"] is True
    assert [item["id"] for item in compared["added"]] == [second["id"]]
    assert [item["id"] for item in compared["removed"]] == [first["id"]]


def test_p3_scoring_review_digests_and_rollback_context(repo: Path) -> None:
    config = load_config(repo)
    dao = memory_write_record(
        config,
        content_markdown="# Stable Principle\n\nMemory views are rebuildable, sources are truth.\n",
        record_kind="system_rule",
        scope="project_shared",
        status="published",
        author="lead",
        tags=["mcp"],
        occurred_at="2026-04-23T09:00:00+00:00",
        cognitive_level="dao",
        memory_tier="hot",
    )
    fa = memory_write_record(
        config,
        content_markdown="# Governance Rule\n\nSnapshot outputs must not rewrite source records.\n",
        record_kind="decision",
        scope="project_shared",
        status="validated",
        author="lead",
        tags=["mcp"],
        occurred_at="2026-04-23T10:00:00+00:00",
        cognitive_level="fa",
        task_id="task_p3",
        module_names=["MemoryServer"],
        system_area="memory",
    )
    shu = memory_write_record(
        config,
        content_markdown="# Operator Procedure\n\n## Next Steps\n\n- Run snapshot compile.\n- Review the queue.\n",
        record_kind="procedure",
        scope="project_shared",
        status="validated",
        author="lead",
        tags=["mcp"],
        occurred_at="2026-04-23T11:00:00+00:00",
        cognitive_level="shu",
        task_id="task_p3",
        module_names=["MemoryServer"],
        supersedes=[fa["id"]],
        system_area="memory",
    )

    dao_digest = memory_compile(config, target="dao_digest")
    fa_digest = memory_compile(config, target="fa_digest")
    shu_digest = memory_compile(config, target="shu_digest")
    review = memory_compile(config, target="review_queue")
    rollback = memory_compile(config, target="rollback_context", task_id="task_p3")

    assert dao_digest["ok"] is True
    assert fa_digest["ok"] is True
    assert shu_digest["ok"] is True
    assert dao["id"] in dao_digest["included_record_ids"]
    assert fa["id"] in fa_digest["included_record_ids"]
    assert shu["id"] in shu_digest["included_record_ids"]
    assert review["ok"] is True
    assert review["ranked"][0]["importance_score"] >= review["ranked"][-1]["importance_score"]
    assert rollback["ok"] is True
    assert fa["id"] in rollback["included_record_ids"]
    assert shu["id"] in rollback["included_record_ids"]


def test_p3_retrieve_context_assembles_required_sections(repo: Path) -> None:
    config = load_config(repo)
    rule = memory_write_record(
        config,
        content_markdown="# Texture Pipeline Rule\n\n## Decision\n\nUse deterministic context assembly for texture pipeline work.\n",
        record_kind="decision",
        scope="project_shared",
        status="validated",
        author="alice",
        tags=["mcp"],
        occurred_at="2026-04-23T09:00:00+00:00",
        cognitive_level="fa",
        module_names=["MemoryServer"],
        system_area="memory",
    )
    evidence = memory_write_record(
        config,
        content_markdown="# Texture Pipeline Evidence\n\nThe new texture pipeline path was observed in dispatch tests.\n",
        record_kind="observation",
        scope="session",
        status="raw",
        author="alice",
        tags=["mcp"],
        occurred_at="2026-04-23T10:00:00+00:00",
        cognitive_level="shu",
        module_names=["MemoryServer"],
        system_area="memory",
    )
    other = memory_write_record(
        config,
        content_markdown="# Other Area\n\nShould be removed by facet filtering.\n",
        record_kind="decision",
        scope="project_shared",
        status="validated",
        author="alice",
        tags=["mcp"],
        occurred_at="2026-04-23T11:00:00+00:00",
        cognitive_level="fa",
        module_names=["OtherModule"],
        system_area="other",
        conflicts_with=[rule["id"]],
    )
    memory_compile(config, target="daily_snapshot", as_of="2026-04-23")

    direct = memory_retrieve_context(
        config,
        query="texture pipeline",
        window_start="2026-04-23T00:00:00+00:00",
        window_end="2026-04-23T23:59:59+00:00",
        system_area="memory",
        module_names=["MemoryServer"],
        top_k=5,
    )
    facade = _dispatch_tool(
        config,
        "memory_read",
        {
            "operation": "retrieve_context",
            "query": "texture pipeline",
            "system_area": "memory",
            "module_names": ["MemoryServer"],
            "top_k": 5,
        },
    )
    compared = memory_compare_snapshots(
        config,
        path="memory-bank/compiled/snapshots/daily/2026-04-23.md",
        other_path="memory-bank/compiled/snapshots/daily/2026-04-23.md",
    )

    assert direct["ok"] is True
    assert facade["ok"] is True
    selected_ids = {item["id"] for item in direct["selected_records"]}
    context_ids = {item["id"] for item in direct["context_items"]}
    assert rule["id"] in selected_ids
    assert evidence["id"] in selected_ids
    assert rule["id"] in context_ids
    assert evidence["id"] in context_ids
    assert other["id"] not in selected_ids
    assert direct["core_constraints"]
    assert direct["relevant_rules"]
    assert direct["key_evidence"]
    assert direct["budget_report"]["used_items"] == len(direct["context_items"])
    assert direct["pipeline"]["budget_first_packing"] == len(direct["context_items"])
    assert direct["recent_snapshots"][0]["snapshot_id"] == "daily_snapshot:2026-04-23"
    assert direct["pipeline"]["scope_filter"] >= direct["pipeline"]["facet_filter"]
    assert compared["ok"] is True
    assert compared["stats"]["added"] == 0


def test_p3_retrieve_context_accepts_budget_controls(repo: Path) -> None:
    config = load_config(repo)
    rule = memory_write_record(
        config,
        content_markdown=(
            "# Budgeted Rule\n\n## Decision\n\n"
            "Use compact context assembly when returning pipeline memory to runtime callers.\n\n"
            "## Details\n\n- item 1\n- item 2\n- item 3\n- item 4\n"
        ),
        record_kind="decision",
        scope="project_shared",
        status="validated",
        author="alice",
        tags=["mcp"],
        occurred_at="2026-04-23T09:00:00+00:00",
        cognitive_level="fa",
        module_names=["MemoryServer"],
        system_area="memory",
    )
    evidence = memory_write_record(
        config,
        content_markdown=(
            "# Budgeted Evidence\n\nObserved the compact retrieval path during smoke tests and "
            "confirmed the result was enough to continue the task.\n"
        ),
        record_kind="observation",
        scope="session",
        status="raw",
        author="alice",
        tags=["mcp"],
        occurred_at="2026-04-23T10:00:00+00:00",
        cognitive_level="shu",
        module_names=["MemoryServer"],
        system_area="memory",
    )

    result = memory_retrieve_context(
        config,
        query="compact pipeline memory",
        system_area="memory",
        module_names=["MemoryServer"],
        top_k=5,
        max_items=1,
        max_chars=260,
        max_tokens=90,
    )

    assert result["ok"] is True
    assert len(result["selected_records"]) == 1
    assert result["selected_records"][0]["id"] in {rule["id"], evidence["id"]}
    assert len(result["context_items"]) == 1
    assert result["budget_report"]["used_items"] <= 1
    assert result["budget_report"]["used_chars"] <= 260
    assert result["budget_report"]["used_tokens_est"] <= 90
    assert result["dropped_candidates"] or len({rule["id"], evidence["id"]}) == 1
    assert "important_memories" not in result


def test_p3_important_memories_budget_first_output(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            frozen = datetime(2026, 5, 23, tzinfo=timezone.utc)
            if tz is None:
                return frozen.replace(tzinfo=None)
            return frozen.astimezone(tz)

    monkeypatch.setattr(memory_retrieval_module, "datetime", FrozenDateTime)

    config = load_config(repo)
    stable = memory_write_record(
        config,
        content_markdown=(
            "# Stable Pipeline Memory\n\n## Decision\n\n"
            "Always prefer the compact important-memory output when an agent resumes a task.\n\n"
            "## Details\n\nThis memory was reused across retrieval, compile, and runtime smoke tests.\n"
        ),
        record_kind="decision",
        scope="project_shared",
        status="published",
        author="lead",
        tags=["mcp"],
        source_refs=["evt_pipeline_1", "evt_pipeline_2"],
        validated_by="lead",
        occurred_at="2026-04-23T09:00:00+00:00",
        cognitive_level="fa",
        module_names=["MemoryServer"],
        system_area="memory",
    )
    support = memory_write_record(
        config,
        content_markdown=(
            "# Pipeline Evidence Pack\n\nObserved the compact output in task resumption and "
            "confirmed the selected memory was enough to unblock the next agent turn.\n"
        ),
        record_kind="observation",
        scope="session",
        status="raw",
        author="lead",
        tags=["mcp"],
        source_refs=["evt_pipeline_3"],
        occurred_at="2026-04-23T10:00:00+00:00",
        cognitive_level="shu",
        module_names=["MemoryServer"],
        system_area="memory",
    )
    extra = memory_write_record(
        config,
        content_markdown=(
            "# Secondary Pipeline Detail\n\nExtra context that should be considered but may be "
            "dropped when the important-memory budget is tight.\n"
        ),
        record_kind="procedure",
        scope="project_shared",
        status="validated",
        author="lead",
        tags=["mcp"],
        occurred_at="2026-04-23T11:00:00+00:00",
        cognitive_level="shu",
        module_names=["MemoryServer"],
        system_area="memory",
    )

    direct = memory_get_important_memories(
        config,
        query="pipeline agent resume",
        system_area="memory",
        module_names=["MemoryServer"],
        max_items=2,
        max_chars=420,
        max_tokens=120,
    )
    facade = _dispatch_tool(
        config,
        "memory_read",
        {
            "operation": "important_memories",
            "query": "pipeline agent resume",
            "system_area": "memory",
            "module_names": ["MemoryServer"],
            "max_items": 2,
            "max_chars": 420,
            "max_tokens": 120,
        },
    )

    assert direct["ok"] is True
    assert facade["ok"] is True
    direct_ids = {item["id"] for item in direct["important_memories"]}
    assert stable["id"] in direct_ids
    assert direct["budget_report"]["used_items"] <= 2
    assert direct["budget_report"]["used_chars"] <= 420
    assert direct["budget_report"]["used_tokens_est"] <= 120
    assert direct["evidence_refs"]
    assert any(item["id"] == stable["id"] for item in direct["suggested_externalization"])
    assert direct["stats"]["returned_records"] == len(direct["important_memories"])
    assert len(direct["important_memories"]) == len(facade["important_memories"])
    assert direct["dropped_candidates"] or len(direct["important_memories"]) < 3
    if len(direct["important_memories"]) == 2:
        assert support["id"] in direct_ids or extra["id"] in direct_ids


def test_p3_private_scopes_isolate_authors_in_retrieval(repo: Path) -> None:
    """Both legacy `personal` and schema v2 `user_private` records must be
    invisible to other users in retrieve_context / important_memories."""

    config = load_config(repo)
    alice_personal = memory_write_record(
        config,
        content_markdown="# Alice Personal\n\nAlice private note about texture pipeline.\n",
        record_kind="note",
        scope="personal",
        status="validated",
        author="alice",
        tags=["mcp"],
        occurred_at="2026-04-23T08:00:00+00:00",
        system_area="memory",
    )
    alice_private = memory_write_record(
        config,
        content_markdown="# Alice Private\n\nAlice schema v2 private note about texture pipeline.\n",
        record_kind="note",
        scope="user_private",
        status="validated",
        author="alice",
        tags=["mcp"],
        occurred_at="2026-04-23T09:00:00+00:00",
        system_area="memory",
    )
    bob_shared = memory_write_record(
        config,
        content_markdown="# Bob Shared\n\nBob shared decision about texture pipeline.\n",
        record_kind="decision",
        scope="project_shared",
        status="validated",
        author="bob",
        tags=["mcp"],
        occurred_at="2026-04-23T10:00:00+00:00",
        cognitive_level="fa",
        system_area="memory",
    )

    bob_view = memory_retrieve_context(
        config,
        query="texture pipeline",
        user="bob",
        include_scopes=["personal", "user_private", "project_shared"],
        include_statuses=["validated"],
        top_k=10,
    )
    assert bob_view["ok"] is True
    bob_ids = {item["id"] for item in bob_view["selected_records"]}
    assert alice_personal["id"] not in bob_ids
    assert alice_private["id"] not in bob_ids
    assert bob_shared["id"] in bob_ids

    bob_important = memory_get_important_memories(
        config,
        query="texture pipeline",
        user="bob",
        include_scopes=["personal", "user_private", "project_shared"],
        include_statuses=["validated"],
        max_items=10,
        max_chars=4000,
        max_tokens=1200,
    )
    assert bob_important["ok"] is True
    bob_imp_ids = {item["id"] for item in bob_important["important_memories"]}
    assert alice_personal["id"] not in bob_imp_ids
    assert alice_private["id"] not in bob_imp_ids

    alice_view = memory_retrieve_context(
        config,
        query="texture pipeline",
        user="alice",
        include_scopes=["personal", "user_private", "project_shared"],
        include_statuses=["validated"],
        top_k=10,
    )
    alice_ids = {item["id"] for item in alice_view["selected_records"]}
    assert alice_personal["id"] in alice_ids
    assert alice_private["id"] in alice_ids


def test_retrieve_context_uses_record_index_prefilter_for_metadata_and_facets(repo: Path) -> None:
    config = load_config(repo)
    target = memory_write_record(
        config,
        content_markdown="# Target Procedure\n\nIndexed retrieval prefilter should keep this procedure.\n",
        record_kind="procedure",
        scope="project_shared",
        status="validated",
        author="alice",
        tags=["workflow"],
        system_area="memory",
        module_names=["MemoryServer"],
    )
    assert target["ok"] is True
    for idx in range(12):
        other = memory_write_record(
            config,
            content_markdown=f"# Other {idx}\n\nIndexed retrieval prefilter should skip this unrelated note.\n",
            record_kind="note",
            scope="project_shared",
            status="validated",
            author="alice",
            tags=["workflow"],
            system_area="other",
            module_names=[f"OtherModule{idx}"],
        )
        assert other["ok"] is True

    rebuild = memory_rebuild_index(config)
    assert rebuild["ok"] is True

    result = memory_retrieve_context(
        config,
        query="prefilter procedure",
        include_scopes=["project_shared"],
        include_statuses=["validated"],
        system_area="memory",
        module_names=["MemoryServer"],
        max_items=5,
    )

    assert result["ok"] is True
    assert result["stats"]["prefilter"]["enabled"] is True
    assert result["stats"]["prefilter"]["candidate_paths"] == 1
    assert result["stats"]["scanned_files"] == 1
    assert [item["id"] for item in result["selected_records"]] == [target["id"]]


def test_retrieve_context_falls_back_when_record_index_is_missing(repo: Path) -> None:
    config = load_config(repo)
    target = memory_write_record(
        config,
        content_markdown="# Fallback Procedure\n\nMarkdown fallback should still find this procedure.\n",
        record_kind="procedure",
        scope="project_shared",
        status="validated",
        author="alice",
        tags=["workflow"],
        system_area="memory",
        module_names=["FallbackModule"],
    )
    assert target["ok"] is True

    result = memory_retrieve_context(
        config,
        query="fallback procedure",
        include_scopes=["project_shared"],
        include_statuses=["validated"],
        system_area="memory",
        module_names=["FallbackModule"],
        max_items=5,
    )

    assert result["ok"] is True
    assert result["stats"]["prefilter"]["enabled"] is False
    assert result["stats"]["prefilter"]["fallback_reason"] == "index_missing"
    assert target["id"] in {item["id"] for item in result["selected_records"]}
