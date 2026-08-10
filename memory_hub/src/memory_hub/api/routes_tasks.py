from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from memory_hub.api.dependencies import require_principal
from memory_hub.auth.permissions import Principal
from memory_hub.tasks.projector import task_catalog, task_graph_bundle, task_history

router = APIRouter()


def _assert_project(principal: Principal, project_id: str) -> None:
    if principal.project_id != project_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "project access denied")


@router.get("/v1/projects/{project_id}/tasks")
def tasks(
    project_id: str,
    request: Request,
    state: str = Query(default="working", max_length=32),
    q: str = Query(default="", max_length=256),
    agent: str = Query(default="", max_length=256),
    cursor: str | None = Query(default=None, max_length=1024),
    limit: int = Query(default=40, ge=1, le=100),
    principal: Principal = Depends(require_principal("context:read")),
) -> dict[str, object]:
    _assert_project(principal, project_id)
    if not request.app.state.rate_limiter.allow(principal.token_id, "task_graph_read", 120):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded")
    try:
        with request.app.state.session_factory() as session:
            return task_catalog(session, project_id, state=state, search=q, agent=agent, cursor=cursor, limit=limit)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.get("/v1/projects/{project_id}/task-graph")
def task_graph(
    project_id: str,
    request: Request,
    task_id: str | None = Query(default=None, max_length=256),
    agent_id: str | None = Query(default=None, max_length=256),
    max_nodes: int = Query(default=200, ge=1, le=200),
    max_edges: int = Query(default=400, ge=1, le=400),
    principal: Principal = Depends(require_principal("context:read")),
) -> dict[str, object]:
    _assert_project(principal, project_id)
    if not request.app.state.rate_limiter.allow(principal.token_id, "task_graph_read", 120):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded")
    with request.app.state.session_factory() as session:
        return task_graph_bundle(
            session,
            project_id,
            task_id=task_id,
            agent_id=agent_id,
            max_nodes=max_nodes,
            max_edges=max_edges,
        )


@router.get("/v1/projects/{project_id}/task-events")
def task_events(
    project_id: str,
    request: Request,
    task_id: str | None = Query(default=None, max_length=256),
    cursor: int = Query(default=0, ge=0),
    max_items: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(require_principal("context:read")),
) -> dict[str, object]:
    _assert_project(principal, project_id)
    if not request.app.state.rate_limiter.allow(principal.token_id, "task_graph_read", 120):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded")
    with request.app.state.session_factory() as session:
        return task_history(session, project_id, task_id=task_id, after_seq=cursor, max_items=max_items)