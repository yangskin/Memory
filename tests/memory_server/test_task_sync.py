"""memory_task_sync 薄客户端测试：转发、身份、待发队列、失败可见与旧数据转换。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from servers.memory_server import memory_task_sync as module
from servers.memory_server.memory_sync_config import SharedMemoryConfig
from servers.memory_server.memory_sync_store import SyncStore

CONNECTION = {"ok": True, "device_name": "PC-A", "capabilities": {"task_commands": True}}


def _config(tmp_path, **options):
    return SimpleNamespace(repo_root=tmp_path, shared_memory=SharedMemoryConfig(
        enabled=True, server_url="https://hub.example", project_id="project/one", local_token="test-token", **options))


class FakeHub:
    """记录请求的 HTTP 替身；命令响应按队列或回调给出。"""

    def __init__(self, monkeypatch, *, connection=None, command=None, gets=None, local="PC-A"):
        self.calls: list[tuple[str, str, object]] = []
        self.connection = CONNECTION if connection is None else connection
        self.command = command or (lambda body: (200, {"ok": True, "task_id": body.get("task_id"), "version": 2}))
        self.gets = gets or {}
        module._device_cache.clear()
        module._capability_cache.clear()
        # 本机设备名固定为计算机名替身，测试不依赖运行机器。
        monkeypatch.setattr(module, "local_device_name", lambda: local)
        hub = self

        def get(_client, path, params, timeout):
            hub.calls.append(("GET", path, params))
            if path.endswith("/connection"):
                return (200, hub.connection) if isinstance(hub.connection, dict) else hub.connection
            for suffix, response in hub.gets.items():
                if path.endswith(suffix):
                    return response(params) if callable(response) else response
            return 404, {"error": "not_found"}

        def post(_client, path, payload, timeout):
            hub.calls.append(("POST", path, payload))
            if path.endswith("/device-binding"):
                return 200, {"ok": True, "device_name": payload["device_name"]}
            return hub.command(payload)

        monkeypatch.setattr(module.MemoryHubClient, "get", get)
        monkeypatch.setattr(module.MemoryHubClient, "post", post)

    def commands(self):
        return [payload for method, path, payload in self.calls if method == "POST" and path.endswith("/task-commands")]


def test_without_a_hub_every_action_requires_one(tmp_path, monkeypatch):
    monkeypatch.setattr(module.MemoryHubClient, "post", lambda *_: pytest.fail("network must not be called"))
    config = SimpleNamespace(repo_root=tmp_path, shared_memory=SharedMemoryConfig(enabled=False))
    for action in ("sync", "create", "report"):
        assert module.task_sync(config, {"action": action, "command_id": "c"})["error"] == "task_hub_required"
    no_project_scope = _config(tmp_path, sync_scopes=("personal",))
    assert module.task_sync(no_project_scope, {"action": "sync"})["error"] == "task_hub_required"
    assert not (tmp_path / ".ai-memory" / "task-graph.db").exists()


def test_inactive_hub_is_not_ready(tmp_path):
    config = SimpleNamespace(repo_root=tmp_path, shared_memory=SimpleNamespace(enabled=True, active=False, sync_scopes=("project_shared",)))
    assert module.task_sync(config, {"action": "sync"})["error"] == "hub_not_ready"


def test_commands_forward_intent_only_and_return_hub_results(tmp_path, monkeypatch):
    hub = FakeHub(monkeypatch, command=lambda body: (200, {"ok": True, "command_id": body["command_id"], "task_id": "t",
                                                          "version": 3, "attempt_id": "attempt_x", "idempotent": False}))
    result = module.task_sync(_config(tmp_path), {"action": "assign", "command_id": "c1", "task_id": "t", "expected_version": 2,
                                                  "assignee": "PC-B", "channel": "dispatch", "agent": "agent:lead",
                                                  "context_token": "ignored", "max_items": 5})
    assert result["ok"] and result["status"] == "accepted" and result["authoritative"] and result["attempt_id"] == "attempt_x"
    assert hub.commands() == [{"action": "assign", "command_id": "c1", "task_id": "t", "expected_version": 2,
                               "assignee": "PC-B", "channel": "dispatch", "agent": "agent-lead"}]
    assert module.task_sync(_config(tmp_path), {"action": "create", "title": "T"})["error"] == "invalid_input"


def test_self_reported_actor_is_rejected_before_network(tmp_path, monkeypatch):
    hub = FakeHub(monkeypatch)
    assert module.task_sync(_config(tmp_path), {"action": "claim", "actor_id": "PC-B", "command_id": "c"})["error"] == "actor_not_accepted"
    assert hub.calls == []


def test_semantic_rejection_is_returned_with_current_state_and_never_queued(tmp_path, monkeypatch):
    current = {"task_id": "t", "state": "open", "version": 4, "assignment_epoch": 2, "current_attempt": {"assignee": "PC-C"}}
    FakeHub(monkeypatch, command=lambda body: (409, {"ok": False, "error": "assignment_epoch_conflict", "message": "moved", "current": current}))
    result = module.task_sync(_config(tmp_path), {"action": "submit", "command_id": "s", "task_id": "t", "expected_version": 3,
                                                  "expected_assignment_epoch": 1, "summary": "x"})
    assert not result["ok"] and result["error"] == "assignment_epoch_conflict" and result["current"] == current
    assert module.TaskCommandOutbox(tmp_path).entries() == []


@pytest.mark.parametrize("status", [0, 408, 429, 503])
def test_unreachable_hub_queues_only_report_and_submit(tmp_path, monkeypatch, status):
    FakeHub(monkeypatch, command=lambda body: (status, {"error": "down"}))
    queued = module.task_sync(_config(tmp_path), {"action": "report", "command_id": "r1", "task_id": "t",
                                                  "expected_assignment_epoch": 1, "summary": "progress"})
    assert queued["ok"] and queued["status"] == "queued" and queued["authoritative"] is False
    refused = module.task_sync(_config(tmp_path), {"action": "claim", "command_id": "c1", "task_id": "t",
                                                   "expected_version": 2, "expected_assignment_epoch": 1})
    assert not refused["ok"] and refused["error"] == "task_authority_unavailable" and refused["retryable"]
    assert [entry["command_id"] for entry in module.TaskCommandOutbox(tmp_path).entries()] == ["r1"]


def test_unreachable_connection_queues_submit_only_for_a_confirmed_hub(tmp_path, monkeypatch):
    FakeHub(monkeypatch, connection=(0, {"error": "down"}), command=lambda body: (0, {"error": "down"}))
    submit = {"action": "submit", "command_id": "s1", "task_id": "t", "expected_version": 3,
              "expected_assignment_epoch": 1, "summary": "done"}
    # 从未确认过支持任务命令的 Hub：不排队（可能是尚未升级的 Hub）。
    unconfirmed = module.task_sync(_config(tmp_path), submit)
    assert not unconfirmed["ok"] and unconfirmed["retryable"] and module.TaskCommandOutbox(tmp_path).entries() == []
    FakeHub(monkeypatch, gets={"/tasks": (200, {"items": [], "next_cursor": None})})
    assert module.task_sync(_config(tmp_path), {"action": "sync"})["ok"]
    FakeHub(monkeypatch, connection=(0, {"error": "down"}), command=lambda body: (0, {"error": "down"}))
    assert module.task_sync(_config(tmp_path), submit)["status"] == "queued"


def test_queue_write_failure_is_an_error(tmp_path, monkeypatch):
    FakeHub(monkeypatch, command=lambda body: (503, {}))
    monkeypatch.setattr(module.TaskCommandOutbox, "enqueue", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    result = module.task_sync(_config(tmp_path), {"action": "report", "command_id": "r", "task_id": "t",
                                                  "expected_assignment_epoch": 1, "summary": "x"})
    assert not result["ok"] and result["error"] == "task_outbox_unavailable"


def test_flush_resends_in_order_with_original_ids_and_surfaces_failures(tmp_path, monkeypatch):
    responses = {"r1": (200, {"ok": True}), "s1": (409, {"error": "assignment_epoch_conflict", "message": "moved",
                                                         "current": {"version": 5}}), "r2": (200, {"ok": True, "idempotent": True})}
    hub = FakeHub(monkeypatch, command=lambda body: responses[body["command_id"]])
    outbox = module.TaskCommandOutbox(tmp_path)
    for command_id, action in (("r1", "report"), ("s1", "submit"), ("r2", "report")):
        outbox.enqueue({"action": action, "command_id": command_id, "task_id": "t", "expected_assignment_epoch": 1, "summary": "x"})
    counts = outbox.flush(_config(tmp_path).shared_memory)
    assert counts == {"sent": 2, "retry": 0, "failed": 1}
    assert [body["command_id"] for body in hub.commands()] == ["r1", "s1", "r2"]
    failures = outbox.failures()
    assert [(f["command_id"], f["error"], f["current"]) for f in failures] == [("s1", "assignment_epoch_conflict", {"version": 5})]
    listed = module.task_sync(_config(tmp_path), {"action": "outbox"})
    assert [entry["command_id"] for entry in listed["entries"]] == ["s1"] and listed["outbox_failures"]
    assert module.task_outbox_failure_count(tmp_path) == 1
    dismissed = module.task_sync(_config(tmp_path), {"action": "outbox_dismiss", "command_ids": ["s1", "missing"]})
    assert dismissed["removed"] == ["s1"] and dismissed["not_failed"] == ["missing"]
    assert module.task_outbox_failure_count(tmp_path) == 0


def test_retryable_head_blocks_later_commands_of_the_same_task_only(tmp_path, monkeypatch):
    responses = {"a1": (503, {}), "a2": (200, {}), "b1": (200, {})}
    hub = FakeHub(monkeypatch, command=lambda body: responses[body["command_id"]])
    outbox = module.TaskCommandOutbox(tmp_path)
    for command_id, task_id in (("a1", "a"), ("a2", "a"), ("b1", "b")):
        outbox.enqueue({"action": "report", "command_id": command_id, "task_id": task_id, "expected_assignment_epoch": 1, "summary": "x"})
    assert outbox.flush(_config(tmp_path).shared_memory) == {"sent": 1, "retry": 1, "failed": 0}
    assert [body["command_id"] for body in hub.commands()] == ["a1", "b1"]
    pending = outbox.entries()
    assert [(entry["command_id"], entry["attempts"]) for entry in pending] == [("a1", 1), ("a2", 0)]


def test_tool_calls_flush_the_queue_before_executing(tmp_path, monkeypatch):
    hub = FakeHub(monkeypatch, command=lambda body: (200, {"ok": True}),
                  gets={"/tasks": (200, {"items": [], "next_cursor": None})})
    module.TaskCommandOutbox(tmp_path).enqueue({"action": "report", "command_id": "r0", "task_id": "t",
                                                "expected_assignment_epoch": 1, "summary": "x"})
    result = module.task_sync(_config(tmp_path), {"action": "sync"})
    assert result["ok"] and [body["command_id"] for body in hub.commands()] == ["r0"]
    assert module.TaskCommandOutbox(tmp_path).entries() == []


def test_sync_scans_this_device_exactly_until_the_end(tmp_path, monkeypatch):
    pages = {None: {"items": [{"task_id": "a"}], "next_cursor": "c1"}, "c1": {"items": [{"task_id": "b"}], "next_cursor": None}}
    hub = FakeHub(monkeypatch, gets={"/tasks": lambda params: (200, pages[params.get("cursor")])})
    result = module.task_sync(_config(tmp_path), {"action": "sync"})
    assert result["ok"] and [task["task_id"] for task in result["tasks"]] == ["a", "b"] and result["device_name"] == "PC-A"
    reads = [params for method, path, params in hub.calls if path.endswith("/tasks")]
    assert reads[0] == {"state": "working", "assignee": "PC-A", "order": "created", "limit": 100}
    assert hub.calls[0][1] == "/v1/projects/project%2Fone/connection"


def test_sync_reports_an_incomplete_scan(tmp_path, monkeypatch):
    FakeHub(monkeypatch, gets={"/tasks": (200, {"items": [], "next_cursor": "same"})})
    assert module.task_sync(_config(tmp_path), {"action": "sync"})["error"] == "hub_scan_incomplete"
    FakeHub(monkeypatch, gets={"/tasks": lambda params: (200, {"items": [{"task_id": "x"}], "next_cursor": f"c{params.get('cursor')}x"})})
    assert module.task_sync(_config(tmp_path), {"action": "sync"})["error"] == "hub_scan_incomplete"


def test_unbound_credentials_are_bound_with_the_computer_name_and_old_hubs_are_rejected(tmp_path, monkeypatch):
    hub = FakeHub(monkeypatch, connection={"ok": True, "device_name": None, "capabilities": {"task_commands": True}},
                  gets={"/tasks": (200, {"items": [], "next_cursor": None})}, local="LEGACY-PC")
    assert module.task_sync(_config(tmp_path), {"action": "sync"})["device_name"] == "LEGACY-PC"
    assert ("POST", "/v1/projects/project%2Fone/device-binding", {"device_name": "LEGACY-PC"}) in hub.calls
    old = FakeHub(monkeypatch, connection={"ok": True, "device_name": "PC-A", "capabilities": {}})
    assert module.task_sync(_config(tmp_path / "old"), {"action": "sync"})["error"] == "hub_version_unsupported"
    assert not [c for c in old.calls if c[0] == "POST"]


def test_bound_name_must_match_this_computer(tmp_path, monkeypatch):
    hub = FakeHub(monkeypatch, local="PC-NEW")
    mismatch = module.task_sync(_config(tmp_path), {"action": "sync"})
    assert mismatch["error"] == "device_identity_mismatch"
    assert mismatch["bound_device"] == "PC-A" and mismatch["local_device"] == "PC-NEW"
    assert not [c for c in hub.calls if c[0] == "POST"]


def test_command_body_keeps_only_fields_the_action_accepts(tmp_path, monkeypatch):
    hub = FakeHub(monkeypatch)
    module.task_sync(_config(tmp_path), {"action": "create", "command_id": "c", "title": "T", "expected_version": 0,
                                         "expected_assignment_epoch": 0, "assignee": "PC-B", "summary": "s"})
    module.task_sync(_config(tmp_path), {"action": "report", "command_id": "r", "task_id": "t", "expected_version": 7,
                                         "expected_assignment_epoch": 1, "summary": "s"})
    assert hub.commands() == [{"action": "create", "command_id": "c", "title": "T", "expected_version": 0},
                              {"action": "report", "command_id": "r", "task_id": "t", "expected_assignment_epoch": 1, "summary": "s"}]


def test_background_flush_waits_for_a_hub_with_task_commands(tmp_path, monkeypatch):
    store = SyncStore(tmp_path / ".ai-memory" / "shared-sync.db")
    store.enqueue("e1", _legacy(tmp_path, "TaskReported", command_id="r", summary="progress"), "h")
    FakeHub(monkeypatch, connection={"ok": True, "device_name": "PC-A", "capabilities": {}})
    assert module.flush_task_outbox(_config(tmp_path)) == {"sent": 0, "retry": 0, "failed": 0}
    assert [row["event_id"] for row in store.due_events(10)] == ["e1"]  # 旧 Hub：旧事件照常走原协议上传
    hub = FakeHub(monkeypatch)
    assert module.flush_task_outbox(_config(tmp_path))["sent"] == 1
    assert [body["command_id"] for body in hub.commands()] == ["r"]


def test_capability_is_rechecked_after_an_old_hub_upgrades(tmp_path, monkeypatch):
    """长驻进程先看到旧 Hub：不支持的结果按间隔复查，Hub 升级后无需重启即可开始补发。"""
    clock = [1000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    old = FakeHub(monkeypatch, connection={"ok": True, "device_name": "PC-A", "capabilities": {}})
    config = _config(tmp_path)
    assert module.hub_supports_task_commands(config) is False
    assert module.hub_supports_task_commands(config) is False
    assert len([c for c in old.calls if c[1].endswith("/connection")]) == 1  # 间隔内使用缓存
    old.connection = CONNECTION
    clock[0] += module.CAPABILITY_RECHECK_SECONDS + 1
    assert module.hub_supports_task_commands(config) is True
    old.connection = {"ok": True, "device_name": "PC-A", "capabilities": {}}
    clock[0] += module.CAPABILITY_RECHECK_SECONDS + 1
    assert module.hub_supports_task_commands(config) is True  # 支持的结果永久缓存


def test_events_rejected_as_retired_are_still_converted(tmp_path, monkeypatch):
    store = SyncStore(tmp_path / ".ai-memory" / "shared-sync.db")
    store.enqueue("e1", _legacy(tmp_path, "TaskReported", command_id="r", summary="progress"), "h")
    store.reject("e1", "task_sync_retired")
    assert module.migrate_legacy_task_data(_config(tmp_path)) == {"converted": 1, "failed": 0}


def test_reads_validate_inputs_and_responses(tmp_path, monkeypatch):
    event = {"seq": 3, "task_seq": 2, "event_type": "TaskClaimed"}
    hub = FakeHub(monkeypatch, gets={"/task-events": (200, {"events": [event], "cursor": "3"}),
                                     "/tasks/a%2Fb": (200, {"ok": True, "task": {"task_id": "a/b"}})})
    history = module.task_sync(_config(tmp_path), {"action": "history", "task_id": "t", "cursor": 1, "max_items": 20})
    assert history["events"] == [event] and history["cursor"] == "3"
    assert module.task_sync(_config(tmp_path), {"action": "task", "task_id": "a/b"})["task"] == {"task_id": "a/b"}
    for args in ({"cursor": -1}, {"max_items": 0}, {"max_items": True}, {"task_id": 5}):
        assert module.task_sync(_config(tmp_path), {"action": "history", **args})["error"] == "invalid_input"
    assert module.task_sync(_config(tmp_path), {"action": "task"})["error"] == "invalid_input"
    FakeHub(monkeypatch, gets={"/task-events": (200, {"events": {}, "cursor": "x"})})
    assert module.task_sync(_config(tmp_path), {"action": "history"})["error"] == "invalid_hub_response"
    FakeHub(monkeypatch, gets={"/tasks/t": (404, {"ok": False, "error": "task_not_found", "message": "missing"})})
    missing = module.task_sync(_config(tmp_path), {"action": "task", "task_id": "t"})
    assert missing["error"] == "task_not_found" and missing["remote_status"] == 404
    assert len([c for c in hub.calls if c[1].endswith("/connection")]) == 1


def _legacy(tmp_path, event_type, **fields):
    event = {"command_id": fields.pop("command_id"), "event_type": event_type, "task_id": "t", "actor_id": fields.pop("actor_id", "PC-A"),
             "expected_version": fields.pop("expected_version", 3), "expected_assignment_epoch": 1, "payload": fields}
    return {"operation": "task_sync", "metadata": {"task_event": event}}


def test_legacy_task_events_are_converted_before_the_old_upload_can_lose_them(tmp_path, monkeypatch):
    store = SyncStore(tmp_path / ".ai-memory" / "shared-sync.db")
    store.enqueue("e1", _legacy(tmp_path, "TaskReported", command_id="r", summary="progress"), "h")
    store.enqueue("e2", _legacy(tmp_path, "TaskSubmitted", command_id="s", expected_version=4, summary="done",
                                evidence=["pytest"], actor_id="PC-A-dispatch"), "h")
    store.enqueue("e3", _legacy(tmp_path, "TaskReassigned", command_id="x"), "h")
    store.enqueue("e4", {"operation": "record", "metadata": {}}, "h")
    graph = tmp_path / ".ai-memory" / "task-graph.db"
    graph.write_bytes(b"legacy")
    counts = module.migrate_legacy_task_data(_config(tmp_path))
    assert counts == {"converted": 2, "failed": 1} and not graph.exists()
    entries = {entry["command_id"]: entry for entry in module.TaskCommandOutbox(tmp_path).entries()}
    assert entries["x"]["state"] == "failed" and entries["x"]["error"] == "legacy_task_event_not_replayable"
    connection = module.TaskCommandOutbox(tmp_path)._connect()
    requests = {row["command_id"]: json.loads(row["request"]) for row in connection.execute("SELECT * FROM pending_commands WHERE state='pending'")}
    connection.close()
    assert requests["r"] == {"command_id": "r", "task_id": "t", "expected_assignment_epoch": 1, "action": "report", "summary": "progress"}
    assert requests["s"]["expected_version"] == 3 and requests["s"]["channel"] == "dispatch" and "actor_id" not in requests["s"]
    remaining = [row["event_id"] for row in store.due_events(10)]
    assert remaining == ["e4"]
    assert module.migrate_legacy_task_data(_config(tmp_path)) == {"converted": 0, "failed": 0}
