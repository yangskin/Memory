from memory_hub.worker.runner import _bounded_event_payloads, _brief_input_tokens


def test_prompt_budget_keeps_newest_events_and_truncates_content() -> None:
    events = [
        {
            "event_id": f"event-{index}",
            "content_markdown": "x" * 2_000,
            "scope": "project_shared",
            "user_id": "user",
            "task_id": None,
            "agent_instance_id": "agent",
            "occurred_at": "2026-08-27T00:00:00+00:00",
        }
        for index in range(4)
    ]

    bounded = _bounded_event_payloads(
        "project_recent",
        events,
        prompt_token_budget=1_024,
    )

    assert bounded
    assert _brief_input_tokens("project_recent", bounded) <= 1_024
    assert bounded[-1]["event_id"] == "event-3"
    assert len(bounded[-1]["content_markdown"]) < len(events[-1]["content_markdown"])
