"""Deterministic repository operations; authorization stays outside the LLM."""

from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, delete, desc, func, or_, select
from sqlalchemy.orm import Session

from .models import AccessToken, BoardPost, BriefJob, MemoryEvent


def active_token(session: Session, token_id: str) -> AccessToken | None:
    now = datetime.now(UTC)
    return session.scalar(select(AccessToken).where(AccessToken.token_id == token_id, AccessToken.revoked_at.is_(None), (AccessToken.expires_at.is_(None) | (AccessToken.expires_at > now))))


def event_by_id(session: Session, project_id: str, event_id: UUID) -> MemoryEvent | None:
    return session.scalar(select(MemoryEvent).where(MemoryEvent.project_id == project_id, MemoryEvent.event_id == event_id))


def latest_event_seq(session: Session, project_id: str) -> int:
    return int(session.scalar(select(func.coalesce(func.max(MemoryEvent.server_seq), 0)).where(MemoryEvent.project_id == project_id)) or 0)


def mark_brief_jobs_dirty(
    session: Session,
    project_id: str,
    user_id: str,
    through_seq: int,
    *,
    user_debounce_seconds: int = 120,
    project_debounce_seconds: int = 300,
    include_user: bool = True,
    include_project: bool = True,
) -> None:
    jobs = []
    if include_user:
        jobs.append(("user_recent", user_id, user_debounce_seconds))
    if include_project:
        jobs.append(("project_recent", "", project_debounce_seconds))
    for brief_type, subject, debounce_seconds in jobs:
        key = f"{brief_type}:{project_id}:{subject or '-'}"
        not_before = datetime.now(UTC) + timedelta(seconds=debounce_seconds)
        job = session.get(BriefJob, key)
        if job is None:
            job = BriefJob(job_key=key, project_id=project_id, brief_type=brief_type, subject_user_id=subject or None, requested_through_seq=through_seq, not_before=not_before, status="pending")
            session.add(job)
        else:
            job.requested_through_seq = max(job.requested_through_seq, through_seq)
            if job.status in {"completed", "failed"}:
                was_failed = job.status == "failed"
                job.status = "pending"
                job.not_before = not_before
                if was_failed:
                    job.attempts = 0
                    job.last_error = None
            elif job.status == "pending":
                # Debounce from the latest relevant event, not the first event in a burst.
                job.not_before = max(job.not_before, not_before)
            job.updated_at = datetime.now(UTC)


def board_post_by_id(session: Session, project_id: str, post_id: UUID) -> BoardPost | None:
    return session.scalar(select(BoardPost).where(BoardPost.project_id == project_id, BoardPost.post_id == post_id))


BOARD_PAGE_MAX = 50


def _utc(value: datetime) -> datetime:
    """统一为带时区的 UTC：SQLite 读回 naive，PostgreSQL 读回 aware，游标两端必须一致。"""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def encode_board_cursor(post: BoardPost) -> str:
    """游标 = 最后一条的 (created_at, post_id)；不含任何用户或项目信息，且只能用于同一过滤条件。"""
    payload = json.dumps({"c": _utc(post.created_at).isoformat(), "p": str(post.post_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_board_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        created_at = datetime.fromisoformat(str(value["c"]))
        post_id = UUID(str(value["p"]))
    except (binascii.Error, KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("invalid board cursor") from exc
    if created_at.tzinfo is None:
        raise ValueError("invalid board cursor")
    return created_at, post_id


def list_board_posts(
    session: Session,
    *,
    project_id: str,
    user_id: str | None = None,
    agent_instance_id: str | None = None,
    task_id: str | None = None,
    status: str | None = None,
    post_type: str | None = None,
    thread_id: UUID | None = None,
    unresolved_only: bool = False,
    max_items: int = 20,
    cursor: str | None = None,
    include_expired: bool = True,
    now: datetime | None = None,
    author_agents: list[str] | None = None,
    exclude_author_agents: list[str] | None = None,
) -> list[BoardPost]:
    """返回最多 max_items 条，按 (created_at, post_id) 倒序；传 cursor 时从其后继续。

    include_expired 默认 True 以保持既有调用（回帖寻根、共享看板）行为不变；
    board/query 显式传 False，让 expires_at 真正生效。

    author_agents / exclude_author_agents 按发布通道（author_agent_id）分流，
    供共享看板把机器状态心跳与人类留言分开取数，避免心跳占用留言配额。
    """
    clauses = [BoardPost.project_id == project_id]
    if unresolved_only:
        clauses.append(BoardPost.status == "open")
    if user_id:
        clauses.append(BoardPost.author_user_id == user_id)
    if agent_instance_id:
        clauses.append(BoardPost.author_agent_instance_id == agent_instance_id)
    if task_id:
        clauses.append(BoardPost.task_id == task_id)
    if status:
        clauses.append(BoardPost.status == status)
    if post_type:
        clauses.append(BoardPost.post_type == post_type)
    if thread_id is not None:
        clauses.append(BoardPost.thread_id == thread_id)
    if not include_expired:
        clauses.append(or_(BoardPost.expires_at.is_(None), BoardPost.expires_at > (now or datetime.now(UTC))))
    if author_agents:
        clauses.append(BoardPost.author_agent_id.in_(list(author_agents)))
    if exclude_author_agents:
        # NOT IN 遇 NULL 会得到 NULL，必须显式放行无发布通道的普通留言。
        clauses.append(or_(BoardPost.author_agent_id.is_(None),
                           BoardPost.author_agent_id.notin_(list(exclude_author_agents))))
    if cursor:
        cursor_at, cursor_id = decode_board_cursor(cursor)
        clauses.append(or_(
            BoardPost.created_at < cursor_at,
            and_(BoardPost.created_at == cursor_at, BoardPost.post_id < cursor_id),
        ))

    query = (
        select(BoardPost)
        .where(and_(*clauses))
        .order_by(desc(BoardPost.created_at), desc(BoardPost.post_id))
        .limit(max(1, min(200, max_items)))
    )
    return list(session.scalars(query))


def purge_resolved_board_posts(
    session: Session,
    *,
    retention: timedelta,
    now: datetime | None = None,
    batch: int = 500,
) -> int:
    """删除已结案且超过保留期的整条线程；只要线程内仍有 open 帖就整条保留。

    - 以线程为单位判断，避免把根帖与回帖拆开，也不会误删仍在门禁依据中的 open 帖。
    - 已过期（expires_at 已到）的帖子同样只在线程内全部非 open 时才会被清理。
    - 单次最多处理 batch 条，调用方循环或定时重复即可，避免长事务。
    """
    current = now or datetime.now(UTC)
    cutoff = current - retention
    open_threads = select(BoardPost.thread_id).where(BoardPost.status == "open").distinct()
    # 线程最近一次变更（含回帖）都早于 cutoff，才视为可清理。
    stale_threads = (
        select(BoardPost.project_id, BoardPost.thread_id)
        .group_by(BoardPost.project_id, BoardPost.thread_id)
        .having(func.max(BoardPost.updated_at) < cutoff)
    ).subquery()
    victims = list(session.scalars(
        select(BoardPost.post_id)
        .join(stale_threads, and_(BoardPost.project_id == stale_threads.c.project_id,
                                  BoardPost.thread_id == stale_threads.c.thread_id))
        .where(BoardPost.thread_id.not_in(open_threads))
        .limit(max(1, batch))
    ))
    if not victims:
        return 0
    session.execute(delete(BoardPost).where(BoardPost.post_id.in_(victims)))
    session.commit()
    return len(victims)


def purge_transient_board_notes(
    session: Session,
    *,
    retention: timedelta,
    now: datetime | None = None,
    batch: int = 500,
) -> int:
    """逐条删除“已处理且已过期”的短生命周期 note，用于心跳类重复帖。

    与 ``purge_resolved_board_posts`` 按线程判断不同，这里按单帖判断：心跳线程里永远有一条
    open 的当前心跳，整线程规则永远清不掉旧心跳，所以必须允许“线程还活着、旧帖先删”。

    只处理同时满足以下条件的帖子，宁可少删：
    - ``post_type == "note"``：任务、请求、回复仍只受整线程保留期约束；
    - ``status != "open"``：处理中的帖子永不删除；
    - ``expires_at`` 已到期：只有显式声明为临时的帖子才会被回收；
    - ``updated_at`` 早于保留期：至少保留这么久，便于回溯最近状态；
    - 没有其它帖子以它的 post_id 作为 thread_id，避免删掉仍被回帖引用的根帖。
    """
    current = now or datetime.now(UTC)
    cutoff = current - retention
    candidates = list(session.scalars(
        select(BoardPost.post_id)
        .where(
            BoardPost.post_type == "note",
            BoardPost.status != "open",
            BoardPost.expires_at.is_not(None),
            BoardPost.expires_at <= current,
            BoardPost.updated_at < cutoff,
        )
        .order_by(BoardPost.updated_at)
        .limit(max(1, batch))
    ))
    if not candidates:
        return 0
    referenced = set(session.scalars(
        select(BoardPost.thread_id).where(
            BoardPost.thread_id.in_(candidates),
            BoardPost.post_id.not_in(candidates),
        )
    ))
    victims = [post_id for post_id in candidates if post_id not in referenced]
    if not victims:
        return 0
    session.execute(delete(BoardPost).where(BoardPost.post_id.in_(victims)))
    session.commit()
    return len(victims)