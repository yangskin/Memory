from __future__ import annotations

import json
from types import SimpleNamespace

from servers.memory_server import memory_task_sync as task_sync_module
from servers.memory_server.memory_sync_store import SyncStore
from servers.memory_server.memory_task_sync import enqueue_task_sync_event, task_sync


def test_create_task_persists_event_projection_and_graph_bundle(tmp_path) -> None:
    config = SimpleNamespace(repo_root=tmp_path)
    created = task_sync(
        config,
        {
            "action": "create",
            "command_id": "create-task-1",
            "expected_version": 0,
            "task_id": "task-1",
            "actor_id": "agent:lead",
            "title": "Implement task graph",
            "objective": "Persist graph-backed task state.",
            "acceptance": "A task node is visible in the graph bundle.",
            "depends_on": ["task-0"],
            "produced_memory": ["memory:task-design"],
        },
    )

    assert created["ok"] is True
    assert created["version"] == 1
    assert created["task_event"]["event_type"] == "TaskCreated"
    assert created["bundle"]["roots"]["attention"] == ["task:task-1"]
    assert {edge["relation_type"] for edge in created["bundle"]["edges"]} == {"depends_on", "produced_memory"}

    bundle = task_sync(config, {"action": "sync", "task_id": "task-1"})

    assert bundle["ok"] is True
    assert bundle["bundle"]["cursor"] == "1"
    assert {node["id"] for node in bundle["bundle"]["nodes"]} == {
        "task:task-0",
        "task:task-1",
        "asset:memory:task-design",
    }


def test_task_lifecycle_projects_attempt_submission_and_review_history(tmp_path) -> None:
    config = SimpleNamespace(repo_root=tmp_path)

    created = task_sync(
        config,
        {
            "action": "create",
            "command_id": "lifecycle-create",
            "expected_version": 0,
            "task_id": "task-lifecycle",
            "actor_id": "agent:lead",
            "title": "Lifecycle task",
        },
    )
    assigned = task_sync(
        config,
        {
            "action": "assign",
            "command_id": "lifecycle-assign",
            "expected_version": created["version"],
            "task_id": "task-lifecycle",
            "actor_id": "agent:lead",
            "assignee": "agent:worker",
        },
    )
    claimed = task_sync(
        config,
        {
            "action": "claim",
            "command_id": "lifecycle-claim",
            "expected_version": assigned["version"],
            "expected_assignment_epoch": assigned["assignment_epoch"],
            "task_id": "task-lifecycle",
            "actor_id": "agent:worker",
        },
    )
    blocked = task_sync(
        config,
        {
            "action": "block",
            "command_id": "lifecycle-block",
            "expected_version": claimed["version"],
            "expected_assignment_epoch": claimed["assignment_epoch"],
            "task_id": "task-lifecycle",
            "actor_id": "agent:worker",
            "reason": "Waiting for an API contract.",
        },
    )
    resumed = task_sync(
        config,
        {
            "action": "resume",
            "command_id": "lifecycle-resume",
            "expected_version": blocked["version"],
            "expected_assignment_epoch": blocked["assignment_epoch"],
            "task_id": "task-lifecycle",
            "actor_id": "agent:worker",
        },
    )
    reported = task_sync(
        config,
        {
            "action": "report",
            "command_id": "lifecycle-report",
            "expected_version": resumed["version"],
            "expected_assignment_epoch": resumed["assignment_epoch"],
            "task_id": "task-lifecycle",
            "actor_id": "agent:worker",
            "summary": "Implementation complete; tests are next.",
        },
    )
    submitted = task_sync(
        config,
        {
            "action": "submit",
            "command_id": "lifecycle-submit-1",
            "expected_version": reported["version"],
            "expected_assignment_epoch": reported["assignment_epoch"],
            "task_id": "task-lifecycle",
            "actor_id": "agent:worker",
            "summary": "First implementation",
            "evidence": ["pytest tests/task_sync.py"],
        },
    )
    requested_changes = task_sync(
        config,
        {
            "action": "review",
            "command_id": "lifecycle-review-1",
            "expected_version": submitted["version"],
            "task_id": "task-lifecycle",
            "actor_id": "agent:reviewer",
            "decision": "changes_requested",
            "summary": "Please add an epoch conflict test.",
        },
    )
    resubmitted = task_sync(
        config,
        {
            "action": "submit",
            "command_id": "lifecycle-submit-2",
            "expected_version": requested_changes["version"],
            "expected_assignment_epoch": requested_changes["assignment_epoch"],
            "task_id": "task-lifecycle",
            "actor_id": "agent:worker",
            "summary": "Implementation with conflict coverage",
        },
    )
    approved = task_sync(
        config,
        {
            "action": "review",
            "command_id": "lifecycle-review-2",
            "expected_version": resubmitted["version"],
            "task_id": "task-lifecycle",
            "actor_id": "agent:reviewer",
            "decision": "approved",
            "summary": "Approved.",
        },
    )

    assert approved["ok"] is True
    assert approved["version"] == 10
    bundle = approved["bundle"]
    assert bundle["roots"]["review"] == []
    task_node = next(node for node in bundle["nodes"] if node["id"] == "task:task-lifecycle")
    assert task_node["metadata"]["state"] == "done"
    assert {node["type"] for node in bundle["nodes"]} >= {"task", "agent", "attempt", "submission", "review"}
    assert {edge["relation_type"] for edge in bundle["edges"]} >= {
        "current_attempt",
        "assigned_to",
        "assigned_by",
        "has_submission",
        "has_review",
    }
    history = task_sync(config, {"action": "history", "task_id": "task-lifecycle"})
    assert [event["event_type"] for event in history["events"]] == [
        "TaskCreated",
        "TaskAssigned",
        "TaskClaimed",
        "TaskBlocked",
        "TaskResumed",
        "TaskReported",
        "TaskSubmitted",
        "TaskReviewed",
        "TaskSubmitted",
        "TaskReviewed",
    ]


def test_review_rejects_the_submission_executor_and_preserves_review_state(tmp_path) -> None:
    config = SimpleNamespace(repo_root=tmp_path)
    created = task_sync(
        config,
        {
            "action": "create",
            "command_id": "self-review-create",
            "expected_version": 0,
            "task_id": "task-self-review",
            "actor_id": "agent:lead",
            "title": "Independent review task",
        },
    )
    assigned = task_sync(
        config,
        {
            "action": "assign",
            "command_id": "self-review-assign",
            "expected_version": created["version"],
            "task_id": "task-self-review",
            "actor_id": "agent:lead",
            "assignee": "agent:worker",
        },
    )
    claimed = task_sync(
        config,
        {
            "action": "claim",
            "command_id": "self-review-claim",
            "expected_version": assigned["version"],
            "expected_assignment_epoch": assigned["assignment_epoch"],
            "task_id": "task-self-review",
            "actor_id": "agent:worker",
        },
    )
    submitted = task_sync(
        config,
        {
            "action": "submit",
            "command_id": "self-review-submit",
            "expected_version": claimed["version"],
            "expected_assignment_epoch": claimed["assignment_epoch"],
            "task_id": "task-self-review",
            "actor_id": "agent:worker",
            "summary": "Ready for independent review.",
        },
    )
    rejected = task_sync(
        config,
        {
            "action": "review",
            "command_id": "self-review-attempt",
            "expected_version": submitted["version"],
            "task_id": "task-self-review",
            "actor_id": "agent:worker",
            "decision": "approved",
        },
    )
    bundle = task_sync(config, {"action": "sync", "task_id": "task-self-review"})
    history = task_sync(config, {"action": "history", "task_id": "task-self-review"})

    assert rejected["ok"] is False
    assert rejected["error"] == "reviewer_conflict"
    assert bundle["bundle"]["roots"]["review"] == ["task:task-self-review"]
    assert [event["event_type"] for event in history["events"]] == [
        "TaskCreated",
        "TaskAssigned",
        "TaskClaimed",
        "TaskSubmitted",
    ]


def test_reassignment_rejects_stale_epoch_and_command_replay_is_idempotent(tmp_path) -> None:
    config = SimpleNamespace(repo_root=tmp_path)
    create_args = {
        "action": "create",
        "command_id": "epoch-create",
        "expected_version": 0,
        "task_id": "task-epoch",
        "actor_id": "agent:lead",
        "title": "Epoch task",
    }
    created = task_sync(config, create_args)
    replayed = task_sync(config, create_args)
    assigned = task_sync(
        config,
        {
            "action": "assign",
            "command_id": "epoch-assign",
            "expected_version": created["version"],
            "task_id": "task-epoch",
            "actor_id": "agent:lead",
            "assignee": "agent:worker-a",
        },
    )
    claimed = task_sync(
        config,
        {
            "action": "claim",
            "command_id": "epoch-claim",
            "expected_version": assigned["version"],
            "expected_assignment_epoch": assigned["assignment_epoch"],
            "task_id": "task-epoch",
            "actor_id": "agent:worker-a",
        },
    )
    reassigned = task_sync(
        config,
        {
            "action": "reassign",
            "command_id": "epoch-reassign",
            "expected_version": claimed["version"],
            "expected_assignment_epoch": claimed["assignment_epoch"],
            "task_id": "task-epoch",
            "actor_id": "agent:lead",
            "assignee": "agent:worker-b",
        },
    )
    stale_submit = task_sync(
        config,
        {
            "action": "submit",
            "command_id": "epoch-stale-submit",
            "expected_version": reassigned["version"],
            "expected_assignment_epoch": claimed["assignment_epoch"],
            "task_id": "task-epoch",
            "actor_id": "agent:worker-a",
            "summary": "Late submission",
        },
    )

    assert replayed["ok"] is True
    assert replayed["idempotent"] is True
    assert reassigned["assignment_epoch"] == 2
    assert stale_submit["ok"] is False
    assert stale_submit["error"] == "assignment_epoch_conflict"


def test_decline_then_cancel_leaves_a_terminal_task_projection(tmp_path) -> None:
    config = SimpleNamespace(repo_root=tmp_path)
    created = task_sync(
        config,
        {
            "action": "create",
            "command_id": "decline-create",
            "expected_version": 0,
            "task_id": "task-decline",
            "actor_id": "agent:lead",
            "title": "Declined task",
        },
    )
    assigned = task_sync(
        config,
        {
            "action": "assign",
            "command_id": "decline-assign",
            "expected_version": created["version"],
            "task_id": "task-decline",
            "actor_id": "agent:lead",
            "assignee": "agent:worker",
        },
    )
    declined = task_sync(
        config,
        {
            "action": "decline",
            "command_id": "decline-action",
            "expected_version": assigned["version"],
            "expected_assignment_epoch": assigned["assignment_epoch"],
            "task_id": "task-decline",
            "actor_id": "agent:worker",
        },
    )
    cancelled = task_sync(
        config,
        {
            "action": "cancel",
            "command_id": "decline-cancel",
            "expected_version": declined["version"],
            "task_id": "task-decline",
            "actor_id": "agent:lead",
            "reason": "No longer needed.",
        },
    )

    assert cancelled["ok"] is True
    task_node = next(node for node in cancelled["bundle"]["nodes"] if node["id"] == "task:task-decline")
    assert task_node["metadata"]["state"] == "cancelled"


def test_agent_filter_limits_bundle_to_the_current_assignee(tmp_path) -> None:
    config = SimpleNamespace(repo_root=tmp_path)
    first = task_sync(
        config,
        {
            "action": "create",
            "command_id": "agent-filter-create-a",
            "expected_version": 0,
            "task_id": "task-agent-a",
            "actor_id": "agent:lead",
            "title": "Agent A task",
        },
    )
    second = task_sync(
        config,
        {
            "action": "create",
            "command_id": "agent-filter-create-b",
            "expected_version": 0,
            "task_id": "task-agent-b",
            "actor_id": "agent:lead",
            "title": "Agent B task",
        },
    )
    task_sync(
        config,
        {
            "action": "assign",
            "command_id": "agent-filter-assign-a",
            "expected_version": first["version"],
            "task_id": "task-agent-a",
            "actor_id": "agent:lead",
            "assignee": "agent:a",
        },
    )
    task_sync(
        config,
        {
            "action": "assign",
            "command_id": "agent-filter-assign-b",
            "expected_version": second["version"],
            "task_id": "task-agent-b",
            "actor_id": "agent:lead",
            "assignee": "agent:b",
        },
    )

    bundle = task_sync(config, {"action": "sync", "agent_id": "agent:a"})["bundle"]

    assert {node["id"] for node in bundle["nodes"] if node["type"] == "task"} == {"task:task-agent-a"}
    assert bundle["roots"]["assigned"] == ["task:task-agent-a"]


def test_shared_task_authority_only_allows_report_and_submit_while_offline(tmp_path) -> None:
    local_config = SimpleNamespace(repo_root=tmp_path)
    created = task_sync(
        local_config,
        {
            "action": "create",
            "command_id": "offline-create",
            "expected_version": 0,
            "task_id": "task-offline-authority",
            "actor_id": "agent:lead",
            "title": "Offline authority task",
        },
    )
    assigned = task_sync(
        local_config,
        {
            "action": "assign",
            "command_id": "offline-assign",
            "expected_version": created["version"],
            "task_id": "task-offline-authority",
            "actor_id": "agent:lead",
            "assignee": "agent:worker",
        },
    )
    claimed = task_sync(
        local_config,
        {
            "action": "claim",
            "command_id": "offline-claim-before-disconnect",
            "expected_version": assigned["version"],
            "expected_assignment_epoch": assigned["assignment_epoch"],
            "task_id": "task-offline-authority",
            "actor_id": "agent:worker",
        },
    )
    shared_offline = SimpleNamespace(
        repo_root=tmp_path,
        shared_memory=SimpleNamespace(enabled=True, active=False, sync_scopes=frozenset({"project_shared"})),
    )

    reported = task_sync(
        shared_offline,
        {
            "action": "report",
            "command_id": "offline-report",
            "expected_version": claimed["version"],
            "expected_assignment_epoch": claimed["assignment_epoch"],
            "task_id": "task-offline-authority",
            "actor_id": "agent:worker",
            "summary": "Progress recorded offline.",
        },
    )
    submitted = task_sync(
        shared_offline,
        {
            "action": "submit",
            "command_id": "offline-submit",
            "expected_version": reported["version"],
            "expected_assignment_epoch": reported["assignment_epoch"],
            "task_id": "task-offline-authority",
            "actor_id": "agent:worker",
            "summary": "Offline submission.",
        },
    )
    blocked_review = task_sync(
        shared_offline,
        {
            "action": "review",
            "command_id": "offline-review",
            "expected_version": submitted["version"],
            "task_id": "task-offline-authority",
            "actor_id": "agent:reviewer",
            "decision": "approved",
        },
    )

    assert reported["ok"] is True
    assert submitted["ok"] is True
    assert blocked_review["ok"] is False
    assert blocked_review["error"] == "task_authority_unavailable"
    assert blocked_review["offline_allowed_actions"] == ["report", "submit"]


def test_hub_rejection_rolls_back_the_local_task_command(tmp_path, monkeypatch) -> None:
    remote_events: list[dict[str, object]] = []

    def fake_post(_self, _path, payload, _timeout_seconds):
        event = payload["events"][0]
        remote_events.append(event)
        task_event = event["metadata"]["task_event"]
        if task_event["event_type"] == "TaskClaimed":
            return 200, {
                "rejected": [
                    {
                        "event_id": event["event_id"],
                        "code": "assignment_epoch_conflict",
                        "message": "Hub has a newer assignment.",
                    }
                ]
            }
        return 200, {"accepted": [event["event_id"]]}

    monkeypatch.setattr(task_sync_module.MemoryHubClient, "post", fake_post)
    config = SimpleNamespace(
        repo_root=tmp_path,
        shared_memory=SimpleNamespace(
            enabled=True,
            active=True,
            project_id="project-authority",
            sync_scopes=frozenset({"project_shared"}),
            task_command_timeout_seconds=0.1,
        ),
    )
    created = task_sync(
        config,
        {
            "action": "create",
            "command_id": "authority-create",
            "expected_version": 0,
            "task_id": "task-authority",
            "actor_id": "agent:lead",
            "title": "Hub authoritative task",
        },
    )
    assigned = task_sync(
        config,
        {
            "action": "assign",
            "command_id": "authority-assign",
            "expected_version": created["version"],
            "task_id": "task-authority",
            "actor_id": "agent:lead",
            "assignee": "agent:worker",
        },
    )
    rejected = task_sync(
        config,
        {
            "action": "claim",
            "command_id": "authority-claim",
            "expected_version": assigned["version"],
            "expected_assignment_epoch": assigned["assignment_epoch"],
            "task_id": "task-authority",
            "actor_id": "agent:worker",
        },
    )
    history = task_sync(config, {"action": "history", "task_id": "task-authority"})

    assert created["shared_sync"]["mode"] == "hub_authoritative"
    assert assigned["shared_sync"]["mode"] == "hub_authoritative"
    assert rejected["ok"] is False
    assert rejected["error"] == "assignment_epoch_conflict"
    assert rejected["remote_authority"] is True
    assert [event["event_type"] for event in history["events"]] == ["TaskCreated", "TaskAssigned"]
    assert [event["metadata"]["task_event"]["event_type"] for event in remote_events] == [
        "TaskCreated",
        "TaskAssigned",
        "TaskClaimed",
    ]


def test_hub_retries_a_transient_authority_failure_without_duplicate_local_events(tmp_path, monkeypatch) -> None:
    remote_events: list[dict[str, object]] = []

    def transient_post(_self, _path, payload, _timeout_seconds):
        event = payload["events"][0]
        remote_events.append(event)
        if len(remote_events) == 1:
            return 0, {"error": "remote_unavailable"}
        return 200, {"duplicates": [event["event_id"]]}

    monkeypatch.setattr(task_sync_module.MemoryHubClient, "post", transient_post)
    config = SimpleNamespace(
        repo_root=tmp_path,
        shared_memory=SimpleNamespace(
            enabled=True,
            active=True,
            project_id="project-transient-authority",
            sync_scopes=frozenset({"project_shared"}),
            task_command_timeout_seconds=0.1,
        ),
    )
    created = task_sync(
        config,
        {
            "action": "create",
            "command_id": "transient-authority-create",
            "expected_version": 0,
            "task_id": "task-transient-authority",
            "actor_id": "agent:lead",
            "title": "Transient authority task",
        },
    )
    history = task_sync(config, {"action": "history", "task_id": "task-transient-authority"})

    assert created["ok"] is True
    assert created["shared_sync"]["mode"] == "hub_authoritative"
    assert created["shared_sync"]["authority_attempts"] == 2
    assert created["shared_sync"]["recovered_after_retry"] is True
    assert len(remote_events) == 2
    assert remote_events[0]["event_id"] == remote_events[1]["event_id"]
    assert [event["event_type"] for event in history["events"]] == ["TaskCreated"]


def test_active_hub_outage_only_defers_report_and_submit(tmp_path, monkeypatch) -> None:
    def unavailable_post(_self, _path, _payload, _timeout_seconds):
        return 0, {"error": "remote_unavailable"}

    monkeypatch.setattr(task_sync_module.MemoryHubClient, "post", unavailable_post)
    local_config = SimpleNamespace(repo_root=tmp_path)
    created = task_sync(
        local_config,
        {
            "action": "create",
            "command_id": "outage-create",
            "expected_version": 0,
            "task_id": "task-hub-outage",
            "actor_id": "agent:lead",
            "title": "Hub outage task",
        },
    )
    assigned = task_sync(
        local_config,
        {
            "action": "assign",
            "command_id": "outage-assign",
            "expected_version": created["version"],
            "task_id": "task-hub-outage",
            "actor_id": "agent:lead",
            "assignee": "agent:worker",
        },
    )
    claimed = task_sync(
        local_config,
        {
            "action": "claim",
            "command_id": "outage-claim",
            "expected_version": assigned["version"],
            "expected_assignment_epoch": assigned["assignment_epoch"],
            "task_id": "task-hub-outage",
            "actor_id": "agent:worker",
        },
    )
    shared_config = SimpleNamespace(
        repo_root=tmp_path,
        shared_memory=SimpleNamespace(
            enabled=True,
            active=True,
            project_id="project-outage",
            sync_scopes=frozenset({"project_shared"}),
            task_command_timeout_seconds=0.1,
        ),
    )

    blocked = task_sync(
        shared_config,
        {
            "action": "block",
            "command_id": "outage-block",
            "expected_version": claimed["version"],
            "expected_assignment_epoch": claimed["assignment_epoch"],
            "task_id": "task-hub-outage",
            "actor_id": "agent:worker",
            "reason": "Hub unavailable.",
        },
    )
    reported = task_sync(
        shared_config,
        {
            "action": "report",
            "command_id": "outage-report",
            "expected_version": claimed["version"],
            "expected_assignment_epoch": claimed["assignment_epoch"],
            "task_id": "task-hub-outage",
            "actor_id": "agent:worker",
            "summary": "Recorded while the Hub is unavailable.",
        },
    )
    submitted = task_sync(
        shared_config,
        {
            "action": "submit",
            "command_id": "outage-submit",
            "expected_version": reported["version"],
            "expected_assignment_epoch": reported["assignment_epoch"],
            "task_id": "task-hub-outage",
            "actor_id": "agent:worker",
            "summary": "Submitted while the Hub is unavailable.",
        },
    )
    queued = enqueue_task_sync_event(shared_config, reported)
    history = task_sync(local_config, {"action": "history", "task_id": "task-hub-outage"})

    assert blocked["ok"] is False
    assert blocked["error"] == "task_authority_unavailable"
    assert blocked["authority_attempts"] == 2
    assert reported["shared_sync"] == {
        "enabled": True,
        "mode": "offline_pending",
        "queued": True,
        "remote_status": 0,
    }
    assert queued is reported
    assert submitted["ok"] is True
    assert submitted["shared_sync"]["mode"] == "offline_pending"
    assert [event["event_type"] for event in history["events"]] == [
        "TaskCreated",
        "TaskAssigned",
        "TaskClaimed",
        "TaskReported",
        "TaskSubmitted",
    ]


def test_active_hub_authorization_failure_does_not_defer_a_report(tmp_path, monkeypatch) -> None:
    def denied_post(_self, _path, _payload, _timeout_seconds):
        return 401, {"error": "unauthorized"}

    monkeypatch.setattr(task_sync_module.MemoryHubClient, "post", denied_post)
    local_config = SimpleNamespace(repo_root=tmp_path)
    created = task_sync(
        local_config,
        {
            "action": "create",
            "command_id": "denied-create",
            "expected_version": 0,
            "task_id": "task-hub-denied",
            "actor_id": "agent:lead",
            "title": "Hub authorization task",
        },
    )
    assigned = task_sync(
        local_config,
        {
            "action": "assign",
            "command_id": "denied-assign",
            "expected_version": created["version"],
            "task_id": "task-hub-denied",
            "actor_id": "agent:lead",
            "assignee": "agent:worker",
        },
    )
    claimed = task_sync(
        local_config,
        {
            "action": "claim",
            "command_id": "denied-claim",
            "expected_version": assigned["version"],
            "expected_assignment_epoch": assigned["assignment_epoch"],
            "task_id": "task-hub-denied",
            "actor_id": "agent:worker",
        },
    )
    shared_config = SimpleNamespace(
        repo_root=tmp_path,
        shared_memory=SimpleNamespace(
            enabled=True,
            active=True,
            project_id="project-denied",
            sync_scopes=frozenset({"project_shared"}),
            task_command_timeout_seconds=0.1,
        ),
    )

    denied = task_sync(
        shared_config,
        {
            "action": "report",
            "command_id": "denied-report",
            "expected_version": claimed["version"],
            "expected_assignment_epoch": claimed["assignment_epoch"],
            "task_id": "task-hub-denied",
            "actor_id": "agent:worker",
            "summary": "This must not be deferred.",
        },
    )
    history = task_sync(local_config, {"action": "history", "task_id": "task-hub-denied"})

    assert denied["ok"] is False
    assert denied["error"] == "task_authority_rejected"
    assert denied["remote_authority"] is True
    assert [event["event_type"] for event in history["events"]] == [
        "TaskCreated",
        "TaskAssigned",
        "TaskClaimed",
    ]


def test_completed_task_command_is_queued_for_hub_without_waiting(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("USER", "outbox-author")
    monkeypatch.setenv("USERNAME", "outbox-author")
    local_config = SimpleNamespace(repo_root=tmp_path)
    created = task_sync(
        local_config,
        {
            "action": "create",
            "command_id": "outbox-create",
            "expected_version": 0,
            "task_id": "task-outbox",
            "actor_id": "agent:lead",
            "title": "Outbox task",
        },
    )
    config = SimpleNamespace(
        repo_root=tmp_path,
        shared_memory=SimpleNamespace(enabled=True, sync_scopes=frozenset({"project_shared"})),
    )

    queued = enqueue_task_sync_event(config, created)
    first_sync = dict(queued["shared_sync"])
    replayed = enqueue_task_sync_event(config, created)
    rows = SyncStore(tmp_path / ".ai-memory" / "shared-sync.db").due_events(10)

    assert first_sync == {"enabled": True, "queued": True}
    assert replayed["shared_sync"] == {"enabled": True, "queued": False}
    assert len(rows) == 1
    assert rows[0]["user_id"] == "outbox-author"
    event = json.loads(rows[0]["payload_json"])
    assert event["operation"] == "task_sync"
    assert event["content_markdown"] == ""
    assert event["metadata"]["task_event"]["command_id"] == "outbox-create"