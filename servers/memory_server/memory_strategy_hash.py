"""P2-3: scoring strategy schema hash (v0.6.0 OOTB hardening).

Compute a stable digest of the scoring components in use so that any
silent change to weights/component set produces a detectable
``scoring_strategy_changed`` warning when scanning recent events.

Pure helpers, never raise.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .memory_config import MemoryConfig

# Declarative manifest of the scoring strategy. Keep alphabetical and
# tuple-of-(component, default_weight) so any reorder/rename produces a
# different digest. Update this constant *intentionally* whenever the
# underlying scoring code changes — the warning surfaces accidental
# divergence between code and data on disk.
STRATEGY_MANIFEST: dict[str, Any] = {
    "version": "1.0",
    "components": [
        ("conflict", 1.0),
        ("decay", 1.0),
        ("governance", 1.0),
        ("impact", 1.0),
        ("novelty", 1.0),
        ("usage", 1.0),
    ],
    "tier_order": ["cold", "warm", "hot"],
}


def current_strategy_hash() -> str:
    """Stable SHA-256 (hex, first 16 chars) of the strategy manifest."""
    payload = json.dumps(STRATEGY_MANIFEST, sort_keys=True, default=list)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def latest_recorded_hash(config: MemoryConfig, *, scan_lines: int = 500) -> str | None:
    """Read the most recent ``scoring_strategy_hash`` from events.jsonl.

    Returns ``None`` when not found. Cheap: tails the last ~N lines.
    """
    path = config.events_file
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            tail = fh.readlines()[-scan_lines:]
    except OSError:
        return None
    for line in reversed(tail):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = rec.get("payload") or {}
        h = payload.get("scoring_strategy_hash")
        if isinstance(h, str) and h:
            return h
    return None


def detect_strategy_drift(config: MemoryConfig) -> dict[str, Any]:
    """Compare current vs latest-recorded strategy hash."""
    cur = current_strategy_hash()
    prev = latest_recorded_hash(config)
    drift = (prev is not None) and (prev != cur)
    return {
        "ok": True,
        "current": cur,
        "previous": prev,
        "drift": drift,
    }
