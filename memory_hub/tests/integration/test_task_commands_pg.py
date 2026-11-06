"""PostgreSQL 专项：行锁并发、唯一序号、目录分页与 0015→0016 迁移演练。

需要 MEMORY_HUB_DATABASE_URL（以及迁移演练所需的 CREATE DATABASE 权限）。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError

pytestmark = pytest.mark.skipif(not os.getenv("MEMORY_HUB_DATABASE_URL"), reason="requires PostgreSQL")


def _seed(app, project_id: str, device: str, scopes=("events:write", "context:read")) -> dict[str, str]:
    from memory_hub.auth.tokens import create_token
    from memory_hub.db.models import AccessToken, TokenDevice

    token_id, token, secret_hash = create_token()
    with app.state.session_factory() as session:
        session.add(AccessToken(token_id=token_id, token_secret_hash=secret_hash, token_prefix=token[:20],
                                user_id="task-user", project_id=project_id, scopes=list(scopes)))
        session.add(TokenDevice(token_id=token_id, project_id=project_id, device_name=device, source="binding",
                                bound_at=datetime.now(UTC)))
        session.commit()
    return {"Authorization": f"Bearer {token}"}


def _post(client, project_id, headers, action, **body):
    body.setdefault("command_id", uuid4().hex)
    return client.post(f"/v1/projects/{project_id}/task-commands", headers=headers, json={"action": action, **body})


def test_concurrent_commands_on_one_task_serialize() -> None:
    from memory_hub.api.main import create_app
    from memory_hub.domain.tasks import TaskCommandError, parse_command
    from memory_hub.tasks.commands import execute_command

    app = create_app()
    project_id = f"task-pg-{uuid4().hex}"
    lead = _seed(app, project_id, "PC-A")
    _seed(app, project_id, "PC-B")
    client = TestClient(app)
    assert _post(client, project_id, lead, "create", task_id="t", title="T").status_code == 200
    assert _post(client, project_id, lead, "assign", task_id="t", expected_version=1, assignee="PC-B").status_code == 200

    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def run(device: str, raw: dict) -> None:
        barrier.wait()
        try:
            with app.state.session_factory() as session:
                execute_command(session, project_id, device, parse_command(raw))
            outcomes.append("ok")
        except TaskCommandError as exc:
            outcomes.append(exc.code)

    threads = [
        threading.Thread(target=run, args=("PC-A", {"command_id": "re", "action": "reassign", "task_id": "t",
                                                    "expected_version": 2, "expected_assignment_epoch": 1, "assignee": "PC-C"})),
        threading.Thread(target=run, args=("PC-B", {"command_id": "cl", "action": "claim", "task_id": "t",
                                                    "expected_version": 2, "expected_assignment_epoch": 1})),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(outcomes)[0] in {"assignment_epoch_conflict", "version_conflict"} and outcomes.count("ok") == 1
    with app.state.session_factory() as session:
        seqs = list(session.scalars(text("SELECT task_seq FROM task_events WHERE project_id = :p AND task_id = 't' ORDER BY task_seq"),
                                    {"p": project_id}))
    assert seqs == [1, 2, 3]


def test_concurrent_create_with_same_task_id_yields_task_exists() -> None:
    from memory_hub.api.main import create_app
    from memory_hub.domain.tasks import TaskCommandError, parse_command
    from memory_hub.tasks.commands import execute_command

    app = create_app()
    project_id = f"task-pg-{uuid4().hex}"
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def run(index: int) -> None:
        barrier.wait()
        try:
            with app.state.session_factory() as session:
                execute_command(session, project_id, "PC-A", parse_command({"command_id": f"c{index}", "action": "create",
                                                                            "task_id": "same", "title": "T"}))
            outcomes.append("ok")
        except TaskCommandError as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=run, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(outcomes) == ["ok", "task_exists"]


def test_catalog_pages_beyond_the_former_graph_limit() -> None:
    from memory_hub.api.main import create_app
    from memory_hub.db.models import Task

    app = create_app()
    project_id = f"task-index-{uuid4().hex}"
    headers = _seed(app, project_id, "PC-A", scopes=("context:read",))
    now = datetime.now(UTC)
    with app.state.session_factory() as session:
        for index in range(245):
            stamp = now - timedelta(minutes=index)
            session.add(Task(project_id=project_id, task_id=f"task-{index:03d}", title=f"T{index}", objective="", acceptance="",
                             priority="normal", state="done" if index % 3 == 0 else "active", version=1, assignment_epoch=0,
                             current_attempt_id=None, last_task_seq=0, created_at=stamp, updated_at=stamp))
        session.commit()
    client = TestClient(app)
    seen: list[str] = []
    cursor = None
    for _ in range(5):
        params = {"state": "all", "limit": 100, "order": "created"}
        if cursor:
            params["cursor"] = cursor
        body = client.get(f"/v1/projects/{project_id}/tasks", headers=headers, params=params).json()
        seen += [item["task_id"] for item in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 245
    done = client.get(f"/v1/projects/{project_id}/tasks", headers=headers, params={"state": "done", "limit": 100}).json()
    assert done["total"] == 82


def _alembic(url: str) -> Config:
    hub_root = Path(__file__).resolve().parents[2]
    config = Config(str(hub_root / "alembic.ini"))
    config.set_main_option("script_location", str(hub_root / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def test_migration_0016_preserves_task_history(monkeypatch) -> None:
    configured = make_url(os.environ["MEMORY_HUB_DATABASE_URL"])
    name = f"memory_hub_0016_{uuid4().hex}"
    admin = create_engine(configured.set(database="postgres"), isolation_level="AUTOCOMMIT")
    target = configured.set(database=name)
    url = target.render_as_string(hide_password=False)
    engine = None
    try:
        try:
            with admin.connect() as connection:
                connection.execute(text(f'CREATE DATABASE "{name}"'))
        except ProgrammingError as exc:
            pytest.skip(f"requires CREATE DATABASE permission: {exc.orig}")
        monkeypatch.setenv("MEMORY_HUB_DATABASE_URL", url.replace("%", "%%"))
        config = _alembic(url)
        command.upgrade(config, "0015_device_status")
        engine = create_engine(target)
        now = datetime.now(UTC)
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO access_tokens(token_id, token_secret_hash, token_prefix, user_id, project_id, scopes) "
                                    "VALUES ('tok_a', :h, 'p', 'u', 'proj', ARRAY['events:write'])"), {"h": "0" * 64})
            connection.execute(text("INSERT INTO device_enrollments(enrollment_id, project_id, device_name, token_id, token_secret_hash, "
                                    "poll_secret_hash, status, approved_by, created_at, expires_at) VALUES "
                                    "(:e, 'proj', 'PC-A', 'tok_a', :h, :h, 'approved', 'open-registration', :n, :n)"),
                               {"e": uuid4().hex, "h": "0" * 64, "n": now})
            for task_id, state in (("dep", "active"), ("child", "open")):
                connection.execute(text("INSERT INTO tasks(project_id, task_id, title, state, version, assignment_epoch, created_at, updated_at) "
                                        "VALUES ('proj', :t, :t, :s, 2, 0, :n, :n)"), {"t": task_id, "s": state, "n": now})
            events = [("dep", "TaskCreated", {"title": "dep"}), ("dep", "TaskReported", {"summary": "x"}),
                      ("child", "TaskCreated", {"title": "child", "depends_on": ["dep", "child", " "], "parent_task_id": "dep",
                                                "produced_memory": ["mem_1"]})]
            for index, (task_id, kind, payload) in enumerate(events):
                event_id = uuid4()
                connection.execute(text("INSERT INTO memory_events(event_id, project_id, user_id, agent_id, agent_instance_id, task_id, "
                                        "operation, scope, metadata, occurred_at, content_hash) VALUES "
                                        "(:e, 'proj', 'u', 'a', 'i', :t, 'task_sync', 'project_shared', '{}', :n, :h)"),
                                   {"e": event_id, "t": task_id, "n": now, "h": "sha256:" + "0" * 64})
                connection.execute(text("INSERT INTO task_events(project_id, source_event_id, command_id, task_id, event_type, actor_id, "
                                        "expected_version, task_version, assignment_epoch, payload, occurred_at) VALUES "
                                        "('proj', :e, :c, :t, :k, 'PC-A', :v, :w, 0, CAST(:p AS jsonb), :n)"),
                                   {"e": event_id, "c": f"cmd{index}", "t": task_id, "k": kind, "v": index % 2,
                                    "w": index % 2 + 1, "p": json.dumps(payload), "n": now + timedelta(seconds=index)})
            connection.execute(text("INSERT INTO memory_events(event_id, project_id, user_id, agent_id, agent_instance_id, operation, "
                                    "scope, content_markdown, metadata, occurred_at, content_hash) VALUES "
                                    "(:e, 'proj', 'u', 'a', 'i', 'record', 'project_shared', 'keep', '{}', :n, :h)"),
                               {"e": uuid4(), "n": now, "h": "sha256:" + "1" * 64})
        command.upgrade(config, "head")
        with engine.connect() as connection:
            seqs = connection.execute(text("SELECT task_id, task_seq FROM task_events ORDER BY task_event_seq")).all()
            links = set(connection.execute(text("SELECT task_id, relation, target FROM task_links")).all())
            last = dict(connection.execute(text("SELECT task_id, last_task_seq FROM tasks")).all())
            devices = connection.execute(text("SELECT token_id, device_name, source FROM token_devices")).all()
            operations = list(connection.scalars(text("SELECT operation FROM memory_events")))
            graph = connection.scalar(text("SELECT to_regclass('public.graph_nodes')"))
            columns = set(connection.scalars(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'task_events'")))
        assert seqs == [("dep", 1), ("dep", 2), ("child", 1)]
        assert links == {("child", "depends_on", "dep"), ("child", "parent", "dep"), ("child", "produced_memory", "mem_1")}
        assert last == {"dep": 2, "child": 1}
        assert devices == []  # 不从注册记录回填，客户端以计算机名自动绑定
        assert operations == ["record"]
        assert graph is None and "source_event_id" not in columns
        with pytest.raises(RuntimeError, match="cannot be downgraded"):
            command.downgrade(config, "0015_device_status")
    finally:
        if engine is not None:
            engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
