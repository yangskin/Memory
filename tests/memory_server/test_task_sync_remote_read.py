from __future__ import annotations

from types import SimpleNamespace

import pytest

from servers.memory_server import memory_task_sync as module
from servers.memory_server.memory_sync_config import SharedMemoryConfig


def _config(tmp_path, **options):
    return SimpleNamespace(repo_root=tmp_path, shared_memory=SharedMemoryConfig(
        enabled=True, server_url="https://hub.example", project_id="project/one",
        local_token="test-token", **options,
    ))


def _bundle():
    return {"version": "1.0", "roots": {key: [] for key in ("current", "assigned", "review", "attention")},
            "nodes": [{"id": "task-id", "type": "task", "key": "remote-task", "metadata": {"state": "active", "version": 7}}],
            "edges": [], "cursor": "17"}


def _snapshot():
    return {"task_id": "remote-task", "title": "Remote task", "objective": "", "acceptance": "",
            "priority": "normal", "state": "active", "version": 7, "assignment_epoch": 2,
            "current_attempt": {"attempt_id": "remote-attempt", "status": "active", "assignee": "hex34-owner", "epoch": 2}}


def test_shared_sync_reads_remote_state_without_creating_local_store(tmp_path, monkeypatch):
    calls = []
    expected = _bundle()

    def get(self, path, params, timeout):
        calls.append((path, params, timeout))
        return 200, {"items": [_snapshot()]} if path.endswith("/tasks") else expected

    monkeypatch.setattr(module.MemoryHubClient, "get", get)
    result = module.task_sync(_config(tmp_path), {"action": "sync", "task_id": "remote-task", "agent_id": "hex34-owner", "cursor": 5})
    assert result["ok"] and result["source"] == "remote" and result["authoritative"]
    assert result["bundle"] == expected
    assert calls == [("/v1/projects/project%2Fone/task-graph", {"task_id": "remote-task", "agent_id": "hex34-owner"}, 1.2),
                     ("/v1/projects/project%2Fone/tasks", {"state": "all", "q": "remote-task", "limit": 100}, 1.2)]
    assert result["task_snapshot"]["version"] == 7
    assert result["bundle"]["nodes"][0]["metadata"]["assignment_epoch"] == 2
    assert not (tmp_path / ".ai-memory/task-graph.db").exists()


def test_remote_history_preserves_versions_epochs_and_pagination(tmp_path, monkeypatch):
    event = {"seq": 12, "task_version": 7, "assignment_epoch": 2, "event_type": "TaskClaimed",
             "command_id": "claim", "task_id": "remote-task", "actor_id": "worker", "expected_version": 6,
             "expected_assignment_epoch": 2, "payload": {}, "occurred_at": "2026-10-16T00:00:00Z"}

    def get(self, path, params, timeout):
        assert path.endswith("/task-events")
        assert params == {"task_id": "remote-task", "cursor": 10, "max_items": 200}
        return 200, {"events": [event], "cursor": "12"}

    monkeypatch.setattr(module.MemoryHubClient, "get", get)
    result = module.task_sync(_config(tmp_path), {"action": "history", "task_id": "remote-task", "cursor": 10, "max_items": 999})
    assert result["events"] == [event] and result["cursor"] == "12" and result["authoritative"]


@pytest.mark.parametrize("status", [0, 401, 403, 404, 429, 503])
def test_remote_failure_never_reports_local_empty_success(tmp_path, monkeypatch, status):
    monkeypatch.setattr(module.MemoryHubClient, "get", lambda *_: (status, {"error": "failed"}))
    result = module.task_sync(_config(tmp_path), {"action": "sync", "task_id": "remote-task"})
    assert not result["ok"] and result["remote_status"] == status
    assert "bundle" not in result and result["source"] == "remote"
    assert not (tmp_path / ".ai-memory/task-graph.db").exists()


@pytest.mark.parametrize("body", [{}, {"events": [], "cursor": "bad"}, {"events": {}, "cursor": "0"}, {"roots": {}, "nodes": [], "edges": [], "cursor": "0"}])
@pytest.mark.parametrize("action", ["sync", "history"])
def test_invalid_remote_schema_is_an_explicit_error(tmp_path, monkeypatch, body, action):
    monkeypatch.setattr(module.MemoryHubClient, "get", lambda *_: (200, body))
    result = module.task_sync(_config(tmp_path), {"action": action})
    assert not result["ok"] and result["error"] == "task_authority_invalid_response"


@pytest.mark.parametrize("options", [{"read_enabled": False}, {"enabled": True, "server_url": ""}])
def test_inactive_or_read_disabled_shared_mode_does_not_read_local_store(tmp_path, monkeypatch, options):
    config = _config(tmp_path)
    config.shared_memory = SimpleNamespace(active=False, enabled=True, **{k: v for k, v in options.items() if k != "enabled"})
    monkeypatch.setattr(module.MemoryHubClient, "get", lambda *_: pytest.fail("network must not be called"))
    assert module.task_sync(config, {"action": "sync"})["error"] == "task_authority_unavailable"


@pytest.mark.parametrize("args", [{"cursor": -1}, {"task_id": 5}, {"max_items": 0}, {"cursor": True}])
def test_invalid_query_is_rejected_before_network(tmp_path, monkeypatch, args):
    monkeypatch.setattr(module.MemoryHubClient, "get", lambda *_: pytest.fail("network must not be called"))
    assert module.task_sync(_config(tmp_path), {"action": "history", **args})["error"] == "invalid_input"


def test_nonshared_mode_keeps_local_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(module.MemoryHubClient, "get", lambda *_: pytest.fail("network must not be called"))
    result = module.task_sync(SimpleNamespace(repo_root=tmp_path), {"action": "sync"})
    assert result["ok"] and result["bundle"]["nodes"] == []


def test_placeholder_task_is_enriched_from_authoritative_catalog(tmp_path, monkeypatch):
    graph = _bundle()
    graph["nodes"][0]["metadata"] = {"external": True}
    def get(self, path, params, timeout):
        return (200, {"items": [_snapshot()]}) if path.endswith("/tasks") else (200, graph)
    monkeypatch.setattr(module.MemoryHubClient, "get", get)
    result = module.task_sync(_config(tmp_path), {"action": "sync", "task_id": "remote-task"})
    assert result["bundle"]["nodes"][0]["metadata"]["version"] == 7
    assert "external" not in result["bundle"]["nodes"][0]["metadata"]


def test_catalog_failure_does_not_expose_placeholder_as_a_valid_task(tmp_path, monkeypatch):
    def get(self, path, params, timeout):
        return (403, {}) if path.endswith("/tasks") else (200, _bundle())
    monkeypatch.setattr(module.MemoryHubClient, "get", get)
    result = module.task_sync(_config(tmp_path), {"action": "sync", "task_id": "remote-task"})
    assert not result["ok"] and "bundle" not in result


def test_cross_machine_claim_replays_only_accepted_events_and_posts_one_claim(tmp_path, monkeypatch):
    remote_root = tmp_path / "other-machine"
    other = SimpleNamespace(repo_root=remote_root)
    create = module.task_sync(other, {"action": "create", "command_id": "remote-create", "expected_version": 0,
                                      "task_id": "remote-task", "actor_id": "lead", "title": "Remote task"})
    assign = module.task_sync(other, {"action": "assign", "command_id": "remote-assign", "expected_version": 1,
                                      "task_id": "remote-task", "actor_id": "lead", "assignee": "worker"})
    events = []
    for seq, result in enumerate((create, assign), 1):
        events.append({**result["task_event"], "seq": seq})
    def get(self, path, params, timeout):
        assert path.endswith("/task-events")
        selected = [event for event in events if event["seq"] > params["cursor"]]
        return 200, {"events": selected, "cursor": str(selected[-1]["seq"] if selected else params["cursor"])}
    posted = []
    def post(self, path, payload, timeout):
        posted.extend(payload["events"])
        return 200, {"accepted": [event["event_id"] for event in payload["events"]]}
    monkeypatch.setattr(module.MemoryHubClient, "get", get)
    monkeypatch.setattr(module.MemoryHubClient, "post", post)
    result = module.task_sync(_config(tmp_path), {"action": "claim", "command_id": "worker-claim", "task_id": "remote-task",
                                               "actor_id": "worker", "expected_version": 2, "expected_assignment_epoch": 1})
    assert result["ok"] and result["version"] == 3
    assert result["shared_sync"]["mode"] == "hub_authoritative"
    assert [event["metadata"]["task_event"]["event_type"] for event in posted] == ["TaskClaimed"]
    assert result["task_event"]["payload"]["attempt_id"] == assign["task_event"]["payload"]["attempt_id"]


def test_exact_catalog_match_on_later_page(tmp_path, monkeypatch):
    def get(self, path, params, timeout):
        if path.endswith("/task-graph"):
            return 200, _bundle()
        if "cursor" not in params:
            return 200, {"items": [{"task_id": "other"}], "next_cursor": "page-two"}
        assert params["cursor"] == "page-two"
        return 200, {"items": [_snapshot()], "next_cursor": None}
    monkeypatch.setattr(module.MemoryHubClient, "get", get)
    result = module.task_sync(_config(tmp_path), {"action": "sync", "task_id": "remote-task"})
    assert result["ok"] and result["task_snapshot"]["task_id"] == "remote-task"


def test_cross_machine_accepted_retry_stays_idempotent_without_reposting(tmp_path, monkeypatch):
    other = SimpleNamespace(repo_root=tmp_path / "other")
    commands = [
        {"action": "create", "command_id": "c", "expected_version": 0, "task_id": "remote-task", "actor_id": "lead", "title": "Task"},
        {"action": "assign", "command_id": "a", "expected_version": 1, "task_id": "remote-task", "actor_id": "lead", "assignee": "worker"},
        {"action": "claim", "command_id": "k", "expected_version": 2, "expected_assignment_epoch": 1, "task_id": "remote-task", "actor_id": "worker"},
    ]
    events = [{**module.task_sync(other, command)["task_event"], "seq": i} for i, command in enumerate(commands, 1)]
    def get(self, path, params, timeout):
        selected = [event for event in events if event["seq"] > params["cursor"]]
        return 200, {"events": selected, "cursor": str(selected[-1]["seq"] if selected else params["cursor"])}
    monkeypatch.setattr(module.MemoryHubClient, "get", get)
    monkeypatch.setattr(module.MemoryHubClient, "post", lambda *_: pytest.fail("retry must not post"))
    config = _config(tmp_path)
    for _ in range(2):
        result = module.task_sync(config, commands[-1])
        assert result["ok"] and result["idempotent"] and result["version"] == 3
    monkeypatch.setattr(module.MemoryHubClient, "get", lambda *_: pytest.fail("reconciled retries are local"))
    assert module.task_sync(config, commands[-1])["idempotent"]
    changed = module.task_sync(config, {**commands[-1], "actor_id": "someone-else"})
    assert changed["error"] == "command_id_conflict"


def test_reference_placeholder_does_not_force_hydration_of_existing_task(tmp_path, monkeypatch):
    local = SimpleNamespace(repo_root=tmp_path)
    module.task_sync(local, {"action": "create", "command_id": "c", "expected_version": 0, "task_id": "t", "actor_id": "lead", "title": "T"})
    module.task_sync(local, {"action": "assign", "command_id": "a", "expected_version": 1, "task_id": "t", "actor_id": "lead", "assignee": "worker"})
    module.task_sync(local, {"action": "claim", "command_id": "k", "expected_version": 2, "expected_assignment_epoch": 1, "task_id": "t", "actor_id": "worker"})
    module.task_sync(local, {"action": "create", "command_id": "dependent", "expected_version": 0, "task_id": "d", "actor_id": "lead", "title": "D", "depends_on": ["t"]})
    monkeypatch.setattr(module.MemoryHubClient, "get", lambda *_: pytest.fail("canonical task row already has the expected version"))
    def post(self, path, payload, timeout):
        return 200, {"accepted": [e["event_id"] for e in payload["events"]]}
    monkeypatch.setattr(module.MemoryHubClient, "post", post)
    result = module.task_sync(_config(tmp_path), {"action": "report", "command_id": "r", "expected_version": 3,
                                                "expected_assignment_epoch": 1, "task_id": "t", "actor_id": "worker", "summary": "OK"})
    assert result["ok"] and result["version"] == 4


def test_accepted_cancel_without_reason_is_retry_safe(tmp_path, monkeypatch):
    local = SimpleNamespace(repo_root=tmp_path)
    create = {"action": "create", "command_id": "c", "expected_version": 0, "task_id": "t", "actor_id": "lead", "title": "T"}
    cancel = {"action": "cancel", "command_id": "cancel", "expected_version": 1, "task_id": "t", "actor_id": "lead"}
    events = [{**module.task_sync(local, cmd)["task_event"], "seq": i} for i, cmd in enumerate([create, cancel], 1)]
    def get(self, path, params, timeout):
        selected = [e for e in events if e["seq"] > params["cursor"]]
        return 200, {"events": selected, "cursor": str(selected[-1]["seq"] if selected else params["cursor"])}
    monkeypatch.setattr(module.MemoryHubClient, "get", get)
    monkeypatch.setattr(module.MemoryHubClient, "post", lambda *_: pytest.fail("retry must not post"))
    assert module.task_sync(_config(tmp_path), cancel)["idempotent"]


@pytest.mark.parametrize("body,action", [({"version": "1.0", "roots": {key: [] for key in ("current", "assigned", "review", "attention")}, "nodes": [None], "edges": [], "cursor": "0"}, "sync"),
                                       ({"events": [None], "cursor": "0"}, "history"),
                                       ({"events": [{}], "cursor": "0"}, "history")])
def test_nested_malformed_records_are_errors(tmp_path, monkeypatch, body, action):
    monkeypatch.setattr(module.MemoryHubClient, "get", lambda *_: (200, body))
    assert module.task_sync(_config(tmp_path), {"action": action})["error"] == "task_authority_invalid_response"


def test_malformed_catalog_task_is_error(tmp_path, monkeypatch):
    broken = _snapshot()
    del broken["title"]
    def get(self, path, params, timeout):
        return (200, {"items": [broken]}) if path.endswith("/tasks") else (200, _bundle())
    monkeypatch.setattr(module.MemoryHubClient, "get", get)
    assert module.task_sync(_config(tmp_path), {"action": "sync", "task_id": "remote-task"})["error"] == "task_authority_invalid_response"


@pytest.mark.parametrize("action", ["report", "submit"])
def test_offline_pending_command_retries_use_local_deduplication(tmp_path, monkeypatch, action):
    local = SimpleNamespace(repo_root=tmp_path)
    commands = [
        {"action": "create", "command_id": "c", "expected_version": 0, "task_id": "t", "actor_id": "lead", "title": "T"},
        {"action": "assign", "command_id": "a", "expected_version": 1, "task_id": "t", "actor_id": "lead", "assignee": "worker"},
        {"action": "claim", "command_id": "k", "expected_version": 2, "expected_assignment_epoch": 1, "task_id": "t", "actor_id": "worker"},
    ]
    for command in commands:
        assert module.task_sync(local, command)["ok"]
    monkeypatch.setattr(module.MemoryHubClient, "get", lambda *_: pytest.fail("local retry must not read Hub"))
    monkeypatch.setattr(module.MemoryHubClient, "post", lambda *_: (503, {"error": "unavailable"}))
    command = {"action": action, "command_id": "pending", "expected_version": 3, "expected_assignment_epoch": 1,
               "task_id": "t", "actor_id": "worker", "summary": "offline work"}
    config = _config(tmp_path)
    first = module.task_sync(config, command)
    assert first["ok"] and first["shared_sync"]["mode"] == "offline_pending"
    monkeypatch.setattr(module.MemoryHubClient, "post", lambda *_: pytest.fail("retry must not write Hub"))
    retry = module.task_sync(config, command)
    assert retry["ok"] and retry["idempotent"] and retry["shared_sync"]["mode"] == "offline_pending"


@pytest.mark.parametrize("explicit_task_id", [True, False])
def test_creation_retry_after_hydration_keeps_accepted_identity(tmp_path, monkeypatch, explicit_task_id):
    other = SimpleNamespace(repo_root=tmp_path / "other")
    create = {"action": "create", "command_id": "original-create", "expected_version": 0, "actor_id": "lead", "title": "Task"}
    if explicit_task_id:
        create["task_id"] = "t"
    created = module.task_sync(other, create)
    tid = created["task_id"]
    assigned = module.task_sync(other, {"action": "assign", "command_id": "a", "expected_version": 1,
                                       "task_id": tid, "actor_id": "lead", "assignee": "worker"})
    events = [{**item["task_event"], "seq": i} for i, item in enumerate((created, assigned), 1)]
    def get(self, path, params, timeout):
        selected = [e for e in events if e["seq"] > params["cursor"]]
        return 200, {"events": selected, "cursor": str(selected[-1]["seq"] if selected else params["cursor"])}
    def post(self, path, payload, timeout):
        for event in payload["events"]:
            events.append({**event["metadata"]["task_event"], "seq": len(events) + 1})
        return 200, {"accepted": [e["event_id"] for e in payload["events"]]}
    monkeypatch.setattr(module.MemoryHubClient, "get", get)
    monkeypatch.setattr(module.MemoryHubClient, "post", post)
    config = _config(tmp_path)
    claimed = module.task_sync(config, {"action": "claim", "command_id": "k", "expected_version": 2,
                                      "expected_assignment_epoch": 1, "task_id": tid, "actor_id": "worker"})
    assert claimed["ok"]
    monkeypatch.setattr(module.MemoryHubClient, "post", lambda *_: pytest.fail("creation retry must not post"))
    retried = module.task_sync(config, create)
    assert retried["ok"] and retried["idempotent"] and retried["task_id"] == tid and retried["version"] == 1


@pytest.mark.parametrize("automatic_task_id", [False, True])
def test_accepted_retry_uses_execution_normalization_and_defaults(tmp_path, monkeypatch, automatic_task_id):
    other = SimpleNamespace(repo_root=tmp_path / "other")
    create = {"action": "create", "command_id": "  c  ", "expected_version": 0, "actor_id": "  lead  ", "title": "  Task  ", "priority": None}
    if not automatic_task_id:
        create["task_id"] = "  t  "
    created = module.task_sync(other, create)
    assert created["ok"]
    tid = created["task_id"]
    assign = {"action": "assign", "command_id": "  a  ", "expected_version": 1, "task_id": "  " + tid + "  ",
              "actor_id": "  lead  ", "assignee": "  worker  ", "attempt_id": "  attempt-explicit  "}
    assigned = module.task_sync(other, assign)
    assert assigned["ok"]
    events = [{**item["task_event"], "seq": i} for i, item in enumerate((created, assigned), 1)]
    def get(self, path, params, timeout):
        selected = [e for e in events if e["seq"] > params["cursor"]]
        return 200, {"events": selected, "cursor": str(selected[-1]["seq"] if selected else params["cursor"])}
    monkeypatch.setattr(module.MemoryHubClient, "get", get)
    monkeypatch.setattr(module.MemoryHubClient, "post", lambda *_: pytest.fail("accepted retry must not post"))
    config = _config(tmp_path)
    for cmd in (assign, create):
        result = module.task_sync(config, cmd)
        assert result["ok"] and result["idempotent"]
