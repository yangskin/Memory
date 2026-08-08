from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from memory_hub.api.dependencies import effective_user_id, require_principal
from memory_hub.auth.permissions import Principal
from memory_hub.db.models import BriefHead, BriefSnapshot, MemoryEvent
from memory_hub.db.models import BriefHead, BriefSnapshot, ContextUsageDaily, MemoryEvent
from memory_hub.domain.shared_context import SharedContextRequest
from memory_hub.domain.shared_feed import SharedFeedRequest

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


def _project_visible_content() -> object:
    """Restrict dashboard and brief inputs to events with a displayable body.

    Empty project-shared checkpoints carry graph-projection metadata only. They
    remain in the event log, but must not crowd out substantive shared memory.
    """
    return and_(
        MemoryEvent.scope.in_(_PROJECT_VISIBLE_SCOPES),
        MemoryEvent.content_markdown.is_not(None),
        func.btrim(MemoryEvent.content_markdown) != "",
    )


def _item(event: MemoryEvent) -> dict[str, object]:
    metadata = {
        key: value
        for key, value in event.metadata_json.items()
        if key in _SAFE_METADATA_KEYS
    }
    return {"event_id": str(event.event_id), "user_id": event.user_id, "agent_id": event.agent_id, "agent_instance_id": event.agent_instance_id, "source_node_id": event.source_node_id, "runtime_node_id": event.runtime_node_id, "source_node_name": event.source_node_name, "workspace_id": event.workspace_id, "agent_session_id": event.agent_session_id, "transport_id": event.transport_id, "task_id": event.task_id, "task_run_id": event.task_run_id, "record_kind": event.record_kind, "task_phase": event.task_phase, "occurred_at": event.occurred_at.isoformat(), "last_reported_at": event.occurred_at.isoformat(), "metadata": metadata}


def _shared_item(event: MemoryEvent, *, include_content: bool) -> dict[str, object]:
    """Event projection safe for the shared dashboard.

    Only called for events whose ``scope`` is project-visible, so including
    ``content_markdown`` here cannot leak a user's personal notes.
    """
    item = _item(event)
    item["scope"] = event.scope
    item["operation"] = event.operation
    content = event.content_markdown or ""
    item["content_markdown"] = content if include_content else content[:512]
    item["content_truncated"] = not include_content and len(content) > 512
    return item


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
    markdown = snapshot.rendered_markdown or ""
    return {"generated_at": snapshot.generated_at.isoformat(), "covers_through_seq": snapshot.input_seq_to, "markdown": markdown[:4000], "truncated": len(markdown) > 4000}, snapshot.input_seq_to

def _record_context_usage(session, project_id: str, include: list[str], returned_events: int, returned_briefs: int) -> None:
    usage_date = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    values = {
        "user_brief_requests": int("user_brief" in include),
        "project_brief_requests": int("project_brief" in include),
        "same_task_requests": int("same_task_agents" in include),
        "other_agents_requests": int("my_other_agents" in include),
        "other_tasks_requests": int("other_tasks" in include),
        "project_activity_requests": int("project_activity" in include),
    }
    statement = insert(ContextUsageDaily).values(project_id=project_id, usage_date=usage_date, request_count=1, returned_event_count=returned_events, returned_brief_count=returned_briefs, **values)
    statement = statement.on_conflict_do_update(
        index_elements=["project_id", "usage_date"],
        set_={
            "request_count": ContextUsageDaily.request_count + 1,
            "returned_event_count": ContextUsageDaily.returned_event_count + returned_events,
            "returned_brief_count": ContextUsageDaily.returned_brief_count + returned_briefs,
            **{name: getattr(ContextUsageDaily, name) + value for name, value in values.items()},
        },
    )
    session.execute(statement)


@router.post("/v1/projects/{project_id}/context")
def shared_context(project_id: str, payload: SharedContextRequest, request: Request, principal: Principal = Depends(require_principal("context:read"))) -> dict[str, object]:
    if principal.project_id != project_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "project access denied")
    if not request.app.state.rate_limiter.allow(principal.token_id, "context", 120):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded")
    since = datetime.now(UTC) - timedelta(minutes=payload.max_age_minutes)
    factory = request.app.state.session_factory
    with factory() as session:
        user_id = effective_user_id(request, principal)
        visibility = or_(MemoryEvent.user_id == user_id, MemoryEvent.scope.in_(_PROJECT_VISIBLE_SCOPES))
        displayable = or_(
            MemoryEvent.user_id == user_id,
            _project_visible_content(),
        )
        events = list(session.scalars(select(MemoryEvent).where(MemoryEvent.project_id == project_id, MemoryEvent.occurred_at >= since, visibility, displayable).order_by(desc(MemoryEvent.occurred_at)).limit(500)))
        current = payload.agent_instance_id
        latest_seq = max((event.server_seq for event in events), default=0)
        project_latest_seq = int(session.scalar(select(func.coalesce(func.max(MemoryEvent.server_seq), 0)).where(MemoryEvent.project_id == project_id, MemoryEvent.occurred_at >= since, _project_visible_content())) or 0)
        user_brief, user_watermark = _brief(session, project_id, "user_recent", user_id)
        project_brief, project_watermark = _brief(session, project_id, "project_recent", "")
        def is_pending(event: MemoryEvent) -> bool:
            watermark = user_watermark if event.user_id == user_id and event.scope not in _PROJECT_VISIBLE_SCOPES else project_watermark
            return event.server_seq > watermark

        result: dict[str, object] = {"pending_updates": [_item(event) for event in events if is_pending(event)][:10], "freshness": {"latest_event_seq": latest_seq, "user_brief_lag_events": max(0, latest_seq - user_watermark), "project_brief_lag_events": max(0, project_latest_seq - project_watermark)}}
        if "user_brief" in payload.include and user_brief:
            result["user_brief"] = user_brief
        if "project_brief" in payload.include and project_brief:
            result["project_brief"] = project_brief
        if "same_task_agents" in payload.include and payload.task_id:
            result["same_task_agents"] = [_item(event) for event in _latest_per_workstream([event for event in events if event.task_id == payload.task_id and event.agent_instance_id != current], payload.max_items)]
        if "my_other_agents" in payload.include:
            result["my_other_agents"] = [_item(event) for event in _latest_per_workstream([event for event in events if event.user_id == user_id and event.agent_instance_id != current], payload.max_items)]
        if "other_tasks" in payload.include:
            result["other_tasks"] = [_item(event) for event in _latest_per_workstream([event for event in events if event.task_id != payload.task_id], payload.max_items, task_grouped=True)]
        if "project_activity" in payload.include:
            result["project_activity"] = [_item(event) for event in _latest_per_workstream([event for event in events if event.agent_instance_id != current], payload.max_items, per_user_limit=3)]
        returned_events = len(result["pending_updates"])
        returned_events += sum(len(result.get(name, [])) for name in ("same_task_agents", "my_other_agents", "other_tasks", "project_activity"))
        returned_briefs = int("user_brief" in result) + int("project_brief" in result)
        _record_context_usage(session, project_id, payload.include, returned_events, returned_briefs)
        session.commit()
        return result


@router.get("/v1/projects/{project_id}/context/usage")
def context_usage(project_id: str, request: Request, principal: Principal = Depends(require_principal("context:read")), days: int = Query(30, ge=1, le=90)) -> dict[str, object]:
    if principal.project_id != project_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "project access denied")
    since = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    with request.app.state.session_factory() as session:
        rows = list(session.scalars(select(ContextUsageDaily).where(ContextUsageDaily.project_id == project_id, ContextUsageDaily.usage_date >= since).order_by(ContextUsageDaily.usage_date)))
        return {
            "project_id": project_id,
            "days": [
                {
                    "date": row.usage_date.date().isoformat(),
                    "request_count": row.request_count,
                    "returned_event_count": row.returned_event_count,
                    "returned_brief_count": row.returned_brief_count,
                    "include_requests": {
                        "user_brief": row.user_brief_requests,
                        "project_brief": row.project_brief_requests,
                        "same_task_agents": row.same_task_requests,
                        "my_other_agents": row.other_agents_requests,
                        "other_tasks": row.other_tasks_requests,
                        "project_activity": row.project_activity_requests,
                    },
                }
                for row in rows
            ],
        }


@router.post("/v1/shared-feed")
def shared_feed(payload: SharedFeedRequest, request: Request, principal: Principal = Depends(require_principal("context:read"))) -> dict[str, object]:
    """Read-only dashboard feed of project-visible shared memory only.

    The project is taken from the token's principal, never from the request
    body. The query is scoped strictly to shared/project-visible scopes and
    the LLM project brief; a user's personal-scope events are never selected,
    so the response cannot leak private content to a browser.
    """
    if not request.app.state.rate_limiter.allow(principal.token_id, "shared_feed", 120):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded")
    project_id = principal.project_id
    since = datetime.now(UTC) - timedelta(minutes=payload.max_age_minutes)
    factory = request.app.state.session_factory
    with factory() as session:
        visible = _project_visible_content()
        events = list(session.scalars(select(MemoryEvent).where(MemoryEvent.project_id == project_id, MemoryEvent.occurred_at >= since, visible).order_by(desc(MemoryEvent.occurred_at)).limit(payload.max_items)))
        events_from_history = False
        if not events:
            events = list(session.scalars(select(MemoryEvent).where(MemoryEvent.project_id == project_id, visible).order_by(desc(MemoryEvent.occurred_at)).limit(payload.max_items)))
            events_from_history = bool(events)
        latest_seq = max((event.server_seq for event in events), default=0)
        project_latest_seq = int(session.scalar(select(func.coalesce(func.max(MemoryEvent.server_seq), 0)).where(MemoryEvent.project_id == project_id, MemoryEvent.occurred_at >= since, visible)) or 0)

        head = session.get(BriefHead, (project_id, "project_recent", ""))
        brief: dict[str, object] | None = None
        watermark = 0
        if head is not None:
            snapshot = session.get(BriefSnapshot, head.current_brief_id)
            if snapshot is not None:
                brief = {
                    "generated_at": snapshot.generated_at.isoformat(),
                    "covers_through_seq": snapshot.input_seq_to,
                    "markdown": snapshot.rendered_markdown if payload.include_brief_details else snapshot.rendered_markdown[:4000],
                }
                if payload.include_brief_details:
                    brief["structured"] = snapshot.structured_brief
                watermark = snapshot.input_seq_to
        return {
            "project_id": project_id,
            "generated_at": datetime.now(UTC).isoformat(),
            "freshness": {
                "latest_shared_seq": latest_seq,
                "project_brief_generated_at": brief["generated_at"] if brief else None,
                "project_brief_covers_through_seq": watermark,
                "project_brief_lag_events": max(0, project_latest_seq - watermark),
            },
            "brief": brief,
            "events_from_history": events_from_history,
            "events": [_shared_item(event, include_content=payload.include_content) for event in events],
        }