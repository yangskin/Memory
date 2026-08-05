from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class GraphQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str | None = None
    files: list[str] = Field(default_factory=list, max_length=50)
    classes: list[str] = Field(default_factory=list, max_length=50)
    modules: list[str] = Field(default_factory=list, max_length=50)
    assets: list[str] = Field(default_factory=list, max_length=50)
    blueprints: list[str] = Field(default_factory=list, max_length=50)
    maps: list[str] = Field(default_factory=list, max_length=50)
    plugins: list[str] = Field(default_factory=list, max_length=50)
    system_areas: list[str] = Field(default_factory=list, max_length=50)
    depth: int = Field(default=1, ge=0, le=2)
    max_nodes: int = Field(default=50, ge=1, le=200)
    max_edges: int = Field(default=100, ge=1, le=400)
    include_metadata: bool = False
    include_source_event_ids: bool = False