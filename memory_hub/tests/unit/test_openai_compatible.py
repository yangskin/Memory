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
        self.url: str | None = None
        self.payload: dict[str, object] | None = None

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def post(self, url: str, **kwargs: object) -> _Response:
        self.url = url
        self.payload = kwargs.get("json") if isinstance(kwargs.get("json"), dict) else None
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


def test_provider_accepts_a_complete_chat_completions_endpoint(monkeypatch) -> None:
    import memory_hub.llm.openai_compatible as provider_module

    client = _Client({"summary": "Project summary"})
    monkeypatch.setattr(provider_module.httpx, "Client", lambda **kwargs: client)
    endpoint = "https://provider.example.com/plan/v3/chat/completions"
    provider = OpenAICompatibleBriefProvider(endpoint, "secret", "test-model")

    provider.generate_project_brief(ProjectBriefRequest(project_id="project-1", events=[]))

    assert client.url == endpoint


def test_provider_caps_completion_tokens(monkeypatch) -> None:
    import memory_hub.llm.openai_compatible as provider_module

    client = _Client({"summary": "Project summary"})
    monkeypatch.setattr(provider_module.httpx, "Client", lambda **kwargs: client)
    provider = OpenAICompatibleBriefProvider(
        "https://provider.example.com/v1",
        "secret",
        "test-model",
        max_output_tokens=321,
    )

    provider.generate_project_brief(ProjectBriefRequest(project_id="project-1", events=[]))

    assert client.payload is not None
    assert client.payload["max_tokens"] == 321
