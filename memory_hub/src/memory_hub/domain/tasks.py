"""Validated Task Graph event envelopes shared by Hub ingestion and projection."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TASK_EVENT_TYPES = frozenset(
    {
        "TaskCreated",
        "TaskAssigned",
        "TaskClaimed",
        "TaskDeclined",
        "TaskReported",
        "TaskBlocked",
        "TaskResumed",
        "TaskSubmitted",
        "TaskReviewed",
        "TaskReassigned",
        "TaskCancelled",
    }
)


class TaskEventPayload(BaseModel):
    """A command event produced by the local Task Graph event store."""

    model_config = ConfigDict(extra="forbid")

    version: Literal["1.0"]
    command_id: str = Field(min_length=1, max_length=256)
    event_type: Literal[
        "TaskCreated",
        "TaskAssigned",
        "TaskClaimed",
        "TaskDeclined",
        "TaskReported",
        "TaskBlocked",
        "TaskResumed",
        "TaskSubmitted",
        "TaskReviewed",
        "TaskReassigned",
        "TaskCancelled",
    ]
    task_id: str = Field(min_length=1, max_length=256)
    actor_id: str = Field(min_length=1, max_length=256)
    expected_version: int = Field(ge=0)
    expected_assignment_epoch: int | None = Field(default=None, ge=0)
    task_version: int = Field(ge=1)
    assignment_epoch: int = Field(ge=0)
    payload: dict[str, object] = Field(default_factory=dict, max_length=32)
    occurred_at: datetime


def task_event_from_metadata(metadata: object, task_id: str | None) -> TaskEventPayload:
    """Parse a task event and bind it to the immutable outer event task id."""

    if not isinstance(metadata, dict):
        raise ValueError("task_sync metadata must be an object")
    raw_event = metadata.get("task_event")
    if not isinstance(raw_event, dict):
        raise ValueError("task_sync metadata.task_event must be an object")
    event = TaskEventPayload.model_validate(raw_event)
    outer_task_id = str(task_id or "").strip()
    if not outer_task_id or event.task_id != outer_task_id:
        raise ValueError("task_event.task_id must match the outer event task_id")
    return event


__all__ = ["TASK_EVENT_TYPES", "TaskEventPayload", "task_event_from_metadata"]