"""One deterministic brief-worker pass; API threads never call this code."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from memory_hub.db.models import BriefHead, BriefJob, BriefSnapshot, MemoryEvent
from memory_hub.llm.base import ProjectBriefRequest, UserBriefRequest
from memory_hub.llm.fake import FakeBriefProvider


def _render(structured: dict[str, object]) -> str:
    return str(structured.get("summary") or "No recent reports.")


def _visible_event(event: MemoryEvent, job: BriefJob) -> dict[str, object]:
    body = event.content_markdown
    if job.brief_type == "project_recent" and event.user_id != job.subject_user_id and event.scope not in {"shared", "project_shared", "org_shared"}:
        body = None
    return {"event_id": str(event.event_id), "content_markdown": body, "scope": event.scope, "user_id": event.user_id, "task_id": event.task_id, "agent_instance_id": event.agent_instance_id, "occurred_at": event.occurred_at.isoformat()}


def run_once(session: Session, provider: FakeBriefProvider | None = None) -> int:
    provider = provider or FakeBriefProvider()
    jobs = list(session.scalars(select(BriefJob).where(BriefJob.status == "pending").limit(10)))
    for job in jobs:
        records = list(session.scalars(select(MemoryEvent).where(MemoryEvent.project_id == job.project_id).order_by(MemoryEvent.server_seq).limit(500)))
        if job.brief_type == "user_recent" and job.subject_user_id:
            records = [event for event in records if event.user_id == job.subject_user_id or event.scope in {"shared", "project_shared", "org_shared"}]
            structured = provider.generate_user_brief(UserBriefRequest(project_id=job.project_id, user_id=job.subject_user_id, events=[_visible_event(event, job) for event in records])).structured_brief
            subject = job.subject_user_id
        else:
            structured = provider.generate_project_brief(ProjectBriefRequest(project_id=job.project_id, events=[_visible_event(event, job) for event in records])).structured_brief
            subject = ""
        source_ids = structured.get("source_event_ids")
        if not isinstance(source_ids, list):
            job.status, job.last_error = "failed", "brief missing source_event_ids"
            continue
        brief_id = uuid4()
        snapshot = BriefSnapshot(brief_id=brief_id, project_id=job.project_id, brief_type=job.brief_type, subject_user_id=subject, input_seq_from=job.processed_through_seq or None, input_seq_to=job.requested_through_seq, structured_brief=structured, rendered_markdown=_render(structured), model="fake", prompt_version="v1", generated_at=datetime.now(UTC), source_event_ids=source_ids, status="completed")
        session.add(snapshot)
        session.flush()
        session.execute(delete(BriefHead).where(BriefHead.project_id == job.project_id, BriefHead.brief_type == job.brief_type, BriefHead.subject_user_id == subject))
        session.add(BriefHead(project_id=job.project_id, brief_type=job.brief_type, subject_user_id=subject, current_brief_id=brief_id))
        job.processed_through_seq = job.requested_through_seq
        job.status = "completed"
    session.commit()
    return len(jobs)