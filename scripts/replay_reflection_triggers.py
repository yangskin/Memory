from __future__ import annotations

import argparse
import heapq
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def replay(events: list[dict[str, Any]], *, day: str, quiet: float = 300,
           maximum: float = 1800, utc_offset_hours: int = 8) -> dict[str, Any]:
    zone = timezone(timedelta(hours=utc_offset_hours))
    start = datetime.fromisoformat(day).replace(tzinfo=zone).timestamp()
    end = start + 86400
    pending: dict[str, tuple[float, float, int]] = {}
    revisions: Counter[str] = Counter()
    handled: dict[str, int] = {}
    ready: list[tuple[float, int, str]] = []
    counters: Counter[str] = Counter()
    baseline_tokens = 0
    sequence = 0

    def drain(before: float) -> None:
        while ready and ready[0][0] <= before:
            due, serial, task = heapq.heappop(ready)
            if pending.get(task, (None, None, None))[2] != serial:
                continue
            pending.pop(task)
            counters["debounced_rounds"] += 1
            revision = max(1, revisions[task])
            if handled.get(task) == revision:
                counters["no_new_evidence_rounds"] += 1
            else:
                counters["changed_evidence_rounds"] += 1
                handled[task] = revision

    for event in sorted(events, key=lambda row: row["ts"]):
        when = datetime.fromisoformat(event["ts"].replace("Z", "+00:00")).timestamp()
        if when >= end:
            break
        if when >= start:
            drain(when)
        payload = event.get("payload") or {}
        task = str(payload.get("task_id") or "")
        kind = event.get("event_type")
        if kind == "memory_write_record" and task:
            revisions[task] += 1
        if when < start:
            continue
        if kind == "project_reflection_queued" and task:
            counters["trigger_events"] += 1
            first = pending.get(task, (when, when, 0))[0]
            due = min(when + quiet, first + maximum)
            sequence += 1
            pending[task] = (first, due, sequence)
            heapq.heappush(ready, (due, sequence, task))
        elif kind == "project_reflection_finished":
            counters["baseline_finished_attempts"] += 1
        elif kind == "llm_capability_invoked" and payload.get("capability") == "project_reflection":
            counters["baseline_capability_runs"] += 1
            counters["baseline_api_calls"] += int(payload.get("call_count") or 0)
            counters["baseline_budget_blocked"] += int(payload.get("status") == "budget_exceeded")
            baseline_tokens += int(payload.get("total_tokens") or 0)
    drain(float("inf"))
    return {
        "day": day,
        "mode": "scheduling_only_instantaneous_worker",
        "quiet_seconds": quiet,
        "max_wait_seconds": maximum,
        **counters,
        "baseline_reported_tokens": baseline_tokens,
        "two_pass_call_ceiling_for_changed_rounds": counters["changed_evidence_rounds"] * 2,
        "limits": [
            "Write events are change signals; legacy events do not carry content versions or lineage.",
            "Worker duration is zero in this scheduling replay; live concurrency and failures are tested separately.",
            "Follow-ups draining after midnight are included. Input/output split and new token costs cannot be reconstructed.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events-directory", type=Path, required=True)
    parser.add_argument("--day", required=True)
    parser.add_argument("--quiet-seconds", type=float, default=300)
    parser.add_argument("--max-wait-seconds", type=float, default=1800)
    parser.add_argument("--utc-offset-hours", type=int, default=8)
    args = parser.parse_args()
    if args.quiet_seconds < 0 or args.max_wait_seconds < args.quiet_seconds:
        parser.error("max wait must be >= quiet window >= 0")
    events = []
    for path in sorted(args.events_directory.glob("events.jsonl*")):
        with path.open(encoding="utf-8", errors="strict") as stream:
            events.extend(json.loads(line) for line in stream if line.strip())
    print(json.dumps(replay(events, day=args.day, quiet=args.quiet_seconds,
                            maximum=args.max_wait_seconds, utc_offset_hours=args.utc_offset_hours),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
