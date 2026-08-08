from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from memory_hub.config import load_settings
from memory_hub.db.models import BriefJob
from memory_hub.db.session import create_session_factory
from memory_hub.domain.events import EventPayload
from memory_hub.services.event_ingest import ingest_events


pytestmark = pytest.mark.skipif(not os.getenv("MEMORY_HUB_DATABASE_URL"), reason="requires PostgreSQL")


def test_entity_annotated_shared_content_ingest_schedules_project_graph_job() -> None:
    factory = create_session_factory(load_settings().database_url or "")
    project_id = f"graph-schedule-{uuid4().hex}"
    event_id = uuid4()
    payload = EventPayload(
        schema_version="1.0",
        event_id=event_id,
        agent_id="pytest",
        agent_instance_id="pytest-1",
        operation="record",
        scope="project_shared",
        content_markdown="Checkout validates payments.",
        metadata={"system_area": "Checkout", "class_names": ["CheckoutVerifier"], "module_names": ["payments"]},
        occurred_at=datetime.now(UTC),
        content_hash="sha256:" + event_id.hex.ljust(64, "0"),
    )

    with factory() as session:
        response = ingest_events(session, project_id, "alice", [payload], user_debounce_seconds=0, project_debounce_seconds=0)
        assert response.accepted == [event_id]
        job = session.get(BriefJob, f"project_graph:{project_id}:-")
        assert job is not None
        assert job.brief_type == "project_graph"


def test_system_area_only_shared_content_does_not_schedule_project_graph_job() -> None:
    factory = create_session_factory(load_settings().database_url or "")
    project_id = f"graph-no-entities-{uuid4().hex}"
    event_id = uuid4()
    payload = EventPayload(
        schema_version="1.0",
        event_id=event_id,
        agent_id="pytest",
        agent_instance_id="pytest-1",
        operation="record",
        scope="project_shared",
        content_markdown="Checkout validation report.",
        metadata={"system_area": "Checkout validation report"},
        occurred_at=datetime.now(UTC),
        content_hash="sha256:" + event_id.hex.ljust(64, "0"),
    )

    with factory() as session:
        response = ingest_events(session, project_id, "alice", [payload], user_debounce_seconds=0, project_debounce_seconds=0)
        assert response.accepted == [event_id]
        assert session.get(BriefJob, f"project_graph:{project_id}:-") is None