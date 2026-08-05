"""Project Board API contracts."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class BoardPostRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    post_type: str = Field(pattern=r"^(note|question|request|warning|handoff|proposal)$")
    content: str = Field(min_length=1, max_length=65536)
    task_id: str | None = None
    thread_id: UUID | None = None
    references_json: list[object] = Field(default_factory=list, max_length=64)
    expires_at: datetime | None = None
    author_agent_id: str | None = None
    author_agent_instance_id: str | None = None


class BoardReplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=65536)
    thread_id: UUID | None = None
    reply_to: UUID | None = None
    task_id: str | None = None
    references_json: list[object] = Field(default_factory=list, max_length=64)
    expires_at: datetime | None = None
    author_agent_id: str | None = None
    author_agent_instance_id: str | None = None


class BoardResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    post_id: UUID


class BoardQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filter: str = Field(default="all", pattern=r"^(all|unresolved)$")
    user_id: str | None = None
    agent_instance_id: str | None = None
    task_id: str | None = None
    status: str | None = Field(default=None, pattern=r"^(open|resolved)$")
    post_type: str | None = Field(default=None, pattern=r"^(note|question|request|warning|handoff|proposal|reply)$")
    thread_id: UUID | None = None
    max_items: int = Field(default=20, ge=1, le=200)
