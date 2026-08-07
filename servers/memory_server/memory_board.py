from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .memory_config import MemoryConfig
from .memory_events import get_current_user
from .memory_identity import canonical_identity
from .memory_locks import file_lock
from .memory_result import error_result, ok_result

ALLOWED_POST_TYPES = {"note", "question", "request", "warning", "handoff", "proposal", "reply"}
ALLOWED_STATUSES = {"open", "resolved"}
MAX_CONTENT_CHARS = 64 * 1024
BOARD_SYNC_CLAIM_TTL_SECONDS = 60
BOARD_SYNC_MAX_BACKOFF_SECONDS = 300

_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\bapi[_-]?key\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}", re.IGNORECASE),
    re.compile(r"\bbearer\s+[A-Za-z0-9\-_=]+(?:\.[A-Za-z0-9\-_=]+){1,2}", re.IGNORECASE),
    re.compile(r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?)://[^\s:@]+:[^\s@]+@", re.IGNORECASE),
)


def _now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _board_path(config: MemoryConfig) -> Path:
    return config.repo_root / ".ai-memory" / "board_posts.jsonl"


def _project_id(config: MemoryConfig) -> str:
    shared_cfg = getattr(config, "shared_memory", None)
    value = str(getattr(shared_cfg, "project_id", "") or "").strip()
    if value:
        return value
    return config.repo_root.name


def _contains_forbidden_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def _load_posts(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    posts: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                posts.append(item)
    except (OSError, json.JSONDecodeError):
        return []
    return posts


def _append_post(path: Path, post: dict[str, Any]) -> dict[str, Any] | None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(path.parent.parent, path):
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(post, ensure_ascii=False) + "\n")
    except OSError as exc:
        return error_result("write_failed", f"failed to append board post: {exc}")
    return None


def _rewrite_posts(path: Path, posts: list[dict[str, Any]]) -> dict[str, Any] | None:
    payload = "\n".join(json.dumps(item, ensure_ascii=False) for item in posts)
    if payload:
        payload += "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(path.parent.parent, path):
            path.write_text(payload, encoding="utf-8")
    except OSError as exc:
        return error_result("write_failed", f"failed to update board posts: {exc}")
    return None


def _rewrite_posts_locked(path: Path, posts: list[dict[str, Any]]) -> dict[str, Any] | None:
    payload = "\n".join(json.dumps(item, ensure_ascii=False) for item in posts)
    if payload:
        payload += "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
    except OSError as exc:
        return error_result("write_failed", f"failed to update board posts: {exc}")
    return None


def mark_board_post_pending(config: MemoryConfig, post_id: str) -> dict[str, Any] | None:
    path = _board_path(config)
    target = str(post_id or "").strip()
    with file_lock(config.repo_root, path):
        posts = _load_posts(path)
        for item in posts:
            if str(item.get("post_id") or "") == target:
                item["remote_sync"] = "pending"
                item["remote_sync_updated_at"] = _now_text()
                return _rewrite_posts_locked(path, posts)
    return error_result("not_found", f"post not found: {target}")


def mark_board_post_synced(
    config: MemoryConfig,
    post_id: str,
    remote_post: dict[str, Any],
) -> dict[str, Any] | None:
    path = _board_path(config)
    target = str(post_id or "").strip()
    with file_lock(config.repo_root, path):
        posts = _load_posts(path)
        for item in posts:
            if str(item.get("post_id") or "") == target:
                item["remote_sync"] = "synced"
                item["remote_post_id"] = str(remote_post.get("post_id") or "") or None
                item["remote_thread_id"] = str(remote_post.get("thread_id") or "") or None
                item["remote_sync_updated_at"] = _now_text()
                item["remote_sync_last_error"] = None
                item["remote_sync_next_retry_at"] = None
                return _rewrite_posts_locked(path, posts)
    return error_result("not_found", f"post not found: {target}")


def pending_board_posts(config: MemoryConfig, *, max_items: int = 20) -> list[dict[str, Any]]:
    project_id = _project_id(config)
    pending = [
        item
        for item in _load_posts(_board_path(config))
        if str(item.get("project_id") or "") == project_id
        and str(item.get("remote_sync") or "") == "pending"
    ]
    pending.sort(key=lambda item: str(item.get("created_at") or ""))
    return pending[: max(1, min(100, int(max_items or 20)))]


def claim_pending_board_posts(config: MemoryConfig, *, max_items: int = 20) -> list[dict[str, Any]]:
    path = _board_path(config)
    project_id = _project_id(config)
    now = datetime.now(timezone.utc)
    claim_before = now - timedelta(seconds=BOARD_SYNC_CLAIM_TTL_SECONDS)
    claimed: list[dict[str, Any]] = []
    with file_lock(config.repo_root, path):
        posts = _load_posts(path)
        for item in posts:
            if len(claimed) >= max(1, min(100, int(max_items or 20))):
                break
            if str(item.get("project_id") or "") != project_id:
                continue
            state = str(item.get("remote_sync") or "")
            claim_time = _parse_time(item.get("remote_sync_claimed_at"))
            if state == "syncing" and claim_time is not None and claim_time > claim_before:
                continue
            if state not in {"pending", "syncing"}:
                continue
            next_retry = _parse_time(item.get("remote_sync_next_retry_at"))
            if next_retry is not None and next_retry > now:
                continue
            item["remote_sync"] = "syncing"
            item["remote_sync_claimed_at"] = now.isoformat()
            item["remote_sync_updated_at"] = now.isoformat()
            claimed.append(dict(item))
        if claimed:
            _rewrite_posts_locked(path, posts)
    return claimed


def mark_board_post_sync_failed(
    config: MemoryConfig,
    post_id: str,
    error: str,
    *,
    retry_after_seconds: float | None = None,
) -> dict[str, Any] | None:
    path = _board_path(config)
    target = str(post_id or "").strip()
    now = datetime.now(timezone.utc)
    with file_lock(config.repo_root, path):
        posts = _load_posts(path)
        for item in posts:
            if str(item.get("post_id") or "") != target:
                continue
            attempts = int(item.get("remote_sync_attempts") or 0) + 1
            delay = retry_after_seconds if retry_after_seconds is not None else min(
                BOARD_SYNC_MAX_BACKOFF_SECONDS, 2 ** min(attempts, 8)
            )
            item["remote_sync"] = "pending"
            item["remote_sync_attempts"] = attempts
            item["remote_sync_last_error"] = str(error or "remote_sync_failed")[:1000]
            item["remote_sync_next_retry_at"] = (now + timedelta(seconds=max(1.0, float(delay)))).isoformat()
            item["remote_sync_updated_at"] = now.isoformat()
            return _rewrite_posts_locked(path, posts)
    return error_result("not_found", f"post not found: {target}")


def remote_board_post_id(config: MemoryConfig, local_post_id: str | None) -> str | None:
    target = str(local_post_id or "").strip()
    if not target:
        return None
    for item in _load_posts(_board_path(config)):
        if str(item.get("post_id") or "") != target:
            continue
        return str(item.get("remote_post_id") or target).strip() or target
    return target


def mark_board_resolve_pending(config: MemoryConfig, post_id: str) -> dict[str, Any] | None:
    path = _board_path(config)
    target = str(post_id or "").strip()
    with file_lock(config.repo_root, path):
        posts = _load_posts(path)
        for item in posts:
            if str(item.get("post_id") or "") == target:
                item["remote_resolve_sync"] = "pending"
                item["remote_sync_updated_at"] = _now_text()
                return _rewrite_posts_locked(path, posts)
    return error_result("not_found", f"post not found: {target}")


def mark_board_resolve_synced(config: MemoryConfig, post_id: str) -> dict[str, Any] | None:
    path = _board_path(config)
    target = str(post_id or "").strip()
    with file_lock(config.repo_root, path):
        posts = _load_posts(path)
        for item in posts:
            if str(item.get("post_id") or "") == target:
                item["remote_resolve_sync"] = "synced"
                item["remote_sync_updated_at"] = _now_text()
                item["remote_resolve_last_error"] = None
                item["remote_resolve_next_retry_at"] = None
                return _rewrite_posts_locked(path, posts)
    return error_result("not_found", f"post not found: {target}")


def pending_board_resolves(config: MemoryConfig, *, max_items: int = 20) -> list[dict[str, Any]]:
    project_id = _project_id(config)
    items = [
        item
        for item in _load_posts(_board_path(config))
        if str(item.get("project_id") or "") == project_id
        and str(item.get("remote_resolve_sync") or "") == "pending"
    ]
    return items[: max(1, min(100, int(max_items or 20)))]


def claim_pending_board_resolves(config: MemoryConfig, *, max_items: int = 20) -> list[dict[str, Any]]:
    path = _board_path(config)
    project_id = _project_id(config)
    now = datetime.now(timezone.utc)
    claim_before = now - timedelta(seconds=BOARD_SYNC_CLAIM_TTL_SECONDS)
    claimed: list[dict[str, Any]] = []
    with file_lock(config.repo_root, path):
        posts = _load_posts(path)
        for item in posts:
            if len(claimed) >= max(1, min(100, int(max_items or 20))):
                break
            if str(item.get("project_id") or "") != project_id:
                continue
            state = str(item.get("remote_resolve_sync") or "")
            claim_time = _parse_time(item.get("remote_resolve_claimed_at"))
            if state == "syncing" and claim_time is not None and claim_time > claim_before:
                continue
            if state not in {"pending", "syncing"}:
                continue
            next_retry = _parse_time(item.get("remote_resolve_next_retry_at"))
            if next_retry is not None and next_retry > now:
                continue
            item["remote_resolve_sync"] = "syncing"
            item["remote_resolve_claimed_at"] = now.isoformat()
            item["remote_sync_updated_at"] = now.isoformat()
            claimed.append(dict(item))
        if claimed:
            _rewrite_posts_locked(path, posts)
    return claimed


def mark_board_resolve_sync_failed(
    config: MemoryConfig,
    post_id: str,
    error: str,
    *,
    retry_after_seconds: float | None = None,
) -> dict[str, Any] | None:
    path = _board_path(config)
    target = str(post_id or "").strip()
    now = datetime.now(timezone.utc)
    with file_lock(config.repo_root, path):
        posts = _load_posts(path)
        for item in posts:
            if str(item.get("post_id") or "") != target:
                continue
            attempts = int(item.get("remote_resolve_attempts") or 0) + 1
            delay = retry_after_seconds if retry_after_seconds is not None else min(
                BOARD_SYNC_MAX_BACKOFF_SECONDS, 2 ** min(attempts, 8)
            )
            item["remote_resolve_sync"] = "pending"
            item["remote_resolve_attempts"] = attempts
            item["remote_resolve_last_error"] = str(error or "remote_sync_failed")[:1000]
            item["remote_resolve_next_retry_at"] = (now + timedelta(seconds=max(1.0, float(delay)))).isoformat()
            item["remote_sync_updated_at"] = now.isoformat()
            return _rewrite_posts_locked(path, posts)
    return error_result("not_found", f"post not found: {target}")


def cache_remote_board_items(config: MemoryConfig, items: list[dict[str, Any]]) -> None:
    if not items:
        return
    path = _board_path(config)
    with file_lock(config.repo_root, path):
        posts = _load_posts(path)
        by_id = {str(item.get("post_id") or ""): item for item in posts}
        changed = False
        for remote in items:
            post_id = str(remote.get("post_id") or "").strip()
            if not post_id or post_id in by_id:
                continue
            cached = dict(remote)
            cached["remote_sync"] = "synced"
            cached["remote_post_id"] = post_id
            posts.append(cached)
            by_id[post_id] = cached
            changed = True
        if changed:
            _rewrite_posts_locked(path, posts)


def _normalize_content(content_markdown: str | None) -> str:
    return str(content_markdown or "").strip()


def _base_author(author: str | None) -> str:
    return canonical_identity(author or "")


def _new_post(
    config: MemoryConfig,
    *,
    post_type: str,
    content: str,
    task_id: str | None,
    thread_id: str,
    reply_to: str | None,
    references_json: list[Any] | None,
    expires_at: str | None,
    author_user_id: str,
    author_agent_id: str | None,
    author_agent_instance_id: str | None,
    runtime_node_id: str | None,
    source_node_name: str | None,
    workspace_id: str | None,
    agent_session_id: str | None,
    transport_id: str | None,
) -> dict[str, Any]:
    now = _now_text()
    post_id = str(uuid.uuid4())
    return {
        "post_id": post_id,
        "project_id": _project_id(config),
        "author_user_id": author_user_id,
        "author_agent_id": str(author_agent_id or "").strip() or None,
        "author_agent_instance_id": str(author_agent_instance_id or "").strip() or None,
        "runtime_node_id": str(runtime_node_id or "").strip() or None,
        "source_node_name": str(source_node_name or "").strip() or None,
        "workspace_id": str(workspace_id or "").strip() or None,
        "agent_session_id": str(agent_session_id or "").strip() or None,
        "transport_id": str(transport_id or "").strip() or None,
        "post_type": post_type,
        "content": content,
        "task_id": str(task_id or "").strip() or None,
        "thread_id": thread_id,
        "reply_to": str(reply_to or "").strip() or None,
        "references_json": list(references_json or []),
        "status": "open",
        "created_at": now,
        "updated_at": now,
        "expires_at": str(expires_at or "").strip() or None,
    }


def board_post(
    config: MemoryConfig,
    *,
    post_type: str,
    content_markdown: str,
    task_id: str | None = None,
    thread_id: str | None = None,
    references_json: list[Any] | None = None,
    expires_at: str | None = None,
    author_user_id: str | None = None,
    author_agent_id: str | None = None,
    author_agent_instance_id: str | None = None,
    runtime_node_id: str | None = None,
    source_node_name: str | None = None,
    workspace_id: str | None = None,
    agent_session_id: str | None = None,
    transport_id: str | None = None,
) -> dict[str, Any]:
    normalized_type = str(post_type or "").strip().lower()
    if normalized_type not in (ALLOWED_POST_TYPES - {"reply"}):
        return error_result(
            "invalid_input",
            "post_type must be one of: handoff, note, proposal, question, request, warning",
        )
    content = _normalize_content(content_markdown)
    if not content:
        return error_result("invalid_input", "content_markdown must not be empty")
    if len(content) > MAX_CONTENT_CHARS:
        return error_result("invalid_input", f"content_markdown exceeds max size {MAX_CONTENT_CHARS} chars")
    if _contains_forbidden_secret(content):
        return error_result("invalid_input", "board content appears to include secret material and was rejected")

    user_id = _base_author(author_user_id or get_current_user(config.repo_root))
    post = _new_post(
        config,
        post_type=normalized_type,
        content=content,
        task_id=task_id,
        thread_id=str(thread_id or "").strip() or "",
        reply_to=None,
        references_json=references_json,
        expires_at=expires_at,
        author_user_id=user_id,
        author_agent_id=author_agent_id,
        author_agent_instance_id=author_agent_instance_id,
        runtime_node_id=runtime_node_id,
        source_node_name=source_node_name,
        workspace_id=workspace_id,
        agent_session_id=agent_session_id,
        transport_id=transport_id,
    )
    if not post["thread_id"]:
        post["thread_id"] = post["post_id"]

    write_error = _append_post(_board_path(config), post)
    if write_error is not None:
        return write_error
    return ok_result("board post created", operation="board", action="post", post=post)


def board_reply(
    config: MemoryConfig,
    *,
    content_markdown: str,
    thread_id: str | None = None,
    reply_to: str | None = None,
    task_id: str | None = None,
    references_json: list[Any] | None = None,
    expires_at: str | None = None,
    author_user_id: str | None = None,
    author_agent_id: str | None = None,
    author_agent_instance_id: str | None = None,
    runtime_node_id: str | None = None,
    source_node_name: str | None = None,
    workspace_id: str | None = None,
    agent_session_id: str | None = None,
    transport_id: str | None = None,
) -> dict[str, Any]:
    content = _normalize_content(content_markdown)
    if not content:
        return error_result("invalid_input", "content_markdown must not be empty")
    if len(content) > MAX_CONTENT_CHARS:
        return error_result("invalid_input", f"content_markdown exceeds max size {MAX_CONTENT_CHARS} chars")
    if _contains_forbidden_secret(content):
        return error_result("invalid_input", "board content appears to include secret material and was rejected")

    path = _board_path(config)
    with file_lock(config.repo_root, path):
        posts = _load_posts(path)
        target_project = _project_id(config)
        by_id = {str(item.get("post_id") or ""): item for item in posts if str(item.get("project_id") or "") == target_project}

    reply_to_id = str(reply_to or "").strip() or None
    effective_thread = str(thread_id or "").strip() or None
    if reply_to_id:
        parent = by_id.get(reply_to_id)
        if not isinstance(parent, dict) and not effective_thread:
            return error_result("not_found", f"reply_to post not found: {reply_to_id}")
        if isinstance(parent, dict):
            effective_thread = str(parent.get("thread_id") or parent.get("post_id") or "").strip() or effective_thread
    if not effective_thread:
        return error_result("invalid_input", "thread_id or reply_to is required for board reply")

    thread_exists = any(
        str(item.get("thread_id") or "") == effective_thread and str(item.get("project_id") or "") == _project_id(config)
        for item in by_id.values()
    )
    if not thread_exists and not reply_to_id:
        return error_result("not_found", f"thread not found: {effective_thread}")

    user_id = _base_author(author_user_id or get_current_user(config.repo_root))
    post = _new_post(
        config,
        post_type="reply",
        content=content,
        task_id=task_id,
        thread_id=effective_thread,
        reply_to=reply_to_id,
        references_json=references_json,
        expires_at=expires_at,
        author_user_id=user_id,
        author_agent_id=author_agent_id,
        author_agent_instance_id=author_agent_instance_id,
        runtime_node_id=runtime_node_id,
        source_node_name=source_node_name,
        workspace_id=workspace_id,
        agent_session_id=agent_session_id,
        transport_id=transport_id,
    )
    write_error = _append_post(path, post)
    if write_error is not None:
        return write_error
    return ok_result("board reply created", operation="board", action="reply", post=post)


def board_resolve(
    config: MemoryConfig,
    *,
    post_id: str,
    resolved_by: str | None = None,
) -> dict[str, Any]:
    target = str(post_id or "").strip()
    if not target:
        return error_result("invalid_input", "post_id is required")

    path = _board_path(config)
    with file_lock(config.repo_root, path):
        posts = _load_posts(path)
        project_id = _project_id(config)
        matched = None
        for item in posts:
            if str(item.get("project_id") or "") != project_id:
                continue
            if str(item.get("post_id") or "") != target:
                continue
            item["status"] = "resolved"
            item["updated_at"] = _now_text()
            item["resolved_by"] = _base_author(resolved_by or get_current_user(config.repo_root))
            matched = item
            break
        if matched is None:
            return error_result("not_found", f"post not found: {target}")
        write_error = _rewrite_posts_locked(path, posts)
        if write_error is not None:
            return write_error
    return ok_result("board post resolved", operation="board", action="resolve", post=matched)


def board_query(
    config: MemoryConfig,
    *,
    user_id: str | None = None,
    agent_instance_id: str | None = None,
    task_id: str | None = None,
    status: str | None = None,
    post_type: str | None = None,
    thread_id: str | None = None,
    filter_mode: str | None = None,
    max_items: int = 20,
) -> dict[str, Any]:
    if status is not None and str(status).strip() and str(status).strip() not in ALLOWED_STATUSES:
        return error_result("invalid_input", "status must be one of: open, resolved")
    normalized_type = str(post_type or "").strip().lower()
    if normalized_type and normalized_type not in ALLOWED_POST_TYPES:
        return error_result(
            "invalid_input",
            "post_type must be one of: handoff, note, proposal, question, reply, request, warning",
        )

    items = _load_posts(_board_path(config))
    project_id = _project_id(config)
    filtered: list[dict[str, Any]] = []
    user_filter = canonical_identity(str(user_id or "").strip()) if user_id else ""
    agent_instance_filter = str(agent_instance_id or "").strip()
    task_filter = str(task_id or "").strip()
    status_filter = str(status or "").strip()
    thread_filter = str(thread_id or "").strip()
    unresolved_only = str(filter_mode or "").strip().lower() == "unresolved"
    cap = max(1, min(200, int(max_items or 20)))

    for item in items:
        if str(item.get("project_id") or "") != project_id:
            continue
        if unresolved_only and str(item.get("status") or "") != "open":
            continue
        if user_filter and canonical_identity(str(item.get("author_user_id") or "")) != user_filter:
            continue
        if agent_instance_filter and str(item.get("author_agent_instance_id") or "") != agent_instance_filter:
            continue
        if task_filter and str(item.get("task_id") or "") != task_filter:
            continue
        if status_filter and str(item.get("status") or "") != status_filter:
            continue
        if normalized_type and str(item.get("post_type") or "") != normalized_type:
            continue
        if thread_filter and str(item.get("thread_id") or "") != thread_filter:
            continue
        filtered.append(item)

    filtered.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return ok_result(
        "board items queried",
        operation="board",
        action="query",
        filter=filter_mode or "all",
        total=len(filtered),
        items=filtered[:cap],
    )
