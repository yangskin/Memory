"""HubTaskRunner 测试替身与真实 Hub 命令服务的语义一致性：同一命令脚本，结果码、状态、版本与 epoch 必须相同。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import BigInteger, Integer, JSON, create_engine
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from memory_hub.db.models import Base
from memory_hub.domain.tasks import TaskCommandError, parse_command
from memory_hub.tasks.commands import execute_command

ROOT = Path(__file__).resolve().parents[5]


@pytest.fixture
def fake_module():
    """导入 HubTaskRunner 替身后恢复 sys.path/sys.modules，避免让其它用例误以为本地 MCP 源可用。"""
    saved_path, saved_modules = list(sys.path), set(sys.modules)
    sys.path.insert(0, str(ROOT / "Tools" / "HubTaskRunner" / "tests"))
    try:
        import fake_task_hub

        yield fake_task_hub
    finally:
        sys.path[:] = saved_path
        for name in set(sys.modules) - saved_modules:
            sys.modules.pop(name, None)

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


def test_fake_hub_matches_the_real_command_service(session_factory, fake_module):
    FakeServer, Rejected = fake_module.FakeServer, fake_module.Rejected
    fake = FakeServer()
    for index, (device, raw) in enumerate(SCRIPT):
        body = {"command_id": f"c{index}", **raw}
        try:
            with session_factory() as session:
                real = _outcome(execute_command(session, "project", device, parse_command(body)))
        except TaskCommandError as exc:
            real = ("error", exc.status, exc.code)
        try:
            mirrored = _outcome(fake.execute(device, body))
        except Rejected as exc:
            mirrored = ("error", exc.status, exc.code)
        assert mirrored == real, (index, raw)
    # 同一 command_id 重放：两者都返回首次结果。
    with session_factory() as session:
        replay = execute_command(session, "project", "PC-A", parse_command({"command_id": "c0", **SCRIPT[0][1]}))
    assert replay["idempotent"] and fake.execute("PC-A", {"command_id": "c0", **SCRIPT[0][1]})["idempotent"]
