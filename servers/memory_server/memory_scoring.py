from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .memory_config import MemoryConfig


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def parse_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text + "T00:00:00+00:00")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_usage_stats(config: MemoryConfig) -> dict[str, dict[str, Any]]:
    stats_path = config.repo_root / ".ai-memory" / "usage-stats.json"
    if not stats_path.is_file():
        return {}
    try:
        raw = json.loads(stats_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    data: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            data[str(key)] = value
    return data


def build_reference_counts(records: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        metadata = getattr(record, "metadata")
        for field in ("derived_from_record_ids", "supersedes", "conflicts_with"):
            values = metadata.get(field)
            if not isinstance(values, list):
                continue
            for value in values:
                record_id = str(value).strip()
                if record_id:
                    counts[record_id] = counts.get(record_id, 0) + 1
    return counts


def _primary_timestamp(metadata: dict[str, Any], usage_entry: dict[str, Any]) -> datetime | None:
    for key in ("occurred_at", "valid_from", "updated_at", "created_at"):
        parsed = parse_timestamp(metadata.get(key))
        if parsed is not None:
            return parsed
    return parse_timestamp(usage_entry.get("last_used_at"))


def score_governance(metadata: dict[str, Any]) -> float:
    status = str(metadata.get("status", ""))
    base = {
        "published": 0.26,
        "validated": 0.22,
        "candidate": 0.12,
        "raw": 0.09,
        "degraded": 0.05,
        "archived": 0.02,
    }.get(status, 0.08)
    if metadata.get("validated_by"):
        base += 0.04
    if metadata.get("supersedes"):
        base += 0.04
    if metadata.get("conflicts_with"):
        base += 0.02
    return clamp01(base)


def score_usage(metadata: dict[str, Any], usage_entry: dict[str, Any], reference_count: int) -> float:
    compile_hits = int(usage_entry.get("compile_hit_count", 0) or 0)
    unique_targets = usage_entry.get("compile_targets", [])
    if not isinstance(unique_targets, list):
        unique_targets = []
    score = min(0.18, compile_hits * 0.03)
    score += min(0.06, len({str(item) for item in unique_targets if str(item).strip()}) * 0.02)
    score += min(0.08, reference_count * 0.02)
    if metadata.get("task_id"):
        score += 0.02
    if metadata.get("branch"):
        score += 0.01
    return clamp01(score)


def score_impact(metadata: dict[str, Any]) -> float:
    breadth = 0
    for key in (
        "related_artifact_ids",
        "asset_paths",
        "map_names",
        "plugin_names",
        "module_names",
        "class_names",
        "blueprint_paths",
    ):
        values = metadata.get(key)
        if isinstance(values, list):
            breadth += len([item for item in values if str(item).strip()])
    breadth_score = min(0.12, breadth * 0.02)
    scope_score = {
        "org_shared": 0.08,
        "project_shared": 0.06,
        "shared": 0.05,
        "task_or_branch": 0.04,
        "session": 0.03,
        "personal": 0.02,
        "user_private": 0.02,
        "local": 0.01,
        "archive": 0.0,
    }.get(str(metadata.get("scope", "")), 0.0)
    if metadata.get("system_area"):
        scope_score += 0.02
    return clamp01(breadth_score + scope_score)


def score_novelty(metadata: dict[str, Any], usage_entry: dict[str, Any], now: datetime) -> float:
    kind = str(metadata.get("record_kind", ""))
    base = {
        "incident": 0.15,
        "decision": 0.13,
        "procedure": 0.11,
        "observation": 0.1,
        "system_rule": 0.08,
        "snapshot_daily": 0.04,
        "snapshot_weekly": 0.03,
        "snapshot_monthly": 0.02,
    }.get(kind, 0.06)
    created_at = _primary_timestamp(metadata, usage_entry)
    if created_at is not None:
        age_days = max(0.0, (now - created_at).total_seconds() / 86400.0)
        if age_days <= 1:
            base += 0.06
        elif age_days <= 7:
            base += 0.04
        elif age_days <= 30:
            base += 0.02
    return clamp01(base)


def score_conflict(metadata: dict[str, Any], reference_count: int) -> float:
    conflicts = metadata.get("conflicts_with")
    if not isinstance(conflicts, list):
        conflicts = []
    score = min(0.12, len([item for item in conflicts if str(item).strip()]) * 0.04)
    if str(metadata.get("status", "")) == "degraded":
        score += 0.03
    if reference_count > 0 and conflicts:
        score += 0.01
    return clamp01(score)


def score_decay(metadata: dict[str, Any], usage_entry: dict[str, Any], now: datetime) -> float:
    timestamp = _primary_timestamp(metadata, usage_entry)
    if timestamp is None:
        return 0.03
    age_days = max(0.0, (now - timestamp).total_seconds() / 86400.0)
    compile_hits = int(usage_entry.get("compile_hit_count", 0) or 0)
    if age_days <= 3:
        return 0.12
    if age_days <= 14:
        return 0.09
    if age_days <= 45:
        return 0.06
    if compile_hits >= 3:
        return 0.08
    if compile_hits >= 1:
        return 0.05
    return 0.02


def suggest_memory_tier(metadata: dict[str, Any], total_score: float) -> str:
    declared = str(metadata.get("memory_tier", "") or "").strip()
    if declared:
        return declared
    status = str(metadata.get("status", ""))
    if status in {"archived", "degraded"}:
        return "fossil"
    if total_score >= 0.72:
        return "hot"
    if total_score >= 0.48:
        return "warm"
    if total_score >= 0.26:
        return "cold"
    return "fossil"


def score_record(
    metadata: dict[str, Any],
    *,
    usage_entry: dict[str, Any] | None = None,
    reference_count: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    effective_usage = usage_entry or {}
    effective_now = now or datetime.now(timezone.utc)
    components = {
        "governance": score_governance(metadata),
        "usage": score_usage(metadata, effective_usage, reference_count),
        "impact": score_impact(metadata),
        "novelty": score_novelty(metadata, effective_usage, effective_now),
        "conflict": score_conflict(metadata, reference_count),
        "decay": score_decay(metadata, effective_usage, effective_now),
    }
    total = clamp01(sum(components.values()))
    return {
        "total": round(total, 4),
        "components": {key: round(value, 4) for key, value in components.items()},
        "effective_memory_tier": suggest_memory_tier(metadata, total),
        "usage": {
            "compile_hit_count": int(effective_usage.get("compile_hit_count", 0) or 0),
            "last_used_at": effective_usage.get("last_used_at"),
        },
    }
