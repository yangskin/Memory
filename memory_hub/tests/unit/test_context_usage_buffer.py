"""/context 用量计数在进程内聚合，按间隔合并写库；失败时计数保留。"""
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from memory_hub.db.models import ContextUsageDaily
from memory_hub.services.context_usage import ContextUsageBuffer, usage_counters


@pytest.fixture
def factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    ContextUsageDaily.__table__.create(engine)
    writes = []
    event.listen(engine, "before_cursor_execute",
                 lambda conn, cursor, statement, *args: writes.append(statement) if statement.startswith("INSERT") else None)
    try:
        yield sessionmaker(engine, expire_on_commit=False), writes
    finally:
        engine.dispose()


def test_many_reads_are_merged_into_one_write_per_interval(factory):
    sessions, writes = factory
    clock = [0.0]
    buffer = ContextUsageBuffer(flush_interval_seconds=60, monotonic=lambda: clock[0])
    now = datetime(2026, 11, 5, 8, tzinfo=UTC)
    for _ in range(100):
        buffer.add("p", usage_counters(["project_brief", "project_activity", "project_activity"], 2, 1), now=now)
        assert not buffer.due()
    clock[0] = 61
    assert buffer.due()
    with sessions() as session:
        assert buffer.flush(session) == 1
    assert len(writes) == 1
    with sessions() as session:
        row = session.scalar(select(ContextUsageDaily))
    assert (row.request_count, row.returned_event_count, row.returned_brief_count) == (100, 200, 100)
    assert row.project_brief_requests == 100 and row.project_activity_requests == 100
    assert row.user_brief_requests == 0
    # 再次写出会累加到同一天的同一行。
    buffer.add("p", usage_counters([], 0, 0), now=now)
    with sessions() as session:
        buffer.flush(session)
        assert session.scalar(select(ContextUsageDaily.request_count)) == 101
    assert not buffer.due()


def test_failed_flush_keeps_counters_for_the_next_attempt(factory):
    sessions, _ = factory
    buffer = ContextUsageBuffer(flush_interval_seconds=0)
    buffer.add("p", usage_counters(["user_brief"], 1, 0))

    class Broken:
        bind = sessions().bind

        def execute(self, statement):
            raise RuntimeError("database unavailable")

        def rollback(self):
            pass

    with pytest.raises(RuntimeError):
        buffer.flush(Broken())
    with sessions() as session:
        assert buffer.flush(session) == 1
        assert session.scalar(select(ContextUsageDaily.user_brief_requests)) == 1
