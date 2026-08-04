from types import SimpleNamespace

import pytest

from memory_hub.worker.runner import _validate_structured


def test_worker_rejects_unknown_source_event() -> None:
    job = SimpleNamespace(brief_type="user_recent")
    payload = {
        "schema_version": "1.0",
        "as_of": "2026-08-04T00:00:00Z",
        "summary": "report",
        "workstreams": [],
        "cross_agent_overlaps": [],
        "stale_workstreams": [],
        "source_event_ids": ["unknown"],
    }
    with pytest.raises(ValueError, match="outside"):
        _validate_structured(job, payload, {"evt_1"})


def test_worker_accepts_valid_user_brief() -> None:
    job = SimpleNamespace(brief_type="user_recent")
    payload = {
        "schema_version": "1.0",
        "as_of": "2026-08-04T00:00:00Z",
        "summary": "report",
        "workstreams": [],
        "cross_agent_overlaps": [],
        "stale_workstreams": [],
        "source_event_ids": ["evt_1"],
    }
    assert _validate_structured(job, payload, {"evt_1"})["summary"] == "report"


def test_worker_rejects_conclusion_without_its_own_sources() -> None:
    job = SimpleNamespace(brief_type="user_recent")
    payload = {
        "schema_version": "1.0",
        "as_of": "2026-08-04T00:00:00Z",
        "summary": "report",
        "workstreams": [{"task_id": "task-1"}],
        "cross_agent_overlaps": [],
        "stale_workstreams": [],
        "source_event_ids": ["evt_1"],
    }
    with pytest.raises(ValueError, match="missing source_event_ids"):
        _validate_structured(job, payload, {"evt_1"})