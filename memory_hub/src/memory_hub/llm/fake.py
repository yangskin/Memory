"""Deterministic provider used by tests and default local development."""

from __future__ import annotations

from datetime import UTC, datetime

from .base import ProjectBriefRequest, ProjectBriefResult, ProjectGraphRequest, ProjectGraphResult, UserBriefRequest, UserBriefResult


def _event_ids(events: list[dict[str, object]]) -> list[str]:
    return [str(event["event_id"]) for event in events if event.get("event_id")]


class FakeBriefProvider:
    """Generate valid, data-only brief payloads without external calls."""

    def generate_user_brief(self, request: UserBriefRequest) -> UserBriefResult:
        now = datetime.now(UTC).isoformat()
        source_ids = _event_ids(request.events)
        return UserBriefResult(
            structured_brief={
                "schema_version": "1.0",
                "as_of": now,
                "summary": f"{len(source_ids)} recent reports for {request.user_id}.",
                "workstreams": [],
                "cross_agent_overlaps": [],
                "stale_workstreams": [],
                "source_event_ids": source_ids,
            }
        )

    def generate_project_brief(self, request: ProjectBriefRequest) -> ProjectBriefResult:
        now = datetime.now(UTC).isoformat()
        source_ids = _event_ids(request.events)
        return ProjectBriefResult(
            structured_brief={
                "schema_version": "1.0",
                "as_of": now,
                "summary": f"{len(source_ids)} recent project reports.",
                "workstreams": [],
                "cross_cutting_changes": [],
                "possible_overlaps": [],
                "project_blockers": [],
                "build_and_test_status": [],
                "recent_decisions": [],
                "source_event_ids": source_ids,
            }
        )

    def generate_project_graph(self, request: ProjectGraphRequest) -> ProjectGraphResult:
        return ProjectGraphResult(
            structured_graph={
                "schema_version": "1.0",
                "as_of": datetime.now(UTC).isoformat(),
                "nodes": [],
                "edges": [],
                "source_event_ids": [],
            }
        )