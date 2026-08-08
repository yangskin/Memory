from __future__ import annotations

import json

from memory_hub.llm.base import ProjectGraphRequest
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


def test_project_graph_prompt_forbids_source_labels_as_entities(monkeypatch) -> None:
    import memory_hub.llm.openai_compatible as provider_module

    requests: list[dict[str, object]] = []

    class CapturingClient(_Client):
        def post(self, *args: object, **kwargs: object) -> _Response:
            requests.append(kwargs["json"])
            return super().post(*args, **kwargs)

    monkeypatch.setattr(provider_module.httpx, "Client", lambda **kwargs: CapturingClient({"nodes": [], "edges": []}))
    provider = OpenAICompatibleBriefProvider("https://provider.example.com/v1", "secret", "test-model")

    provider.generate_project_graph(ProjectGraphRequest(project_id="project", events=[{"event_id": "event-1", "source": {"type": "source", "key": "event:event-1", "name": "report: Checkout"}, "entities": [{"type": "class", "key": "CheckoutVerifier"}]}]))

    prompt = requests[0]["messages"][0]["content"]
    assert "system_area" in prompt
    assert "report titles" in prompt
    assert "never graph endpoints" in prompt