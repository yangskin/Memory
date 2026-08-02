"""Internal/CLI dispatch tests for LLM enhancement helpers (opt-in)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from servers.memory_server import server_dispatch
from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_llm import LLMClient, LLMConfig
from servers.memory_server.server_dispatch import _dispatch_memory_enhance


def _wrap(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, ensure_ascii=False)
    return json.dumps(
        {
            "choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50},
        }
    )


def _stub_factory(payload: dict[str, Any]):
    def transport(url, headers, body, timeout):
        return 200, _wrap(payload)

    def factory(plugin_root=None):
        return (
            LLMClient(
                LLMConfig(
                    api_key="sk-stub",
                    base_url="https://api.test",
                    model="m-stub",
                    max_input_tokens_per_call=32000,
                ),
                transport=transport,
            ),
            None,
        )

    return factory


def test_memory_enhance_classify_dispatches(repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        server_dispatch,
        "_build_llm_client",
        _stub_factory(
            {
                "record_kind": "decision",
                "scope": "project_shared",
                "tags": ["mcp"],
                "confidence": 0.9,
                "rationale": "decision-style",
            }
        ),
    )
    config = load_config(repo)
    result = _dispatch_memory_enhance(
        config,
        {
            "operation": "classify_record",
            "content_markdown": "# Decision\n\nUse spdlog for logging.",
        },
    )
    assert result["ok"] is True, result
    assert result["record_kind"] == "decision"
    assert result["model"] == "m-stub"


def test_memory_enhance_unknown_operation_returns_invalid_input(repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(server_dispatch, "_build_llm_client", _stub_factory({}))
    config = load_config(repo)
    result = _dispatch_memory_enhance(
        config,
        {"operation": "do_magic"},
    )
    assert result["ok"] is False
    assert result["error"] == "invalid_input"


def test_memory_enhance_extract_dispatches(repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        server_dispatch,
        "_build_llm_client",
        _stub_factory(
            {
                "candidates": [
                    {
                        "kind": "claim_candidate",
                        "content_markdown": "# Claim\n\nA causes B.",
                        "confidence": 0.6,
                        "tags": [],
                    }
                ]
            }
        ),
    )
    config = load_config(repo)
    result = _dispatch_memory_enhance(
        config,
        {
            "operation": "extract_candidates",
            "content_markdown": "Long note ...",
            "source_record_id": "raw-1",
        },
    )
    assert result["ok"] is True, result
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["source_record_id"] == "raw-1"


def test_memory_enhance_generate_handoff_dispatches(repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        server_dispatch,
        "_build_llm_client",
        _stub_factory(
            {
                "summary_markdown": "# Handoff\n\nDid X.",
                "key_points": ["X done"],
                "open_questions": [],
                "next_actions": ["Y"],
            }
        ),
    )
    config = load_config(repo)
    result = _dispatch_memory_enhance(
        config,
        {
            "operation": "generate_handoff",
            "records": [{"id": "s-1", "content_markdown": "log"}],
            "task_id": "task-1",
            "branch": "feature/x",
        },
    )
    assert result["ok"] is True, result
    assert result["task_id"] == "task-1"
    assert result["next_actions"] == ["Y"]


def test_memory_enhance_llm_unavailable_in_band(repo: Path, monkeypatch) -> None:
    def factory(plugin_root=None):
        from servers.memory_server.memory_result import error_result
        return None, error_result("llm_unavailable", "no api key configured")

    monkeypatch.setattr(server_dispatch, "_build_llm_client", factory)
    config = load_config(repo)
    result = _dispatch_memory_enhance(
        config,
        {"operation": "classify_record", "content_markdown": "x"},
    )
    assert result["ok"] is False
    assert result["error"] == "llm_unavailable"


def test_memory_enhance_propagates_validation_errors_in_band(repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        server_dispatch,
        "_build_llm_client",
        _stub_factory(
            {
                "record_kind": "rogue",
                "scope": "personal",
                "tags": [],
                "confidence": 0.5,
            }
        ),
    )
    config = load_config(repo)
    result = _dispatch_memory_enhance(
        config,
        {
            "operation": "classify_record",
            "content_markdown": "x",
            "allowed_kinds": ["decision"],
            "allowed_scopes": ["personal"],
        },
    )
    assert result["ok"] is False
    assert result["error"] == "enhance_failed:classify_record"
