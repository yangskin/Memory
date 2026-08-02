"""Shared budget helpers for retrieval / important-memory packing.

Extracted from `memory_retrieval.py` (P1-D) so the same primitive logic can
be reused without dragging in the rest of the retrieval pipeline.

Public surface (kept minimal on purpose):
- Constants: ``IMPORTANT_MEMORY_DEFAULT_MAX_ITEMS``, ``..._MAX_CHARS``,
  ``..._MAX_TOKENS``, ``..._FALLBACK_BODY``, ``..._MIN_BODY_CHARS``.
- ``validate_budget_inputs(...)``
- ``fit_text_to_budget(text, *, remaining_chars, remaining_tokens) ->
  (text, degraded)``
"""

from __future__ import annotations

from typing import Any

from .memory_result import error_result
from .token_estimator import estimate_tokens

IMPORTANT_MEMORY_DEFAULT_MAX_ITEMS = 5
IMPORTANT_MEMORY_DEFAULT_MAX_CHARS = 4000
IMPORTANT_MEMORY_DEFAULT_MAX_TOKENS = 1200
IMPORTANT_MEMORY_FALLBACK_BODY = "_See source record for details._"
IMPORTANT_MEMORY_MIN_BODY_CHARS = 48


def validate_budget_inputs(
    *,
    max_chars: int | None,
    max_tokens: int | None,
    max_items: int | None,
) -> dict[str, Any] | None:
    """Return an `error_result` for any out-of-range budget value, else None.

    `max_chars` and `max_tokens` may be 0 (meaning "produce no body"), but
    must not be negative. `max_items` must be at least 1 if provided.
    """
    if max_chars is not None and max_chars < 0:
        return error_result("invalid_input", "max_chars must be >= 0")
    if max_tokens is not None and max_tokens < 0:
        return error_result("invalid_input", "max_tokens must be >= 0")
    if max_items is not None and max_items <= 0:
        return error_result("invalid_input", "max_items must be >= 1")
    return None


def fit_text_to_budget(
    text: str,
    *,
    remaining_chars: int | None,
    remaining_tokens: int | None,
) -> tuple[str, bool]:
    """Truncate ``text`` to fit within the given char/token budget.

    Returns a ``(fitted_text, degraded)`` tuple. ``degraded`` is True when the
    output had to be shortened. Empty output also sets degraded if the input
    was non-empty but the budget was already exhausted.
    """
    candidate = text.strip()
    degraded = False
    if not candidate:
        return "", degraded

    if remaining_chars is not None and remaining_chars <= 0:
        return "", True
    if remaining_tokens is not None and remaining_tokens <= 0:
        return "", True

    if remaining_chars is not None and len(candidate) > remaining_chars:
        candidate = candidate[:remaining_chars].rstrip()
        degraded = True

    if remaining_tokens is not None:
        token_budget_chars = max(0, remaining_tokens * 4)
        if token_budget_chars > 0 and len(candidate) > token_budget_chars:
            candidate = candidate[:token_budget_chars].rstrip()
            degraded = True

        while candidate and estimate_tokens(candidate) > remaining_tokens:
            new_len = max(0, int(len(candidate) * 0.85))
            if new_len >= len(candidate):
                new_len = len(candidate) - 1
            candidate = candidate[:new_len].rstrip()
            degraded = True

    if not candidate:
        return "", degraded

    if degraded:
        candidate = candidate.rstrip(". ")
        if candidate and not candidate.endswith("..."):
            candidate = f"{candidate}..."
        if remaining_chars is not None and len(candidate) > remaining_chars:
            candidate = candidate[:remaining_chars].rstrip()
        if remaining_tokens is not None and candidate:
            while candidate and estimate_tokens(candidate) > remaining_tokens:
                candidate = candidate[:-1].rstrip()
    return candidate, degraded


__all__ = [
    "IMPORTANT_MEMORY_DEFAULT_MAX_ITEMS",
    "IMPORTANT_MEMORY_DEFAULT_MAX_CHARS",
    "IMPORTANT_MEMORY_DEFAULT_MAX_TOKENS",
    "IMPORTANT_MEMORY_FALLBACK_BODY",
    "IMPORTANT_MEMORY_MIN_BODY_CHARS",
    "validate_budget_inputs",
    "fit_text_to_budget",
]
