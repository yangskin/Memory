"""Public shared-context query contract."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


_SHARED_CONTEXT_SECTIONS = {
    "user_brief",
    "project_brief",
    "same_task_agents",
    "my_other_agents",
    "other_tasks",
    "project_activity",
}


class SharedContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_instance_id: str
    task_id: str | None = None
    include: list[str] = Field(default_factory=list, max_length=6)
    max_age_minutes: int = Field(default=1440, ge=1, le=10080)
    max_items: int = Field(default=10, ge=1, le=20)

    def model_post_init(self, __context: object) -> None:
        if len(set(self.include)) != len(self.include) or any(item not in _SHARED_CONTEXT_SECTIONS for item in self.include):
            raise ValueError("include contains duplicates or unsupported sections")