"""Narrow brief-provider protocol; providers receive data and return JSON only."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class UserBriefRequest(BaseModel):
    project_id: str
    user_id: str
    events: list[dict[str, object]] = Field(default_factory=list)


class ProjectBriefRequest(BaseModel):
    project_id: str
    events: list[dict[str, object]] = Field(default_factory=list)


class ProjectGraphRequest(BaseModel):
    project_id: str
    events: list[dict[str, object]] = Field(default_factory=list)


class UserBriefResult(BaseModel):
    structured_brief: dict[str, object]


class ProjectBriefResult(BaseModel):
    structured_brief: dict[str, object]


class ProjectGraphResult(BaseModel):
    structured_graph: dict[str, object]


class UserBriefDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    as_of: str
    summary: str
    workstreams: list[dict[str, object]] = Field(default_factory=list)
    cross_agent_overlaps: list[object] = Field(default_factory=list)
    stale_workstreams: list[object] = Field(default_factory=list)
    source_event_ids: list[str]


class ProjectBriefDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    as_of: str
    summary: str
    workstreams: list[dict[str, object]] = Field(default_factory=list)
    cross_cutting_changes: list[object] = Field(default_factory=list)
    possible_overlaps: list[object] = Field(default_factory=list)
    project_blockers: list[object] = Field(default_factory=list)
    build_and_test_status: list[object] = Field(default_factory=list)
    recent_decisions: list[object] = Field(default_factory=list)
    source_event_ids: list[str]


class BriefProvider(Protocol):
    def generate_user_brief(self, request: UserBriefRequest) -> UserBriefResult: ...

    def generate_project_brief(self, request: ProjectBriefRequest) -> ProjectBriefResult: ...

    def generate_project_graph(self, request: ProjectGraphRequest) -> ProjectGraphResult: ...