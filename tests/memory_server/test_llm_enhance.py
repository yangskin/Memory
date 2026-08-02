"""Unit tests for memory_llm_enhance helpers (mock transport, no network)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from servers.memory_server.memory_llm import LLMClient, LLMConfig
from servers.memory_server import memory_llm_enhance as enh
from servers.memory_server.memory_llm_enhance import LLMEnhanceError


def _wrap_response(payload: dict[str, Any], *, fenced: bool = False) -> str:
    text = json.dumps(payload, ensure_ascii=False)
    if fenced:
        text = "```json\n" + text + "\n```"
    return json.dumps(
        {
            "choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50},
        }
    )


def _client(transport) -> LLMClient:
    return LLMClient(
        LLMConfig(api_key="sk-stub", base_url="https://x", model="m-stub", max_input_tokens_per_call=32000),
        transport=transport,
    )


def _make_transport(payload: dict[str, Any], *, fenced: bool = False, status: int = 200, calls: list[dict] | None = None):
    body = _wrap_response(payload, fenced=fenced)

    def transport(url, headers, req_body, timeout):
        if calls is not None:
            calls.append({"url": url, "body": req_body})
        return status, body

    return transport


# ── _parse_json_response ────────────────────────────────────────────────────


def test_parse_json_response_strips_code_fences() -> None:
    assert enh._parse_json_response('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_response_extracts_first_object_when_chatter_around() -> None:
    assert enh._parse_json_response('Here you go: {"a": 1, "b": 2}\nThanks!') == {"a": 1, "b": 2}


def test_parse_json_response_rejects_empty() -> None:
    with pytest.raises(LLMEnhanceError):
        enh._parse_json_response("")


def test_parse_json_response_rejects_invalid_json() -> None:
    with pytest.raises(LLMEnhanceError):
        enh._parse_json_response("not json at all")


def test_parse_json_response_rejects_wrong_top_type() -> None:
    with pytest.raises(LLMEnhanceError):
        enh._parse_json_response('[1, 2, 3]', expected_top=dict)


# ── classify_record ─────────────────────────────────────────────────────────


def test_classify_record_filters_disallowed_tags_and_clamps_confidence() -> None:
    transport = _make_transport(
        {
            "record_kind": "decision",
            "scope": "project_shared",
            "tags": ["mcp", "not_allowed", "memory"],
            "confidence": 1.7,
            "rationale": "looks like a decision",
        }
    )
    client = _client(transport)
    out = enh.classify_record(
        client,
        content="Decision: pick logging backend.",
        allowed_kinds=["decision", "note"],
        allowed_scopes=["project_shared", "personal"],
        allowed_tags=["mcp", "memory"],
    )
    assert out["ok"] is True
    assert out["record_kind"] == "decision"
    assert out["scope"] == "project_shared"
    assert out["tags"] == ["mcp", "memory"]
    assert out["confidence"] == 1.0
    assert out["model"] == "m-stub"
    assert out["usage_delta"]["completion_tokens"] == 20


def test_classify_record_rejects_kind_not_in_allowlist() -> None:
    transport = _make_transport(
        {"record_kind": "rogue", "scope": "personal", "tags": [], "confidence": 0.5}
    )
    with pytest.raises(LLMEnhanceError, match="kind 'rogue' not in allowed_kinds"):
        enh.classify_record(
            _client(transport),
            content="x",
            allowed_kinds=["decision"],
            allowed_scopes=["personal"],
            allowed_tags=[],
        )


def test_classify_record_rejects_scope_not_in_allowlist() -> None:
    transport = _make_transport(
        {"record_kind": "decision", "scope": "unknown", "tags": [], "confidence": 0.5}
    )
    with pytest.raises(LLMEnhanceError, match="scope 'unknown' not in allowed_scopes"):
        enh.classify_record(
            _client(transport),
            content="x",
            allowed_kinds=["decision"],
            allowed_scopes=["personal"],
            allowed_tags=[],
        )


def test_classify_record_rejects_empty_content() -> None:
    with pytest.raises(LLMEnhanceError, match="content is empty"):
        enh.classify_record(
            _client(_make_transport({})),
            content="",
            allowed_kinds=["decision"],
            allowed_scopes=["personal"],
            allowed_tags=[],
        )


# ── extract_candidates ──────────────────────────────────────────────────────


def test_extract_candidates_normalises_each_entry() -> None:
    transport = _make_transport(
        {
            "candidates": [
                {
                    "kind": "claim_candidate",
                    "content_markdown": "# Claim\n\nFoo is true.",
                    "confidence": "0.8",
                    "tags": ["mcp", 42],
                    "rationale": "stated explicitly",
                },
                {
                    "kind": "rule_candidate",
                    "content_markdown": "# Rule\n\nAlways back up.",
                    "confidence": -3,
                    "tags": [],
                },
            ]
        }
    )
    out = enh.extract_candidates(
        _client(transport),
        content="long note ...",
        source_record_id="raw-1",
    )
    assert out["ok"] is True
    assert [c["kind"] for c in out["candidates"]] == ["claim_candidate", "rule_candidate"]
    assert out["candidates"][0]["confidence"] == 0.8
    assert out["candidates"][0]["tags"] == ["mcp", "42"]
    assert out["candidates"][0]["source_record_id"] == "raw-1"
    assert out["candidates"][1]["confidence"] == 0.0


def test_extract_candidates_empty_list_is_valid() -> None:
    transport = _make_transport({"candidates": []})
    out = enh.extract_candidates(_client(transport), content="nothing notable here")
    assert out["ok"] is True
    assert out["candidates"] == []


def test_extract_candidates_rejects_invalid_kind() -> None:
    transport = _make_transport(
        {"candidates": [{"kind": "bogus", "content_markdown": "# x"}]}
    )
    with pytest.raises(LLMEnhanceError, match="kind 'bogus' invalid"):
        enh.extract_candidates(_client(transport), content="x")


def test_extract_candidates_rejects_missing_content() -> None:
    transport = _make_transport(
        {"candidates": [{"kind": "claim_candidate", "content_markdown": "  "}]}
    )
    with pytest.raises(LLMEnhanceError, match="content_markdown empty"):
        enh.extract_candidates(_client(transport), content="x")


# ── merge_candidates ────────────────────────────────────────────────────────


def test_merge_candidates_partitions_input() -> None:
    transport = _make_transport(
        {
            "groups": [
                {"candidate_ids": ["a", "b"], "merged_content": "# Merged AB"},
                {"candidate_ids": ["c"], "merged_content": "# Solo C"},
            ]
        }
    )
    out = enh.merge_candidates(
        _client(transport),
        candidates=[
            {"id": "a", "content_markdown": "# A"},
            {"id": "b", "content_markdown": "# B"},
            {"id": "c", "content_markdown": "# C"},
        ],
    )
    assert out["ok"] is True
    assert len(out["groups"]) == 2
    assert sorted(out["groups"][0]["candidate_ids"]) == ["a", "b"]


def test_merge_candidates_rejects_missing_id_in_groups() -> None:
    transport = _make_transport(
        {"groups": [{"candidate_ids": ["a"], "merged_content": "# A"}]}
    )
    with pytest.raises(LLMEnhanceError, match="missing"):
        enh.merge_candidates(
            _client(transport),
            candidates=[
                {"id": "a", "content_markdown": "# A"},
                {"id": "b", "content_markdown": "# B"},
            ],
        )


def test_merge_candidates_rejects_duplicate_assignment() -> None:
    transport = _make_transport(
        {
            "groups": [
                {"candidate_ids": ["a", "b"], "merged_content": "# AB"},
                {"candidate_ids": ["a"], "merged_content": "# A again"},
            ]
        }
    )
    with pytest.raises(LLMEnhanceError, match="appears in multiple"):
        enh.merge_candidates(
            _client(transport),
            candidates=[
                {"id": "a", "content_markdown": "# A"},
                {"id": "b", "content_markdown": "# B"},
            ],
        )


def test_merge_candidates_rejects_unknown_id() -> None:
    transport = _make_transport(
        {"groups": [{"candidate_ids": ["a", "z"], "merged_content": "# X"}]}
    )
    with pytest.raises(LLMEnhanceError, match="unknown id 'z'"):
        enh.merge_candidates(
            _client(transport),
            candidates=[{"id": "a", "content_markdown": "# A"}],
        )


def test_merge_candidates_rejects_empty_input() -> None:
    with pytest.raises(LLMEnhanceError, match="candidates is empty"):
        enh.merge_candidates(_client(_make_transport({})), candidates=[])


# ── generate_skill_candidate ────────────────────────────────────────────────


def test_generate_skill_candidate_returns_structured_payload() -> None:
    transport = _make_transport(
        {
            "title": "Texture Bake Procedure",
            "content_markdown": "# Texture Bake\n\n## Steps\n1. ...",
            "tags": ["asset_pipeline"],
            "confidence": 0.7,
            "rationale": "common pattern",
        }
    )
    out = enh.generate_skill_candidate(
        _client(transport),
        records=[
            {"id": "r-1", "content_markdown": "obs 1"},
            {"id": "r-2", "content_markdown": "obs 2"},
        ],
    )
    assert out["ok"] is True
    assert out["title"] == "Texture Bake Procedure"
    assert out["source_record_count"] == 2
    assert out["confidence"] == 0.7


def test_generate_skill_candidate_truncates_oversize_records() -> None:
    transport = _make_transport(
        {
            "title": "T",
            "content_markdown": "# T\nbody",
            "confidence": 0.5,
        }
    )
    big = "x" * 12000
    out = enh.generate_skill_candidate(
        _client(transport),
        records=[{"id": "r-big", "content_markdown": big}],
        max_chars_per_record=4000,
    )
    assert out["ok"] is True


def test_generate_skill_candidate_rejects_empty_records() -> None:
    with pytest.raises(LLMEnhanceError, match="records must be non-empty"):
        enh.generate_skill_candidate(_client(_make_transport({})), records=[])


# ── explain_conflict ────────────────────────────────────────────────────────


def test_explain_conflict_normalises_response() -> None:
    transport = _make_transport(
        {
            "conflict_type": "contradiction",
            "severity": "HIGH",
            "explanation": "A says X; B says not-X.",
            "resolution_options": ["pick A", "pick B", "ask user"],
        }
    )
    out = enh.explain_conflict(
        _client(transport),
        record_a={"id": "r-1", "content_markdown": "X"},
        record_b={"id": "r-2", "content_markdown": "not X"},
    )
    assert out["ok"] is True
    assert out["conflict_type"] == "contradiction"
    assert out["severity"] == "high"
    assert out["record_ids"] == ["r-1", "r-2"]
    assert len(out["resolution_options"]) == 3


def test_explain_conflict_rejects_invalid_conflict_type() -> None:
    transport = _make_transport(
        {
            "conflict_type": "uhoh",
            "severity": "low",
            "explanation": "x",
        }
    )
    with pytest.raises(LLMEnhanceError, match="invalid conflict_type"):
        enh.explain_conflict(
            _client(transport),
            record_a={"content_markdown": "a"},
            record_b={"content_markdown": "b"},
        )


def test_explain_conflict_rejects_empty_record_content() -> None:
    with pytest.raises(LLMEnhanceError, match="both records must have content"):
        enh.explain_conflict(
            _client(_make_transport({})),
            record_a={"content_markdown": " "},
            record_b={"content_markdown": "b"},
        )


# ── generate_handoff ────────────────────────────────────────────────────────


def test_generate_handoff_returns_structured_payload() -> None:
    transport = _make_transport(
        {
            "summary_markdown": "# Handoff\n\nDid X, Y; left Z open.",
            "key_points": ["did X", "did Y"],
            "open_questions": ["Z?"],
            "next_actions": ["finish Z"],
        }
    )
    out = enh.generate_handoff(
        _client(transport),
        records=[{"id": "s-1", "content_markdown": "session log"}],
        task_id="task-1",
        branch="feature/x",
    )
    assert out["ok"] is True
    assert out["task_id"] == "task-1"
    assert out["branch"] == "feature/x"
    assert out["key_points"] == ["did X", "did Y"]
    assert out["next_actions"] == ["finish Z"]


def test_generate_handoff_filters_empty_strings_in_lists() -> None:
    transport = _make_transport(
        {
            "summary_markdown": "# H",
            "key_points": ["a", "  ", ""],
            "open_questions": [],
            "next_actions": ["next"],
        }
    )
    out = enh.generate_handoff(
        _client(transport),
        records=[{"content_markdown": "x"}],
    )
    assert out["key_points"] == ["a"]
    assert out["open_questions"] == []
    assert out["next_actions"] == ["next"]


def test_generate_handoff_rejects_missing_required_fields() -> None:
    transport = _make_transport({"summary_markdown": "# H", "key_points": []})
    with pytest.raises(LLMEnhanceError, match="missing required keys"):
        enh.generate_handoff(_client(transport), records=[{"content_markdown": "x"}])
