"""Tests for the v0.10.0 snapshot_narrative capability."""

from __future__ import annotations

import json

from servers.memory_server.memory_llm import LLMClient, LLMConfig
from servers.memory_server.memory_llm_pipeline import DistillCache
from servers.memory_server.memory_snapshot_narrative import (
    NARRATIVE_HEADING,
    SnapshotNarrativeResult,
    generate_snapshot_narrative,
    inject_narrative,
)


def _response(text: str, *, prompt_tokens: int = 50, completion_tokens: int = 30) -> str:
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


def _scripted_transport(replies: list[str]):
    bodies = list(replies)

    def _xport(_url, _headers, _body, _timeout):  # noqa: ARG001
        if not bodies:
            raise AssertionError("ran out of canned responses")
        return 200, bodies.pop(0)

    return _xport


def _client(transport) -> LLMClient:
    cfg = LLMConfig(api_key="sk", base_url="https://api.test", model="m-stub")
    return LLMClient(cfg, transport=transport)


def _record(rid: str, *, title: str = "T", body: str = "body", kind: str = "decision") -> dict:
    return {"id": rid, "title": title, "record_kind": kind, "body": body}


# ── generate_snapshot_narrative ───────────────────────────────────────


def test_generate_snapshot_narrative_returns_bullet_body() -> None:
    bullets = "- Top change: shipped P4-C\n- Risk: backlog growing"
    transport = _scripted_transport([_response(bullets)])
    client = _client(transport)

    result = generate_snapshot_narrative(
        client,
        [_record("r1"), _record("r2")],
        target="weekly_snapshot",
        label="2026-W18",
    )
    assert isinstance(result, SnapshotNarrativeResult)
    assert result.ok is True
    assert "Top change" in result.narrative
    assert NARRATIVE_HEADING in result.injected_section
    assert result.record_count == 2
    assert result.target == "weekly_snapshot"


def test_generate_snapshot_narrative_short_circuits_on_empty_records() -> None:
    transport = _scripted_transport([])  # would AssertionError if hit
    client = _client(transport)
    result = generate_snapshot_narrative(client, [], target="weekly_snapshot", label="2026-W18")
    assert result.ok is True
    assert result.narrative == ""
    assert result.injected_section == ""
    assert result.record_count == 0


def test_generate_snapshot_narrative_caches_repeats() -> None:
    cache = DistillCache()
    transport = _scripted_transport([_response("- summary line")])
    client = _client(transport)
    records = [_record("r1")]

    first = generate_snapshot_narrative(
        client, records, target="weekly_snapshot", label="2026-W18", cache=cache
    )
    assert first.ok is True
    assert first.cache_hit is False

    second = generate_snapshot_narrative(
        client, records, target="weekly_snapshot", label="2026-W18", cache=cache
    )
    assert second.ok is True
    assert second.cache_hit is True
    assert second.narrative == first.narrative


def test_generate_snapshot_narrative_distinct_label_misses_cache() -> None:
    cache = DistillCache()
    transport = _scripted_transport([_response("- A"), _response("- B")])
    client = _client(transport)
    r = [_record("r1")]
    first = generate_snapshot_narrative(client, r, target="weekly_snapshot", label="2026-W18", cache=cache)
    second = generate_snapshot_narrative(client, r, target="weekly_snapshot", label="2026-W19", cache=cache)
    assert first.cache_hit is False
    assert second.cache_hit is False
    assert first.narrative != second.narrative


# ── inject_narrative ──────────────────────────────────────────────────


def test_inject_narrative_inserts_after_title() -> None:
    body = "# Weekly Snapshot 2026-W18\n\n## Window\n\n- snapshot_id: foo\n"
    section = f"{NARRATIVE_HEADING}\n\n- bullet a\n- bullet b\n"
    out = inject_narrative(body, section)
    lines = out.splitlines()
    assert lines[0] == "# Weekly Snapshot 2026-W18"
    # The narrative heading should appear before the deterministic ## Window.
    narrative_idx = lines.index(NARRATIVE_HEADING)
    window_idx = lines.index("## Window")
    assert narrative_idx < window_idx


def test_inject_narrative_is_idempotent_on_rerun() -> None:
    body = "# Title\n\n## Window\n\n- foo\n"
    section_a = f"{NARRATIVE_HEADING}\n\n- v1 bullet\n"
    section_b = f"{NARRATIVE_HEADING}\n\n- v2 bullet\n"
    once = inject_narrative(body, section_a)
    twice = inject_narrative(once, section_b)
    # The second inject must replace, not stack, so v1 disappears.
    assert "v1 bullet" not in twice
    assert "v2 bullet" in twice
    # Deterministic body untouched.
    assert "## Window" in twice
    assert "- foo" in twice
    # Only one narrative heading present.
    assert twice.count(NARRATIVE_HEADING) == 1


def test_inject_narrative_noop_for_empty_section() -> None:
    body = "# Title\n\n## Window\n\n- foo\n"
    assert inject_narrative(body, "") == body
    assert inject_narrative(body, "   \n") == body
