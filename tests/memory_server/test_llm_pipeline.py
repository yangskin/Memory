"""Tests for `memory_llm_pipeline`: dedup cache, chunking, map-reduce.

All tests use a stub transport so no network is required and DeepSeek
spend is zero. Live tests live in `test_llm.py` and stay there.
"""

from __future__ import annotations

import json

import pytest

from servers.memory_server.memory_llm import (
    LLMClient,
    LLMConfig,
    LLMConfigError,
    make_distilled_record,
    make_raw_record,
)
from servers.memory_server.memory_llm_pipeline import (
    DEFAULT_PROMPT_OVERHEAD_TOKENS,
    DistillCache,
    chunk_raw_records,
    compute_distill_cache_key,
    map_reduce_distill,
    summarize_records_for_recall,
)


def _summary_response(text: str = "merged summary", prompt_tokens: int = 50, completion_tokens: int = 10) -> str:
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


def _client(transport, *, max_input_tokens_per_call: int = 32000) -> LLMClient:
    cfg = LLMConfig(
        api_key="sk",
        base_url="https://api.test",
        model="m-stub",
        max_input_tokens_per_call=max_input_tokens_per_call,
    )
    return LLMClient(cfg, transport=transport)


def _raw(rid: str, content: str = "body", source: str = "test", captured_at: str = "2026-04-25T00:00:00Z") -> dict:
    return make_raw_record(record_id=rid, content=content, source=source, captured_at=captured_at)


# ── compute_distill_cache_key ────────────────────────────────────────────


def test_cache_key_is_deterministic_for_identical_inputs() -> None:
    raws = [_raw("a"), _raw("b")]
    k1 = compute_distill_cache_key(raws, model="m1", system_prompt="sys")
    k2 = compute_distill_cache_key(raws, model="m1", system_prompt="sys")
    assert k1 == k2 and len(k1) == 64


def test_cache_key_changes_with_model() -> None:
    raws = [_raw("a")]
    k1 = compute_distill_cache_key(raws, model="m1", system_prompt="sys")
    k2 = compute_distill_cache_key(raws, model="m2", system_prompt="sys")
    assert k1 != k2


def test_cache_key_changes_with_system_prompt() -> None:
    raws = [_raw("a")]
    k1 = compute_distill_cache_key(raws, model="m", system_prompt="sys-1")
    k2 = compute_distill_cache_key(raws, model="m", system_prompt="sys-2")
    assert k1 != k2


def test_cache_key_changes_with_record_order() -> None:
    a, b = _raw("a"), _raw("b")
    k1 = compute_distill_cache_key([a, b], model="m", system_prompt="s")
    k2 = compute_distill_cache_key([b, a], model="m", system_prompt="s")
    assert k1 != k2


def test_cache_key_ignores_unrelated_metadata() -> None:
    a = _raw("a", content="same content")
    a_with_extra = make_raw_record(
        record_id="a", content="same content", source="test",
        captured_at="2026-04-25T00:00:00Z",
        extra_meta={"tags": ["unrelated"], "author_role": "tester"},
    )
    k1 = compute_distill_cache_key([a], model="m", system_prompt="s")
    k2 = compute_distill_cache_key([a_with_extra], model="m", system_prompt="s")
    assert k1 == k2


# ── chunk_raw_records ────────────────────────────────────────────────────


def test_chunk_raw_records_single_chunk_when_fits() -> None:
    raws = [_raw(f"r{i}", content="x") for i in range(5)]
    chunks = chunk_raw_records(raws, max_input_tokens=10_000)
    assert len(chunks) == 1
    assert [r["id"] for r in chunks[0]] == [f"r{i}" for i in range(5)]


def test_chunk_raw_records_splits_when_exceeds_budget() -> None:
    big = "x" * 4000  # ~1000 tokens by 4-char rule
    raws = [_raw(f"r{i}", content=big) for i in range(6)]
    # Tiny cap (1500 tokens after 512 overhead → 1024 floor) forces splits.
    chunks = chunk_raw_records(raws, max_input_tokens=1500, overhead_tokens=DEFAULT_PROMPT_OVERHEAD_TOKENS)
    assert len(chunks) >= 2
    # Total records preserved across chunks.
    assert sum(len(c) for c in chunks) == 6


def test_chunk_raw_records_zero_cap_returns_single_chunk() -> None:
    raws = [_raw("a"), _raw("b")]
    assert chunk_raw_records(raws, max_input_tokens=0) == [list(raws)]


def test_chunk_raw_records_empty_input() -> None:
    assert chunk_raw_records([], max_input_tokens=10) == []


def test_chunk_raw_records_rejects_non_dict() -> None:
    with pytest.raises(LLMConfigError):
        chunk_raw_records(["not a dict"], max_input_tokens=10)  # type: ignore[arg-type]


# ── map_reduce_distill: single chunk + cache ─────────────────────────────


def test_map_reduce_single_chunk_skips_reduce() -> None:
    calls = {"n": 0}

    def transport(url, headers, body, timeout):
        calls["n"] += 1
        return 200, _summary_response("partial summary text")

    client = _client(transport)
    raws = [_raw("a", content="hello"), _raw("b", content="world")]
    out = map_reduce_distill(client, raws, record_id="d-1", distilled_at="2026-04-25T00:00:00Z")
    assert out["content"] == "partial summary text"
    assert out["derived_from"] == ["a", "b"]
    assert out["pipeline"]["chunks"] == 1
    assert out["pipeline"]["llm_calls"] == 1
    assert out["pipeline"]["reduced"] is False
    assert calls["n"] == 1  # no reduce call for single chunk


def test_map_reduce_cache_short_circuits_repeat_call() -> None:
    calls = {"n": 0}

    def transport(url, headers, body, timeout):
        calls["n"] += 1
        return 200, _summary_response("cached body")

    client = _client(transport)
    raws = [_raw("a", content="hello")]
    cache = DistillCache()
    map_reduce_distill(client, raws, record_id="d-1", distilled_at="t", cache=cache)
    map_reduce_distill(client, raws, record_id="d-2", distilled_at="t", cache=cache)
    assert calls["n"] == 1  # 2nd call hit the cache, no network


# ── map_reduce_distill: multi-chunk + reduce ─────────────────────────────


def test_map_reduce_multi_chunk_runs_reduce() -> None:
    calls = {"n": 0}
    bodies: list[str] = []

    def transport(url, headers, body, timeout):
        calls["n"] += 1
        payload = json.loads(body.decode("utf-8"))
        bodies.append(payload["messages"][1]["content"])
        return 200, _summary_response(f"chunk-or-merge-{calls['n']}")

    client = _client(transport, max_input_tokens_per_call=1500)
    big = "x" * 4000
    raws = [_raw(f"r{i}", content=big) for i in range(4)]
    out = map_reduce_distill(client, raws, record_id="d-1", distilled_at="t")
    assert out["pipeline"]["chunks"] >= 2
    assert out["pipeline"]["reduced"] is True
    # 1 reduce call appended after the partials.
    assert calls["n"] == out["pipeline"]["chunks"] + 1
    # Final content comes from the last (reduce) call.
    assert out["content"] == f"chunk-or-merge-{calls['n']}"


def test_map_reduce_refuses_distilled_input() -> None:
    client = _client(lambda *a, **k: (200, _summary_response()))
    raw = _raw("a")
    distilled = make_distilled_record(
        record_id="d", content="x", derived_from=["a"], model="m", distilled_at="t",
    )
    with pytest.raises(LLMConfigError):
        map_reduce_distill(client, [raw, distilled], record_id="d-1", distilled_at="t")


def test_map_reduce_requires_records() -> None:
    client = _client(lambda *a, **k: (200, _summary_response()))
    with pytest.raises(LLMConfigError):
        map_reduce_distill(client, [], record_id="d", distilled_at="t")


# ── summarize_records_for_recall ─────────────────────────────────────────


def test_summarize_records_single_chunk() -> None:
    calls = {"n": 0}

    def transport(url, headers, body, timeout):
        calls["n"] += 1
        return 200, _summary_response("recall overview")

    client = _client(transport)
    records = [
        {"id": "r1", "content": "fact one", "record_kind": "note", "scope": "personal"},
        {"id": "r2", "body": "fact two", "record_kind": "note"},
    ]
    out = summarize_records_for_recall(client, records, query="what happened?")
    assert out["summary"] == "recall overview"
    assert out["chunks"] == 1
    assert out["llm_calls"] == 1
    assert out["reduced"] is False
    assert out["model"] == client.config.model


def test_summarize_records_cache_hit() -> None:
    calls = {"n": 0}

    def transport(url, headers, body, timeout):
        calls["n"] += 1
        return 200, _summary_response("first pass")

    client = _client(transport)
    records = [{"id": "r1", "content": "fact"}]
    cache = DistillCache()
    a = summarize_records_for_recall(client, records, query="q", cache=cache)
    b = summarize_records_for_recall(client, records, query="q", cache=cache)
    assert a["summary"] == b["summary"]
    assert calls["n"] == 1
    assert b["cache_hits"] == 1


def test_summarize_records_truncates_per_record() -> None:
    captured: list[str] = []

    def transport(url, headers, body, timeout):
        payload = json.loads(body.decode("utf-8"))
        captured.append(payload["messages"][1]["content"])
        return 200, _summary_response("ok")

    client = _client(transport)
    huge = "y" * 20000
    records = [{"id": "r1", "content": huge}]
    summarize_records_for_recall(client, records, max_chars_per_record=500)
    # Body must be truncated to ~500 chars + ellipsis, NOT 20000.
    assert "y" * 600 not in captured[0]


def test_summarize_records_empty_input_raises() -> None:
    client = _client(lambda *a, **k: (200, _summary_response()))
    with pytest.raises(LLMConfigError):
        summarize_records_for_recall(client, [])


def test_summarize_records_query_changes_cache_key() -> None:
    calls = {"n": 0}

    def transport(url, headers, body, timeout):
        calls["n"] += 1
        return 200, _summary_response(f"v{calls['n']}")

    client = _client(transport)
    records = [{"id": "r1", "content": "fact"}]
    cache = DistillCache()
    summarize_records_for_recall(client, records, query="what?", cache=cache)
    summarize_records_for_recall(client, records, query="why?", cache=cache)
    assert calls["n"] == 2  # different question → fresh LLM call


# ── persistent SQLite distill cache ───────────────────────────────────


def test_sqlite_distill_cache_round_trip(tmp_path) -> None:
    from servers.memory_server.memory_llm_pipeline import SqliteDistillCache

    cache = SqliteDistillCache(tmp_path / "distill.sqlite")
    assert cache.get("missing") is None
    cache.put("k1", "summary one")
    cache.put("k2", "summary two")
    assert cache.get("k1") == "summary one"
    assert cache.get("k2") == "summary two"
    # idempotent overwrite (INSERT OR REPLACE)
    cache.put("k1", "summary one v2")
    assert cache.get("k1") == "summary one v2"


def test_sqlite_distill_cache_persists_across_instances(tmp_path) -> None:
    from servers.memory_server.memory_llm_pipeline import SqliteDistillCache

    path = tmp_path / "nested" / "distill.sqlite"
    cache_a = SqliteDistillCache(path)
    cache_a.put("alpha", "value-A")
    # Reopen — value must survive process-equivalent restart.
    cache_b = SqliteDistillCache(path)
    assert cache_b.get("alpha") == "value-A"


def test_sqlite_distill_cache_ignores_empty_inputs(tmp_path) -> None:
    from servers.memory_server.memory_llm_pipeline import SqliteDistillCache

    cache = SqliteDistillCache(tmp_path / "distill.sqlite")
    cache.put("", "non-empty summary")
    cache.put("k", "")
    assert cache.get("") is None
    assert cache.get("k") is None
