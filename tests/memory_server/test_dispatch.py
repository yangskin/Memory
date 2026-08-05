"""Tests for _dispatch_tool to verify the dispatch layer (server.py).

These tests complement the direct-function tests by ensuring arguments
pass correctly through the dispatch routing, including edge cases
that only manifest at the dispatch level.
"""

from __future__ import annotations

from pathlib import Path

from servers.memory_server.memory_compiler import memory_compile
from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_lineage import memory_trace_lineage
from servers.memory_server.memory_records import memory_write_record
from servers.memory_server.server import _dispatch_tool
from servers.memory_server import server_dispatch as dispatch_module


def test_dispatch_write_file_operation_requires_cli(repo: Path) -> None:
    """File writes are no longer part of the MCP write interface."""
    config = load_config(repo)
    result = _dispatch_tool(config, "memory_write", {
        "operation": "file",
        "path": "memory-bank/notes.md",
        "content": "",
        "mode": "append",
    })
    assert result["ok"] is False
    assert result["error"] == "admin_cli_required"


def test_dispatch_legacy_get_requires_cli(repo: Path) -> None:
    config = load_config(repo)
    result = _dispatch_tool(config, "memory_get", {"path": "memory-bank/notes.md"})
    assert result["ok"] is False
    assert result["error"] == "unknown_tool"


def test_dispatch_legacy_search_requires_cli(repo: Path) -> None:
    config = load_config(repo)
    result = _dispatch_tool(config, "memory_search", {"query": "Boss"})
    assert result["ok"] is False
    assert result["error"] == "unknown_tool"


def test_dispatch_legacy_admin_requires_cli(repo: Path) -> None:
    config = load_config(repo)
    result = _dispatch_tool(config, "memory_guard_check", {})
    assert result["ok"] is False
    assert result["error"] == "unknown_tool"


def test_dispatch_backup_requires_cli(repo: Path) -> None:
    config = load_config(repo)
    result = _dispatch_tool(config, "memory_backup", {"paths": ["memory-bank/notes.md"]})
    assert result["ok"] is False
    assert result["error"] == "unknown_tool"


def test_dispatch_compact_requires_cli(repo: Path) -> None:
    config = load_config(repo)
    result = _dispatch_tool(config, "memory_compact", {
        "path": ".ai-context/current-task.md",
        "policy": "hot_task",
        "dry_run": True,
    })
    assert result["ok"] is False
    assert result["error"] == "unknown_tool"


def test_dispatch_unknown_tool(repo: Path) -> None:
    """Unknown tool name returns error."""
    config = load_config(repo)
    result = _dispatch_tool(config, "nonexistent_tool", {})
    assert result["ok"] is False
    assert result["error"] == "unknown_tool"


def test_dispatch_missing_required_param(repo: Path) -> None:
    """Missing required param returns error, not crash."""
    config = load_config(repo)
    result = _dispatch_tool(config, "memory_read", {"operation": "get"})
    assert result["ok"] is False
    assert "path" in result["message"]


def test_dispatch_write_overwrite_empty_content_rejected(repo: Path) -> None:
    """Empty structured memory content should be rejected."""
    config = load_config(repo)
    result = _dispatch_tool(config, "memory_write", {
        "content": "",
    })
    assert result["ok"] is False
    assert "empty" in result["message"].lower()


def test_dispatch_write_none_content_rejected(repo: Path) -> None:
    """content=None should be caught by _check_required."""
    config = load_config(repo)
    result = _dispatch_tool(config, "memory_write", {
        "content": None,
    })
    assert result["ok"] is False
    assert "content" in result["message"]


def test_dispatch_facade_read_get_and_search(repo: Path) -> None:
    config = load_config(repo)

    get_result = _dispatch_tool(config, "memory_read", {"operation": "get", "path": "memory-bank/notes.md"})
    search_result = _dispatch_tool(config, "memory_read", {"operation": "search", "query": "Boss"})

    assert get_result["ok"] is True
    assert "Boss Notes" in get_result["content"]
    assert search_result["ok"] is True
    assert search_result["stats"]["total_hits"] >= 1


def test_memory_read_task_context_returns_compact_context(repo: Path) -> None:
    generated = (
        "<!-- generated_by=memory-mcp renderer=deterministic "
        "source_record_ids=[mem_1,mem_2,mem_3] generated_at=2026-06-11 -->\n\n"
        "# Active\n\n- compact context only\n"
    )
    (repo / "memory-bank/activeContext.md").write_text(generated, encoding="utf-8")
    active_user_path = repo / "memory-bank/activeContext/alice.md"
    active_user_path.parent.mkdir(parents=True, exist_ok=True)
    active_user_path.write_text(generated, encoding="utf-8")

    config = load_config(repo)
    result = _dispatch_tool(
        config,
        "memory_read",
        {
            "operation": "task_context",
            "user": "alice",
            "agent_id": "pytest",
            "user_goal": "verify compact task context",
        },
    )

    assert result["ok"] is True
    assert "task_context" not in result
    assert "meta" not in result["active_context"]
    assert "source_record_ids" not in result["active_context"]["content"]
    assert result["active_context"]["content"].lstrip().startswith("# Active")
    assert result["current_task"]["content"].lstrip().startswith("# Current Task")


def test_memory_read_retrieval_omits_diagnostics_by_default(repo: Path) -> None:
    config = load_config(repo)
    written = memory_write_record(
        config,
        content_markdown="# Compact Retrieval\n\nKeep only the context an agent needs.\n",
        record_kind="decision",
        scope="project_shared",
        status="validated",
        tags=["mcp"],
        source_refs=["evt_verbose"],
        related_artifact_ids=["artifact_verbose"],
    )

    result = _dispatch_tool(
        config,
        "memory_read",
        {
            "operation": "retrieve_context",
            "query": "compact retrieval context",
            "top_k": 5,
        },
    )

    assert result["ok"] is True
    assert written["id"] in {item["id"] for item in result["context_items"]}
    for noisy_key in (
        "selected_records",
        "dropped_candidates",
        "budget_report",
        "pipeline",
        "stats",
        "evidence_refs",
        "recent_snapshots",
    ):
        assert noisy_key not in result
    item = next(item for item in result["context_items"] if item["id"] == written["id"])
    assert "body" in item
    assert "tokens_est" not in item
    assert "chars" not in item
    assert "source_refs" not in item
    assert "related_artifact_ids" not in item


def test_memory_read_retrieve_context_keeps_requested_shared_context(repo: Path, monkeypatch) -> None:
    config = load_config(repo)
    shared = {
        "status": "fresh",
        "source": "remote",
        "freshness": {"latest_event_seq": 42},
        "project_brief": {"markdown": "Hub project context"},
    }
    monkeypatch.setattr(
        "servers.memory_server.memory_shared_context.get_shared_context",
        lambda *_args, **_kwargs: shared,
    )

    result = _dispatch_tool(
        config,
        "memory_read",
        {
            "operation": "retrieve_context",
            "query": "Memory Hub",
            "include_shared_context": True,
        },
    )

    assert result["ok"] is True
    assert result["shared_context"] == shared



def test_memory_read_project_graph_is_independent_operation(repo: Path, monkeypatch) -> None:
    config = load_config(repo)
    captured: dict = {}

    def fake_graph(_config, args):
        captured.update(args)
        return {"nodes": [{"id": "n1"}], "edges": [], "freshness": {"stale": False}}

    monkeypatch.setattr("servers.memory_server.memory_shared_context.get_project_graph", fake_graph)
    result = _dispatch_tool(config, "memory_read", {"operation": "project_graph", "task_id": "task-1"})
    assert result["ok"] is True
    assert result["operation"] == "project_graph"
    assert result["graph"]["nodes"] == [{"id": "n1"}]
    assert captured["depth"] == 1
    assert captured["max_nodes"] == 50
    assert captured["max_edges"] == 100


def test_memory_read_include_diagnostics_restores_retrieval_metadata(repo: Path) -> None:
    config = load_config(repo)
    memory_write_record(
        config,
        content_markdown="# Diagnostic Retrieval\n\nExpose debug metadata only on request.\n",
        record_kind="decision",
        scope="project_shared",
        status="validated",
        tags=["mcp"],
    )

    result = _dispatch_tool(
        config,
        "memory_read",
        {
            "operation": "retrieve_context",
            "query": "diagnostic retrieval metadata",
            "include_diagnostics": True,
        },
    )

    assert result["ok"] is True
    assert "selected_records" in result
    assert "budget_report" in result
    assert "pipeline" in result
    assert "stats" in result


def test_memory_read_retrieve_context_defaults_to_current_user(repo: Path, monkeypatch) -> None:
    monkeypatch.setenv("MEMORY_MCP_USER", "alice")
    config = load_config(repo)
    alice = memory_write_record(
        config,
        content_markdown="# Vacuum Binding Alice\n\nAlice-only sample binding note.\n",
        record_kind="note",
        scope="personal",
        status="validated",
        author="alice",
    )
    bob = memory_write_record(
        config,
        content_markdown="# Vacuum Binding Bob\n\nBob-only sample binding note.\n",
        record_kind="note",
        scope="personal",
        status="validated",
        author="bob",
    )
    bob_session = memory_write_record(
        config,
        content_markdown="# Vacuum Binding Bob Session\n\nBob session sample binding note.\n",
        record_kind="note",
        scope="session",
        status="raw",
        author="bob",
    )

    result = _dispatch_tool(
        config,
        "memory_read",
        {
            "operation": "retrieve_context",
            "query": "vacuum binding fracture note",
            "top_k": 10,
        },
    )

    ids = {item["id"] for item in result["context_items"]}
    assert result["ok"] is True
    assert result["user"] == "alice"
    assert alice["id"] in ids
    assert bob["id"] not in ids
    assert bob_session["id"] not in ids


def test_memory_read_retrieve_context_token_does_not_limit_to_token_task(repo: Path) -> None:
    config = load_config(repo)
    current = _dispatch_tool(
        config,
        "memory_read",
        {
            "operation": "task_context",
            "user": "alice",
            "agent_id": "copilot",
            "user_goal": "current sample binding task",
            "max_chars": 0,
        },
    )
    other_task = memory_write_record(
        config,
        content_markdown="# SpawnRuntimeSampleGroup\n\nSampleDomain sample binding note from another agent task.\n",
        record_kind="note",
        scope="personal",
        status="validated",
        author="alice",
        task_id="task_other_agent_recent",
    )
    bob_other_task = memory_write_record(
        config,
        content_markdown="# SpawnRuntimeSampleGroup Bob\n\nBob sample binding note from another task.\n",
        record_kind="note",
        scope="personal",
        status="validated",
        author="bob",
        task_id="task_other_agent_recent",
    )

    result = _dispatch_tool(
        config,
        "memory_read",
        {
            "operation": "retrieve_context",
            "query": "SpawnRuntimeSampleGroup sample binding",
            "context_token": current["context_token"],
            "top_k": 10,
        },
    )

    ids = {item["id"] for item in result["context_items"]}
    assert result["ok"] is True
    assert result["user"] == "alice"
    assert result["task_context"]["task_id"] == current["task_id"]
    assert other_task["id"] in ids
    assert bob_other_task["id"] not in ids


def test_memory_read_retrieve_context_respects_explicit_task_id(repo: Path) -> None:
    config = load_config(repo)
    in_task = memory_write_record(
        config,
        content_markdown="# Explicit Task Match\n\nFracture binding explicit task note.\n",
        record_kind="note",
        scope="personal",
        status="validated",
        author="alice",
        task_id="task_target",
    )
    out_of_task = memory_write_record(
        config,
        content_markdown="# Explicit Task Other\n\nFracture binding other task note.\n",
        record_kind="note",
        scope="personal",
        status="validated",
        author="alice",
        task_id="task_other",
    )

    result = _dispatch_tool(
        config,
        "memory_read",
        {
            "operation": "retrieve_context",
            "query": "sample binding task note",
            "user": "alice",
            "task_id": "task_target",
            "top_k": 10,
        },
    )

    ids = {item["id"] for item in result["context_items"]}
    assert result["ok"] is True
    assert in_task["id"] in ids
    assert out_of_task["id"] not in ids


def test_memory_read_latest_memories_returns_current_user_recency_first(repo: Path, monkeypatch) -> None:
    monkeypatch.setenv("MEMORY_MCP_USER", "alice")
    config = load_config(repo)
    older = memory_write_record(
        config,
        content_markdown="# Older Alice Memory\n\nOlder current-user memory.\n",
        record_kind="note",
        scope="personal",
        status="validated",
        author="alice",
        occurred_at="2026-07-06T10:00:00+00:00",
    )
    newest = memory_write_record(
        config,
        content_markdown="# Newest Alice Memory\n\nNewest current-user memory.\n",
        record_kind="note",
        scope="personal",
        status="validated",
        author="alice",
        occurred_at="2026-07-07T10:00:00+00:00",
    )
    bob = memory_write_record(
        config,
        content_markdown="# Newest Bob Memory\n\nNewest other-user memory.\n",
        record_kind="note",
        scope="personal",
        status="validated",
        author="bob",
        occurred_at="2026-07-08T10:00:00+00:00",
    )
    shared = memory_write_record(
        config,
        content_markdown="# Shared Memory\n\nProject-visible memory.\n",
        record_kind="note",
        scope="project_shared",
        status="validated",
        author="bob",
        occurred_at="2026-07-07T12:00:00+00:00",
    )

    result = _dispatch_tool(
        config,
        "memory_read",
        {
            "operation": "latest_memories",
            "top_k": 10,
        },
    )

    ids = [item["id"] for item in result["latest_memories"]]
    assert result["ok"] is True
    assert result["user"] == "alice"
    assert bob["id"] not in ids
    assert ids.index(shared["id"]) < ids.index(newest["id"]) < ids.index(older["id"])


def test_memory_read_latest_memories_includes_agent_authored_user_task(repo: Path) -> None:
    config = load_config(repo)
    current = _dispatch_tool(
        config,
        "memory_read",
        {
            "operation": "task_context",
            "user": "alice",
            "agent_id": "copilot",
            "user_goal": "latest task ownership",
            "max_chars": 0,
        },
    )
    agent_authored = memory_write_record(
        config,
        content_markdown="# Agent Authored Memory\n\nCurrent user's task memory written by agent author.\n",
        record_kind="note",
        scope="personal",
        status="validated",
        author="copilot",
        task_id=current["task_id"],
        occurred_at="2026-07-07T10:00:00+00:00",
    )
    other_agent = memory_write_record(
        config,
        content_markdown="# Other Agent Memory\n\nOther task memory written by agent author.\n",
        record_kind="note",
        scope="personal",
        status="validated",
        author="copilot",
        task_id="task_unknown_other_user",
        occurred_at="2026-07-07T11:00:00+00:00",
    )

    result = _dispatch_tool(
        config,
        "memory_read",
        {
            "operation": "latest_memories",
            "context_token": current["context_token"],
            "top_k": 10,
        },
    )

    ids = {item["id"] for item in result["latest_memories"]}
    assert result["ok"] is True
    assert agent_authored["id"] in ids
    assert other_agent["id"] not in ids


def test_memory_read_search_records_defaults_to_current_user(repo: Path, monkeypatch) -> None:
    monkeypatch.setenv("MEMORY_MCP_USER", "alice")
    config = load_config(repo)
    alice = memory_write_record(
        config,
        content_markdown="# Indexed Private Alice\n\nNeedle private search.\n",
        record_kind="note",
        scope="personal",
        status="validated",
        author="alice",
    )
    bob = memory_write_record(
        config,
        content_markdown="# Indexed Private Bob\n\nNeedle private search.\n",
        record_kind="note",
        scope="personal",
        status="validated",
        author="bob",
    )
    from servers.memory_server.memory_record_index import memory_rebuild_index

    memory_rebuild_index(config)

    result = _dispatch_tool(
        config,
        "memory_read",
        {
            "operation": "search_records",
            "query": "Needle private search",
            "top_k": 10,
        },
    )

    ids = {item["id"] for item in result["results"]}
    assert result["ok"] is True
    assert alice["id"] in ids
    assert bob["id"] not in ids


def test_memory_read_search_records_prunes_non_context_and_vectors(repo: Path, monkeypatch) -> None:
    def fake_search_records(_config, query: str, *, user: str | None = None, top_k: int | None = None) -> dict:
        return {
            "ok": True,
            "error": None,
            "message": "record search completed",
            "query": query,
            "results": [
                {
                    "id": "mem_vector",
                    "path": "memory-bank/people/alice/packs/20260611.md",
                    "schema_version": "2.0",
                    "record_kind": "note",
                    "scope": "personal",
                    "status": "validated",
                    "author": "alice",
                    "tags": ["mcp"],
                    "task_id": "task_verbose",
                    "branch": "main",
                    "memory_tier": "hot",
                    "cognitive_level": "shu",
                    "importance_score": 0.5,
                    "system_area": "memory",
                    "facets": ["MemoryServer", "ExtraFacet"],
                    "title": "Vector Payload",
                    "snippet": "Only this snippet is useful.",
                    "score": 1.0,
                    "embedding": [0.1] * 64,
                    "metadata": {"vectors": [0.2] * 64},
                }
            ],
            "stats": {"total_hits": 1, "db_path": ".ai-memory/memory-index.sqlite3"},
        }

    monkeypatch.setattr(dispatch_module, "memory_search_records", fake_search_records)
    config = load_config(repo)

    compact = _dispatch_tool(config, "memory_read", {"operation": "search_records", "query": "vector"})
    hit = compact["results"][0]
    assert compact["ok"] is True
    assert "stats" not in compact
    assert "schema_version" not in hit
    assert "facets" not in hit
    assert "embedding" not in hit
    assert "metadata" not in hit
    assert hit["snippet"] == "Only this snippet is useful."

    diagnostics = _dispatch_tool(
        config,
        "memory_read",
        {"operation": "search_records", "query": "vector", "include_diagnostics": True},
    )
    diagnostic_hit = diagnostics["results"][0]
    assert diagnostics["stats"]["total_hits"] == 1
    assert "facets" in diagnostic_hit
    assert "embedding" not in diagnostic_hit
    assert "vectors" not in diagnostic_hit["metadata"]


def test_dispatch_facade_write_record_and_context_compile(repo: Path) -> None:
    config = load_config(repo)

    record = _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "record",
            "content_markdown": "# Facade Record\n\nCompiled through facade tools.\n",
            "record_kind": "note",
            "scope": "personal",
            "status": "validated",
            "author": "alice",
            "tags": ["mcp"],
            "task_id": "task_facade",
        },
    )
    compiled = memory_compile(
        config,
        target="runtime_digest",
        user="alice",
        task_id="task_facade",
    )
    fetched = _dispatch_tool(
        config,
        "memory_read",
        {"operation": "runtime_digest", "user": "alice", "task_id": "task_facade"},
    )

    assert record["ok"] is True
    assert compiled["ok"] is True
    assert fetched["ok"] is True
    assert record["id"] in compiled["included_record_ids"]
    assert fetched["content"] == compiled["content"]


def test_dispatch_facade_write_observation_and_trace_lineage(repo: Path) -> None:
    config = load_config(repo)
    source = memory_write_record(config, content_markdown="# Source\n\nBase.\n", record_kind="observation", tags=["mcp"])

    observation = _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "observation",
            "content_markdown": "# Observation\n\nDerived evidence.\n",
            "tags": ["mcp"],
            "derived_from_record_ids": [source["id"]],
        },
    )
    traced = memory_trace_lineage(config, observation["id"])

    assert observation["ok"] is True
    assert traced["ok"] is True
    assert traced["record_id"] == observation["id"]


# ---------------------------------------------------------------------------
# §15.2-B: rewrite_query / narrative facade end-to-end coverage
# Covers the four runner statuses: disabled / unavailable / timeout / ok.
# ---------------------------------------------------------------------------


import json as _json

import pytest

from servers.memory_server import memory_llm_runner as _runner
from servers.memory_server import memory_query_rewrite as _qr_module
from servers.memory_server import memory_compile_views as _compile_views


def _enable_capability(repo: Path, capability: str) -> None:
    cfg_path = repo / ".ai-memory/config.json"
    payload = _json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    llm = payload.setdefault("llm_defaults", {})
    caps = llm.setdefault("capabilities", {})
    caps[capability] = {"enabled": True}
    cfg_path.write_text(_json.dumps(payload), encoding="utf-8")


class _FakeClient:
    """Stand-in for ``LLMClient``; the real one is not invoked because we
    monkeypatch the capability worker (`rewrite_query` /
    `generate_snapshot_narrative`).
    """

    def __init__(self):
        self.config = type("Cfg", (), {"model": "fake-model", "timeout": 30.0,
                                       "max_output_tokens_per_call": 1024})()


def _seed_for_retrieval(config) -> str:
    res = memory_write_record(
        config,
        content_markdown="# Material PBR\n\nMaterial pipeline notes.\n",
        record_kind="note",
        scope="personal",
        status="validated",
        tags=["mcp"],
    )
    return res["id"]


def test_dispatch_rewrite_query_disabled(repo: Path) -> None:
    """Default config keeps query_rewrite disabled → status=disabled,
    variants empty, retrieval still succeeds.
    """
    config = load_config(repo)
    _seed_for_retrieval(config)

    result = _dispatch_tool(
        config,
        "memory_read",
        {
            "operation": "retrieve_context",
            "query": "material pbr",
            "rewrite_query": True,
        },
    )
    assert result["ok"] is True
    qr = result.get("query_rewrite")
    assert qr is not None
    assert qr["status"] == "disabled"
    assert qr["variants"] == []


def test_dispatch_rewrite_query_unavailable(repo: Path, monkeypatch) -> None:
    _enable_capability(repo, "query_rewrite")
    config = load_config(repo)
    _seed_for_retrieval(config)

    from servers.memory_server.memory_llm import LLMConfigError

    def boom(_profile):
        raise LLMConfigError("api key missing")

    monkeypatch.setattr(_runner, "_default_client_factory", boom)

    result = _dispatch_tool(
        config,
        "memory_read",
        {
            "operation": "retrieve_context",
            "query": "material pbr",
            "rewrite_query": True,
        },
    )
    assert result["ok"] is True  # retrieval still works
    qr = result["query_rewrite"]
    assert qr["status"] == "unavailable"
    assert qr["variants"] == []


def test_dispatch_rewrite_query_timeout(repo: Path, monkeypatch) -> None:
    _enable_capability(repo, "query_rewrite")
    config = load_config(repo)
    _seed_for_retrieval(config)

    from servers.memory_server.memory_llm import LLMRequestError

    monkeypatch.setattr(_runner, "_default_client_factory", lambda _p: _FakeClient())

    def slow_call(*_a, **_kw):
        raise LLMRequestError("operation timeout")

    monkeypatch.setattr(_qr_module, "rewrite_query", slow_call)

    result = _dispatch_tool(
        config,
        "memory_read",
        {
            "operation": "retrieve_context",
            "query": "material pbr",
            "rewrite_query": True,
        },
    )
    qr = result["query_rewrite"]
    assert qr["status"] == "timeout"
    assert qr["variants"] == []


def test_dispatch_rewrite_query_ok(repo: Path, monkeypatch) -> None:
    _enable_capability(repo, "query_rewrite")
    config = load_config(repo)
    _seed_for_retrieval(config)

    monkeypatch.setattr(_runner, "_default_client_factory", lambda _p: _FakeClient())

    def ok_call(_client, query, *, max_variants=3, context_hint=None, **_kw):
        return _qr_module.QueryRewriteResult(
            ok=True,
            original=query,
            variants=["material pipeline pbr", "pbr roughness metallic"],
            model="fake-model",
        )

    monkeypatch.setattr(_qr_module, "rewrite_query", ok_call)

    result = _dispatch_tool(
        config,
        "memory_read",
        {
            "operation": "retrieve_context",
            "query": "material pbr",
            "rewrite_query": True,
        },
    )
    qr = result["query_rewrite"]
    assert qr["status"] == "ok"
    assert "material pipeline pbr" in qr["variants"]


# ── narrative compile (now internal/CLI, not an MCP tool) ────────────


def test_dispatch_narrative_disabled(repo: Path) -> None:
    config = load_config(repo)
    _seed_for_retrieval(config)
    result = memory_compile(
        config,
        target="daily_snapshot",
        narrative=True,
    )
    # The compile call itself must succeed; the narrative envelope (if
    # surfaced under either key) must report a non-ok terminal status
    # because snapshot_narrative defaults to disabled.
    assert result.get("ok") is True
    nar = result.get("narrative") or result.get("snapshot_narrative") or {}
    if nar:
        assert nar.get("status") in {"disabled", "skipped", None}


# ---------------------------------------------------------------------------
# Public agent surface lockdown: every legacy MCP name must be rejected with a
# stable error envelope and a per-name migration_hint.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "legacy_name",
    [
        "memory_get",
        "memory_search",
        "memory_search_records",
        "memory_get_runtime_digest",
        "memory_retrieve_context",
        "memory_get_important_memories",
        "memory_remember",
        "memory_write_record",
        "memory_record_observation",
        "memory_context",
        "memory_compile",
        "memory_rebuild_index",
        "memory_update_index",
        "memory_health_check",
        "memory_migrate_records",
        "memory_validate_candidate",
        "memory_publish_candidate",
        "memory_archive_record",
        "memory_delete_record",
        "memory_link_artifact",
        "memory_trace_lineage",
        "memory_list_conflicts",
        "memory_compare_snapshots",
        "memory_backup",
        "memory_compact",
        "memory_guard_check",
        "memory_enhance",
    ],
)
def test_dispatch_legacy_name_rejected_with_migration_hint(repo: Path, legacy_name: str) -> None:
    """All legacy MCP names must be rejected with a stable migration hint."""
    config = load_config(repo)
    result = _dispatch_tool(config, legacy_name, {})
    assert result["ok"] is False
    assert result["error"] == "unknown_tool"
    assert "migration_hint" in result, f"{legacy_name} missing migration_hint"
    hint = result["migration_hint"]
    assert isinstance(hint, str) and hint.strip(), f"{legacy_name} empty migration_hint"
    # Hint must point either at memory_read/memory_write or at the CLI;
    # nothing else is a legal escape route now.
    assert ("memory_read" in hint) or ("memory_write" in hint) or ("CLI" in hint)


def test_dispatch_unknown_memory_name_gets_generic_hint(repo: Path) -> None:
    """A novel memory_* name not in the migration map still gets a CLI hint."""
    config = load_config(repo)
    result = _dispatch_tool(config, "memory_some_future_thing", {})
    assert result["ok"] is False
    assert result["error"] == "unknown_tool"
    assert "migration_hint" in result
    assert "CLI" in result["migration_hint"]


def test_dispatch_non_memory_tool_still_rejected(repo: Path) -> None:
    """Unknown non-memory_* names also reject; they get the generic hint."""
    config = load_config(repo)
    result = _dispatch_tool(config, "ue_ping", {})
    assert result["ok"] is False
    assert result["error"] == "unknown_tool"
    assert "migration_hint" in result


def test_allowed_tools_constant_matches_public_agent_surface() -> None:
    """The exported allow-list includes general memory and dedicated Board tools."""
    from servers.memory_server.server_dispatch import ALLOWED_TOOLS

    assert ALLOWED_TOOLS == frozenset(
        {"memory_read", "memory_write", "memory_board_read", "memory_board_write"}
    )


def test_facade_schema_drops_get_task_context_alias(repo: Path) -> None:
    """memory_read schema must advertise only `task_context`, not the legacy alias."""
    from servers.memory_server.server import _build_tools

    config = load_config(repo)
    tools = _build_tools(config)
    read_tool = next(t for t in tools if t.name == "memory_read")
    op_enum = read_tool.inputSchema["properties"]["operation"]["enum"]
    assert "task_context" in op_enum
    assert "get_task_context" not in op_enum
    assert "include_diagnostics" in read_tool.inputSchema["properties"]
