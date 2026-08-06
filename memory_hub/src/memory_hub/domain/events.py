"""Validated public event contracts; client payloads never carry user identity."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EventPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = Field(pattern=r"^1\.0$")
    event_id: UUID
    source_node_id: str | None = None
    runtime_node_id: str | None = None
    source_node_name: str | None = None
    workspace_id: str | None = None
    agent_session_id: str | None = None
    transport_id: str | None = None
    agent_id: str
    agent_instance_id: str
    task_id: str | None = None
    task_run_id: str | None = None
    operation: str = Field(pattern=r"^(record|observation|checkpoint)$")
    record_kind: str | None = None
    scope: str = Field(pattern=r"^(personal|session|user_private|shared|project_shared|org_shared)$")
    task_phase: str | None = None
    content_markdown: str | None = Field(default=None, max_length=65536)
    metadata: dict[str, object] = Field(default_factory=dict, max_length=64)
    source_record_id: str | None = None
    occurred_at: datetime
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class EventBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[EventPayload] = Field(min_length=1, max_length=20)


class RejectedEvent(BaseModel):
    event_id: UUID
    code: str
    message: str


class EventBatchResponse(BaseModel):
    accepted: list[UUID] = Field(default_factory=list)
    duplicates: list[UUID] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    rejected: list[RejectedEvent] = Field(default_factory=list)