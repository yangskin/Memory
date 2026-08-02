"""Tests for the v0.10.0 query_rewrite capability."""

from __future__ import annotations

import json

from servers.memory_server.memory_llm import LLMClient, LLMConfig
from servers.memory_server.memory_llm_pipeline import DistillCache
from servers.memory_server.memory_query_rewrite import (
    HARD_MAX_VARIANTS,
    QueryRewriteResult,
    rewrite_query,
)


def _response(text: str, *, prompt_tokens: int = 50, completion_tokens: int = 20) -> str:
    return json.dumps(
        {
            "choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
    )


def _client(transport) -> LLMClient:
    cfg = LLMConfig(api_key="sk-test", base_url="https://api.test", model="m-stub")
    return LLMClient(cfg, transport=transport)


def _scripted_transport(replies: list[str]):
    """Return a transport that yields canned bodies in order.

    Mirrors the LLMClient contract: ``transport(url, headers, body, timeout)``
    returns ``(status, body_text)``.
    """
    bodies = list(replies)

    def _xport(_url, _headers, _body, _timeout):  # noqa: ARG001
        if not bodies:
            raise AssertionError("ran out of canned responses")
        return 200, bodies.pop(0)

    return _xport


# ── happy path ─────────────────────────────────────────────────────────


def test_rewrite_query_returns_parsed_variants() -> None:
    transport = _scripted_transport([_response('["how do snapshots compile?", "compile snapshot pipeline"]')])
    client = _client(transport)
    result = rewrite_query(client, "snapshot compile", max_variants=3)
    assert isinstance(result, QueryRewriteResult)
    assert result.ok is True
    assert result.cache_hit is False
    assert "how do snapshots compile?" in result.variants
    assert len(result.variants) <= 3


def test_rewrite_query_handles_markdown_fenced_response() -> None:
    transport = _scripted_transport(
        [_response('```json\n["alt query a", "alt query b"]\n```')]
    )
    client = _client(transport)
    result = rewrite_query(client, "original q")
    assert result.ok is True
    assert result.variants == ["alt query a", "alt query b"]


def test_rewrite_query_caps_variants_to_max() -> None:
    payload = json.dumps([f"variant {i}" for i in range(20)])
    transport = _scripted_transport([_response(payload)])
    client = _client(transport)
    result = rewrite_query(client, "q", max_variants=4)
    assert len(result.variants) == 4


def test_rewrite_query_clamps_to_hard_cap() -> None:
    payload = json.dumps([f"variant {i}" for i in range(20)])
    transport = _scripted_transport([_response(payload)])
    client = _client(transport)
    result = rewrite_query(client, "q", max_variants=999)
    assert len(result.variants) <= HARD_MAX_VARIANTS


def test_rewrite_query_drops_echoes_of_original() -> None:
    transport = _scripted_transport([_response('["snapshot compile", "compile pipeline detail"]')])
    client = _client(transport)
    result = rewrite_query(client, "snapshot compile", max_variants=3)
    # Original verbatim must not be returned.
    lowered = [v.lower() for v in result.variants]
    assert "snapshot compile" not in lowered
    assert "compile pipeline detail" in result.variants


# ── empty / error inputs ───────────────────────────────────────────────


def test_rewrite_query_short_circuits_on_empty_query() -> None:
    transport = _scripted_transport([])  # would assert if called
    client = _client(transport)
    result = rewrite_query(client, "   ")
    assert result.ok is True
    assert result.variants == []


def test_rewrite_query_handles_non_json_response_gracefully() -> None:
    transport = _scripted_transport([_response("sorry, I cannot help with that")])
    client = _client(transport)
    result = rewrite_query(client, "q")
    # Parser failure is non-fatal — empty variants, still ok=True.
    assert result.ok is True
    assert result.variants == []


def test_rewrite_query_returns_empty_when_model_returns_empty_array() -> None:
    transport = _scripted_transport([_response("[]")])
    client = _client(transport)
    result = rewrite_query(client, "q")
    assert result.ok is True
    assert result.variants == []


# ── cache ──────────────────────────────────────────────────────────────


def test_rewrite_query_cache_hit_skips_network() -> None:
    cache = DistillCache()
    transport = _scripted_transport([_response('["alt query"]')])
    client = _client(transport)

    first = rewrite_query(client, "snapshot compile", cache=cache)
    assert first.ok is True
    assert first.cache_hit is False

    # Second call must not trigger transport (would AssertionError if it did
    # because the canned list is empty).
    second = rewrite_query(client, "snapshot compile", cache=cache)
    assert second.ok is True
    assert second.cache_hit is True
    assert second.variants == first.variants


def test_rewrite_query_cache_segregates_by_query() -> None:
    cache = DistillCache()
    transport = _scripted_transport([_response('["a"]'), _response('["b"]')])
    client = _client(transport)

    r1 = rewrite_query(client, "query one", cache=cache)
    r2 = rewrite_query(client, "query two", cache=cache)
    assert r1.variants != r2.variants
    assert r1.cache_hit is False
    assert r2.cache_hit is False
