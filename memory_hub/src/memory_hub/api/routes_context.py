from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import desc, select

from memory_hub.api.dependencies import require_principal
from memory_hub.auth.permissions import Principal
from memory_hub.db.models import BriefHead, BriefSnapshot, MemoryEvent
from memory_hub.domain.shared_context import SharedContextRequest

router = APIRouter()

_SAFE_METADATA_KEYS = frozenset({
    "branch",
    "system_area",
    "module_names",
    "class_names",
    "asset_paths",
    "blueprint_paths",
    "active_files",
    "confidence",
    "validated_by",
})
_PROJECT_VISIBLE_SCOPES = frozenset({"shared", "project_shared", "org_shared"})


def _item(event: MemoryEvent) -> dict[str, object]:
    metadata = {
        key: value
        for key, value in event.metadata_json.items()
        if key in _SAFE_METADATA_KEYS
    }
    return {"event_id": str(event.event_id), "user_id": event.user_id, "agent_id": event.agent_id, "agent_instance_id": event.agent_instance_id, "task_id": event.task_id, "task_run_id": event.task_run_id, "record_kind": event.record_kind, "task_phase": event.task_phase, "occurred_at": event.occurred_at.isoformat(), "last_reported_at": event.occurred_at.isoformat(), "metadata": metadata}


def _latest_per_workstream(events: list[MemoryEvent], limit: int, *, per_user_limit: int | None = None, task_grouped: bool = False) -> list[MemoryEvent]:
    selected: list[MemoryEvent] = []
    seen: set[tuple[str, str | None, str | None]] = set()
    per_user: dict[str, int] = {}
    for event in events:
        key = (event.user_id, event.task_id, event.task_run_id) if task_grouped else (event.user_id, event.agent_instance_id, event.task_run_id)
        if key in seen or (per_user_limit is not None and per_user.get(event.user_id, 0) >= per_user_limit):
            continue
        seen.add(key)
        per_user[event.user_id] = per_user.get(event.user_id, 0) + 1
        selected.append(event)
        if len(selected) >= limit:
            break
    return selected


def _brief(session, project_id: str, brief_type: str, subject_user_id: str) -> tuple[dict[str, object] | None, int]:
    head = session.get(BriefHead, (project_id, brief_type, subject_user_id))
    if head is None:
        return None, 0
    snapshot = session.get(BriefSnapshot, head.current_brief_id)
    if snapshot is None:
        return None, 0
    return {"generated_at": snapshot.generated_at.isoformat(), "covers_through_seq": snapshot.input_seq_to, "markdown": snapshot.rendered_markdown}, snapshot.input_seq_to


@router.post("/v1/projects/{project_id}/context")
def shared_context(project_id: str, payload: SharedContextRequest, request: Request, principal: Principal = Depends(require_principal("context:read"))) -> dict[str, object]:
    if principal.project_id != project_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "project access denied")
    if not request.app.state.rate_limiter.allow(principal.token_id, "context", 120):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded")
    since = datetime.now(UTC) - timedelta(minutes=payload.max_age_minutes)
    factory = request.app.state.session_factory
    with factory() as session:
        events = list(session.scalars(select(MemoryEvent).where(MemoryEvent.project_id == project_id, MemoryEvent.occurred_at >= since).order_by(desc(MemoryEvent.occurred_at)).limit(500)))
        current = payload.agent_instance_id
        latest_seq = max((event.server_seq for event in events), default=0)
        user_brief, user_watermark = _brief(session, project_id, "user_recent", principal.user_id)
        project_brief, project_watermark = _brief(session, project_id, "project_recent", "")
        def is_pending(event: MemoryEvent) -> bool:
            watermark = user_watermark if event.user_id == principal.user_id and event.scope not in _PROJECT_VISIBLE_SCOPES else project_watermark
            return event.server_seq > watermark

        result: dict[str, object] = {"pending_updates": [_item(event) for event in events if is_pending(event)][:10], "freshness": {"latest_event_seq": latest_seq, "user_brief_lag_events": max(0, latest_seq - user_watermark), "project_brief_lag_events": max(0, latest_seq - project_watermark)}}
        if "user_brief" in payload.include and user_brief:
            result["user_brief"] = user_brief
        if "project_brief" in payload.include and project_brief:
            result["project_brief"] = project_brief
        if "same_task_agents" in payload.include and payload.task_id:
            result["same_task_agents"] = [_item(event) for event in _latest_per_workstream([event for event in events if event.task_id == payload.task_id and event.agent_instance_id != current], payload.max_items)]
        if "my_other_agents" in payload.include:
            result["my_other_agents"] = [_item(event) for event in _latest_per_workstream([event for event in events if event.user_id == principal.user_id and event.agent_instance_id != current], payload.max_items)]
        if "other_tasks" in payload.include:
            result["other_tasks"] = [_item(event) for event in _latest_per_workstream([event for event in events if event.task_id != payload.task_id], payload.max_items, task_grouped=True)]
        if "project_activity" in payload.include:
            result["project_activity"] = [_item(event) for event in _latest_per_workstream([event for event in events if event.agent_instance_id != current], payload.max_items, per_user_limit=3)]
        return result