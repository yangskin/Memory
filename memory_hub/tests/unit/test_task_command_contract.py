"""固定命令契约：完整生命周期的结果码、状态、版本、epoch 与执行者身份。"""
from __future__ import annotations

import pytest
from sqlalchemy import BigInteger, Integer, JSON, create_engine
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from memory_hub.db.models import Base
from memory_hub.domain.tasks import TaskCommandError, parse_command
from memory_hub.tasks.commands import execute_command

SCRIPT = [
    ("PC-A", {"action": "create", "task_id": "t", "title": "T"}),
    ("PC-A", {"action": "create", "task_id": "d", "title": "D", "depends_on": ["t"]}),
    ("PC-A", {"action": "create", "task_id": "s", "title": "S", "depends_on": ["s"]}),
    ("PC-A", {"action": "create", "task_id": "m", "title": "M", "parent_task_id": "missing"}),
    ("PC-A", {"action": "create", "task_id": "t", "title": "again"}),
    ("PC-A", {"action": "create", "task_id": "z", "title": "Z", "expected_version": 0}),
    ("PC-A", {"action": "create", "task_id": "y", "title": "Y", "expected_version": 1}),
    ("PC-A", {"action": "claim", "task_id": "t", "expected_version": 1, "expected_assignment_epoch": 0}),
    ("PC-A", {"action": "assign", "task_id": "t", "expected_version": 1, "assignee": "PC-B", "channel": "dispatch"}),
    ("PC-A", {"action": "assign", "task_id": "t", "expected_version": 2, "assignee": "PC-C"}),
    ("PC-C", {"action": "claim", "task_id": "t", "expected_version": 2, "expected_assignment_epoch": 1}),
    ("PC-B", {"action": "claim", "task_id": "t", "expected_version": 1, "expected_assignment_epoch": 1}),
    ("PC-B", {"action": "claim", "task_id": "t", "expected_version": 2, "expected_assignment_epoch": 1}),
    ("PC-B", {"action": "report", "task_id": "t", "expected_assignment_epoch": 1, "summary": "progress"}),
    ("PC-B", {"action": "block", "task_id": "t", "expected_version": 3, "expected_assignment_epoch": 1, "reason": "wait"}),
    ("PC-B", {"action": "report", "task_id": "t", "expected_assignment_epoch": 1, "summary": "blocked progress"}),
    ("PC-B", {"action": "submit", "task_id": "t", "expected_version": 4, "expected_assignment_epoch": 1, "summary": "x"}),
    ("PC-B", {"action": "resume", "task_id": "t", "expected_version": 4, "expected_assignment_epoch": 1}),
    ("PC-B", {"action": "submit", "task_id": "t", "expected_version": 5, "expected_assignment_epoch": 1, "summary": "v1"}),
    ("PC-B", {"action": "review", "task_id": "t", "expected_version": 6, "decision": "approved"}),
    ("PC-A", {"action": "review", "task_id": "t", "expected_version": 6, "decision": "changes_requested", "channel": "dispatch"}),
    ("PC-A", {"action": "reassign", "task_id": "t", "expected_version": 7, "expected_assignment_epoch": 1, "assignee": "PC-C"}),
    ("PC-B", {"action": "report", "task_id": "t", "expected_assignment_epoch": 1, "summary": "late"}),
    ("PC-C", {"action": "decline", "task_id": "t", "expected_version": 8, "expected_assignment_epoch": 2}),
    ("PC-A", {"action": "assign", "task_id": "t", "expected_version": 9, "assignee": "PC-C"}),
    ("PC-C", {"action": "claim", "task_id": "t", "expected_version": 10, "expected_assignment_epoch": 3}),
    ("PC-C", {"action": "submit", "task_id": "t", "expected_version": 11, "expected_assignment_epoch": 3, "summary": "v2"}),
    ("PC-A", {"action": "review", "task_id": "t", "expected_version": 12, "decision": "approved"}),
    ("PC-A", {"action": "cancel", "task_id": "t", "expected_version": 13}),
    ("PC-A", {"action": "cancel", "task_id": "d", "expected_version": 1}),
    ("PC-A", {"action": "assign", "task_id": "d", "expected_version": 2, "assignee": "PC-B"}),
    ("PC-A", {"action": "claim", "task_id": "missing", "expected_version": 1, "expected_assignment_epoch": 1}),
]

EXPECTED = [
    ("ok", "open", 1, 0, "PC-A"),
    ("ok", "open", 1, 0, "PC-A"),
    ("error", 422, "link_cycle"),
    ("error", 422, "link_target_not_found"),
    ("error", 409, "task_exists"),
    ("ok", "open", 1, 0, "PC-A"),
    ("error", 422, "invalid_input"),
    ("error", 409, "invalid_attempt_state"),
    ("ok", "open", 2, 1, "PC-A-dispatch"),
    ("error", 409, "assignment_in_progress"),
    ("error", 403, "assignment_forbidden"),
    ("error", 409, "version_conflict"),
    ("ok", "active", 3, 1, "PC-B"),
    ("ok", "active", 3, 1, "PC-B"),
    ("ok", "blocked", 4, 1, "PC-B"),
    ("ok", "blocked", 4, 1, "PC-B"),
    ("error", 409, "invalid_task_state"),
    ("ok", "active", 5, 1, "PC-B"),
    ("ok", "review", 6, 1, "PC-B"),
    ("error", 409, "reviewer_conflict"),
    ("ok", "active", 7, 1, "PC-A-dispatch"),
    ("ok", "open", 8, 2, "PC-A"),
    ("error", 409, "assignment_epoch_conflict"),
    ("ok", "open", 9, 2, "PC-C"),
    ("ok", "open", 10, 3, "PC-A"),
    ("ok", "active", 11, 3, "PC-C"),
    ("ok", "review", 12, 3, "PC-C"),
    ("ok", "done", 13, 3, "PC-A"),
    ("error", 409, "invalid_task_state"),
    ("ok", "cancelled", 2, 0, "PC-A"),
    ("error", 409, "invalid_task_state"),
    ("error", 404, "task_not_found"),
]


@pytest.fixture
def session_factory():
    originals = []
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, (ARRAY, JSONB, BigInteger)):
                originals.append((column, column.type))
                column.type = Integer() if isinstance(column.type, BigInteger) else JSON()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    try:
        Base.metadata.create_all(engine)
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()
        for column, original in originals:
            column.type = original


def _outcome(result: dict) -> tuple:
    return ("ok", result["state"], result["version"], result["assignment_epoch"], result["actor"])


def test_command_service_matches_fixed_contract(session_factory):
    for index, ((device, raw), expected) in enumerate(zip(SCRIPT, EXPECTED, strict=True)):
        body = {"command_id": f"c{index}", **raw}
        try:
            with session_factory() as session:
                real = _outcome(execute_command(session, "project", device, parse_command(body)))
        except TaskCommandError as exc:
            real = ("error", exc.status, exc.code)
        assert real == expected, (index, raw)
    with session_factory() as session:
        replay = execute_command(session, "project", "PC-A", parse_command({"command_id": "c0", **SCRIPT[0][1]}))
    assert replay["idempotent"]
    assert _outcome(replay) == EXPECTED[0]
    assert replay["task"]["state"] == "done"
    assert replay["task"]["version"] == 13
