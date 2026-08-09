"""Append-only event ingestion, including deterministic secret redaction."""

from __future__ import annotations

import re

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from memory_hub.db.models import MemoryEvent
from memory_hub.db.repositories import event_by_id, mark_brief_jobs_dirty
from memory_hub.domain.events import EventBatchResponse, EventPayload, RejectedEvent
from memory_hub.domain.tasks import task_event_from_metadata
from memory_hub.graph.extractor import InvalidGraphDelta, validate_graph_delta
from memory_hub.graph.semantic import has_project_graph_entities
from memory_hub.tasks.projector import TaskProjectionError, project_task_event

_SECRET = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?:sk|ghp|github_pat)_[A-Za-z0-9_-]{20,}"
    r"|Bearer\s+[A-Za-z0-9._-]{20,}"
    r"|(?:postgres(?:ql)?|mysql)://[^\s]+"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\b(?:ASIA|A3T)[0-9A-Z]{16}\b"
    r"|\bxox[baprs]-[A-Za-z0-9-]{10,}"
    r"|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
    r"|\"type\"\s*:\s*\"service_account\"",
    re.I,
)
_PROJECT_VISIBLE_SCOPES = frozenset({"shared", "project_shared", "org_shared"})


def ingest_events(session: Session, project_id: str, user_id: str, events: list[EventPayload], *, user_debounce_seconds: int = 20, project_debounce_seconds: int = 45) -> EventBatchResponse:
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
        if isinstance(event.metadata, dict) and "graph_delta" in event.metadata:
            try:
                validate_graph_delta(event.metadata, event.task_id or "")
            except InvalidGraphDelta as exc:
                response.rejected.append(
                    RejectedEvent(event_id=event.event_id, code="invalid_graph_delta", message=str(exc))
                )
                continue
        task_event = None
        if event.operation == "task_sync":
            if event.scope not in _PROJECT_VISIBLE_SCOPES:
                response.rejected.append(
                    RejectedEvent(event_id=event.event_id, code="invalid_task_scope", message="task_sync events require a project-visible scope")
                )
                continue
            try:
                task_event = task_event_from_metadata(event.metadata, event.task_id)
            except Exception as exc:
                response.rejected.append(
                    RejectedEvent(event_id=event.event_id, code="invalid_task_event", message=str(exc))
                )
                continue
            if task_event.actor_id != event.agent_id:
                response.rejected.append(
                    RejectedEvent(event_id=event.event_id, code="invalid_task_actor", message="task_event.actor_id must match the outer event agent_id")
                )
                continue
        content = event.content_markdown
        redacted = bool(content and _SECRET.search(content))
        if redacted:
            content = None
            response.warnings.append(f"{event.event_id}: content redacted")
        try:
            with session.begin_nested():
                model = MemoryEvent(event_id=event.event_id, project_id=project_id, user_id=user_id, source_node_id=event.source_node_id, agent_id=event.agent_id, agent_instance_id=event.agent_instance_id, task_id=event.task_id, task_run_id=event.task_run_id, operation=event.operation, record_kind=event.record_kind, scope=event.scope, task_phase=event.task_phase, content_markdown=content, metadata_json=event.metadata, source_record_id=event.source_record_id, occurred_at=event.occurred_at, content_hash=event.content_hash, content_redacted=redacted)
                model.source_node_name = event.source_node_name
                model.runtime_node_id = event.runtime_node_id
                model.workspace_id = event.workspace_id
                model.agent_session_id = event.agent_session_id
                model.transport_id = event.transport_id
                session.add(model)
                session.flush()
                if task_event is not None:
                    project_task_event(session, project_id, model, task_event)
                accepted_sequences.append(model.server_seq)
                response.accepted.append(event.event_id)
                mark_brief_jobs_dirty(
                    session,
                    project_id,
                    user_id,
                    model.server_seq,
                    user_debounce_seconds=user_debounce_seconds,
                    project_debounce_seconds=project_debounce_seconds,
                    include_project_graph=model.scope in _PROJECT_VISIBLE_SCOPES and bool(content and content.strip()) and has_project_graph_entities(event.metadata),
                )
        except TaskProjectionError as exc:
            response.rejected.append(RejectedEvent(event_id=event.event_id, code=exc.code, message=str(exc)))
        except IntegrityError:
            existing = event_by_id(session, project_id, event.event_id)
            if existing is not None and existing.content_hash == event.content_hash:
                response.duplicates.append(event.event_id)
            else:
                response.rejected.append(RejectedEvent(event_id=event.event_id, code="event_id_conflict", message="event_id already exists with different content"))
    session.commit()
    return response