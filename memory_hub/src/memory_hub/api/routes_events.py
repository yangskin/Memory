from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from memory_hub.api.dependencies import require_principal
from memory_hub.auth.permissions import Principal
from memory_hub.domain.events import EventBatchRequest, EventBatchResponse
from memory_hub.services.event_ingest import ingest_events

router = APIRouter()


@router.post("/v1/projects/{project_id}/events/batch", response_model=EventBatchResponse)
def batch_events(project_id: str, payload: EventBatchRequest, request: Request, principal: Principal = Depends(require_principal("events:write"))) -> EventBatchResponse:
    if principal.project_id != project_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "project access denied")
    if len(request.scope.get("body", b"")) > 1_048_576:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "payload too large")
    factory = request.app.state.session_factory
    with factory() as session:
        return ingest_events(session, project_id, principal.user_id, payload.events)