"""Project Board API contracts."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _BoardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator(
        "task_id",
        "post_id",
        "thread_id",
        "reply_to",
        "expires_at",
        "author_agent_id",
        "author_agent_instance_id",
        "user_id",
        "agent_instance_id",
        "status",
        "post_type",
        mode="before",
        check_fields=False,
    )
    @classmethod
    def blank_optional_values_are_none(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value


class BoardPostRequest(_BoardRequest):
    post_id: UUID | None = None
    post_type: str = Field(pattern=r"^(note|question|request|warning|handoff|proposal)$")
    content: str = Field(min_length=1, max_length=65536)
    task_id: str | None = None
    thread_id: UUID | None = None
    references_json: list[object] = Field(default_factory=list, max_length=64)
    expires_at: datetime | None = None
    author_agent_id: str | None = None
    author_agent_instance_id: str | None = None


class BoardReplyRequest(_BoardRequest):
    post_id: UUID | None = None
    content: str = Field(min_length=1, max_length=65536)
    thread_id: UUID | None = None
    reply_to: UUID | None = None
    task_id: str | None = None
    references_json: list[object] = Field(default_factory=list, max_length=64)
    expires_at: datetime | None = None
    author_agent_id: str | None = None
    author_agent_instance_id: str | None = None


class BoardResolveRequest(_BoardRequest):
    post_id: UUID


class BoardQueryRequest(_BoardRequest):
    filter: str = Field(default="all", pattern=r"^(all|unresolved)$")
    user_id: str | None = None
    agent_instance_id: str | None = None
    task_id: str | None = None
    status: str | None = Field(default=None, pattern=r"^(open|resolved)$")
    post_type: str | None = Field(default=None, pattern=r"^(note|question|request|warning|handoff|proposal|reply)$")
    thread_id: UUID | None = None
    max_items: int = Field(default=20, ge=1, le=200)
