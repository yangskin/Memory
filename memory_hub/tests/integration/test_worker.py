from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from memory_hub.config import load_settings
from memory_hub.db.models import BriefHead, BriefJob, BriefSnapshot, MemoryEvent
from memory_hub.db.repositories import mark_brief_jobs_dirty
from memory_hub.db.session import create_session_factory
from memory_hub.llm.fake import FakeBriefProvider
from memory_hub.worker.runner import run_once


pytestmark = pytest.mark.skipif(not os.getenv("MEMORY_HUB_DATABASE_URL"), reason="requires PostgreSQL")


def test_worker_creates_project_brief_snapshot_and_head() -> None:
    factory = create_session_factory(load_settings().database_url or "")
    project_id = f"worker-project-{uuid4().hex}"
    event_id = uuid4()
    with factory() as session:
        event = MemoryEvent(event_id=event_id, project_id=project_id, user_id="worker-user", agent_id="pytest", agent_instance_id="pytest-1", operation="record", scope="project_shared", content_markdown="validated implementation", metadata_json={}, occurred_at=datetime.now(UTC), content_hash="sha256:" + "b" * 64)
        session.add(event)
        session.flush()
        session.add(BriefJob(job_key=f"project_recent:{project_id}:-", project_id=project_id, brief_type="project_recent", subject_user_id=None, requested_through_seq=event.server_seq, not_before=datetime.now(UTC) - timedelta(seconds=1), status="pending"))
        session.commit()
        assert run_once(session, worker_id="integration-worker", max_jobs=1000) >= 1
        head = session.get(BriefHead, (project_id, "project_recent", ""))
        assert head is not None
        snapshot = session.get(BriefSnapshot, head.current_brief_id)
        assert snapshot is not None
        assert str(event_id) in snapshot.source_event_ids


def test_worker_falls_back_to_latest_shared_events_when_project_window_is_empty() -> None:
    factory = create_session_factory(load_settings().database_url or "")
    project_id = f"worker-project-fallback-{uuid4().hex}"
    shared_event_id = uuid4()
    personal_event_id = uuid4()
    with factory() as session:
        occurred_at = datetime.now(UTC) - timedelta(hours=25)
        shared_event = MemoryEvent(event_id=shared_event_id, project_id=project_id, user_id="worker-user", agent_id="pytest", agent_instance_id="pytest-1", operation="record", scope="project_shared", content_markdown="recent shared fallback", metadata_json={}, occurred_at=occurred_at, content_hash="sha256:" + "9" * 64)
        personal_event = MemoryEvent(event_id=personal_event_id, project_id=project_id, user_id="worker-user", agent_id="pytest", agent_instance_id="pytest-1", operation="record", scope="personal", content_markdown="private event", metadata_json={}, occurred_at=occurred_at, content_hash="sha256:" + "a" * 64)
        session.add_all([shared_event, personal_event])
        session.flush()
        session.add(BriefJob(job_key=f"project_recent:{project_id}:-", project_id=project_id, brief_type="project_recent", subject_user_id=None, requested_through_seq=personal_event.server_seq, not_before=datetime.now(UTC) - timedelta(seconds=1), status="pending"))
        session.commit()
        assert run_once(session, worker_id="fallback-worker", max_jobs=1000) >= 1
        head = session.get(BriefHead, (project_id, "project_recent", ""))
        assert head is not None
        snapshot = session.get(BriefSnapshot, head.current_brief_id)
        assert snapshot is not None
        assert snapshot.source_event_ids == [str(shared_event_id)]


def test_worker_ignores_empty_shared_checkpoints_when_rebuilding_project_brief() -> None:
    factory = create_session_factory(load_settings().database_url or "")
    project_id = f"worker-checkpoint-filter-{uuid4().hex}"
    shared_event_id = uuid4()
    with factory() as session:
        old_shared_event = MemoryEvent(event_id=shared_event_id, project_id=project_id, user_id="worker-user", agent_id="pytest", agent_instance_id="pytest-1", operation="record", scope="project_shared", content_markdown="retained project memory", metadata_json={}, occurred_at=datetime.now(UTC) - timedelta(hours=25), content_hash="sha256:" + "b" * 64)
        checkpoint = MemoryEvent(event_id=uuid4(), project_id=project_id, user_id="worker-user", agent_id="pytest", agent_instance_id="pytest-1", operation="checkpoint", scope="project_shared", content_markdown="", metadata_json={}, occurred_at=datetime.now(UTC), content_hash="sha256:" + "c" * 64)
        session.add_all([old_shared_event, checkpoint])
        session.flush()
        session.add(BriefJob(job_key=f"project_recent:{project_id}:-", project_id=project_id, brief_type="project_recent", subject_user_id=None, requested_through_seq=checkpoint.server_seq, not_before=datetime.now(UTC) - timedelta(seconds=1), status="pending"))
        session.commit()

        assert run_once(session, worker_id="checkpoint-filter-worker", max_jobs=1000) >= 1
        head = session.get(BriefHead, (project_id, "project_recent", ""))
        assert head is not None
        snapshot = session.get(BriefSnapshot, head.current_brief_id)
        assert snapshot is not None
        assert snapshot.source_event_ids == [str(shared_event_id)]


def test_worker_keeps_dirty_job_when_event_arrives_during_generation() -> None:
    factory = create_session_factory(load_settings().database_url or "")
    project_id = f"worker-race-{uuid4().hex}"
    with factory() as session:
        first = MemoryEvent(event_id=uuid4(), project_id=project_id, user_id="worker-user", agent_id="pytest", agent_instance_id="pytest-1", operation="record", scope="personal", content_markdown="first", metadata_json={}, occurred_at=datetime.now(UTC), content_hash="sha256:" + "c" * 64)
        session.add(first)
        session.flush()
        session.add(BriefJob(job_key=f"user_recent:{project_id}:worker-user", project_id=project_id, brief_type="user_recent", subject_user_id="worker-user", requested_through_seq=first.server_seq, not_before=datetime.now(UTC) - timedelta(seconds=1), status="pending"))
        session.commit()

    class InjectingProvider(FakeBriefProvider):
        def generate_user_brief(self, request):
            with factory() as other_session:
                second = MemoryEvent(event_id=uuid4(), project_id=project_id, user_id="worker-user", agent_id="pytest", agent_instance_id="pytest-2", operation="record", scope="personal", content_markdown="second", metadata_json={}, occurred_at=datetime.now(UTC), content_hash="sha256:" + "d" * 64)
                other_session.add(second)
                other_session.flush()
                mark_brief_jobs_dirty(other_session, project_id, "worker-user", second.server_seq, user_debounce_seconds=1, project_debounce_seconds=1)
                other_session.commit()
            return super().generate_user_brief(request)

    with factory() as session:
        assert run_once(session, InjectingProvider(), worker_id="race-worker") >= 1
        job = session.get(BriefJob, f"user_recent:{project_id}:worker-user")
        assert job is not None
        assert job.status == "pending"
        assert job.requested_through_seq > job.processed_through_seq


def test_worker_releases_database_transaction_before_generation() -> None:
    factory = create_session_factory(load_settings().database_url or "")
    project_id = f"worker-transaction-{uuid4().hex}"
    with factory() as session:
        event = MemoryEvent(event_id=uuid4(), project_id=project_id, user_id="transaction-user", agent_id="pytest", agent_instance_id="pytest", operation="record", scope="personal", content_markdown="event", metadata_json={}, occurred_at=datetime.now(UTC), content_hash="sha256:" + "7" * 64)
        session.add(event)
        session.flush()
        session.add(BriefJob(job_key=f"user_recent:{project_id}:transaction-user", project_id=project_id, brief_type="user_recent", subject_user_id="transaction-user", requested_through_seq=event.server_seq, not_before=datetime.now(UTC) - timedelta(seconds=1), status="pending"))
        session.commit()

        class TransactionCheckingProvider(FakeBriefProvider):
            def generate_user_brief(self, request):
                assert not session.in_transaction()
                return super().generate_user_brief(request)

        assert run_once(session, TransactionCheckingProvider(), worker_id="transaction-worker") >= 1


def test_worker_bounds_large_briefs_to_the_latest_window_events() -> None:
    factory = create_session_factory(load_settings().database_url or "")
    project_id = f"worker-batch-{uuid4().hex}"
    with factory() as session:
        for index in range(501):
            session.add(MemoryEvent(event_id=uuid4(), project_id=project_id, user_id="batch-user", agent_id="pytest", agent_instance_id="pytest", operation="record", scope="personal", content_markdown=f"event-{index}", metadata_json={}, occurred_at=datetime.now(UTC), content_hash="sha256:" + f"{index:064x}"))
        session.flush()
        last_seq = session.query(MemoryEvent.server_seq).filter_by(project_id=project_id).order_by(MemoryEvent.server_seq.desc()).first()[0]
        session.add(BriefJob(job_key=f"user_recent:{project_id}:batch-user", project_id=project_id, brief_type="user_recent", subject_user_id="batch-user", requested_through_seq=last_seq, not_before=datetime.now(UTC) - timedelta(seconds=1), status="pending"))
        session.commit()
        run_once(session, worker_id="batch-worker")
        job = session.get(BriefJob, f"user_recent:{project_id}:batch-user")
        assert job is not None
        assert job.status == "completed"
        assert job.processed_through_seq == job.requested_through_seq
        head = session.get(BriefHead, (project_id, "user_recent", "batch-user"))
        assert head is not None
        snapshot = session.get(BriefSnapshot, head.current_brief_id)
        assert snapshot is not None
        assert len(snapshot.source_event_ids) == 500


def test_worker_rebuilds_brief_from_full_recent_window_after_incremental_write() -> None:
    factory = create_session_factory(load_settings().database_url or "")
    project_id = f"worker-window-{uuid4().hex}"
    with factory() as session:
        first = MemoryEvent(event_id=uuid4(), project_id=project_id, user_id="window-user", agent_id="pytest", agent_instance_id="pytest", operation="record", scope="personal", content_markdown="first", metadata_json={}, occurred_at=datetime.now(UTC), content_hash="sha256:" + "1" * 64)
        session.add(first)
        session.flush()
        session.add(BriefJob(job_key=f"user_recent:{project_id}:window-user", project_id=project_id, brief_type="user_recent", subject_user_id="window-user", requested_through_seq=first.server_seq, not_before=datetime.now(UTC) - timedelta(seconds=1), status="pending"))
        session.commit()
        run_once(session, worker_id="window-worker")
        second = MemoryEvent(event_id=uuid4(), project_id=project_id, user_id="window-user", agent_id="pytest", agent_instance_id="pytest", operation="record", scope="personal", content_markdown="second", metadata_json={}, occurred_at=datetime.now(UTC), content_hash="sha256:" + "2" * 64)
        session.add(second)
        session.flush()
        mark_brief_jobs_dirty(session, project_id, "window-user", second.server_seq, user_debounce_seconds=1, project_debounce_seconds=1)
        session.execute(__import__("sqlalchemy").update(BriefJob).where(BriefJob.job_key == f"user_recent:{project_id}:window-user").values(not_before=datetime.now(UTC) - timedelta(seconds=1)))
        session.commit()
        run_once(session, worker_id="window-worker")
        head = session.get(BriefHead, (project_id, "user_recent", "window-user"))
        assert head is not None
        snapshot = session.get(BriefSnapshot, head.current_brief_id)
        assert snapshot is not None
        assert set(snapshot.source_event_ids) == {str(first.event_id), str(second.event_id)}


def test_worker_failure_keeps_existing_brief_head() -> None:
    factory = create_session_factory(load_settings().database_url or "")
    project_id = f"worker-failure-{uuid4().hex}"
    old_brief_id = uuid4()
    with factory() as session:
        event = MemoryEvent(event_id=uuid4(), project_id=project_id, user_id="failure-user", agent_id="pytest", agent_instance_id="pytest", operation="record", scope="personal", content_markdown="event", metadata_json={}, occurred_at=datetime.now(UTC), content_hash="sha256:" + "e" * 64)
        session.add(event)
        session.flush()
        session.add(BriefSnapshot(brief_id=old_brief_id, project_id=project_id, brief_type="user_recent", subject_user_id="failure-user", input_seq_to=0, structured_brief={}, rendered_markdown="old", prompt_version="v1", generated_at=datetime.now(UTC), source_event_ids=[], status="completed"))
        session.add(BriefHead(project_id=project_id, brief_type="user_recent", subject_user_id="failure-user", current_brief_id=old_brief_id))
        session.add(BriefJob(job_key=f"user_recent:{project_id}:failure-user", project_id=project_id, brief_type="user_recent", subject_user_id="failure-user", requested_through_seq=event.server_seq, not_before=datetime.now(UTC) - timedelta(seconds=1), status="pending"))
        session.commit()

        class FailingProvider(FakeBriefProvider):
            def generate_user_brief(self, request):
                raise TimeoutError("provider timeout")

        run_once(session, FailingProvider(), worker_id="failure-worker")
        assert session.get(BriefHead, (project_id, "user_recent", "failure-user")).current_brief_id == old_brief_id
        assert session.get(BriefJob, f"user_recent:{project_id}:failure-user").status == "pending"


def test_worker_rebases_old_brief_from_raw_events() -> None:
    factory = create_session_factory(load_settings().database_url or "")
    project_id = f"worker-rebase-{uuid4().hex}"
    old_brief_id = uuid4()
    with factory() as session:
        event = MemoryEvent(event_id=uuid4(), project_id=project_id, user_id="rebase-user", agent_id="pytest", agent_instance_id="pytest", operation="record", scope="project_shared", content_markdown="rebase input", metadata_json={}, occurred_at=datetime.now(UTC), content_hash="sha256:" + "f" * 64)
        session.add(event)
        session.flush()
        session.add(BriefSnapshot(brief_id=old_brief_id, project_id=project_id, brief_type="project_recent", subject_user_id="", input_seq_to=event.server_seq, structured_brief={}, rendered_markdown="old", prompt_version="v1", generated_at=datetime.now(UTC) - timedelta(hours=2), source_event_ids=[], status="completed"))
        session.add(BriefHead(project_id=project_id, brief_type="project_recent", subject_user_id="", current_brief_id=old_brief_id))
        session.add(BriefJob(job_key=f"project_recent:{project_id}:-", project_id=project_id, brief_type="project_recent", subject_user_id=None, requested_through_seq=event.server_seq, processed_through_seq=event.server_seq, not_before=datetime.now(UTC), status="completed"))
        session.commit()
        run_once(session, worker_id="rebase-worker", rebase_interval_seconds=1, max_jobs=1000)
        head = session.get(BriefHead, (project_id, "project_recent", ""))
        assert head is not None
        assert head.current_brief_id != old_brief_id
        assert str(event.event_id) in session.get(BriefSnapshot, head.current_brief_id).source_event_ids


def test_worker_skips_rebase_when_recent_window_input_is_unchanged() -> None:
    factory = create_session_factory(load_settings().database_url or "")
    project_id = f"worker-unchanged-{uuid4().hex}"
    brief_id = uuid4()
    event_id = uuid4()
    with factory() as session:
        event = MemoryEvent(event_id=event_id, project_id=project_id, user_id="unchanged-user", agent_id="pytest", agent_instance_id="pytest", operation="record", scope="project_shared", content_markdown="unchanged input", metadata_json={}, occurred_at=datetime.now(UTC), content_hash="sha256:" + "8" * 64)
        session.add(event)
        session.flush()
        from memory_hub.worker.runner import _input_fingerprint, _visible_event
        fingerprint = _input_fingerprint("project_recent", [_visible_event(event, "project_recent")])
        session.add(BriefSnapshot(brief_id=brief_id, project_id=project_id, brief_type="project_recent", subject_user_id="", input_seq_to=event.server_seq, structured_brief={}, rendered_markdown="existing", prompt_version="v1", generated_at=datetime.now(UTC) - timedelta(hours=2), source_event_ids=[str(event_id)], input_fingerprint=fingerprint, status="completed"))
        session.add(BriefHead(project_id=project_id, brief_type="project_recent", subject_user_id="", current_brief_id=brief_id))
        session.add(BriefJob(job_key=f"project_recent:{project_id}:-", project_id=project_id, brief_type="project_recent", subject_user_id=None, requested_through_seq=event.server_seq, processed_through_seq=event.server_seq, not_before=datetime.now(UTC), status="completed", updated_at=datetime.now(UTC) - timedelta(hours=2)))
        session.commit()

        class UnexpectedProvider(FakeBriefProvider):
            def generate_project_brief(self, request):
                raise AssertionError("unchanged input must not call the provider")

        assert run_once(session, UnexpectedProvider(), worker_id="unchanged-worker", rebase_interval_seconds=1, max_jobs=1000) >= 1
        assert session.get(BriefHead, (project_id, "project_recent", "")).current_brief_id == brief_id
        assert session.query(BriefSnapshot).filter_by(project_id=project_id).count() == 1