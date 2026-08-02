"""P2-1: scale baseline (v0.6.0 OOTB hardening).

Captures a snapshot of "how big is this repo right now?" for later
performance-regression detection. The numbers themselves are cheap to
gather; the value comes from comparing them across time.

Baseline file: ``.ai-memory/baseline.json``.

Schema:
    {
      "captured_at": "<ISO>",
      "git_sha": "<short>",                  # optional
      "metrics": {
        "memory_bank_files": int,
        "memory_bank_total_bytes": int,
        "records_count": int,
        "events_total_bytes": int,
        "index_db_bytes": int,
      }
    }
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .memory_artifact_paths import attach_git_sha
from .memory_config import MemoryConfig


_BASELINE_REL = Path(".ai-memory") / "baseline.json"
_DEFAULT_REGRESSION_FACTOR: float = 2.0


def _gather_metrics(config: MemoryConfig) -> dict[str, int]:
    metrics: dict[str, int] = {
        "memory_bank_files": 0,
        "memory_bank_total_bytes": 0,
        "records_count": 0,
        "events_total_bytes": 0,
        "index_db_bytes": 0,
    }

    memory_bank = config.repo_root / "memory-bank"
    if memory_bank.is_dir():
        for child in memory_bank.rglob("*.md"):
            try:
                metrics["memory_bank_files"] += 1
                metrics["memory_bank_total_bytes"] += child.stat().st_size
            except OSError:
                continue

    try:
        from .memory_record_io import iter_parsed_records

        records, _stats = iter_parsed_records(config)
        metrics["records_count"] = len(records)
    except (OSError, ValueError):
        pass

    try:
        metrics["events_total_bytes"] = config.events_file.stat().st_size
    except OSError:
        pass

    db_path = config.repo_root / ".ai-memory" / "search.db"
    if db_path.exists():
        try:
            metrics["index_db_bytes"] = db_path.stat().st_size
        except OSError:
            pass

    return metrics


def write_baseline(config: MemoryConfig, *, now: datetime | None = None) -> dict[str, Any]:
    moment = now or datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "captured_at": moment.isoformat(),
        "metrics": _gather_metrics(config),
    }
    attach_git_sha(config.repo_root, payload)

    out_path = config.repo_root / _BASELINE_REL
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "path": str(out_path), **payload}


def load_baseline(config: MemoryConfig) -> dict[str, Any] | None:
    path = config.repo_root / _BASELINE_REL
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def detect_regressions(
    config: MemoryConfig,
    *,
    factor: float = _DEFAULT_REGRESSION_FACTOR,
) -> dict[str, Any]:
    """Compare current metrics to baseline; warn when a metric grew by
    ``factor`` or more.

    Returns ``{ok, baseline_present, regressions: [...]}``.  Never
    raises.
    """
    baseline = load_baseline(config)
    if baseline is None:
        return {"ok": True, "baseline_present": False, "regressions": []}

    baseline_metrics = baseline.get("metrics") or {}
    current = _gather_metrics(config)

    regressions: list[dict[str, Any]] = []
    for key, baseline_val in baseline_metrics.items():
        try:
            base_n = float(baseline_val)
        except (TypeError, ValueError):
            continue
        cur_n = float(current.get(key, 0))
        if base_n <= 0:
            continue  # cannot compute ratio; skip
        ratio = cur_n / base_n
        if ratio >= factor:
            regressions.append(
                {
                    "metric": key,
                    "baseline": baseline_val,
                    "current": current.get(key, 0),
                    "ratio": round(ratio, 2),
                    "factor_threshold": factor,
                }
            )
    return {
        "ok": True,
        "baseline_present": True,
        "factor_threshold": factor,
        "regressions": regressions,
    }
