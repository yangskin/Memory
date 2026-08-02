"""Tests for §15.2-B LLM-assisted metadata normalization.

Covers the opt-in soft preflight on:

- ``memory_write(operation="record", llm_normalize_tags=True)`` — when the
  caller passes business-domain tags that fail the controlled vocabulary,
  ``_dispatch_memory_write`` routes through ``classify_record`` to rewrite
  the tag set, park rejected words on ``system_area``, and emit a
  ``metadata_normalized_by_llm`` warning.
- ``memory_read(operation="task_context", llm_suggest_metadata=True)`` —
  ``_dispatch_memory_read`` attaches a ``suggested_metadata`` envelope
  built from ``user_goal`` + ``active_files``.

All tests stub the LLM transport via the existing ``_build_llm_client``
seam — no network access. Behaviour when LLM is unavailable is verified
to remain backwards-compatible (no silent rewriting).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from servers.memory_server import server_dispatch
from servers.memory_server.memory_config import MemoryConfig, load_config
from servers.memory_server.memory_llm import LLMClient, LLMConfig
from servers.memory_server.server_dispatch import (
    _dispatch_memory_read,
    _dispatch_memory_write,
)


# ── helpers ─────────────────────────────────────────────────────────────────


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


def _unavailable_factory():
    from servers.memory_server.memory_result import error_result

    def factory(plugin_root=None):
        return None, error_result("llm_unavailable", "no api key configured")

    return factory


def _make_config(tmp_path: Path) -> MemoryConfig:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "memory-bank").mkdir()
    (workspace / ".ai-context").mkdir()
    (workspace / ".ai-memory").mkdir()
    cfg_path = workspace / ".ai-memory" / "config.json"
    cfg_path.write_text(json.dumps({}), encoding="utf-8")
    return load_config(str(workspace), str(cfg_path))


# ── record: LLM normalization ────────────────────────────────────────────────


def test_record_llm_normalize_rewrites_unknown_tags_and_emits_warning(tmp_path, monkeypatch):
    """Unknown business tags get parked on system_area; in-vocab suggestions merged in."""
    monkeypatch.setattr(
        server_dispatch,
        "_build_llm_client",
        _stub_factory(
            {
                "record_kind": "decision",
                "scope": "project_shared",
                "tags": ["mcp", "high_value"],
                "confidence": 0.85,
                "rationale": "decision about wall prefab pipeline",
            }
        ),
    )
    config = _make_config(tmp_path)
    result = _dispatch_memory_write(
        config,
        {
            "operation": "record",
            "content_markdown": "# Sample prefab decision\n\nSampleDomain scene uses prefab approach.",
            "record_kind": "decision",
            "scope": "project_shared",
            "tags": ["high_value", "sample_domain", "sample_prefab"],
            "llm_normalize_tags": True,
        },
    )
    assert result["ok"] is True, result
    suggestion = result["metadata_suggestion"]
    assert suggestion["status"] == "ok"
    assert suggestion["applied"] is True
    assert suggestion["accepted_tags"] == ["high_value"]
    assert suggestion["rejected_tags"] == ["sample_domain", "sample_prefab"]
    assert set(suggestion["final_tags"]) == {"high_value", "mcp"}
    assert suggestion["suggested_system_area"] == "sample_domain.sample_prefab"
    warnings = result.get("warnings") or []
    codes = [w.get("code") for w in warnings]
    assert "metadata_normalized_by_llm" in codes
    warn = next(w for w in warnings if w.get("code") == "metadata_normalized_by_llm")
    assert sorted(warn["from_tags"]) == ["high_value", "sample_domain", "sample_prefab"]
    assert warn["rejected_tags"] == ["sample_domain", "sample_prefab"]
    assert warn["system_area"] == "sample_domain.sample_prefab"


def test_record_llm_normalize_writes_system_area_into_record(tmp_path, monkeypatch):
    """The rejected-tags-derived system_area is forwarded to memory_write_record."""
    monkeypatch.setattr(
        server_dispatch,
        "_build_llm_client",
        _stub_factory(
            {
                "record_kind": "note",
                "scope": "personal",
                "tags": ["mcp"],
                "confidence": 0.7,
                "rationale": "ok",
            }
        ),
    )
    config = _make_config(tmp_path)
    result = _dispatch_memory_write(
        config,
        {
            "operation": "record",
            "content_markdown": "# Body\n\ntext",
            "record_kind": "note",
            "tags": ["my_module"],
            "llm_normalize_tags": True,
        },
    )
    assert result["ok"] is True, result
    # Read the persisted record file and verify system_area was injected.
    record_path = Path(config.repo_root) / result["path"]
    assert record_path.exists(), record_path
    text = record_path.read_text(encoding="utf-8")
    assert "system_area: my_module" in text


def test_record_llm_normalize_preserves_explicit_system_area(tmp_path, monkeypatch):
    """When caller already provided system_area, the LLM-derived one must not overwrite."""
    monkeypatch.setattr(
        server_dispatch,
        "_build_llm_client",
        _stub_factory(
            {
                "record_kind": "note",
                "scope": "personal",
                "tags": ["mcp"],
                "confidence": 0.7,
                "rationale": "ok",
            }
        ),
    )
    config = _make_config(tmp_path)
    result = _dispatch_memory_write(
        config,
        {
            "operation": "record",
            "content_markdown": "# Body\n\ntext",
            "record_kind": "note",
            "tags": ["my_module"],
            "system_area": "caller_set_area",
            "llm_normalize_tags": True,
        },
    )
    assert result["ok"] is True, result
    record_path = Path(config.repo_root) / result["path"]
    text = record_path.read_text(encoding="utf-8")
    assert "system_area: caller_set_area" in text
    assert "system_area: my_module" not in text


def test_record_llm_normalize_skipped_when_all_tags_valid(tmp_path, monkeypatch):
    """No LLM call should happen when every tag is already in the vocabulary."""
    called: dict[str, int] = {"n": 0}

    def factory(plugin_root=None):
        called["n"] += 1
        return None, None  # would crash classify_record; ensures we don't get here

    monkeypatch.setattr(server_dispatch, "_build_llm_client", factory)
    config = _make_config(tmp_path)
    result = _dispatch_memory_write(
        config,
        {
            "operation": "record",
            "content_markdown": "# Body\n\ntext",
            "record_kind": "note",
            "tags": ["mcp", "high_value"],
            "llm_normalize_tags": True,
        },
    )
    assert result["ok"] is True, result
    assert "metadata_suggestion" not in result
    assert called["n"] == 0


def test_record_llm_normalize_disabled_by_default_returns_invalid_input(tmp_path, monkeypatch):
    """Without llm_normalize_tags=True the legacy schema validation must still reject."""
    monkeypatch.setattr(server_dispatch, "_build_llm_client", _stub_factory({}))
    config = _make_config(tmp_path)
    result = _dispatch_memory_write(
        config,
        {
            "operation": "record",
            "content_markdown": "# Body\n\ntext",
            "record_kind": "note",
            "tags": ["sample_domain"],
        },
    )
    assert result["ok"] is False
    assert result["error"] == "invalid_input"
    assert "metadata_suggestion" not in result
    assert "tags contain unsupported value" in result["message"]
    assert result["invalid_field"] == "tags"
    assert result["rejected_tags"] == ["sample_domain"]
    assert "mcp" in result["allowed_tags"]
    assert result["tag_schema_version"] == "v1"


def test_record_llm_unavailable_falls_back_to_invalid_input_with_suggestion(tmp_path, monkeypatch):
    """LLM unreachable → no silent rewrite → invalid_input + diagnostic suggestion."""
    monkeypatch.setattr(server_dispatch, "_build_llm_client", _unavailable_factory())
    config = _make_config(tmp_path)
    result = _dispatch_memory_write(
        config,
        {
            "operation": "record",
            "content_markdown": "# Body\n\ntext",
            "record_kind": "note",
            "tags": ["sample_domain"],
            "llm_normalize_tags": True,
        },
    )
    assert result["ok"] is False
    assert result["error"] == "invalid_input"
    suggestion = result.get("metadata_suggestion")
    assert suggestion is not None
    assert suggestion["status"] == "llm_unavailable"
    assert suggestion["applied"] is False
    assert suggestion["rejected_tags"] == ["sample_domain"]
    assert "no api key" in suggestion["message"]


def test_record_llm_failure_returns_status_llm_failed(tmp_path, monkeypatch):
    """LLM raises (e.g. bad JSON) → in-band suggestion with status=llm_failed."""

    def transport(url, headers, body, timeout):
        return 200, json.dumps(
            {
                "choices": [{"message": {"role": "assistant", "content": "not json"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )

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

    monkeypatch.setattr(server_dispatch, "_build_llm_client", factory)
    config = _make_config(tmp_path)
    result = _dispatch_memory_write(
        config,
        {
            "operation": "record",
            "content_markdown": "# Body\n\ntext",
            "record_kind": "note",
            "tags": ["sample_domain"],
            "llm_normalize_tags": True,
        },
    )
    assert result["ok"] is False
    assert result["error"] == "invalid_input"
    suggestion = result["metadata_suggestion"]
    assert suggestion["status"] == "llm_failed"
    assert suggestion["applied"] is False


# ── task_context: suggested_metadata ────────────────────────────────────────


def test_task_context_attaches_suggested_metadata_when_requested(tmp_path, monkeypatch):
    monkeypatch.setattr(
        server_dispatch,
        "_build_llm_client",
        _stub_factory(
            {
                "record_kind": "procedure",
                "scope": "project_shared",
                "tags": ["workflow", "mcp"],
                "confidence": 0.8,
                "rationale": "user goal hints at a workflow procedure",
            }
        ),
    )
    config = _make_config(tmp_path)
    result = _dispatch_memory_read(
        config,
        {
            "operation": "task_context",
            "user_goal": "Add LLM-assisted tag normalization to memory write",
            "active_files": ["servers/memory_server/server_dispatch.py"],
            "llm_suggest_metadata": True,
        },
    )
    assert result["ok"] is True, result
    suggested = result["suggested_metadata"]
    assert suggested["status"] == "ok"
    assert suggested["applied"] is True
    assert suggested["suggested_record_kind"] == "procedure"
    assert suggested["suggested_scope"] == "project_shared"
    assert set(suggested["suggested_tags"]) == {"workflow", "mcp"}


def test_task_context_omits_suggested_metadata_by_default(tmp_path, monkeypatch):
    """Without the opt-in flag, no LLM call should fire and no field is attached."""

    def factory(plugin_root=None):  # would explode if called
        raise AssertionError("LLM client should not be built without llm_suggest_metadata")

    monkeypatch.setattr(server_dispatch, "_build_llm_client", factory)
    config = _make_config(tmp_path)
    result = _dispatch_memory_read(
        config,
        {
            "operation": "task_context",
            "user_goal": "anything",
        },
    )
    assert result["ok"] is True, result
    assert "suggested_metadata" not in result


def test_task_context_suggested_metadata_handles_llm_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(server_dispatch, "_build_llm_client", _unavailable_factory())
    config = _make_config(tmp_path)
    result = _dispatch_memory_read(
        config,
        {
            "operation": "task_context",
            "user_goal": "Investigate wall prefab issue",
            "llm_suggest_metadata": True,
        },
    )
    assert result["ok"] is True, result
    suggested = result["suggested_metadata"]
    assert suggested["status"] == "llm_unavailable"
    assert suggested["applied"] is False
