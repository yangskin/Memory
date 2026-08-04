"""Narrow brief-provider protocol; providers receive data and return JSON only."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field


class UserBriefRequest(BaseModel):
    project_id: str
    user_id: str
    events: list[dict[str, object]] = Field(default_factory=list)


class ProjectBriefRequest(BaseModel):
    project_id: str
    events: list[dict[str, object]] = Field(default_factory=list)


class UserBriefResult(BaseModel):
    structured_brief: dict[str, object]


class ProjectBriefResult(BaseModel):
    structured_brief: dict[str, object]


class BriefProvider(Protocol):
    def generate_user_brief(self, request: UserBriefRequest) -> UserBriefResult: ...

    def generate_project_brief(self, request: ProjectBriefRequest) -> ProjectBriefResult: ...