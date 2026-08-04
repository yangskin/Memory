"""Tests for the LLM-pipeline dispatch glue (distill on write, summarize on recall)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from servers.memory_server import server_dispatch
from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_frontmatter import parse_record_markdown
from servers.memory_server.memory_llm import LLMClient, LLMConfig
from servers.memory_server.server import _dispatch_tool


def _summary_response(text: str = "distilled overview") -> str:
    return json.dumps(
        {
            "choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
        }
    )


def _stub_client_factory(transport):
    def _factory(plugin_root=None):
        cfg = LLMConfig(
            api_key="sk-stub",
            base_url="https://api.test",
            model="m-stub",
            max_input_tokens_per_call=32000,
        )
        return LLMClient(cfg, transport=transport), None
    return _factory


# ── distill on write ─────────────────────────────────────────────────────


def test_dispatch_write_record_with_distill_persists_summary(repo: Path, monkeypatch) -> None:
    transport_calls = {"n": 0}

    def transport(url, headers, body, timeout):
        transport_calls["n"] += 1
        return 200, _summary_response("auto distilled summary")

    monkeypatch.setattr(server_dispatch, "_build_llm_client", _stub_client_factory(transport))

    config = load_config(repo)
    result = _dispatch_tool(config, "memory_write", {
        "operation": "record",
        "content_markdown": "Decision: switch logging backend to spdlog because of perf gains.",
        "record_kind": "decision",
        "scope": "personal",
        "distill": True,
    })

    assert result["ok"] is True, result
    raw_id = result["id"]
    distilled = result["distilled"]
    assert distilled["ok"] is True, distilled
    assert distilled["summary"] == "auto distilled summary"
    assert distilled["distilled_record_id"] and distilled["distilled_record_id"] != raw_id
    assert distilled["pipeline"]["chunks"] == 1
    assert distilled["pipeline"]["llm_calls"] == 1
    assert transport_calls["n"] == 1
    json.dumps(result)

    # Persisted distilled record exists as a replaceable derived layer and links back to raw.
    persisted_path = Path(repo) / distilled["distilled_path"]
    assert persisted_path.exists()
    body = persisted_path.read_text(encoding="utf-8")
    assert "auto distilled summary" in body
    metadata, _ = parse_record_markdown(body)
    assert metadata["record_kind"] == "distilled_summary"
    assert metadata["status"] == "distilled"
    assert metadata["provenance"] == "llm"
    assert metadata["replaceable"] == "true"
    assert metadata["authoritative"] == "false"
    assert metadata["model"] == "m-stub"
    assert raw_id in metadata["derived_from_record_ids"]


def test_dispatch_write_record_distill_default_off(repo: Path, monkeypatch) -> None:
    """distill is opt-in: omitting the flag never invokes the LLM."""
    def transport(url, headers, body, timeout):  # pragma: no cover — must NOT be called
        raise AssertionError("transport called when distill=False")

    monkeypatch.setattr(server_dispatch, "_build_llm_client", _stub_client_factory(transport))
    config = load_config(repo)
    result = _dispatch_tool(config, "memory_write", {
        "operation": "record",
        "content_markdown": "Note without distill request.",
        "record_kind": "note",
    })
    assert result["ok"] is True
    assert "distilled" not in result


def test_dispatch_write_record_distill_unavailable_returns_inband_error(repo: Path, monkeypatch) -> None:
    """LLM unavailable must NOT lose the primary write."""
    def _factory(plugin_root=None):
        from servers.memory_server.memory_result import error_result
        return None, error_result("llm_unavailable", "no api key configured")
    monkeypatch.setattr(server_dispatch, "_build_llm_client", _factory)

    config = load_config(repo)
    result = _dispatch_tool(config, "memory_write", {
        "operation": "record",
        "content_markdown": "still gets written even without LLM.",
        "record_kind": "note",
        "distill": True,
    })
    assert result["ok"] is True
    raw_path = Path(repo) / result["path"]
    assert raw_path.exists()
    assert result["distilled"]["ok"] is False
    assert result["distilled"]["error"] == "llm_unavailable"


# ── summarize on retrieve_context ────────────────────────────────────────


def _seed_records(repo: Path) -> None:
    config = load_config(repo)
    from servers.memory_server.memory_records import memory_write_record
    for i in range(3):
        memory_write_record(
            config,
            content_markdown=f"# Subsystem A note {i}\n\n## Decision\n\nFact about subsystem A iteration {i}.",
            record_kind="decision",
            scope="project_shared",
            status="validated",
            author="alice",
            tags=["mcp"],
            occurred_at="2026-04-23T10:00:00+00:00",
            cognitive_level="fa",
            module_names=["MemoryServer"],
            system_area="memory",
        )


def test_dispatch_retrieve_context_with_summarize_attaches_summary(repo: Path, monkeypatch) -> None:
    _seed_records(repo)

    transport_calls = {"n": 0}

    def transport(url, headers, body, timeout):
        transport_calls["n"] += 1
        return 200, _summary_response("subsystem A insights overview")

    monkeypatch.setattr(server_dispatch, "_build_llm_client", _stub_client_factory(transport))

    config = load_config(repo)
    result = _dispatch_tool(config, "memory_read", {
        "operation": "retrieve_context",
        "query": "subsystem A",
        "system_area": "memory",
        "module_names": ["MemoryServer"],
        "top_k": 5,
        "summarize": True,
    })
    assert result["ok"] is True, result
    summary = result.get("summary")
    assert summary and summary["ok"] is True
    assert summary["summary"] == "subsystem A insights overview"
    assert summary["chunks"] >= 1
    assert summary["llm_calls"] >= 1
    assert transport_calls["n"] == summary["llm_calls"]


def test_dispatch_retrieve_context_summarize_default_off(repo: Path, monkeypatch) -> None:
    _seed_records(repo)

    def transport(url, headers, body, timeout):  # pragma: no cover
        raise AssertionError("transport called when summarize=False")

    monkeypatch.setattr(server_dispatch, "_build_llm_client", _stub_client_factory(transport))
    config = load_config(repo)
    result = _dispatch_tool(config, "memory_read", {
        "operation": "retrieve_context",
        "query": "subsystem A",
    })
    assert result["ok"] is True
    assert "summary" not in result


def test_dispatch_retrieve_context_summarize_no_records(repo: Path, monkeypatch) -> None:
    """Empty recall set → summarize_skipped, primary result unchanged."""
    monkeypatch.setattr(
        server_dispatch,
        "_build_llm_client",
        _stub_client_factory(lambda *a, **k: (200, _summary_response())),
    )
    config = load_config(repo)
    result = _dispatch_tool(config, "memory_read", {
        "operation": "retrieve_context",
        "query": "no-such-keyword-zzzz",
        "summarize": True,
    })
    assert result["ok"] is True
    summary = result.get("summary")
    assert summary is not None
    assert summary["ok"] is False
    assert summary["error"] == "summarize_skipped"
