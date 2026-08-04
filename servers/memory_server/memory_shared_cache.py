"""Cache freshness decisions for optional shared context."""

from __future__ import annotations

from datetime import UTC, datetime


def cache_age_seconds(fetched_at: str) -> float:
    return max(0.0, (datetime.now(UTC) - datetime.fromisoformat(fetched_at)).total_seconds())


def cache_state(fetched_at: str, fresh_seconds: int, usable_seconds: int) -> str:
    age = cache_age_seconds(fetched_at)
    if age <= fresh_seconds:
        return "fresh"
    if age <= usable_seconds:
        return "aging"
    return "stale"