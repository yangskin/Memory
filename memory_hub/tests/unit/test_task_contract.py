from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from memory_hub.domain.events import EventPayload
from memory_hub.domain.tasks import TaskEventPayload, task_event_from_metadata
from memory_hub.tasks.projector import TaskProjectionError, _require_current_attempt_payload


def _task_event() -> dict[str, object]:
    return {
        "version": "1.0",
        "command_id": "task-command-1",
        "event_type": "TaskCreated",
        "task_id": "task-1",
        "actor_id": "agent:lead",
        "expected_version": 0,
        "expected_assignment_epoch": None,
        "task_version": 1,
        "assignment_epoch": 0,
        "payload": {"title": "Task graph"},
        "occurred_at": "2026-08-09T00:00:00+00:00",
    }


def test_task_sync_event_contract_binds_metadata_to_outer_task_id() -> None:
    parsed = task_event_from_metadata({"task_event": _task_event()}, "task-1")

    assert parsed.command_id == "task-command-1"
    assert parsed.event_type == "TaskCreated"

    with pytest.raises(ValueError, match="outer event task_id"):
        task_event_from_metadata({"task_event": _task_event()}, "other-task")


def test_event_payload_permits_task_sync_without_client_identity_fields() -> None:
    payload = EventPayload.model_validate(
        {
            "schema_version": "1.0",
            "event_id": "e6c8364c-5c38-4655-8618-c1c3a37e26a1",
            "agent_id": "agent:lead",
            "agent_instance_id": "agent-instance-1",
            "task_id": "task-1",
            "operation": "task_sync",
            "scope": "project_shared",
            "metadata": {"task_event": _task_event()},
            "occurred_at": "2026-08-09T00:00:00+00:00",
            "content_hash": "sha256:" + "a" * 64,
        }
    )

    assert payload.operation == "task_sync"


@pytest.mark.parametrize("attempt_id", [None, "attempt-stale"])
def test_executor_event_requires_its_current_attempt_id(attempt_id: str | None) -> None:
    payload = {} if attempt_id is None else {"attempt_id": attempt_id}
    event = TaskEventPayload(
        version="1.0",
        command_id="claim-command",
        event_type="TaskClaimed",
        task_id="task-1",
        actor_id="agent:worker",
        expected_version=2,
        expected_assignment_epoch=1,
        task_version=3,
        assignment_epoch=1,
        payload=payload,
        occurred_at=datetime(2026, 8, 9, tzinfo=UTC),
    )

    with pytest.raises(TaskProjectionError) as caught:
        _require_current_attempt_payload(event, SimpleNamespace(attempt_id="attempt-current"))

    assert caught.value.code in {"invalid_task_event", "attempt_conflict"}