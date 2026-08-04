from memory_hub.llm.fake import FakeBriefProvider
from memory_hub.llm.base import ProjectBriefRequest, UserBriefRequest


def test_fake_provider_returns_source_event_ids() -> None:
    provider = FakeBriefProvider()
    events = [{"event_id": "evt_1"}, {"event_id": "evt_2"}]

    user_result = provider.generate_user_brief(
        UserBriefRequest(project_id="prj_1", user_id="alice", events=events)
    )
    project_result = provider.generate_project_brief(ProjectBriefRequest(project_id="prj_1", events=events))

    assert user_result.structured_brief["source_event_ids"] == ["evt_1", "evt_2"]
    assert project_result.structured_brief["source_event_ids"] == ["evt_1", "evt_2"]