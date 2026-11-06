from __future__ import annotations

import pytest

from memory_hub.domain.events import EventPayload
from memory_hub.domain.tasks import MAX_INT, TaskCommandError, parse_command
from memory_hub.tasks.queries import _decode_cursor


def _error(raw: dict[str, object]) -> TaskCommandError:
    with pytest.raises(TaskCommandError) as caught:
        parse_command(raw)
    return caught.value


def test_create_normalizes_and_deduplicates() -> None:
    command = parse_command({"command_id": "c1", "action": "create", "title": "  Title  ",
                             "depends_on": ["a", " a ", "b"], "agent": "codex", "channel": "dispatch"})
    assert command.values["title"] == "Title"
    assert command.values["priority"] == "normal"
    assert command.values["depends_on"] == ["a", "b"]
    assert command.task_id is None and command.channel == "dispatch" and command.agent == "codex"


@pytest.mark.parametrize("key", ["actor_id"])
def test_actor_identity_is_never_accepted(key: str) -> None:
    error = _error({"command_id": "c", "action": "create", "title": "T", key: "PC-B"})
    assert (error.status, error.code) == (422, "actor_not_accepted")


@pytest.mark.parametrize("key", ["occurred_at", "task_version", "assignment_epoch", "attempt_id", "review_id"])
def test_derived_fields_are_rejected(key: str) -> None:
    error = _error({"command_id": "c", "action": "claim", "task_id": "t", "expected_version": 1,
                    "expected_assignment_epoch": 1, key: "x"})
    assert (error.status, error.code, error.field) == (422, "invalid_input", key)


def test_unknown_field_and_channel_are_rejected() -> None:
    assert _error({"command_id": "c", "action": "create", "title": "T", "extra": 1}).field == "extra"
    assert _error({"command_id": "c", "action": "create", "title": "T", "channel": "admin"}).code == "invalid_channel"
    assert _error({"command_id": "c", "action": "submit", "task_id": "t", "expected_version": 1,
                   "expected_assignment_epoch": 1, "summary": "s", "submission_id": "x"}).field == "submission_id"


def test_required_fields_and_epoch() -> None:
    assert _error({"command_id": "c", "action": "create", "title": "   "}).field == "title"
    assert _error({"command_id": "c", "action": "claim", "task_id": "t", "expected_version": 1}).code == "missing_assignment_epoch"
    assert _error({"command_id": "c", "action": "assign", "task_id": "t", "assignee": "PC"}).field == "expected_version"
    report = parse_command({"command_id": "c", "action": "report", "task_id": "t", "expected_assignment_epoch": 1, "summary": "s"})
    assert report.expected_version is None


@pytest.mark.parametrize("value", [MAX_INT + 1, -1, True, "1", 1.5])
def test_integer_bounds(value: object) -> None:
    error = _error({"command_id": "c", "action": "claim", "task_id": "t", "expected_version": 1,
                    "expected_assignment_epoch": value})
    assert (error.status, error.field) == (422, "expected_assignment_epoch")


def test_text_and_list_limits() -> None:
    assert _error({"command_id": "c", "action": "create", "title": "x" * 1025}).field == "title"
    assert _error({"command_id": "c", "action": "create", "title": "T", "depends_on": [f"t{i}" for i in range(65)]}).field == "depends_on"
    assert _error({"command_id": "c", "action": "create", "title": "T", "depends_on": [""]}).field == "depends_on"
    assert _error({"command_id": "c", "action": "submit", "task_id": "t", "expected_version": 1,
                   "expected_assignment_epoch": 1, "summary": "s", "evidence": ["e"] * 33}).field == "evidence"
    assert _error({"command_id": " c", "action": "create", "title": "T"}).field == "command_id"
    assert _error({"command_id": "c\n", "action": "create", "title": "T"}).field == "command_id"
    assert _error({"command_id": "c", "action": "create", "title": "T", "task_id": "x" * 257}).field == "task_id"


def test_review_decision_is_validated() -> None:
    assert _error({"command_id": "c", "action": "review", "task_id": "t", "expected_version": 1, "decision": "ok"}).field == "decision"


def test_event_payload_still_names_task_sync_for_explicit_retirement() -> None:
    payload = EventPayload.model_validate({
        "schema_version": "1.0", "event_id": "e6c8364c-5c38-4655-8618-c1c3a37e26a1", "agent_id": "agent:lead",
        "agent_instance_id": "agent-instance-1", "task_id": "task-1", "operation": "task_sync", "scope": "project_shared",
        "metadata": {}, "occurred_at": "2026-08-09T00:00:00+00:00", "content_hash": "sha256:" + "a" * 64,
    })
    assert payload.operation == "task_sync"


def test_task_catalog_rejects_a_malformed_cursor() -> None:
    with pytest.raises(ValueError, match="invalid task cursor"):
        _decode_cursor("a", "updated")
