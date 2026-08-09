from __future__ import annotations

import json

from memory_hub.llm.base import ProjectBriefRequest
from memory_hub.llm.openai_compatible import OpenAICompatibleBriefProvider


class _Response:
    def __init__(self, content: object) -> None:
        self._content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {"choices": [{"message": {"content": json.dumps(self._content)}}]}


class _Client:
    def __init__(self, content: object) -> None:
        self._content = content

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def post(self, *args: object, **kwargs: object) -> _Response:
        return _Response(self._content)


def test_provider_normalizes_incomplete_and_uncited_model_output(monkeypatch) -> None:
    import memory_hub.llm.openai_compatible as provider_module

    monkeypatch.setattr(provider_module.httpx, "Client", lambda **kwargs: _Client({
        "summary": "A model summary",
        "workstreams": [
            {"task_id": "valid", "source_event_ids": ["event-1"]},
            {"task_id": "uncited"},
            {"task_id": "forged", "source_event_ids": ["not-an-input"]},
        ],
    }))
    provider = OpenAICompatibleBriefProvider("https://provider.example.com/v1", "secret", "test-model")

    result = provider.generate_user_brief(type("Request", (), {"events": [{"event_id": "event-1"}]})())

    brief = result.structured_brief
    assert brief["schema_version"] == "1.0"
    assert brief["source_event_ids"] == ["event-1"]
    assert brief["workstreams"] == [{"task_id": "valid", "source_event_ids": ["event-1"]}]
    assert brief["cross_agent_overlaps"] == []
    assert brief["stale_workstreams"] == []


def test_provider_normalizes_project_brief_output(monkeypatch) -> None:
    import memory_hub.llm.openai_compatible as provider_module

    monkeypatch.setattr(provider_module.httpx, "Client", lambda **kwargs: _Client({
        "summary": "Project summary",
        "cross_cutting_changes": [
            {"summary": "Valid change", "source_event_ids": ["event-1"]},
            {"summary": "Uncited change"},
        ],
        "possible_overlaps": [{"summary": "Forged", "source_event_ids": ["not-an-input"]}],
        "project_blockers": [{"summary": "Valid blocker", "source_event_ids": ["event-2"]}],
        "build_and_test_status": [],
        "recent_decisions": [{"summary": "Valid decision", "source_event_ids": ["event-1", "event-2"]}],
    }))
    provider = OpenAICompatibleBriefProvider("https://provider.example.com/v1", "secret", "test-model")

    result = provider.generate_project_brief(ProjectBriefRequest(
        project_id="project-1",
        events=[{"event_id": "event-1"}, {"event_id": "event-2"}],
    ))

    brief = result.structured_brief
    assert brief["source_event_ids"] == ["event-1", "event-2"]
    assert brief["cross_cutting_changes"] == [{"summary": "Valid change", "source_event_ids": ["event-1"]}]
    assert brief["possible_overlaps"] == []
    assert brief["project_blockers"] == [{"summary": "Valid blocker", "source_event_ids": ["event-2"]}]
    assert brief["recent_decisions"] == [
        {"summary": "Valid decision", "source_event_ids": ["event-1", "event-2"]}
    ]
