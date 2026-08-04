"""Deterministic repository operations; authorization stays outside the LLM."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import AccessToken, BriefJob, MemoryEvent


def active_token(session: Session, token_id: str) -> AccessToken | None:
    now = datetime.now(UTC)
    return session.scalar(select(AccessToken).where(AccessToken.token_id == token_id, AccessToken.revoked_at.is_(None), (AccessToken.expires_at.is_(None) | (AccessToken.expires_at > now))))


def event_by_id(session: Session, project_id: str, event_id: UUID) -> MemoryEvent | None:
    return session.scalar(select(MemoryEvent).where(MemoryEvent.project_id == project_id, MemoryEvent.event_id == event_id))


def latest_event_seq(session: Session, project_id: str) -> int:
    return int(session.scalar(select(func.coalesce(func.max(MemoryEvent.server_seq), 0)).where(MemoryEvent.project_id == project_id)) or 0)


def mark_brief_jobs_dirty(session: Session, project_id: str, user_id: str, through_seq: int) -> None:
    for brief_type, subject in (("user_recent", user_id), ("project_recent", None)):
        key = f"{brief_type}:{project_id}:{subject or '-'}"
        job = session.get(BriefJob, key)
        if job is None:
            job = BriefJob(job_key=key, project_id=project_id, brief_type=brief_type, subject_user_id=subject, requested_through_seq=through_seq, not_before=datetime.now(UTC), status="pending")
            session.add(job)
        else:
            job.requested_through_seq = max(job.requested_through_seq, through_seq)
            job.status = "pending"
            job.updated_at = datetime.now(UTC)