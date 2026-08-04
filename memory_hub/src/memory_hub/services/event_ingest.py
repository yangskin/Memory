"""Append-only event ingestion, including deterministic secret redaction."""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from memory_hub.db.models import MemoryEvent
from memory_hub.db.repositories import event_by_id, mark_brief_jobs_dirty
from memory_hub.domain.events import EventBatchResponse, EventPayload, RejectedEvent

_SECRET = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|(?:sk|ghp|github_pat)_[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._-]{20,}|(?:postgres(?:ql)?|mysql)://[^\s]+", re.I)


def ingest_events(session: Session, project_id: str, user_id: str, events: list[EventPayload]) -> EventBatchResponse:
    response = EventBatchResponse()
    accepted_sequences: list[int] = []
    for event in events:
        existing = event_by_id(session, project_id, event.event_id)
        if existing is not None:
            if existing.content_hash == event.content_hash:
                response.duplicates.append(event.event_id)
            else:
                response.rejected.append(RejectedEvent(event_id=event.event_id, code="event_id_conflict", message="event_id already exists with different content"))
            continue
        content = event.content_markdown
        redacted = bool(content and _SECRET.search(content))
        if redacted:
            content = None
            response.warnings.append(f"{event.event_id}: content redacted")
        model = MemoryEvent(event_id=event.event_id, project_id=project_id, user_id=user_id, source_node_id=event.source_node_id, agent_id=event.agent_id, agent_instance_id=event.agent_instance_id, task_id=event.task_id, task_run_id=event.task_run_id, operation=event.operation, record_kind=event.record_kind, scope=event.scope, task_phase=event.task_phase, content_markdown=content, metadata_json=event.metadata, source_record_id=event.source_record_id, occurred_at=event.occurred_at, content_hash=event.content_hash, content_redacted=redacted)
        session.add(model)
        session.flush()
        accepted_sequences.append(model.server_seq)
        response.accepted.append(event.event_id)
        mark_brief_jobs_dirty(session, project_id, user_id, model.server_seq)
    session.commit()
    return response