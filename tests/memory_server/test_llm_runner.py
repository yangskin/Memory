"""Tests for the v0.10.0 unified LLM capability runner.

Covers profile resolution precedence (cap_overrides > global > built-in),
the seven status codes (ok / disabled / unavailable / timeout / budget /
failed / invalid), fallback bookkeeping, and the policy guard rejecting
non-LLM capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from servers.memory_server.memory_llm import LLMBudgetExceeded, LLMConfigError, LLMRequestError
from servers.memory_server.memory_llm_runner import (
    DEFAULT_CAPABILITY_PROFILES,
    LLMRunResult,
    STATUS_BUDGET,
    STATUS_DISABLED,
    STATUS_FAILED,
    STATUS_INVALID,
    STATUS_OK,
    STATUS_TIMEOUT,
    STATUS_UNAVAILABLE,
    resolve_capability_profile,
    run_llm_capability,
)


# ── lightweight stand-ins ────────────────────────────────────────────────


@dataclass
class _StubConfig:
    """Just enough of MemoryConfig for the runner."""

    llm_defaults: dict | None = None


class _StubClient:
    """Object the runner hands to user callables; opaque marker."""

    pass


def _enabled_factory(_profile):
    return _StubClient()


def _err_factory(exc):
    def _factory(_profile):
        raise exc

    return _factory


# ── profile resolution ──────────────────────────────────────────────────


def test_default_profiles_cover_all_v010_capabilities() -> None:
    expected = {
        "distill_summary",
        "summarize_recall",
        "rebuild_key_document",
        "guard_compaction",
        "auto_memory_gate",
        "query_rewrite",
        "snapshot_narrative",
    }
    assert expected.issubset(DEFAULT_CAPABILITY_PROFILES.keys())
    for cap in expected:
        # Hard requirement: every default ships disabled so no capability
        # secretly opts the user into LLM spend.
        assert DEFAULT_CAPABILITY_PROFILES[cap].enabled is False


def test_resolve_uses_built_in_when_no_overrides() -> None:
    config = _StubConfig(llm_defaults=None)
    profile = resolve_capability_profile(config, "query_rewrite")
    assert profile == DEFAULT_CAPABILITY_PROFILES["query_rewrite"]


def test_resolve_global_enabled_propagates() -> None:
    config = _StubConfig(llm_defaults={"enabled": True, "timeout": 5})
    profile = resolve_capability_profile(config, "query_rewrite")
    assert profile.enabled is True
    assert profile.timeout == 5


def test_resolve_capability_override_beats_global() -> None:
    config = _StubConfig(
        llm_defaults={
            "enabled": True,
            "timeout": 5,
            "capabilities": {"query_rewrite": {"enabled": False}},
        }
    )
    profile = resolve_capability_profile(config, "query_rewrite")
    assert profile.enabled is False  # cap override wins
    assert profile.timeout == 5  # global timeout still inherited


def test_resolve_unknown_capability_falls_through_to_global() -> None:
    config = _StubConfig(llm_defaults={"enabled": True})
    profile = resolve_capability_profile(config, "no_such_capability")
    # The resolver itself is pure: globals propagate.  The actual rejection
    # of unknown capabilities happens inside :func:`run_llm_capability`
    # via the policy matrix (see ``test_invalid_capability_rejected_by_policy``).
    assert profile.enabled is True


# ── runner status matrix ────────────────────────────────────────────────


def test_invalid_capability_rejected_by_policy() -> None:
    config = _StubConfig()
    result = run_llm_capability(
        config,
        "memory_get",  # registered as non_llm
        lambda _client, _profile: "should not run",
        client_factory=_enabled_factory,
    )
    assert isinstance(result, LLMRunResult)
    assert result.ok is False
    assert result.status == STATUS_INVALID


def test_disabled_capability_short_circuits() -> None:
    config = _StubConfig(llm_defaults=None)  # all defaults are disabled
    result = run_llm_capability(
        config,
        "query_rewrite",
        lambda _client, _profile: "should not run",
        client_factory=_enabled_factory,
    )
    assert result.ok is False
    assert result.status == STATUS_DISABLED


def test_unavailable_when_client_factory_rejects() -> None:
    config = _StubConfig(llm_defaults={"enabled": True})
    result = run_llm_capability(
        config,
        "query_rewrite",
        lambda _client, _profile: "unreachable",
        client_factory=_err_factory(LLMConfigError("no api key")),
    )
    assert result.ok is False
    assert result.status == STATUS_UNAVAILABLE
    assert "no api key" in (result.error or "")


def test_timeout_classification_from_request_error() -> None:
    config = _StubConfig(llm_defaults={"enabled": True})

    def _explode(_client, _profile):
        raise LLMRequestError("urlopen timed out after 60s")

    result = run_llm_capability(
        config,
        "query_rewrite",
        _explode,
        client_factory=_enabled_factory,
    )
    assert result.ok is False
    assert result.status == STATUS_TIMEOUT


def test_generic_request_error_maps_to_failed() -> None:
    config = _StubConfig(llm_defaults={"enabled": True})

    def _explode(_client, _profile):
        raise LLMRequestError("HTTP 500 from upstream")

    result = run_llm_capability(
        config,
        "query_rewrite",
        _explode,
        client_factory=_enabled_factory,
    )
    assert result.ok is False
    assert result.status == STATUS_FAILED


def test_budget_exceeded_maps_to_budget_status() -> None:
    config = _StubConfig(llm_defaults={"enabled": True})

    def _explode(_client, _profile):
        raise LLMBudgetExceeded("cumulative cap reached")

    result = run_llm_capability(
        config,
        "query_rewrite",
        _explode,
        client_factory=_enabled_factory,
    )
    assert result.ok is False
    assert result.status == STATUS_BUDGET


def test_ok_path_returns_value_unchanged() -> None:
    config = _StubConfig(llm_defaults={"enabled": True})
    payload = {"variants": ["alt query"]}
    result = run_llm_capability(
        config,
        "query_rewrite",
        lambda _client, _profile: payload,
        client_factory=_enabled_factory,
    )
    assert result.ok is True
    assert result.status == STATUS_OK
    assert result.value is payload
    assert result.fallback_used is False


# ── fallback bookkeeping ────────────────────────────────────────────────


def test_fallback_marks_ok_but_preserves_failure_status() -> None:
    config = _StubConfig(llm_defaults={"enabled": True})

    def _explode(_client, _profile):
        raise LLMRequestError("HTTP 503")

    result = run_llm_capability(
        config,
        "query_rewrite",
        _explode,
        client_factory=_enabled_factory,
        fallback=lambda: {"variants": [], "fallback": True},
    )
    # Caller-visible: succeeded (we have a value to return).
    assert result.ok is True
    # Diagnostic: the original failure reason is still observable.
    assert result.status == STATUS_FAILED
    assert result.fallback_used is True
    assert result.value == {"variants": [], "fallback": True}


def test_fallback_skipped_when_disabled_and_no_callable() -> None:
    config = _StubConfig(llm_defaults=None)
    result = run_llm_capability(
        config,
        "query_rewrite",
        lambda _client, _profile: "unreachable",
        client_factory=_enabled_factory,
    )
    assert result.ok is False
    assert result.status == STATUS_DISABLED
    assert result.fallback_used is False


def test_to_dict_round_trip() -> None:
    result = LLMRunResult(
        ok=True,
        status=STATUS_OK,
        capability="query_rewrite",
        value={"variants": []},
    )
    payload = result.to_dict()
    assert payload["ok"] is True
    assert payload["status"] == STATUS_OK
    assert payload["capability"] == "query_rewrite"


# ── llm_capability_invoked event emission ──────────────────────────────


def test_run_llm_capability_emits_event_on_success(monkeypatch) -> None:
    """A successful capability run must append exactly one
    ``llm_capability_invoked`` event with status=ok."""
    captured: list[dict] = []

    def fake_append_event(config, event_type, payload, status="ok"):
        captured.append({"event_type": event_type, "payload": payload, "status": status})

    monkeypatch.setattr(
        "servers.memory_server.memory_events.append_event",
        fake_append_event,
    )

    cfg = _StubConfig()
    result = run_llm_capability(
        cfg,
        "query_rewrite",
        lambda client, profile: "summary text",
        client_factory=_enabled_factory,
        force_enabled=True,
    )
    assert result.status == STATUS_OK
    assert len(captured) == 1
    evt = captured[0]
    assert evt["event_type"] == "llm_capability_invoked"
    assert evt["status"] == "ok"
    assert evt["payload"]["capability"] == "query_rewrite"
    assert evt["payload"]["status"] == STATUS_OK
    assert evt["payload"]["fallback_used"] is False
    assert "latency_ms" in evt["payload"]


def test_run_llm_capability_emits_event_on_failure_with_fallback(monkeypatch) -> None:
    """Failure with fallback: result.ok is True (fallback used), but
    event must report status='ok' (caller succeeded) with
    fallback_used=True and the original error string."""
    captured: list[dict] = []
    monkeypatch.setattr(
        "servers.memory_server.memory_events.append_event",
        lambda config, event_type, payload, status="ok": captured.append(
            {"event_type": event_type, "payload": payload, "status": status}
        ),
    )

    def _raises(_client, _profile):
        raise LLMRequestError("HTTP 503: down")

    cfg = _StubConfig()
    result = run_llm_capability(
        cfg,
        "query_rewrite",
        _raises,
        fallback=lambda: "deterministic",
        client_factory=_enabled_factory,
        force_enabled=True,
    )
    assert result.fallback_used is True
    assert len(captured) == 1
    p = captured[0]["payload"]
    assert p["fallback_used"] is True
    assert p["status"] == STATUS_FAILED
    assert "503" in (p.get("error") or "")


def test_run_llm_capability_event_failure_does_not_propagate(monkeypatch) -> None:
    """If ``append_event`` raises, the LLM result must still be returned."""
    def boom(*_args, **_kwargs):
        raise RuntimeError("audit log unavailable")

    monkeypatch.setattr(
        "servers.memory_server.memory_events.append_event",
        boom,
    )
    cfg = _StubConfig()
    result = run_llm_capability(
        cfg,
        "query_rewrite",
        lambda client, profile: "x",
        client_factory=_enabled_factory,
        force_enabled=True,
    )
    assert result.status == STATUS_OK
    assert result.value == "x"
