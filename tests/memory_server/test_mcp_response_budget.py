from __future__ import annotations

import json

from servers.memory_server.memory_response_budget import _estimate_tokens, finalize_mcp_response


def _serialized(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def test_small_response_is_returned_unchanged() -> None:
    result = {"ok": True, "operation": "get", "content": "small"}

    assert finalize_mcp_response(result) is result


def test_large_cjk_response_respects_character_and_token_budgets() -> None:
    result = finalize_mcp_response(
        {"ok": True, "operation": "search", "content": "共享记忆" * 10_000},
        max_chars=4_000,
        max_tokens=1_000,
    )
    serialized = _serialized(result)

    assert result["ok"] is True
    assert result["operation"] == "search"
    assert result["response_truncated"] is True
    assert result["response_budget"]["original_estimated_tokens"] > 1_000
    assert len(serialized) <= 4_000
    assert _estimate_tokens(serialized) <= 1_000


def test_non_dict_result_remains_valid_json() -> None:
    result = finalize_mcp_response(["item"] * 10_000, max_chars=2_000, max_tokens=500)

    assert json.loads(_serialized(result)) == result
    assert result["ok"] is True
    assert result["response_truncated"] is True


def test_tight_budget_preserves_compact_shared_context_envelope() -> None:
    result = finalize_mcp_response(
        {
            "ok": True,
            "operation": "retrieve_context",
            "context_items": [{"body": "noise" * 5_000}],
            "shared_context": {
                "status": "fresh",
                "source": "remote",
                "freshness": {"latest_event_seq": 42},
                "project_brief": {"markdown": "brief" * 2_000},
            },
        },
        max_chars=900,
        max_tokens=500,
    )
    serialized = _serialized(result)

    assert result["shared_context"]["status"] == "fresh"
    assert result["shared_context"]["source"] == "remote"
    assert "freshness" in result["shared_context"]
    assert len(serialized) <= 900
    assert _estimate_tokens(serialized) <= 500