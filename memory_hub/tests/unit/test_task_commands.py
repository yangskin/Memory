"""任务命令服务的真实 HTTP/鉴权/SQL 路由测试；SQLite 类型替换仅限测试。

PostgreSQL 专属的行锁并发与迁移演练见 tests/integration/test_task_commands_pg.py。
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, Integer, JSON, create_engine, event
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from memory_hub.api import routes_board, routes_context, routes_enrollment, routes_events, routes_tasks
from memory_hub.auth.rate_limit import TokenRateLimiter
from memory_hub.auth.tokens import create_token
from memory_hub.config import load_settings
from memory_hub.db.models import AccessToken, Base, Task, TokenDevice

BASE = "/v1/projects/project"


class _Limiter(TokenRateLimiter):
    def allow(self, *args, **kwargs) -> bool:  # 测试关注语义而非限流
        return True


@pytest.fixture
def hub():
    originals = []
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, (ARRAY, JSONB, BigInteger)):
                originals.append((column, column.type))
                column.type = Integer() if isinstance(column.type, BigInteger) else JSON()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    event.listen(engine, "connect", lambda db, _: db.create_function("btrim", 1, lambda value: value.strip() if value is not None else None))
    try:
        Base.metadata.create_all(engine)
        app = FastAPI()
        app.state.session_factory = sessionmaker(engine, expire_on_commit=False)
        app.state.rate_limiter = _Limiter()
        app.state.settings = load_settings()
        for module in (routes_enrollment, routes_board, routes_context, routes_events, routes_tasks):
            app.include_router(module.router)

        def seed(device: str | None = "PC-A", project: str = "project", scopes=None):
            tid, raw, hashed = create_token()
            with app.state.session_factory() as session:
                session.add(AccessToken(token_id=tid, token_secret_hash=hashed, token_prefix="test", project_id=project,
                                        user_id="owner", scopes=scopes or ["events:write", "context:read"]))
                if device:
                    session.add(TokenDevice(token_id=tid, project_id=project, device_name=device, source="binding",
                                            bound_at=datetime.now(UTC)))
                session.commit()
            return {"Authorization": "Bearer " + raw}

        yield TestClient(app), app, seed
    finally:
        engine.dispose()
        for column, original in originals:
            column.type = original


def _cmd(client, headers, action, **body):
    body.setdefault("command_id", uuid4().hex)
    return client.post(BASE + "/task-commands", headers=headers, json={"action": action, **body})


def _ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def _err(response, status, code):
    assert response.status_code == status, response.text
    assert response.json()["error"] == code, response.text
    return response.json()


def _active_task(client, lead, worker, task_id="t", assignee="PC-B"):
    _ok(_cmd(client, lead, "create", task_id=task_id, title="Task " + task_id))
    _ok(_cmd(client, lead, "assign", task_id=task_id, expected_version=1, assignee=assignee))
    return _ok(_cmd(client, worker, "claim", task_id=task_id, expected_version=2, expected_assignment_epoch=1))


def test_full_lifecycle_is_computed_by_the_hub(hub):
    client, _, seed = hub
    lead, worker, reviewer = seed("PC-A"), seed("PC-B"), seed("PC-C")
    created = _ok(_cmd(client, lead, "create", title="Hub task", agent="codex"))
    task_id = created["task_id"]
    assert task_id.startswith("task_") and created["version"] == 1 and created["task"]["state"] == "open"
    assigned = _ok(_cmd(client, lead, "assign", task_id=task_id, expected_version=1, assignee="PC-B", channel="dispatch"))
    assert assigned["attempt_id"].startswith("attempt_") and assigned["assignment_epoch"] == 1
    assert assigned["task"]["current_attempt"]["assigned_by"] == "PC-A-dispatch"
    _ok(_cmd(client, worker, "claim", task_id=task_id, expected_version=2, expected_assignment_epoch=1))
    for index in range(2):
        report = _ok(_cmd(client, worker, "report", task_id=task_id, expected_assignment_epoch=1, summary=f"step {index}"))
        assert report["version"] == 3
    submitted = _ok(_cmd(client, worker, "submit", task_id=task_id, expected_version=3, expected_assignment_epoch=1,
                         summary="done", evidence=["pytest"], agent="codebuddy"))
    assert submitted["version"] == 4 and submitted["submission_id"].startswith("submission_")
    reviewed = _ok(_cmd(client, reviewer, "review", task_id=task_id, expected_version=4, decision="approved"))
    detail = reviewed["task"]
    assert detail["state"] == "done" and detail["version"] == 5
    assert detail["submissions"][0]["executor"] == "PC-B" and detail["submissions"][0]["agent"] == "codebuddy"
    assert detail["reviews"][0]["reviewer"] == "PC-C"
    events = client.get(BASE + "/task-events", headers=lead, params={"task_id": task_id}).json()["events"]
    assert [item["task_seq"] for item in events] == list(range(1, 8))
    assert [item["event_type"] for item in events] == ["TaskCreated", "TaskAssigned", "TaskClaimed", "TaskReported",
                                                         "TaskReported", "TaskSubmitted", "TaskReviewed"]
    assert {item["actor_id"] for item in events} == {"PC-A", "PC-A-dispatch", "PC-B", "PC-C"}


def test_identity_is_derived_from_the_credential(hub):
    client, _, seed = hub
    lead, unbound = seed("PC-A"), seed(None)
    _err(_cmd(client, lead, "create", title="T", actor_id="PC-B"), 422, "actor_not_accepted")
    _err(_cmd(client, unbound, "create", title="T"), 403, "device_identity_required")
    _err(_cmd(client, lead, "create", title="T", channel="admin"), 422, "invalid_channel")
    other = seed("PC-X", project="other")
    assert _cmd(client, other, "create", title="T").status_code == 403


def test_create_accepts_zero_expected_version_for_existing_call_style(hub):
    client, _, seed = hub
    lead = seed("PC-A")
    created = _ok(_cmd(client, lead, "create", command_id="c-zero", task_id="z", title="Z", expected_version=0))
    assert created["version"] == 1
    # 归一化后 0 与省略是同一意图：用同一 command_id 重试得到幂等结果。
    assert _ok(_cmd(client, lead, "create", command_id="c-zero", task_id="z", title="Z"))["idempotent"]
    _err(_cmd(client, lead, "create", task_id="y", title="Y", expected_version=1), 422, "invalid_input")


def test_shared_credentials_cannot_become_devices(hub):
    client, _, seed = hub
    shared = seed(None, scopes=["events:write", "context:read", "identity:delegate"])
    refused = client.post(BASE + "/device-binding", headers=shared, json={"device_name": "PC-S"})
    assert refused.status_code == 403 and "shared_credential_not_bindable" in refused.text
    # 即使数据库里已有绑定（例如人工写入），共享凭据也不推导设备身份。
    delegate = seed("PC-S", scopes=["events:write", "context:read", "identity:delegate"])
    _err(_cmd(client, delegate, "create", title="T"), 403, "device_identity_required")
    assert _ok(client.get(BASE + "/connection", headers=delegate))["device_name"] is None


def test_new_device_commands_on_two_tasks_do_not_interfere(hub):
    client, _, seed = hub
    lead = seed("PC-A")
    for task_id in ("p", "q"):
        _ok(_cmd(client, lead, "create", task_id=task_id, title=task_id))
        _ok(_cmd(client, lead, "assign", task_id=task_id, expected_version=1, assignee="PC-NEW"))
    fresh = seed("PC-NEW")
    for task_id in ("p", "q"):
        _ok(_cmd(client, fresh, "claim", task_id=task_id, expected_version=2, expected_assignment_epoch=1))


def test_command_ids_are_idempotent_and_conflicts_are_explicit(hub):
    client, _, seed = hub
    lead, worker = seed("PC-A"), seed("PC-B")
    _active_task(client, lead, worker)
    submit = {"command_id": "submit-1", "task_id": "t", "expected_version": 3, "expected_assignment_epoch": 1, "summary": "ok"}
    first = _ok(_cmd(client, worker, "submit", **submit))
    again = _ok(_cmd(client, worker, "submit", **submit))
    assert again["idempotent"] and again["submission_id"] == first["submission_id"]
    assert again["task"]["submissions_total"] == 1
    _err(_cmd(client, worker, "submit", **{**submit, "summary": "changed"}), 409, "command_id_conflict")
    _err(_cmd(client, lead, "submit", **submit), 409, "command_id_conflict")
    # 被拒绝的命令不占用 command_id，修正后可复用。
    stale = _err(_cmd(client, lead, "cancel", command_id="cancel-1", task_id="t", expected_version=1), 409, "version_conflict")
    assert stale["current"]["version"] == 4 and stale["current"]["current_attempt"]["assignee"] == "PC-B"
    _ok(_cmd(client, lead, "cancel", command_id="cancel-1", task_id="t", expected_version=4))


def test_lifecycle_guards(hub):
    client, _, seed = hub
    lead, worker, other = seed("PC-A"), seed("PC-B"), seed("PC-C")
    _active_task(client, lead, worker)
    _err(_cmd(client, lead, "assign", task_id="t", expected_version=3, assignee="PC-C"), 409, "assignment_in_progress")
    _err(_cmd(client, other, "submit", task_id="t", expected_version=3, expected_assignment_epoch=1, summary="x"), 403, "assignment_forbidden")
    _ok(_cmd(client, worker, "submit", task_id="t", expected_version=3, expected_assignment_epoch=1, summary="x"))
    _err(_cmd(client, worker, "review", task_id="t", expected_version=4, decision="approved"), 409, "reviewer_conflict")
    _ok(_cmd(client, other, "review", task_id="t", expected_version=4, decision="approved"))
    _err(_cmd(client, lead, "cancel", task_id="t", expected_version=5), 409, "invalid_task_state")
    _err(_cmd(client, lead, "assign", task_id="t", expected_version=5, assignee="PC-C"), 409, "invalid_task_state")
    _err(_cmd(client, lead, "claim", task_id="missing", expected_version=1, expected_assignment_epoch=1), 404, "task_not_found")


def test_same_device_dispatch_channel_may_review_its_own_dispatch(hub):
    client, _, seed = hub
    device = seed("PC-A")
    _ok(_cmd(client, device, "create", task_id="self", title="Self"))
    _ok(_cmd(client, device, "assign", task_id="self", expected_version=1, assignee="PC-A", channel="dispatch"))
    _ok(_cmd(client, device, "claim", task_id="self", expected_version=2, expected_assignment_epoch=1))
    _ok(_cmd(client, device, "submit", task_id="self", expected_version=3, expected_assignment_epoch=1, summary="x"))
    _err(_cmd(client, device, "review", task_id="self", expected_version=4, decision="approved"), 409, "reviewer_conflict")
    _ok(_cmd(client, device, "review", task_id="self", expected_version=4, decision="approved", channel="dispatch"))


def test_reassign_supersedes_and_stale_executor_actions_conflict(hub):
    client, _, seed = hub
    lead, worker, second = seed("PC-A"), seed("PC-B"), seed("PC-C")
    _active_task(client, lead, worker)
    _ok(_cmd(client, worker, "report", task_id="t", expected_assignment_epoch=1, summary="progress"))
    moved = _ok(_cmd(client, lead, "reassign", task_id="t", expected_version=3, expected_assignment_epoch=1, assignee="PC-C"))
    assert moved["assignment_epoch"] == 2 and moved["task"]["state"] == "open"
    assert [a["status"] for a in moved["task"]["attempts"]] == ["superseded", "offered"]
    _err(_cmd(client, worker, "report", task_id="t", expected_assignment_epoch=1, summary="late"), 409, "assignment_epoch_conflict")
    _err(_cmd(client, worker, "submit", task_id="t", expected_version=4, expected_assignment_epoch=2, summary="x"), 409, "invalid_task_state")
    _ok(_cmd(client, second, "claim", task_id="t", expected_version=4, expected_assignment_epoch=2))


def test_review_must_target_the_latest_submission(hub):
    client, _, seed = hub
    lead, worker = seed("PC-A"), seed("PC-B")
    _active_task(client, lead, worker)
    first = _ok(_cmd(client, worker, "submit", task_id="t", expected_version=3, expected_assignment_epoch=1, summary="v1"))
    _ok(_cmd(client, lead, "review", task_id="t", expected_version=4, decision="changes_requested", summary="again"))
    _ok(_cmd(client, worker, "submit", task_id="t", expected_version=5, expected_assignment_epoch=1, summary="v2"))
    _err(_cmd(client, lead, "review", task_id="t", expected_version=6, decision="approved",
              submission_id=first["submission_id"]), 409, "submission_conflict")
    _ok(_cmd(client, lead, "review", task_id="t", expected_version=6, decision="approved"))


def test_links_never_modify_the_referenced_task(hub):
    client, _, seed = hub
    lead, worker = seed("PC-A"), seed("PC-B")
    _active_task(client, lead, worker)
    before = client.get(BASE + "/tasks/t", headers=lead).json()["task"]
    dependent = _ok(_cmd(client, lead, "create", task_id="d", title="D", depends_on=["t"], parent_task_id="t",
                         produced_memory=["mem_1"]))
    assert dependent["task"]["links"] == {"depends_on": ["t"], "parent_task_id": "t", "produced_memory": ["mem_1"]}
    after = client.get(BASE + "/tasks/t", headers=lead).json()["task"]
    assert {k: after[k] for k in ("title", "state", "version", "current_attempt")} == {k: before[k] for k in ("title", "state", "version", "current_attempt")}
    _err(_cmd(client, lead, "create", task_id="s", title="S", depends_on=["s"]), 422, "link_cycle")
    _err(_cmd(client, lead, "create", task_id="s", title="S", parent_task_id="s"), 422, "link_cycle")
    _err(_cmd(client, lead, "create", task_id="s", title="S", depends_on=["missing"]), 422, "link_target_not_found")
    assert client.get(BASE + "/tasks/s", headers=lead).status_code == 404
    _err(_cmd(client, lead, "create", task_id="d", title="D again"), 409, "task_exists")


def test_input_bounds_are_rejected_without_server_errors(hub):
    client, _, seed = hub
    lead = seed("PC-A")
    _ok(_cmd(client, lead, "create", task_id="t", title="T"))
    _err(_cmd(client, lead, "claim", task_id="t", expected_version=1, expected_assignment_epoch=2147483648), 422, "invalid_input")
    big = client.post(BASE + "/task-commands", headers=lead, content=json.dumps({"action": "create", "command_id": "big",
                      "title": "T", "objective": "x" * 70000}).encode())
    _err(big, 422, "invalid_input")
    _err(client.post(BASE + "/task-commands", headers=lead, content=b"{not json"), 422, "invalid_input")
    _err(client.post(BASE + "/task-commands", headers=lead, json=[1]), 422, "invalid_input")


def test_catalog_filters_exactly_and_pages_stably(hub):
    client, app, seed = hub
    lead = seed("PC-A")
    for task_id, assignee in (("a", "PC-1"), ("b", "PC-10"), ("c", "PC-1-dispatch"), ("d", "PC-1")):
        _ok(_cmd(client, lead, "create", task_id=task_id, title="Rate 50% " + task_id if task_id == "a" else "Rate 500 " + task_id))
        _ok(_cmd(client, lead, "assign", task_id=task_id, expected_version=1, assignee=assignee))
    exact = client.get(BASE + "/tasks", headers=lead, params={"assignee": "PC-1", "state": "working"}).json()
    assert {item["task_id"] for item in exact["items"]} == {"a", "d"}
    literal = client.get(BASE + "/tasks", headers=lead, params={"q": "50%", "state": "all"}).json()
    assert [item["task_id"] for item in literal["items"]] == ["a"]
    first = client.get(BASE + "/tasks", headers=lead, params={"order": "created", "limit": 2, "state": "all"}).json()
    _ok(_cmd(client, lead, "cancel", task_id="a", expected_version=2))  # 翻页期间更新不影响 created 顺序
    second = client.get(BASE + "/tasks", headers=lead, params={"order": "created", "limit": 2, "state": "all",
                                                               "cursor": first["next_cursor"]}).json()
    seen = [item["task_id"] for item in first["items"] + second["items"]]
    assert sorted(seen) == ["a", "b", "c", "d"] and len(set(seen)) == 4 and second["next_cursor"] is None
    assert client.get(BASE + "/tasks", headers=lead, params={"order": "updated", "cursor": first["next_cursor"]}).status_code == 422
    assert client.get(BASE + "/tasks", headers=lead, params={"state": "bogus"}).status_code == 422
    assert client.get(BASE + "/tasks", headers=lead, params={"agent": "PC-1"}).status_code == 422  # 旧子串参数明确拒绝


def test_detail_is_bounded_and_marks_truncation(hub):
    client, _, seed = hub
    lead, worker = seed("PC-A"), seed("PC-B")
    _active_task(client, lead, worker)
    for index in range(55):
        _ok(_cmd(client, worker, "report", task_id="t", expected_assignment_epoch=1, summary=f"r{index}"))
    detail = client.get(BASE + "/tasks/t", headers=lead).json()["task"]
    assert detail["events_total"] == 58 and len(detail["recent_events"]) == 50 and detail["truncated"]
    assert detail["recent_events"][-1]["payload"]["summary"] == "r54"
    assert client.get(BASE + "/tasks/missing", headers=lead).json()["error"] == "task_not_found"


def test_legacy_task_endpoints_fail_explicitly(hub):
    client, _, seed = hub
    lead = seed("PC-A")
    retired = client.get(BASE + "/task-graph", headers=lead)
    assert retired.status_code == 410 and retired.json()["error"] == "task_graph_retired"

    def envelope(operation):
        return {"schema_version": "1.0", "event_id": str(uuid4()), "agent_id": "agent", "agent_instance_id": "i",
                "task_id": "t", "operation": operation, "scope": "project_shared", "content_markdown": None,
                "metadata": {}, "occurred_at": datetime.now(UTC).isoformat(), "content_hash": "sha256:" + uuid4().hex * 2}
    batch = [envelope("record") for _ in range(3)] + [envelope("task_sync")]
    body = _ok(client.post(BASE + "/events/batch", headers=lead, json={"events": batch}))
    assert len(body["accepted"]) == 3 and body["rejected"][0]["code"] == "task_sync_retired"


def test_device_binding_and_enrollment(hub):
    client, app, seed = hub
    unbound = seed(None)
    info = _ok(client.get(BASE + "/connection", headers=unbound))
    assert info["device_name"] is None and info["capabilities"]["task_commands"]
    bound = _ok(client.post(BASE + "/device-binding", headers=unbound, json={"device_name": "PC-OLD"}))
    assert bound["device_name"] == "PC-OLD"
    assert _ok(client.post(BASE + "/device-binding", headers=unbound, json={"device_name": "PC-OLD"}))["device_name"] == "PC-OLD"
    conflict = client.post(BASE + "/device-binding", headers=unbound, json={"device_name": "PC-NEW"})
    assert conflict.status_code == 409
    assert _ok(client.get(BASE + "/connection", headers=unbound))["device_name"] == "PC-OLD"
    assert client.post("/v1/projects/other/device-binding", headers=unbound, json={"device_name": "PC-OLD"}).status_code == 403
    assert client.post(BASE + "/device-binding", headers=unbound, json={"device_name": "_bad"}).status_code == 422
    _ok(_cmd(client, unbound, "create", title="after binding"))

    import hashlib
    import secrets
    app.state.settings = replace(app.state.settings, open_enrollment_projects=("project",))
    tid, raw, hashed = create_token()
    payload = {"enrollment_id": uuid4().hex, "device_name": "NEW-PC", "token_id": tid, "token_secret_hash": hashed,
               "poll_secret_hash": hashlib.sha256(secrets.token_bytes(32)).hexdigest()}
    assert client.post(BASE + "/device-enrollments/open", json=payload).status_code == 200
    fresh = {"Authorization": "Bearer " + raw}
    assert _ok(client.get(BASE + "/connection", headers=fresh))["device_name"] == "NEW-PC"
    created = _ok(_cmd(client, fresh, "create", title="new device", channel="dispatch"))
    assert created["actor"] == "NEW-PC-dispatch"
    with app.state.session_factory() as session:
        assert session.get(Task, ("project", created["task_id"])) is not None
