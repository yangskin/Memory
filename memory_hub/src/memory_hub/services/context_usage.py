"""In-process aggregation of /context usage counters.

Every /context read used to upsert ``context_usage_daily`` in the same request.
Counters are now accumulated in memory and merged into the table at most once
per flush interval (and on shutdown or before the usage report is read), so a
burst of reads costs one write per project per interval instead of one each.
The counters are statistics only; a hard crash can lose at most one interval.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session

COUNTER_FIELDS = (
    "request_count",
    "returned_event_count",
    "returned_brief_count",
    "user_brief_requests",
    "project_brief_requests",
    "same_task_requests",
    "other_agents_requests",
    "other_tasks_requests",
    "project_activity_requests",
)
_INCLUDE_FIELDS = {
    "user_brief": "user_brief_requests",
    "project_brief": "project_brief_requests",
    "same_task_agents": "same_task_requests",
    "my_other_agents": "other_agents_requests",
    "other_tasks": "other_tasks_requests",
    "project_activity": "project_activity_requests",
}

Key = tuple[str, datetime]


def usage_day(now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)).replace(hour=0, minute=0, second=0, microsecond=0)


def usage_counters(include: list[str], returned_events: int, returned_briefs: int) -> dict[str, int]:
    counters = dict.fromkeys(COUNTER_FIELDS, 0)
    counters.update(request_count=1, returned_event_count=returned_events, returned_brief_count=returned_briefs)
    for name in set(include):
        if name in _INCLUDE_FIELDS:
            counters[_INCLUDE_FIELDS[name]] = 1
    return counters


def upsert_usage(session: Session, project_id: str, usage_date: datetime, counters: dict[str, int]) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from memory_hub.db.models import ContextUsageDaily

    insert = sqlite_insert if session.bind.dialect.name == "sqlite" else pg_insert
    statement = insert(ContextUsageDaily).values(project_id=project_id, usage_date=usage_date, **counters)
    statement = statement.on_conflict_do_update(
        index_elements=["project_id", "usage_date"],
        set_={name: getattr(ContextUsageDaily, name) + value for name, value in counters.items()},
    )
    session.execute(statement)


class ContextUsageBuffer:
    def __init__(self, flush_interval_seconds: float = 60.0, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._interval = flush_interval_seconds
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._pending: dict[Key, dict[str, int]] = {}
        self._last_flush = monotonic()

    def add(self, project_id: str, counters: dict[str, int], *, now: datetime | None = None) -> None:
        key = (project_id, usage_day(now))
        with self._lock:
            total = self._pending.setdefault(key, dict.fromkeys(COUNTER_FIELDS, 0))
            for name, value in counters.items():
                total[name] += value

    def due(self) -> bool:
        with self._lock:
            return bool(self._pending) and self._monotonic() - self._last_flush >= self._interval

    def _drain(self) -> dict[Key, dict[str, int]]:
        with self._lock:
            pending, self._pending = self._pending, {}
            self._last_flush = self._monotonic()
            return pending

    def _restore(self, pending: dict[Key, dict[str, int]]) -> None:
        with self._lock:
            for key, counters in pending.items():
                total = self._pending.setdefault(key, dict.fromkeys(COUNTER_FIELDS, 0))
                for name, value in counters.items():
                    total[name] += value

    def flush(self, session: Session) -> int:
        """Merge pending counters in the caller's transaction and commit; restore them on failure."""
        pending = self._drain()
        if not pending:
            return 0
        try:
            for (project_id, usage_date), counters in pending.items():
                upsert_usage(session, project_id, usage_date, counters)
            session.commit()
        except Exception:
            session.rollback()
            self._restore(pending)
            raise
        return len(pending)
