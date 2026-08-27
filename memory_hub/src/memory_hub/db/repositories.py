"""Deterministic repository operations; authorization stays outside the LLM."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, desc, func, select
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
) -> list[BoardPost]:
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

    query = (
        select(BoardPost)
        .where(and_(*clauses))
        .order_by(desc(BoardPost.created_at))
        .limit(max(1, min(200, max_items)))
    )
    return list(session.scalars(query))