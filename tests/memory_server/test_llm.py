"""Tests for the OpenAI-compatible LLM client (memory_llm)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from servers.memory_server import memory_llm
from servers.memory_server.memory_llm import (
    DEFAULT_BASE_URL,
    DEFAULT_DISTILL_SYSTEM_PROMPT,
    DEFAULT_INPUT_CNY_PER_MTOK,
    DEFAULT_MAX_INPUT_TOKENS_PER_CALL,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_MAX_TOTAL_OUTPUT_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_OUTPUT_CNY_PER_MTOK,
    PROVENANCE_LLM,
    PROVENANCE_RAW,
    VALID_REASONING_EFFORT,
    LLMBudgetExceeded,
    LLMClient,
    LLMConfig,
    LLMConfigError,
    LLMInputTooLarge,
    LLMRequestError,
    RawImmutableError,
    assert_raw_writable,
    build_chat_payload,
    distill_raw_records,
    extract_text,
    load_llm_config,
    make_distilled_record,
    make_raw_record,
    supersede_distilled,
)


# ── Config loading ─────────────────────────────────────────────────────────


def test_load_config_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(LLMConfigError):
        load_llm_config(plugin_root=tmp_path, env={})


def test_load_config_from_env(tmp_path: Path) -> None:
    cfg = load_llm_config(
        plugin_root=tmp_path,
        env={"MEMORY_LLM_API_KEY": "sk-env", "MEMORY_LLM_MODEL": "deepseek-v4-pro"},
    )
    assert cfg.api_key == "sk-env"
    assert cfg.model == "deepseek-v4-pro"
    assert cfg.base_url == DEFAULT_BASE_URL


def test_load_config_env_fallback_keys(tmp_path: Path) -> None:
    cfg = load_llm_config(plugin_root=tmp_path, env={"DEEPSEEK_API_KEY": "sk-ds"})
    assert cfg.api_key == "sk-ds"
    cfg2 = load_llm_config(plugin_root=tmp_path, env={"OPENAI_API_KEY": "sk-oa"})
    assert cfg2.api_key == "sk-oa"


def test_load_config_from_local_file(tmp_path: Path) -> None:
    (tmp_path / "llm_config.local.json").write_text(
        json.dumps(
            {
                "api_key": "sk-file",
                "base_url": "https://example.test/v1",
                "model": "my-model",
                "timeout": 12,
            }
        ),
        encoding="utf-8",
    )
    cfg = load_llm_config(plugin_root=tmp_path, env={})
    assert cfg.api_key == "sk-file"
    assert cfg.base_url == "https://example.test/v1"
    assert cfg.model == "my-model"
    assert cfg.timeout == 12.0


def test_load_config_overrides_win(tmp_path: Path) -> None:
    (tmp_path / "llm_config.local.json").write_text(
        json.dumps({"api_key": "sk-file", "model": "file-model"}), encoding="utf-8"
    )
    cfg = load_llm_config(
        plugin_root=tmp_path,
        env={"MEMORY_LLM_API_KEY": "sk-env", "MEMORY_LLM_MODEL": "env-model"},
        overrides={"api_key": "sk-call", "model": "call-model"},
    )
    assert cfg.api_key == "sk-call"
    assert cfg.model == "call-model"


def test_load_config_invalid_json(tmp_path: Path) -> None:
    (tmp_path / "llm_config.local.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(LLMConfigError):
        load_llm_config(plugin_root=tmp_path, env={"MEMORY_LLM_API_KEY": "sk"})


def test_chat_completions_url() -> None:
    cfg = LLMConfig(api_key="x", base_url="https://api.deepseek.com/")
    assert cfg.chat_completions_url() == "https://api.deepseek.com/chat/completions"


# ── Payload building ───────────────────────────────────────────────────────


def test_build_payload_basic() -> None:
    payload = build_chat_payload(
        [{"role": "user", "content": "hello"}],
        model="m",
        temperature=0.2,
        max_tokens=128,
    )
    assert payload["model"] == "m"
    assert payload["messages"] == [{"role": "user", "content": "hello"}]
    assert payload["temperature"] == 0.2
    assert payload["max_tokens"] == 128
    assert payload["stream"] is False
    # thinking defaults to disabled (explicit, so providers like
    # deepseek-v4-flash that auto-enable reasoning are turned off)
    assert payload["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in payload


def test_build_payload_thinking_enabled_default_effort() -> None:
    payload = build_chat_payload(
        [{"role": "user", "content": "hi"}],
        model="m",
        thinking=True,
    )
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "medium"


def test_build_payload_thinking_enabled_custom_effort() -> None:
    payload = build_chat_payload(
        [{"role": "user", "content": "hi"}],
        model="m",
        thinking=True,
        reasoning_effort="high",
    )
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "high"


def test_build_payload_invalid_reasoning_effort() -> None:
    with pytest.raises(LLMConfigError):
        build_chat_payload(
            [{"role": "user", "content": "hi"}],
            model="m",
            thinking=True,
            reasoning_effort="insane",
        )


def test_build_payload_extra_overrides_thinking() -> None:
    """Power users can fully override the auto-injected reasoning fields via extra."""
    payload = build_chat_payload(
        [{"role": "user", "content": "hi"}],
        model="m",
        thinking=False,
        extra={"thinking": {"type": "enabled"}, "reasoning_effort": "low"},
    )
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "low"


def test_valid_reasoning_effort_constant() -> None:
    assert VALID_REASONING_EFFORT == {"low", "medium", "high"}


def test_build_payload_extra_does_not_override_core() -> None:
    payload = build_chat_payload(
        [{"role": "user", "content": "hi"}],
        model="m",
        extra={"model": "evil", "messages": [], "top_p": 0.9, "response_format": {"type": "json_object"}},
    )
    assert payload["model"] == "m"
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert payload["top_p"] == 0.9
    assert payload["response_format"] == {"type": "json_object"}


def test_build_payload_rejects_empty() -> None:
    with pytest.raises(LLMConfigError):
        build_chat_payload([], model="m")


def test_build_payload_rejects_invalid_message() -> None:
    with pytest.raises(LLMConfigError):
        build_chat_payload([{"role": "user"}], model="m")  # missing content


# ── Response extraction ────────────────────────────────────────────────────


def test_extract_text_string_content() -> None:
    resp = {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}
    assert extract_text(resp) == "hi"


def test_extract_text_list_content_parts() -> None:
    resp = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "hello "},
                        {"type": "text", "text": "world"},
                    ],
                }
            }
        ]
    }
    assert extract_text(resp) == "hello world"


def test_extract_text_missing_choices() -> None:
    with pytest.raises(LLMRequestError):
        extract_text({})


# ── Client behaviour with mocked transport ────────────────────────────────


def _make_client(transport, *, model: str = "m") -> LLMClient:
    cfg = LLMConfig(api_key="sk-test", base_url="https://api.test", model=model, timeout=5.0)
    return LLMClient(cfg, transport=transport)


def test_client_sends_expected_request() -> None:
    captured: dict = {}

    def fake_transport(url, headers, body, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json.loads(body.decode("utf-8"))
        captured["timeout"] = timeout
        return 200, json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": "pong"}}]}
        )

    client = _make_client(fake_transport)
    response = client.chat([{"role": "user", "content": "ping"}], temperature=0.5)

    assert captured["url"] == "https://api.test/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["body"]["model"] == "m"
    assert captured["body"]["messages"] == [{"role": "user", "content": "ping"}]
    assert captured["body"]["temperature"] == 0.5
    assert captured["body"]["stream"] is False
    # By default the client must explicitly disable thinking mode.
    assert captured["body"]["thinking"] == {"type": "disabled"}
    assert captured["timeout"] == 5.0
    assert extract_text(response) == "pong"


def test_client_thinking_enabled_request() -> None:
    captured: dict = {}

    def fake_transport(url, headers, body, timeout):
        captured["body"] = json.loads(body.decode("utf-8"))
        return 200, json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

    client = _make_client(fake_transport)
    client.chat([{"role": "user", "content": "x"}], thinking=True, reasoning_effort="high")
    assert captured["body"]["thinking"] == {"type": "enabled"}
    assert captured["body"]["reasoning_effort"] == "high"


def test_client_complete_text_helper() -> None:
    def fake_transport(url, headers, body, timeout):
        payload = json.loads(body.decode("utf-8"))
        # Echo the last user message back as the response.
        last_user = next(m for m in reversed(payload["messages"]) if m["role"] == "user")
        return 200, json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": f"echo:{last_user['content']}"}}]}
        )

    client = _make_client(fake_transport)
    text = client.complete_text("hello", system="be brief")
    assert text == "echo:hello"


def test_client_raises_on_http_error() -> None:
    def fake_transport(url, headers, body, timeout):
        return 401, json.dumps({"error": {"message": "invalid api key"}})

    client = _make_client(fake_transport)
    with pytest.raises(LLMRequestError) as excinfo:
        client.chat([{"role": "user", "content": "x"}])
    assert excinfo.value.status == 401
    assert "invalid api key" in (excinfo.value.body or "")


def test_client_raises_on_invalid_json() -> None:
    def fake_transport(url, headers, body, timeout):
        return 200, "<html>oops</html>"

    client = _make_client(fake_transport)
    with pytest.raises(LLMRequestError):
        client.chat([{"role": "user", "content": "x"}])


def test_client_uses_overridden_model() -> None:
    captured: dict = {}

    def fake_transport(url, headers, body, timeout):
        captured["body"] = json.loads(body.decode("utf-8"))
        return 200, json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

    client = _make_client(fake_transport, model="default-model")
    client.chat([{"role": "user", "content": "x"}], model="override-model")
    assert captured["body"]["model"] == "override-model"


def test_client_extra_headers_merged() -> None:
    captured: dict = {}

    def fake_transport(url, headers, body, timeout):
        captured["headers"] = headers
        return 200, json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

    cfg = LLMConfig(
        api_key="sk",
        base_url="https://api.test",
        extra_headers={"X-Trace": "abc"},
    )
    client = LLMClient(cfg, transport=fake_transport)
    client.chat([{"role": "user", "content": "x"}])
    assert captured["headers"]["X-Trace"] == "abc"
    assert captured["headers"]["Authorization"] == "Bearer sk"


# ── Live integration test (skipped by default) ─────────────────────────────


def _live_llm_available() -> bool:
    if os.environ.get("MEMORY_LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY"):
        return True
    try:
        load_llm_config()
        return True
    except LLMConfigError:
        return False


@pytest.mark.skipif(
    not _live_llm_available(),
    reason="No live LLM API key configured (set MEMORY_LLM_API_KEY / DEEPSEEK_API_KEY or fill llm_config.local.json)",
)
def test_live_chat_smoke() -> None:
    """Optional live smoke test. Enable by exporting MEMORY_LLM_API_KEY
    or filling MCP/Memory/llm_config.local.json."""
    cfg = load_llm_config()
    client = LLMClient(cfg)
    text = client.complete_text(
        "Reply with the single word: pong",
        system="You are a terse echo bot.",
        max_tokens=128,  # Account for reasoning tokens on deepseek-v4-flash
        temperature=0.0,
    )
    assert text.strip()  # non-empty


@pytest.mark.skipif(
    not _live_llm_available(),
    reason="No live LLM API key configured (set MEMORY_LLM_API_KEY / DEEPSEEK_API_KEY or fill llm_config.local.json)",
)
def test_live_chat_smoke_thinking_enabled() -> None:
    """Live test that thinking mode can be turned on and still returns content."""
    cfg = load_llm_config()
    client = LLMClient(cfg)
    response = client.chat(
        [
            {"role": "system", "content": "You are a terse echo bot."},
            {"role": "user", "content": "Reply with the single word: pong"},
        ],
        max_tokens=256,
        temperature=0.0,
        thinking=True,
        reasoning_effort="low",
    )
    text = extract_text(response)
    assert text.strip()  # non-empty even with reasoning consuming tokens


# ── Defaults sanity ────────────────────────────────────────────────────────


def test_defaults_match_deepseek() -> None:
    assert DEFAULT_BASE_URL == "https://api.deepseek.com"
    assert DEFAULT_MODEL == "deepseek-chat"
    # Module re-exports must resolve to the same objects.
    assert memory_llm.LLMClient is LLMClient


# ── Cost controls / budget / usage tracking ────────────────────────────────


def _ok_response(prompt_tokens: int = 5, completion_tokens: int = 7) -> str:
    return json.dumps(
        {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
    )


def test_default_budget_constants_are_positive() -> None:
    assert DEFAULT_MAX_OUTPUT_TOKENS > 0
    assert DEFAULT_MAX_TOTAL_OUTPUT_TOKENS > 0


def test_load_budget_settings_from_env(tmp_path: Path) -> None:
    cfg = load_llm_config(
        plugin_root=tmp_path,
        env={
            "MEMORY_LLM_API_KEY": "sk",
            "MEMORY_LLM_MAX_OUTPUT_TOKENS": "256",
            "MEMORY_LLM_MAX_TOTAL_OUTPUT_TOKENS": "10000",
            "MEMORY_LLM_DEFAULT_THINKING": "true",
            "MEMORY_LLM_DEFAULT_REASONING_EFFORT": "high",
        },
    )
    assert cfg.max_output_tokens_per_call == 256
    assert cfg.max_total_output_tokens == 10000
    assert cfg.default_thinking is True
    assert cfg.default_reasoning_effort == "high"


def test_load_invalid_default_reasoning_effort(tmp_path: Path) -> None:
    with pytest.raises(LLMConfigError):
        load_llm_config(
            plugin_root=tmp_path,
            env={"MEMORY_LLM_API_KEY": "sk", "MEMORY_LLM_DEFAULT_REASONING_EFFORT": "wild"},
        )


def test_max_tokens_clamped_to_per_call_cap() -> None:
    captured: dict = {}

    def fake_transport(url, headers, body, timeout):
        captured["body"] = json.loads(body.decode("utf-8"))
        return 200, _ok_response()

    cfg = LLMConfig(api_key="sk", base_url="https://api.test", max_output_tokens_per_call=64)
    client = LLMClient(cfg, transport=fake_transport)
    client.chat([{"role": "user", "content": "x"}], max_tokens=10_000)
    assert captured["body"]["max_tokens"] == 64


def test_max_tokens_defaults_to_cap_when_omitted() -> None:
    captured: dict = {}

    def fake_transport(url, headers, body, timeout):
        captured["body"] = json.loads(body.decode("utf-8"))
        return 200, _ok_response()

    cfg = LLMConfig(api_key="sk", base_url="https://api.test", max_output_tokens_per_call=512)
    client = LLMClient(cfg, transport=fake_transport)
    client.chat([{"role": "user", "content": "x"}])
    assert captured["body"]["max_tokens"] == 512


def test_usage_tracking_accumulates() -> None:
    def fake_transport(url, headers, body, timeout):
        return 200, _ok_response(prompt_tokens=10, completion_tokens=20)

    cfg = LLMConfig(api_key="sk", base_url="https://api.test")
    client = LLMClient(cfg, transport=fake_transport)
    client.chat([{"role": "user", "content": "a"}])
    client.chat([{"role": "user", "content": "b"}])
    snap = client.usage_snapshot()
    assert snap["call_count"] == 2
    assert snap["total_prompt_tokens"] == 20
    assert snap["total_completion_tokens"] == 40
    assert snap["total_tokens"] == 60
    client.reset_usage()
    assert client.usage_snapshot()["call_count"] == 0


def test_total_budget_blocks_after_exceeded() -> None:
    def fake_transport(url, headers, body, timeout):
        return 200, _ok_response(prompt_tokens=1, completion_tokens=100)

    cfg = LLMConfig(
        api_key="sk",
        base_url="https://api.test",
        max_output_tokens_per_call=128,
        max_total_output_tokens=50,
    )
    client = LLMClient(cfg, transport=fake_transport)
    # First call goes through but pushes us past the budget.
    client.chat([{"role": "user", "content": "x"}])
    assert client.total_completion_tokens == 100
    # Second call must be refused before hitting the network.
    with pytest.raises(LLMBudgetExceeded):
        client.chat([{"role": "user", "content": "y"}])


def test_total_budget_zero_disables_check() -> None:
    def fake_transport(url, headers, body, timeout):
        return 200, _ok_response(prompt_tokens=0, completion_tokens=999_999)

    cfg = LLMConfig(api_key="sk", base_url="https://api.test", max_total_output_tokens=0)
    client = LLMClient(cfg, transport=fake_transport)
    client.chat([{"role": "user", "content": "a"}])
    # No exception even after far exceeding ordinary budgets.
    client.chat([{"role": "user", "content": "b"}])
    assert client.call_count == 2


def test_default_thinking_from_config_applies_when_unspecified() -> None:
    captured: dict = {}

    def fake_transport(url, headers, body, timeout):
        captured["body"] = json.loads(body.decode("utf-8"))
        return 200, _ok_response()

    cfg = LLMConfig(
        api_key="sk",
        base_url="https://api.test",
        default_thinking=True,
        default_reasoning_effort="low",
    )
    client = LLMClient(cfg, transport=fake_transport)
    client.chat([{"role": "user", "content": "x"}])
    assert captured["body"]["thinking"] == {"type": "enabled"}
    assert captured["body"]["reasoning_effort"] == "low"


def test_per_call_thinking_overrides_default() -> None:
    captured: dict = {}

    def fake_transport(url, headers, body, timeout):
        captured["body"] = json.loads(body.decode("utf-8"))
        return 200, _ok_response()

    cfg = LLMConfig(
        api_key="sk",
        base_url="https://api.test",
        default_thinking=True,
        default_reasoning_effort="high",
    )
    client = LLMClient(cfg, transport=fake_transport)
    client.chat([{"role": "user", "content": "x"}], thinking=False)
    assert captured["body"]["thinking"] == {"type": "disabled"}


# ── Raw-immutable / autonomous distillation primitives ───────────────────


def test_make_raw_record_marks_immutable_and_authoritative() -> None:
    rec = make_raw_record(
        record_id="raw-001",
        content="ue_actions_run returned ok=True",
        source="ue_editor_mcp",
        captured_at="2026-04-25T10:00:00Z",
        author="agent",
    )
    assert rec["provenance"] == PROVENANCE_RAW
    assert rec["immutable"] is True
    assert rec["authoritative"] is True
    assert rec["status"] == "raw"
    assert rec["author"] == "agent"
    assert rec["content"].startswith("ue_actions_run")


def test_make_raw_record_rejects_reserved_meta_override() -> None:
    with pytest.raises(RawImmutableError):
        make_raw_record(
            record_id="raw-002",
            content="x",
            source="src",
            captured_at="t",
            extra_meta={"immutable": False},
        )


def test_make_raw_record_requires_core_fields() -> None:
    with pytest.raises(LLMConfigError):
        make_raw_record(record_id="", content="x", source="s", captured_at="t")
    with pytest.raises(LLMConfigError):
        make_raw_record(record_id="r", content="x", source="", captured_at="t")
    with pytest.raises(LLMConfigError):
        make_raw_record(record_id="r", content="x", source="s", captured_at="")


def test_assert_raw_writable_blocks_raw() -> None:
    raw = make_raw_record(record_id="r1", content="x", source="s", captured_at="t")
    with pytest.raises(RawImmutableError):
        assert_raw_writable(raw)


def test_assert_raw_writable_passes_distilled_and_none() -> None:
    distilled = make_distilled_record(
        record_id="d1",
        content="summary",
        derived_from=["r1"],
        model="deepseek-v4-flash",
        distilled_at="t",
    )
    # Should not raise.
    assert_raw_writable(distilled)
    assert_raw_writable(None)


def test_make_distilled_record_marks_replaceable_and_traces_raw() -> None:
    rec = make_distilled_record(
        record_id="d1",
        content="materials pipeline summary",
        derived_from=["raw-001", "raw-002"],
        model="deepseek-v4-flash",
        distilled_at="2026-04-25T10:05:00Z",
        confidence=0.7,
        tags=["material", "asset_pipeline", "material"],  # dup intentionally
    )
    assert rec["provenance"] == PROVENANCE_LLM
    assert rec["immutable"] is False
    assert rec["authoritative"] is False
    assert rec["replaceable"] is True
    assert rec["status"] == "distilled"
    assert rec["derived_from"] == ["raw-001", "raw-002"]
    assert rec["model"] == "deepseek-v4-flash"
    assert rec["confidence"] == 0.7
    # Tags are sorted-dedup.
    assert rec["tags"] == ["asset_pipeline", "material"]


def test_make_distilled_record_requires_derived_from() -> None:
    """Distillation must always trace back to the immutable raw layer."""
    with pytest.raises(LLMConfigError):
        make_distilled_record(
            record_id="d1",
            content="x",
            derived_from=[],
            model="m",
            distilled_at="t",
        )


def test_make_distilled_record_clamps_confidence() -> None:
    rec = make_distilled_record(
        record_id="d1",
        content="x",
        derived_from=["r1"],
        model="m",
        distilled_at="t",
        confidence=2.5,
    )
    assert rec["confidence"] == 1.0
    rec2 = make_distilled_record(
        record_id="d2",
        content="x",
        derived_from=["r1"],
        model="m",
        distilled_at="t",
        confidence=-3,
    )
    assert rec2["confidence"] == 0.0


def test_supersede_distilled_replaces_with_new_pass() -> None:
    """Different LLMs can rewrite distilled records freely; raw stays put."""
    prev = make_distilled_record(
        record_id="d1",
        content="v1 summary",
        derived_from=["raw-001", "raw-002"],
        model="deepseek-v4-flash",
        distilled_at="2026-04-25T10:00:00Z",
        confidence=0.6,
    )
    new = supersede_distilled(
        previous=prev,
        new_content="v2 better summary",
        model="deepseek-v4-pro",
        distilled_at="2026-04-25T11:00:00Z",
        confidence=0.85,
    )
    assert new["id"] == "d1"
    assert new["model"] == "deepseek-v4-pro"
    assert new["content"] == "v2 better summary"
    # Lineage to raw layer is preserved across LLM substitutions.
    assert new["derived_from"] == ["raw-001", "raw-002"]
    assert new["confidence"] == 0.85
    assert new["supersedes"] == "d1"


def test_supersede_distilled_refuses_raw() -> None:
    raw = make_raw_record(record_id="r1", content="x", source="s", captured_at="t")
    with pytest.raises(RawImmutableError):
        supersede_distilled(
            previous=raw,
            new_content="should not be allowed",
            model="m",
            distilled_at="t",
        )


def test_distilled_record_can_be_rebuilt_by_any_llm() -> None:
    """End-to-end: raw is captured once, multiple LLMs rebuild distilled views.

    This test encodes the autonomous-distillation contract: no human gate, but
    the raw layer is never mutated, so any number of LLMs can independently
    derive (and overwrite) the distilled summary while remaining auditable.
    """
    raw = make_raw_record(
        record_id="raw-100",
        content="user said: please open the editor",
        source="chat",
        captured_at="2026-04-25T12:00:00Z",
    )
    summary_a = make_distilled_record(
        record_id="d-100",
        content="open editor",
        derived_from=[raw["id"]],
        model="llm-a",
        distilled_at="2026-04-25T12:00:01Z",
    )
    summary_b = supersede_distilled(
        previous=summary_a,
        new_content="open the editor",
        model="llm-b",
        distilled_at="2026-04-25T12:00:02Z",
    )
    # Raw record is untouched: byte-for-byte equal to the original capture.
    assert raw == make_raw_record(
        record_id="raw-100",
        content="user said: please open the editor",
        source="chat",
        captured_at="2026-04-25T12:00:00Z",
    )
    # Distilled chain still grounds in the raw record.
    assert summary_a["derived_from"] == ["raw-100"]
    assert summary_b["derived_from"] == ["raw-100"]
    # No human validation gate: status is `distilled`, not `validated/published`.
    assert summary_a["status"] == "distilled"
    assert summary_b["status"] == "distilled"


# ── Input-side cap, cost tracking, distill bridge ────────────────────────


def _summary_response(text: str = "summary text", prompt_tokens: int = 100, completion_tokens: int = 20) -> str:
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


def test_default_input_cap_and_pricing_constants() -> None:
    assert DEFAULT_MAX_INPUT_TOKENS_PER_CALL > 0
    # Defaults match deepseek-v4-flash sticker price.
    assert DEFAULT_INPUT_CNY_PER_MTOK == 1.0
    assert DEFAULT_OUTPUT_CNY_PER_MTOK == 2.0


def test_estimate_prompt_tokens_counts_each_message() -> None:
    msgs = [
        {"role": "system", "content": "be concise"},
        {"role": "user", "content": "hello world"},
    ]
    est = LLMClient.estimate_prompt_tokens(msgs)
    # Includes per-message overhead (~4 each), so non-zero even for short text.
    assert est >= 8


def test_input_cap_blocks_oversized_prompt(tmp_path: Path) -> None:
    cfg = LLMConfig(
        api_key="sk",
        base_url="https://api.test",
        max_input_tokens_per_call=5,  # tiny on purpose
    )

    def fake_transport(url, headers, body, timeout):  # pragma: no cover — must NOT be called
        raise AssertionError("transport must not be invoked when input cap is hit")

    client = LLMClient(cfg, transport=fake_transport)
    with pytest.raises(LLMInputTooLarge):
        client.chat([{"role": "user", "content": "this prompt is way too long for the cap"}])
    # No call recorded since we short-circuited before sending.
    assert client.call_count == 0


def test_input_cap_zero_disables_check() -> None:
    captured: dict = {}

    def fake_transport(url, headers, body, timeout):
        captured["called"] = True
        return 200, _ok_response()

    cfg = LLMConfig(api_key="sk", base_url="https://api.test", max_input_tokens_per_call=0)
    client = LLMClient(cfg, transport=fake_transport)
    client.chat([{"role": "user", "content": "x" * 10_000}])
    assert captured.get("called") is True


def test_cost_accounting_uses_pricing_from_config() -> None:
    def fake_transport(url, headers, body, timeout):
        return 200, _summary_response(prompt_tokens=1_000_000, completion_tokens=500_000)

    cfg = LLMConfig(
        api_key="sk",
        base_url="https://api.test",
        input_cny_per_mtok=1.0,
        output_cny_per_mtok=2.0,
    )
    client = LLMClient(cfg, transport=fake_transport)
    client.chat([{"role": "user", "content": "x"}])
    snap = client.usage_snapshot()
    # 1M input * 1 + 0.5M output * 2 = 2.0 CNY
    assert snap["total_estimated_cost_cny"] == pytest.approx(2.0)
    assert snap["total_prompt_tokens"] == 1_000_000
    assert snap["total_completion_tokens"] == 500_000


def test_total_cost_budget_blocks_after_exceeded() -> None:
    def fake_transport(url, headers, body, timeout):
        return 200, _summary_response(prompt_tokens=1_000_000, completion_tokens=0)

    cfg = LLMConfig(
        api_key="sk",
        base_url="https://api.test",
        input_cny_per_mtok=1.0,
        output_cny_per_mtok=2.0,
        max_total_cost_cny=1.5,
    )
    client = LLMClient(cfg, transport=fake_transport)
    # First call costs 1.0 CNY → under budget, allowed.
    client.chat([{"role": "user", "content": "x"}])
    # Cumulative cost (1.0) is now under the 1.5 budget but next call would push
    # over. The pre-check fires once cumulative cost >= budget, so do another
    # call to push us over.
    client.chat([{"role": "user", "content": "y"}])
    # Now cumulative is 2.0 ≥ 1.5; third call must be blocked pre-flight.
    with pytest.raises(LLMBudgetExceeded):
        client.chat([{"role": "user", "content": "z"}])


def test_load_config_reads_input_cap_and_pricing(tmp_path: Path) -> None:
    cfg = load_llm_config(
        plugin_root=tmp_path,
        env={
            "MEMORY_LLM_API_KEY": "sk",
            "MEMORY_LLM_MAX_INPUT_TOKENS": "8000",
            "MEMORY_LLM_INPUT_CNY_PER_MTOK": "12",
            "MEMORY_LLM_OUTPUT_CNY_PER_MTOK": "24",
            "MEMORY_LLM_MAX_TOTAL_COST_CNY": "0.5",
        },
    )
    assert cfg.max_input_tokens_per_call == 8000
    assert cfg.input_cny_per_mtok == 12.0
    assert cfg.output_cny_per_mtok == 24.0
    assert cfg.max_total_cost_cny == 0.5


def test_distill_raw_records_builds_distilled_with_lineage() -> None:
    captured: dict = {}

    def fake_transport(url, headers, body, timeout):
        captured["body"] = json.loads(body.decode("utf-8"))
        return 200, _summary_response("ue_actions_run completed twice")

    cfg = LLMConfig(api_key="sk", base_url="https://api.test")
    client = LLMClient(cfg, transport=fake_transport)
    raws = [
        make_raw_record(
            record_id="r-1", content="ue_actions_run ok=True (1)",
            source="ue_mcp", captured_at="2026-04-25T10:00:00Z",
        ),
        make_raw_record(
            record_id="r-2", content="ue_actions_run ok=True (2)",
            source="ue_mcp", captured_at="2026-04-25T10:01:00Z",
        ),
    ]
    distilled = distill_raw_records(
        client, raws,
        record_id="d-1",
        distilled_at="2026-04-25T10:02:00Z",
        tags=["ue", "mcp"],
        confidence=0.8,
    )
    assert distilled["status"] == "distilled"
    assert distilled["derived_from"] == ["r-1", "r-2"]
    assert distilled["content"] == "ue_actions_run completed twice"
    assert distilled["model"] == cfg.model
    assert distilled["confidence"] == 0.8
    # System prompt is the autonomous-distillation default.
    assert captured["body"]["messages"][0]["content"] == DEFAULT_DISTILL_SYSTEM_PROMPT
    # Both raw bodies were embedded in the user message.
    user_msg = captured["body"]["messages"][1]["content"]
    assert "r-1" in user_msg and "r-2" in user_msg
    assert "ue_actions_run ok=True (1)" in user_msg
    assert "ue_actions_run ok=True (2)" in user_msg


def test_distill_raw_records_refuses_distilled_input() -> None:
    cfg = LLMConfig(api_key="sk", base_url="https://api.test")
    client = LLMClient(cfg, transport=lambda *a, **k: (200, _summary_response()))
    raw = make_raw_record(
        record_id="r-1", content="x", source="s", captured_at="t",
    )
    distilled = make_distilled_record(
        record_id="d-1", content="prev", derived_from=["r-1"],
        model="m", distilled_at="t",
    )
    # Mixing distilled into raw input set is rejected.
    with pytest.raises(LLMConfigError):
        distill_raw_records(
            client, [raw, distilled],
            record_id="d-2", distilled_at="t",
        )


def test_distill_raw_records_requires_non_empty_input() -> None:
    cfg = LLMConfig(api_key="sk", base_url="https://api.test")
    client = LLMClient(cfg, transport=lambda *a, **k: (200, _summary_response()))
    with pytest.raises(LLMConfigError):
        distill_raw_records(client, [], record_id="d-1", distilled_at="t")


def test_distill_raw_records_propagates_input_cap() -> None:
    cfg = LLMConfig(
        api_key="sk", base_url="https://api.test",
        max_input_tokens_per_call=5,
    )
    client = LLMClient(cfg, transport=lambda *a, **k: (200, _summary_response()))
    raw = make_raw_record(
        record_id="r-1",
        content="this raw record is intentionally long to trigger the cap " * 50,
        source="s", captured_at="t",
    )
    with pytest.raises(LLMInputTooLarge):
        distill_raw_records(client, [raw], record_id="d-1", distilled_at="t")


def test_distill_raw_records_raises_on_empty_completion() -> None:
    def fake_transport(url, headers, body, timeout):
        return 200, _summary_response(text="   ")

    cfg = LLMConfig(api_key="sk", base_url="https://api.test")
    client = LLMClient(cfg, transport=fake_transport)
    raw = make_raw_record(
        record_id="r-1", content="x", source="s", captured_at="t",
    )
    with pytest.raises(LLMRequestError):
        distill_raw_records(client, [raw], record_id="d-1", distilled_at="t")


# ── Regression: http.client.HTTPException must be wrapped as LLMRequestError ──


def test_http_post_wraps_remote_disconnected_as_llm_request_error(monkeypatch) -> None:
    """``http.client.RemoteDisconnected`` is NOT a subclass of ``URLError`` —
    historically it leaked out of ``_http_post`` as a raw exception, causing
    the runner to misclassify it as ``unexpected: ...``.  Ensure it is now
    wrapped as :class:`LLMRequestError` (network branch).
    """
    import http.client
    from servers.memory_server import memory_llm as me

    def boom(*args, **kwargs):
        raise http.client.RemoteDisconnected("Remote end closed connection without response")

    monkeypatch.setattr(me.urllib.request, "urlopen", boom)
    with pytest.raises(LLMRequestError, match="network error"):
        me._http_post("https://api.test/x", {}, b"{}", timeout=1.0)


def test_http_post_wraps_connection_reset_error(monkeypatch) -> None:
    from servers.memory_server import memory_llm as me

    def boom(*args, **kwargs):
        raise ConnectionResetError("connection reset by peer")

    monkeypatch.setattr(me.urllib.request, "urlopen", boom)
    with pytest.raises(LLMRequestError, match="network error"):
        me._http_post("https://api.test/x", {}, b"{}", timeout=1.0)


# ── 5xx auto-retry policy ─────────────────────────────────────────────


def _ok_response_body() -> str:
    return _summary_response()


def test_chat_retries_on_5xx_then_succeeds() -> None:
    """A 503 followed by a 200 must result in a single successful call
    with ``retry_count == 1`` and no exception raised.
    """
    calls: list[int] = []
    def transport(url, headers, body, timeout):
        calls.append(1)
        if len(calls) == 1:
            return 503, "service unavailable"
        return 200, _ok_response_body()

    cfg = LLMConfig(api_key="sk", base_url="https://api.test",
                    max_retries=2, retry_backoff_seconds=0.0)
    client = LLMClient(cfg, transport=transport)
    out = client.chat([{"role": "user", "content": "ping"}])
    assert out["choices"][0]["message"]["content"]
    assert client.retry_count == 1
    assert client.last_retry_reason and "503" in client.last_retry_reason


def test_chat_retries_on_network_error_then_succeeds() -> None:
    """A network error followed by a 200 must succeed with retry_count==1."""
    from servers.memory_server.memory_llm import LLMRequestError as _LRE
    calls: list[int] = []
    def transport(url, headers, body, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise _LRE("network error contacting LLM: timeout")
        return 200, _ok_response_body()

    cfg = LLMConfig(api_key="sk", base_url="https://api.test",
                    max_retries=1, retry_backoff_seconds=0.0)
    client = LLMClient(cfg, transport=transport)
    out = client.chat([{"role": "user", "content": "ping"}])
    assert out["choices"][0]["message"]["content"]
    assert client.retry_count == 1


def test_chat_does_not_retry_on_4xx() -> None:
    """4xx must surface as :class:`LLMRequestError` immediately (no retry)."""
    calls: list[int] = []
    def transport(url, headers, body, timeout):
        calls.append(1)
        return 401, "auth failed"

    cfg = LLMConfig(api_key="sk", base_url="https://api.test",
                    max_retries=3, retry_backoff_seconds=0.0)
    client = LLMClient(cfg, transport=transport)
    with pytest.raises(LLMRequestError, match="HTTP 401"):
        client.chat([{"role": "user", "content": "ping"}])
    assert len(calls) == 1
    assert client.retry_count == 0


def test_chat_exhausts_retries_then_raises() -> None:
    """Persistent 500 with max_retries=2 → 3 calls then raise."""
    calls: list[int] = []
    def transport(url, headers, body, timeout):
        calls.append(1)
        return 500, "boom"

    cfg = LLMConfig(api_key="sk", base_url="https://api.test",
                    max_retries=2, retry_backoff_seconds=0.0)
    client = LLMClient(cfg, transport=transport)
    with pytest.raises(LLMRequestError, match="HTTP 500"):
        client.chat([{"role": "user", "content": "ping"}])
    assert len(calls) == 3
    assert client.retry_count == 2
