"""Brief-worker passes; API threads never call this code."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from memory_hub.db.models import BriefHead, BriefJob, BriefSnapshot, MemoryEvent
from memory_hub.llm.base import BriefProvider, ProjectBriefDocument, ProjectBriefRequest, UserBriefDocument, UserBriefRequest
from memory_hub.llm.fake import FakeBriefProvider


def _contentful_event_clauses() -> tuple[object, object]:
    return (
        MemoryEvent.content_markdown.is_not(None),
        func.btrim(MemoryEvent.content_markdown) != "",
    )


def _render(structured: dict[str, object]) -> str:
    return str(structured.get("summary") or "No recent reports.")


def _visible_event(event: MemoryEvent, brief_type: str) -> dict[str, object]:
    body = event.content_markdown
    if brief_type == "project_recent" and event.scope not in {"shared", "project_shared", "org_shared"}:
        body = None
    return {"event_id": str(event.event_id), "content_markdown": body, "scope": event.scope, "user_id": event.user_id, "task_id": event.task_id, "agent_instance_id": event.agent_instance_id, "occurred_at": event.occurred_at.isoformat()}


def _input_fingerprint(brief_type: str, event_payloads: list[dict[str, object]]) -> str:
    payload = {"strategy": "recent-v1", "brief_type": brief_type, "events": event_payloads}
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _claim_jobs(session: Session, *, max_jobs: int, worker_id: str, lease_seconds: int) -> list[BriefJob]:
    now = datetime.now(UTC)
    query = (
        select(BriefJob)
        .where(
            BriefJob.not_before <= now,
            or_(
                BriefJob.status == "pending",
                (BriefJob.status == "running") & (BriefJob.lease_until < now),
            ),
        )
        .order_by(BriefJob.not_before)
        .limit(max_jobs)
        .with_for_update(skip_locked=True)
    )
    jobs = list(session.scalars(query))
    for job in jobs:
        job.status = "running"
        job.worker_id = worker_id
        job.lease_until = now + timedelta(seconds=lease_seconds)
        job.attempts += 1
    session.commit()
    return jobs


def _validate_structured(job: BriefJob, structured: dict[str, object], source_ids: set[str]) -> dict[str, object]:
    model = UserBriefDocument if job.brief_type == "user_recent" else ProjectBriefDocument
    validated = model.model_validate(structured).model_dump()
    output_ids = set(validated["source_event_ids"])
    if not output_ids.issubset(source_ids):
        raise ValueError("brief references an event outside its input")
    if source_ids and not output_ids:
        raise ValueError("brief missing source_event_ids")
    for section in ("workstreams", "cross_agent_overlaps", "stale_workstreams", "cross_cutting_changes", "possible_overlaps", "project_blockers", "build_and_test_status", "recent_decisions"):
        conclusions = validated.get(section)
        if not isinstance(conclusions, list):
            continue
        for conclusion in conclusions:
            if not isinstance(conclusion, dict):
                raise ValueError(f"brief {section} contains an unstructured conclusion")
            conclusion_ids = conclusion.get("source_event_ids")
            if not isinstance(conclusion_ids, list) or not conclusion_ids:
                raise ValueError(f"brief {section} conclusion missing source_event_ids")
            if not set(str(item) for item in conclusion_ids).issubset(source_ids):
                raise ValueError(f"brief {section} references an event outside its input")
    return validated


def _retry(job: BriefJob, error: Exception) -> None:
    delay_seconds = min(300, 2 ** min(job.attempts, 8))
    job.status = "pending"
    job.not_before = datetime.now(UTC) + timedelta(seconds=delay_seconds)
    job.lease_until = None
    job.last_error = type(error).__name__


def _schedule_rebases(session: Session, rebase_interval_seconds: int) -> None:
    cutoff = datetime.now(UTC) - timedelta(seconds=rebase_interval_seconds)
    heads = list(session.execute(select(BriefHead, BriefSnapshot).join(BriefSnapshot, BriefSnapshot.brief_id == BriefHead.current_brief_id).where(BriefSnapshot.generated_at <= cutoff)))
    for head, snapshot in heads:
        latest = int(session.scalar(select(func.coalesce(func.max(MemoryEvent.server_seq), 0)).where(MemoryEvent.project_id == head.project_id)) or 0)
        subject = head.subject_user_id or ""
        key = f"{head.brief_type}:{head.project_id}:{subject or '-'}"
        job = session.get(BriefJob, key)
        if job is not None and job.last_checked_at is not None and job.last_checked_at >= cutoff:
            continue
        if job is None:
            session.add(BriefJob(job_key=key, project_id=head.project_id, brief_type=head.brief_type, subject_user_id=subject or None, requested_through_seq=latest, not_before=datetime.now(UTC), status="pending"))
        elif job.status != "running":
            job.requested_through_seq = max(job.requested_through_seq, latest)
            job.processed_through_seq = 0
            job.status = "pending"
            job.not_before = min(job.not_before, datetime.now(UTC))
    session.commit()


def run_once(session: Session, provider: BriefProvider | None = None, *, worker_id: str = "worker", max_jobs: int = 10, lease_seconds: int = 60, model_name: str = "fake", rebase_interval_seconds: int = 3600) -> int:
    provider = provider or FakeBriefProvider()
    _schedule_rebases(session, rebase_interval_seconds)
    jobs = _claim_jobs(session, max_jobs=max_jobs, worker_id=worker_id, lease_seconds=lease_seconds)
    for job in jobs:
        job_key = job.job_key
        claimed_through_seq = job.requested_through_seq
        brief_type = job.brief_type
        project_id = job.project_id
        subject_user_id = job.subject_user_id
        try:
            window_start = datetime.now(UTC) - timedelta(hours=24)
            visibility = (MemoryEvent.scope.in_({"shared", "project_shared", "org_shared"}),)
            if brief_type == "user_recent" and subject_user_id:
                visibility = (or_(MemoryEvent.user_id == subject_user_id, MemoryEvent.scope.in_({"shared", "project_shared", "org_shared"})),)
            records = list(session.scalars(select(MemoryEvent).where(MemoryEvent.project_id == project_id, MemoryEvent.occurred_at >= window_start, MemoryEvent.server_seq <= claimed_through_seq, *visibility, *_contentful_event_clauses()).order_by(MemoryEvent.occurred_at.desc(), MemoryEvent.server_seq.desc()).limit(500)))
            if brief_type == "project_recent" and not records:
                records = list(session.scalars(select(MemoryEvent).where(MemoryEvent.project_id == project_id, MemoryEvent.server_seq <= claimed_through_seq, *visibility, *_contentful_event_clauses()).order_by(MemoryEvent.occurred_at.desc(), MemoryEvent.server_seq.desc()).limit(500)))
            records.sort(key=lambda event: event.server_seq)
            event_payloads = [_visible_event(event, brief_type) for event in records]
            source_ids = {str(event.event_id) for event in records}
            input_fingerprint = _input_fingerprint(brief_type, event_payloads)
            input_seq_from = min((event.server_seq for event in records), default=None)
            head = session.get(BriefHead, (project_id, brief_type, subject_user_id or ""))
            current_snapshot = session.get(BriefSnapshot, head.current_brief_id) if head is not None else None
            if current_snapshot is not None and current_snapshot.input_fingerprint == input_fingerprint:
                job.processed_through_seq = max(job.processed_through_seq, claimed_through_seq)
                job.status = "completed"
                job.lease_until = None
                job.last_error = None
                job.last_checked_at = datetime.now(UTC)
                job.updated_at = datetime.now(UTC)
                session.commit()
                continue
            session.rollback()
            if brief_type == "user_recent" and subject_user_id:
                structured = provider.generate_user_brief(UserBriefRequest(project_id=project_id, user_id=subject_user_id, events=event_payloads)).structured_brief
                subject = subject_user_id
            else:
                structured = provider.generate_project_brief(ProjectBriefRequest(project_id=project_id, events=event_payloads)).structured_brief
                subject = ""
            validated = _validate_structured(job, structured, source_ids)
            covered_through_seq = claimed_through_seq
            brief_id = uuid4()
            snapshot = BriefSnapshot(brief_id=brief_id, project_id=project_id, brief_type=brief_type, subject_user_id=subject, input_seq_from=input_seq_from, input_seq_to=covered_through_seq, window_start=window_start, window_end=datetime.now(UTC), structured_brief=validated, rendered_markdown=_render(validated), model=model_name, prompt_version="v1", generated_at=datetime.now(UTC), source_event_ids=validated["source_event_ids"], input_fingerprint=input_fingerprint, status="completed")
            session.add(snapshot)
            session.flush()
            live_job = session.scalar(
                select(BriefJob)
                .where(BriefJob.job_key == job_key)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if live_job is None:
                raise RuntimeError("brief job disappeared")
            session.execute(insert(BriefHead).values(project_id=job.project_id, brief_type=job.brief_type, subject_user_id=subject, current_brief_id=brief_id).on_conflict_do_update(index_elements=["project_id", "brief_type", "subject_user_id"], set_={"current_brief_id": brief_id}))
            live_job.processed_through_seq = max(live_job.processed_through_seq, covered_through_seq)
            if live_job.requested_through_seq > claimed_through_seq or live_job.status != "running" or live_job.worker_id != worker_id:
                live_job.status = "pending"
                live_job.not_before = datetime.now(UTC)
            else:
                live_job.status = "completed"
            live_job.lease_until = None
            live_job.last_error = None
            live_job.last_checked_at = datetime.now(UTC)
            live_job.updated_at = datetime.now(UTC)
            session.commit()
        except Exception as exc:
            session.rollback()
            live_job = session.get(BriefJob, job_key)
            if live_job is not None:
                _retry(live_job, exc)
                session.commit()
    return len(jobs)