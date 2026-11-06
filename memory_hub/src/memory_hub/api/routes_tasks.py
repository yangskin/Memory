from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from memory_hub.api.dependencies import require_principal
from memory_hub.auth.permissions import Principal
from memory_hub.domain.tasks import MAX_BODY_BYTES, TaskCommandError, invalid, parse_command
from memory_hub.tasks.commands import execute_command
from memory_hub.tasks.queries import task_catalog, task_detail, task_history

router = APIRouter()


def _assert_project(principal: Principal, project_id: str) -> None:
    if principal.project_id != project_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "project access denied")


def _read_limit(request: Request, principal: Principal) -> None:
    if not request.app.state.rate_limiter.allow(principal.token_id, "task_graph_read", 120):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded")


@router.get("/v1/projects/{project_id}/tasks")
def tasks(
    project_id: str,
    request: Request,
    state: str = Query(default="working", max_length=128),
    q: str = Query(default="", max_length=256),
    assignee: str = Query(default="", max_length=256),
    order: str = Query(default="updated", max_length=16),
    cursor: str | None = Query(default=None, max_length=2048),
    limit: int = Query(default=40, ge=1, le=100),
    principal: Principal = Depends(require_principal("context:read")),
) -> dict[str, object]:
    _assert_project(principal, project_id)
    _read_limit(request, principal)
    if "agent" in request.query_params:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "agent filter is retired; use assignee for exact matching")
    try:
        with request.app.state.session_factory() as session:
            return task_catalog(session, project_id, state=state, search=q, assignee=assignee, order=order, cursor=cursor, limit=limit)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.get("/v1/projects/{project_id}/tasks/{task_id}")
def task(
    project_id: str,
    task_id: str,
    request: Request,
    principal: Principal = Depends(require_principal("context:read")),
):
    _assert_project(principal, project_id)
    _read_limit(request, principal)
    if not 1 <= len(task_id) <= 256:
        return JSONResponse(status_code=422, content={"ok": False, "error": "invalid_input", "message": "task_id must be 1-256 characters", "field": "task_id"})
    with request.app.state.session_factory() as session:
        detail = task_detail(session, project_id, task_id)
    if detail is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "task_not_found", "message": "task_id does not exist"})
    return {"ok": True, "task": detail}


@router.get("/v1/projects/{project_id}/task-events")
def task_events(
    project_id: str,
    request: Request,
    task_id: str | None = Query(default=None, max_length=256),
    cursor: int = Query(default=0, ge=0, le=9_223_372_036_854_775_807),
    max_items: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(require_principal("context:read")),
) -> dict[str, object]:
    _assert_project(principal, project_id)
    _read_limit(request, principal)
    with request.app.state.session_factory() as session:
        return task_history(session, project_id, task_id=task_id, after_seq=cursor, max_items=max_items)


@router.get("/v1/projects/{project_id}/task-graph")
def task_graph_retired(project_id: str) -> JSONResponse:
    """Graph Bundle 已退役；旧客户端得到明确失败，而不是空结果。"""
    return JSONResponse(status_code=410, content={"ok": False, "error": "task_graph_retired",
                                                  "message": "use /tasks, /tasks/{task_id} and /task-events"})


def _run_command(request: Request, project_id: str, device_name: str, raw: object) -> JSONResponse:
    try:
        command = parse_command(raw)
        with request.app.state.session_factory() as session:
            result = execute_command(session, project_id, device_name, command)
    except TaskCommandError as exc:
        return JSONResponse(status_code=exc.status, content=exc.body())
    return JSONResponse(status_code=200, content=result)


@router.post("/v1/projects/{project_id}/task-commands")
async def task_commands(
    project_id: str,
    request: Request,
    principal: Principal = Depends(require_principal("events:write")),
) -> JSONResponse:
    _assert_project(principal, project_id)
    if not request.app.state.rate_limiter.allow(principal.token_id, "task_commands", 120):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded")
    if not principal.device_name:
        return JSONResponse(status_code=403, content={"ok": False, "error": "device_identity_required",
                                                      "message": "bind this credential to a device before sending task commands"})
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return JSONResponse(status_code=422, content=invalid("body", f"request body exceeds {MAX_BODY_BYTES} bytes").body())
    try:
        raw = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return JSONResponse(status_code=422, content=invalid("body", "request body must be UTF-8 JSON").body())
    return await run_in_threadpool(_run_command, request, project_id, principal.device_name, raw)
