"""Public shared-context query contract."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SharedContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_instance_id: str
    task_id: str | None = None
    include: list[str] = Field(default_factory=list)
    max_age_minutes: int = Field(default=1440, ge=1, le=10080)
    max_items: int = Field(default=20, ge=1, le=100)