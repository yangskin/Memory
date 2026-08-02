"""Tests for the shared budget primitives extracted in P1-D."""

from __future__ import annotations

from servers.memory_server.memory_budget import (
    IMPORTANT_MEMORY_DEFAULT_MAX_CHARS,
    IMPORTANT_MEMORY_DEFAULT_MAX_ITEMS,
    IMPORTANT_MEMORY_DEFAULT_MAX_TOKENS,
    fit_text_to_budget,
    validate_budget_inputs,
)


def test_validate_budget_inputs_accepts_none():
    assert validate_budget_inputs(max_chars=None, max_tokens=None, max_items=None) is None


def test_validate_budget_inputs_accepts_zero_chars_and_tokens():
    # Zero is allowed (means "no body"), only negative is invalid.
    assert validate_budget_inputs(max_chars=0, max_tokens=0, max_items=1) is None


def test_validate_budget_inputs_rejects_negative_chars():
    err = validate_budget_inputs(max_chars=-1, max_tokens=None, max_items=None)
    assert err is not None and err["error"] == "invalid_input"


def test_validate_budget_inputs_rejects_negative_tokens():
    err = validate_budget_inputs(max_chars=None, max_tokens=-1, max_items=None)
    assert err is not None and err["error"] == "invalid_input"


def test_validate_budget_inputs_rejects_zero_items():
    err = validate_budget_inputs(max_chars=None, max_tokens=None, max_items=0)
    assert err is not None and err["error"] == "invalid_input"


def test_fit_text_to_budget_no_limits_passes_through():
    text, degraded = fit_text_to_budget("hello world", remaining_chars=None, remaining_tokens=None)
    assert text == "hello world"
    assert degraded is False


def test_fit_text_to_budget_empty_input():
    text, degraded = fit_text_to_budget("", remaining_chars=100, remaining_tokens=100)
    assert text == ""
    assert degraded is False


def test_fit_text_to_budget_zero_chars_is_degraded():
    text, degraded = fit_text_to_budget("hi", remaining_chars=0, remaining_tokens=None)
    assert text == ""
    assert degraded is True


def test_fit_text_to_budget_truncates_chars():
    long = "abcdefghij" * 5
    text, degraded = fit_text_to_budget(long, remaining_chars=12, remaining_tokens=None)
    assert degraded is True
    # Truncation may be slightly over the budget when an ellipsis is appended,
    # but it must always be shorter than the original.
    assert len(text) < len(long)


def test_fit_text_to_budget_truncates_tokens():
    from servers.memory_server.token_estimator import estimate_tokens

    long = " ".join(["word"] * 200)
    text, degraded = fit_text_to_budget(long, remaining_chars=None, remaining_tokens=10)
    assert degraded is True
    assert estimate_tokens(text) <= 10 or text == ""
    assert len(text) < len(long)


def test_default_constants_sane():
    assert IMPORTANT_MEMORY_DEFAULT_MAX_ITEMS >= 1
    assert IMPORTANT_MEMORY_DEFAULT_MAX_CHARS > IMPORTANT_MEMORY_DEFAULT_MAX_ITEMS
    assert IMPORTANT_MEMORY_DEFAULT_MAX_TOKENS > 0


def test_back_compat_aliases_in_memory_retrieval():
    """memory_retrieval still re-exports the same names for old call sites."""
    from servers.memory_server import memory_retrieval as r

    assert r.IMPORTANT_MEMORY_DEFAULT_MAX_ITEMS == IMPORTANT_MEMORY_DEFAULT_MAX_ITEMS
    assert r.IMPORTANT_MEMORY_DEFAULT_MAX_CHARS == IMPORTANT_MEMORY_DEFAULT_MAX_CHARS
    # Aliased function is the same callable.
    assert r._validate_budget_inputs is validate_budget_inputs
    assert r._fit_text_to_budget is fit_text_to_budget
