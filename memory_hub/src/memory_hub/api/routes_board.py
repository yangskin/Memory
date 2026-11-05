from __future__ import annotations

import re
import math
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status

from memory_hub.api.dependencies import effective_user_id, require_principal
from memory_hub.auth.permissions import Principal, author_auth_id
from sqlalchemy import select
from memory_hub.db.models import BoardPost, DeviceEnrollment, AccessToken
from memory_hub.db.repositories import board_post_by_id, encode_board_cursor, list_board_posts
from memory_hub.domain.board import BoardPostRequest, BoardQueryRequest, BoardReplyRequest, BoardResolveRequest
from memory_hub.services.secret_scan import contains_secret

router = APIRouter()

# 设备状态（hub-health / codex-execution）已迁到独立的 device-status 模块，Board 不再接收。
RETIRED_STATUS_AGENTS = ("hub-health", "codex-execution")
# 机器写入的 note 不属于人类留言：原窗口续办收据仍在 Board，遗留状态作者一并排除。
MACHINE_NOTE_AGENTS = RETIRED_STATUS_AGENTS + ("codex-original-wake", "codex-original-wake-status")


def _assert_project(principal: Principal, project_id: str) -> None:
    if principal.project_id != project_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "project access denied")


def _ensure_board_content_safe(content: str) -> None:
    if contains_secret(content):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "board content appears to include secret material")


def _ensure_not_device_status(author_agent_id: str | None) -> None:
    if author_agent_id in RETIRED_STATUS_AGENTS:
        raise HTTPException(status.HTTP_410_GONE, "device status moved to /device-status; board no longer accepts it")


def _enforce_rate_limit(request: Request, principal: Principal, category: str, limit: int) -> None:
    decision = request.app.state.rate_limiter.check(principal.token_id, category, limit)
    if not decision.allowed:
        retry_after = max(1, math.ceil(decision.retry_after_seconds))
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "rate limit exceeded",
            headers={"Retry-After": str(retry_after)},
        )


def _item(post: BoardPost, *, include_content: bool = False, include_references: bool = False) -> dict[str, object]:
    result: dict[str, object] = {
        "post_id": str(post.post_id),
        "project_id": post.project_id,
        "author_user_id": post.author_user_id,
        "author_auth_id": post.author_auth_id,
        "author_agent_id": post.author_agent_id,
        "author_agent_instance_id": post.author_agent_instance_id,
        "runtime_node_id": post.runtime_node_id,
        "source_node_name": post.source_node_name,
        "workspace_id": post.workspace_id,
        "agent_session_id": post.agent_session_id,
        "transport_id": post.transport_id,
        "post_type": post.post_type,
        "content": post.content if include_content else post.content[:512],
        "content_truncated": not include_content and len(post.content) > 512,
        "task_id": post.task_id,
        "thread_id": str(post.thread_id),
        "reply_to": str(post.reply_to) if post.reply_to else None,
        "status": post.status,
        "created_at": post.created_at.isoformat(),
        "updated_at": post.updated_at.isoformat(),
        "expires_at": post.expires_at.isoformat() if post.expires_at else None,
    }
    if include_references:
        result["references_json"] = list(post.references_json or [])
    return result


def auth_id(project_id: str, token_id: str) -> str:
    # 公开不可逆标识，不返回令牌ID、凭据或其秘密部分。
    return author_auth_id(project_id, token_id)


@router.get("/v1/projects/{project_id}/device-identities")
def device_identities(project_id: str, request: Request, device_name: str | None = None,
                      principal: Principal = Depends(require_principal("context:read"))):
    _assert_project(principal, project_id)
    _enforce_rate_limit(request, principal, "board_read", 120)
    if device_name is not None and not re.fullmatch(r"[^\W_][\w.:-]{0,95}", device_name):
        raise HTTPException(422, "invalid device name")
    now = datetime.now(UTC)
    with request.app.state.session_factory() as session:
        query = select(DeviceEnrollment, AccessToken).join(AccessToken, DeviceEnrollment.token_id == AccessToken.token_id).where(
            DeviceEnrollment.project_id == project_id, DeviceEnrollment.status == "approved",
            AccessToken.project_id == project_id, AccessToken.revoked_at.is_(None),
            AccessToken.expires_at.is_(None) | (AccessToken.expires_at > now))
        query = query.where(DeviceEnrollment.device_name == device_name) if device_name is not None else query.where(AccessToken.token_id == principal.token_id)
        rows = session.execute(query.limit(2)).all()
        return {"ok": True, "identities": [{"device_name": enrollment.device_name,
            "author_user_id": token.user_id, "author_auth_id": auth_id(project_id, token.token_id)} for enrollment, token in rows]}


@router.post("/v1/projects/{project_id}/board/query")
def board_query(project_id: str, payload: BoardQueryRequest, request: Request, principal: Principal = Depends(require_principal("context:read"))) -> dict[str, object]:
    _assert_project(principal, project_id)
    _enforce_rate_limit(request, principal, "board_read", 120)
    factory = request.app.state.session_factory
    with factory() as session:
        try:
            # 多取 1 条仅用于判断是否还有下一页，不会返回给调用方。
            fetched = list_board_posts(
                session,
                project_id=project_id,
                user_id=payload.user_id,
                agent_instance_id=payload.agent_instance_id,
                task_id=payload.task_id,
                status=payload.status,
                post_type=payload.post_type,
                thread_id=payload.thread_id,
                unresolved_only=(payload.filter == "unresolved"),
                max_items=payload.max_items + 1,
                cursor=payload.cursor,
                include_expired=False,
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        has_more = len(fetched) > payload.max_items
        items = fetched[: payload.max_items]
        return {
            "ok": True,
            "operation": "board",
            "action": "query",
            "filter": payload.filter,
            "total": len(items),
            "has_more": has_more,
            "next_cursor": encode_board_cursor(items[-1]) if has_more and items else None,
            "items": [_item(item, include_content=payload.include_content, include_references=payload.include_references) for item in items],
        }


@router.post("/v1/projects/{project_id}/board/post")
def board_post(project_id: str, payload: BoardPostRequest, request: Request, principal: Principal = Depends(require_principal("events:write"))) -> dict[str, object]:
    _assert_project(principal, project_id)
    _ensure_board_content_safe(payload.content)
    _ensure_not_device_status(payload.author_agent_id)

    now = datetime.now(UTC)
    post_id = payload.post_id or uuid4()
    thread_id = payload.thread_id or post_id
    factory = request.app.state.session_factory
    with factory() as session:
        existing = session.get(BoardPost, post_id)
        if existing is not None:
            _assert_project(principal, existing.project_id)
            return {"ok": True, "operation": "board", "action": "post", "post": _item(existing)}
        _enforce_rate_limit(request, principal, "board_write", 60)
        item = BoardPost(
            post_id=post_id,
            project_id=project_id,
            author_user_id=effective_user_id(request, principal),
            author_auth_id=auth_id(principal.project_id, principal.token_id),
            author_agent_id=payload.author_agent_id,
            author_agent_instance_id=payload.author_agent_instance_id,
            runtime_node_id=payload.runtime_node_id,
            source_node_name=payload.source_node_name,
            workspace_id=payload.workspace_id,
            agent_session_id=payload.agent_session_id,
            transport_id=payload.transport_id,
            post_type=payload.post_type,
            content=payload.content,
            task_id=payload.task_id,
            thread_id=thread_id,
            reply_to=None,
            references_json=payload.references_json,
            status="open",
            created_at=now,
            updated_at=now,
            expires_at=payload.expires_at,
        )
        session.add(item)
        session.commit()
        session.refresh(item)
        return {"ok": True, "operation": "board", "action": "post", "post": _item(item)}


@router.post("/v1/projects/{project_id}/board/reply")
def board_reply(project_id: str, payload: BoardReplyRequest, request: Request, principal: Principal = Depends(require_principal("events:write"))) -> dict[str, object]:
    _assert_project(principal, project_id)
    _ensure_board_content_safe(payload.content)
    _ensure_not_device_status(payload.author_agent_id)

    factory = request.app.state.session_factory
    with factory() as session:
        if payload.post_id is not None:
            existing = session.get(BoardPost, payload.post_id)
            if existing is not None:
                _assert_project(principal, existing.project_id)
                return {"ok": True, "operation": "board", "action": "reply", "post": _item(existing)}
        _enforce_rate_limit(request, principal, "board_write", 60)
        reply_to = payload.reply_to
        thread_id = payload.thread_id
        if reply_to is not None:
            parent = board_post_by_id(session, project_id, reply_to)
            if parent is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "reply_to post not found")
            thread_id = parent.thread_id
        if thread_id is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "thread_id or reply_to is required")
        thread_head = list_board_posts(session, project_id=project_id, thread_id=thread_id, max_items=1)
        if not thread_head:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "thread not found")

        now = datetime.now(UTC)
        item = BoardPost(
            post_id=payload.post_id or uuid4(),
            project_id=project_id,
            author_user_id=effective_user_id(request, principal),
            author_auth_id=auth_id(principal.project_id, principal.token_id),
            author_agent_id=payload.author_agent_id,
            author_agent_instance_id=payload.author_agent_instance_id,
            runtime_node_id=payload.runtime_node_id,
            source_node_name=payload.source_node_name,
            workspace_id=payload.workspace_id,
            agent_session_id=payload.agent_session_id,
            transport_id=payload.transport_id,
            post_type="reply",
            content=payload.content,
            task_id=payload.task_id,
            thread_id=thread_id,
            reply_to=reply_to,
            references_json=payload.references_json,
            status="open",
            created_at=now,
            updated_at=now,
            expires_at=payload.expires_at,
        )
        session.add(item)
        session.commit()
        session.refresh(item)
        return {"ok": True, "operation": "board", "action": "reply", "post": _item(item)}


@router.post("/v1/projects/{project_id}/board/resolve")
def board_resolve(project_id: str, payload: BoardResolveRequest, request: Request, principal: Principal = Depends(require_principal("events:write"))) -> dict[str, object]:
    _assert_project(principal, project_id)
    factory = request.app.state.session_factory
    with factory() as session:
        item = board_post_by_id(session, project_id, payload.post_id)
        if item is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "post not found")
        if item.status == "resolved":
            return {"ok": True, "operation": "board", "action": "resolve", "post": _item(item)}
        _enforce_rate_limit(request, principal, "board_write", 60)
        item.status = "resolved"
        item.updated_at = datetime.now(UTC)
        session.commit()
        session.refresh(item)
        return {"ok": True, "operation": "board", "action": "resolve", "post": _item(item)}


@router.post("/v1/shared-board")
def shared_board(payload: BoardQueryRequest, request: Request, principal: Principal = Depends(require_principal("context:read"))) -> dict[str, object]:
    """Read-only bounded board feed for the dashboard.

    The project is taken from the token's principal, never from the request
    body. ``items`` holds human posts only; machine-written notes are excluded
    so they never consume the message window or inflate the message counts.
    Device status lives in the separate ``/v1/shared-device-status`` view.
    Full content and references require explicit request flags.
    """
    _enforce_rate_limit(request, principal, "board_read", 120)
    project_id = principal.project_id
    factory = request.app.state.session_factory
    with factory() as session:
        posts = list_board_posts(
            session,
            project_id=project_id,
            max_items=payload.max_items,
            exclude_author_agents=list(MACHINE_NOTE_AGENTS),
        )
        return {
            "project_id": project_id,
            "generated_at": datetime.now(UTC).isoformat(),
            "total": len(posts),
            "items": [_item(post, include_content=payload.include_content, include_references=payload.include_references) for post in posts],
        }
