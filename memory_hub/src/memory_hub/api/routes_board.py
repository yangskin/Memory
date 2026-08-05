from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status

from memory_hub.api.dependencies import effective_user_id, require_principal
from memory_hub.auth.permissions import Principal
from memory_hub.db.models import BoardPost
from memory_hub.db.repositories import board_post_by_id, list_board_posts
from memory_hub.domain.board import BoardPostRequest, BoardQueryRequest, BoardReplyRequest, BoardResolveRequest

router = APIRouter()

_SECRET = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?:sk|ghp|github_pat)_[A-Za-z0-9_-]{20,}"
    r"|Bearer\s+[A-Za-z0-9._-]{20,}"
    r"|(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?)://[^\s]+"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\b(?:ASIA|A3T)[0-9A-Z]{16}\b"
    r"|\bxox[baprs]-[A-Za-z0-9-]{10,}"
    r"|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
    r"|\"type\"\s*:\s*\"service_account\"",
    re.I,
)


def _assert_project(principal: Principal, project_id: str) -> None:
    if principal.project_id != project_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "project access denied")


def _ensure_board_content_safe(content: str) -> None:
    if _SECRET.search(content):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "board content appears to include secret material")


def _item(post: BoardPost, *, include_content: bool = False, include_references: bool = False) -> dict[str, object]:
    result: dict[str, object] = {
        "post_id": str(post.post_id),
        "project_id": post.project_id,
        "author_user_id": post.author_user_id,
        "author_agent_id": post.author_agent_id,
        "author_agent_instance_id": post.author_agent_instance_id,
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


@router.post("/v1/projects/{project_id}/board/query")
def board_query(project_id: str, payload: BoardQueryRequest, request: Request, principal: Principal = Depends(require_principal("context:read"))) -> dict[str, object]:
    _assert_project(principal, project_id)
    if not request.app.state.rate_limiter.allow(principal.token_id, "board_read", 120):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded")
    factory = request.app.state.session_factory
    with factory() as session:
        items = list_board_posts(
            session,
            project_id=project_id,
            user_id=payload.user_id,
            agent_instance_id=payload.agent_instance_id,
            task_id=payload.task_id,
            status=payload.status,
            post_type=payload.post_type,
            thread_id=payload.thread_id,
            unresolved_only=(payload.filter == "unresolved"),
            max_items=payload.max_items,
        )
        return {
            "ok": True,
            "operation": "board",
            "action": "query",
            "filter": payload.filter,
            "total": len(items),
            "items": [_item(item, include_content=payload.include_content, include_references=payload.include_references) for item in items],
        }


@router.post("/v1/projects/{project_id}/board/post")
def board_post(project_id: str, payload: BoardPostRequest, request: Request, principal: Principal = Depends(require_principal("events:write"))) -> dict[str, object]:
    _assert_project(principal, project_id)
    if not request.app.state.rate_limiter.allow(principal.token_id, "board_write", 60):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded")
    _ensure_board_content_safe(payload.content)

    now = datetime.now(UTC)
    post_id = payload.post_id or uuid4()
    thread_id = payload.thread_id or post_id
    factory = request.app.state.session_factory
    with factory() as session:
        existing = session.get(BoardPost, post_id)
        if existing is not None:
            _assert_project(principal, existing.project_id)
            return {"ok": True, "operation": "board", "action": "post", "post": _item(existing)}
        item = BoardPost(
            post_id=post_id,
            project_id=project_id,
            author_user_id=effective_user_id(request, principal),
            author_agent_id=payload.author_agent_id,
            author_agent_instance_id=payload.author_agent_instance_id,
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
    if not request.app.state.rate_limiter.allow(principal.token_id, "board_write", 60):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded")
    _ensure_board_content_safe(payload.content)

    factory = request.app.state.session_factory
    with factory() as session:
        if payload.post_id is not None:
            existing = session.get(BoardPost, payload.post_id)
            if existing is not None:
                _assert_project(principal, existing.project_id)
                return {"ok": True, "operation": "board", "action": "reply", "post": _item(existing)}
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
            author_agent_id=payload.author_agent_id,
            author_agent_instance_id=payload.author_agent_instance_id,
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
    if not request.app.state.rate_limiter.allow(principal.token_id, "board_write", 60):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded")

    factory = request.app.state.session_factory
    with factory() as session:
        item = board_post_by_id(session, project_id, payload.post_id)
        if item is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "post not found")
        item.status = "resolved"
        item.updated_at = datetime.now(UTC)
        session.commit()
        session.refresh(item)
        return {"ok": True, "operation": "board", "action": "resolve", "post": _item(item)}


@router.post("/v1/shared-board")
def shared_board(payload: BoardQueryRequest, request: Request, principal: Principal = Depends(require_principal("context:read"))) -> dict[str, object]:
    """Read-only bounded board feed for the dashboard.

    The project is taken from the token's principal, never from the request
    body. Returns the newest bounded set of open and resolved posts; full
    content and references require explicit request flags.
    """
    if not request.app.state.rate_limiter.allow(principal.token_id, "board_read", 120):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded")
    project_id = principal.project_id
    factory = request.app.state.session_factory
    with factory() as session:
        posts = list_board_posts(session, project_id=project_id, max_items=payload.max_items)
        return {
            "project_id": project_id,
            "generated_at": datetime.now(UTC).isoformat(),
            "total": len(posts),
            "items": [_item(post, include_content=payload.include_content, include_references=payload.include_references) for post in posts],
        }
