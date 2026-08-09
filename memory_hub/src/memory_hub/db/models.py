"""Database tables for append-only shared Memory events and derived briefs."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative metadata owned only by the independent Hub package."""


class AccessToken(Base):
    __tablename__ = "access_tokens"

    token_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    token_secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(256), nullable=False)
    project_id: Mapped[str] = mapped_column(String(256), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemoryEvent(Base):
    __tablename__ = "memory_events"
    __table_args__ = (
        UniqueConstraint("project_id", "event_id", name="uq_memory_events_project_event"),
        Index("idx_events_project_seq", "project_id", "server_seq"),
        Index("idx_events_project_time", "project_id", "occurred_at"),
        Index("idx_events_project_user_time", "project_id", "user_id", "occurred_at"),
        Index("idx_events_project_task_time", "project_id", "task_id", "occurred_at"),
        Index("idx_events_project_agent_time", "project_id", "agent_instance_id", "occurred_at"),
    )

    server_seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[UUID] = mapped_column(nullable=False)
    project_id: Mapped[str] = mapped_column(String(256), nullable=False)
    user_id: Mapped[str] = mapped_column(String(256), nullable=False)
    source_node_id: Mapped[str | None] = mapped_column(String(256))
    runtime_node_id: Mapped[str | None] = mapped_column(String(256))
    source_node_name: Mapped[str | None] = mapped_column(String(256))
    workspace_id: Mapped[str | None] = mapped_column(String(256))
    agent_session_id: Mapped[str | None] = mapped_column(String(256))
    transport_id: Mapped[str | None] = mapped_column(String(128))
    agent_id: Mapped[str | None] = mapped_column(String(256))
    agent_instance_id: Mapped[str | None] = mapped_column(String(256))
    task_id: Mapped[str | None] = mapped_column(String(256))
    task_run_id: Mapped[str | None] = mapped_column(String(256))
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    record_kind: Mapped[str | None] = mapped_column(String(128))
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    task_phase: Mapped[str | None] = mapped_column(String(64))
    content_markdown: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")
    source_record_id: Mapped[str | None] = mapped_column(String(256))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    content_redacted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")


class BriefJob(Base):
    __tablename__ = "brief_jobs"

    job_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(256), nullable=False)
    brief_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_user_id: Mapped[str | None] = mapped_column(String(256))
    requested_through_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    processed_through_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[str | None] = mapped_column(String(256))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BriefSnapshot(Base):
    __tablename__ = "brief_snapshots"

    brief_id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(String(256), nullable=False)
    brief_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_user_id: Mapped[str | None] = mapped_column(String(256))
    input_seq_from: Mapped[int | None] = mapped_column(BigInteger)
    input_seq_to: Mapped[int] = mapped_column(BigInteger, nullable=False)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    structured_brief: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    rendered_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(String(256))
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_event_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    input_fingerprint: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(64), nullable=False)


class BriefHead(Base):
    __tablename__ = "brief_heads"

    project_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    brief_type: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject_user_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    current_brief_id: Mapped[UUID] = mapped_column(nullable=False)


class BoardPost(Base):
    __tablename__ = "board_posts"
    __table_args__ = (
        Index("idx_board_posts_project_created", "project_id", "created_at"),
        Index("idx_board_posts_project_status", "project_id", "status", "created_at"),
        Index("idx_board_posts_project_thread", "project_id", "thread_id", "created_at"),
    )

    post_id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(String(256), nullable=False)
    author_user_id: Mapped[str] = mapped_column(String(256), nullable=False)
    author_agent_id: Mapped[str | None] = mapped_column(String(256))
    author_agent_instance_id: Mapped[str | None] = mapped_column(String(256))
    runtime_node_id: Mapped[str | None] = mapped_column(String(256))
    source_node_name: Mapped[str | None] = mapped_column(String(256))
    workspace_id: Mapped[str | None] = mapped_column(String(256))
    agent_session_id: Mapped[str | None] = mapped_column(String(256))
    transport_id: Mapped[str | None] = mapped_column(String(128))
    post_type: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(256))
    thread_id: Mapped[UUID] = mapped_column(nullable=False)
    reply_to: Mapped[UUID | None] = mapped_column()
    references_json: Mapped[list[object]] = mapped_column(JSONB, nullable=False, server_default="[]")
    status: Mapped[str] = mapped_column(String(64), nullable=False, server_default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GraphNode(Base):
    __tablename__ = "graph_nodes"
    __table_args__ = (
        UniqueConstraint("project_id", "node_type", "node_key", name="uq_graph_nodes_project_type_key"),
        UniqueConstraint("project_id", "id", name="uq_graph_nodes_project_id"),
        Index("idx_graph_nodes_project_type", "project_id", "node_type"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(String(256), nullable=False)
    node_type: Mapped[str] = mapped_column(String(64), nullable=False)
    node_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    name: Mapped[str] = mapped_column(String(1024), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class GraphEdge(Base):
    __tablename__ = "graph_edges"
    __table_args__ = (
        UniqueConstraint("project_id", "source_node_id", "target_node_id", "relation_type", name="uq_graph_edges_project_relation"),
        ForeignKeyConstraint(["project_id", "source_node_id"], ["graph_nodes.project_id", "graph_nodes.id"], ondelete="CASCADE", name="fk_graph_edges_source_project_node"),
        ForeignKeyConstraint(["project_id", "target_node_id"], ["graph_nodes.project_id", "graph_nodes.id"], ondelete="CASCADE", name="fk_graph_edges_target_project_node"),
        Index("idx_graph_edges_project_source", "project_id", "source_node_id"),
        Index("idx_graph_edges_project_target", "project_id", "target_node_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(String(256), nullable=False)
    source_node_id: Mapped[UUID] = mapped_column(nullable=False)
    target_node_id: Mapped[UUID] = mapped_column(nullable=False)
    relation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default="1")
    source_event_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    evidence_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class TaskEvent(Base):
    __tablename__ = "task_events"
    __table_args__ = (
        UniqueConstraint("project_id", "command_id", name="uq_task_events_project_command"),
        UniqueConstraint("project_id", "source_event_id", name="uq_task_events_project_source_event"),
        Index("idx_task_events_project_task_seq", "project_id", "task_id", "task_event_seq"),
        Index("idx_task_events_project_time", "project_id", "occurred_at"),
    )

    task_event_seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(256), nullable=False)
    source_event_id: Mapped[UUID] = mapped_column(nullable=False)
    command_id: Mapped[str] = mapped_column(String(256), nullable=False)
    task_id: Mapped[str] = mapped_column(String(256), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(256), nullable=False)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_assignment_epoch: Mapped[int | None] = mapped_column(Integer)
    task_version: Mapped[int] = mapped_column(Integer, nullable=False)
    assignment_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column("payload", JSONB, nullable=False, server_default="{}")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("idx_tasks_project_state", "project_id", "state", "updated_at"),
        Index("idx_tasks_project_updated", "project_id", "updated_at"),
    )

    project_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    title: Mapped[str] = mapped_column(String(1024), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    acceptance: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    priority: Mapped[str] = mapped_column(String(64), nullable=False, server_default="normal")
    state: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    assignment_epoch: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    current_attempt_id: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TaskAgent(Base):
    __tablename__ = "task_agents"
    __table_args__ = (Index("idx_task_agents_project_status", "project_id", "status"),)

    project_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    role: Mapped[str] = mapped_column(String(256), nullable=False, server_default="")
    capabilities_json: Mapped[list[str]] = mapped_column("capabilities", JSONB, nullable=False, server_default="[]")
    owner: Mapped[str] = mapped_column(String(256), nullable=False, server_default="")
    status: Mapped[str] = mapped_column(String(64), nullable=False, server_default="available")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TaskAttempt(Base):
    __tablename__ = "task_attempts"
    __table_args__ = (
        ForeignKeyConstraint(["project_id", "task_id"], ["tasks.project_id", "tasks.task_id"], ondelete="CASCADE", name="fk_task_attempts_task"),
        UniqueConstraint("project_id", "task_id", "epoch", name="uq_task_attempts_project_task_epoch"),
        Index("idx_task_attempts_project_assignee", "project_id", "assignee", "status"),
    )

    project_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(256), nullable=False)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    assignee: Mapped[str] = mapped_column(String(256), nullable=False)
    assigned_by: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TaskSubmission(Base):
    __tablename__ = "task_submissions"
    __table_args__ = (
        ForeignKeyConstraint(["project_id", "task_id"], ["tasks.project_id", "tasks.task_id"], ondelete="CASCADE", name="fk_task_submissions_task"),
        ForeignKeyConstraint(["project_id", "attempt_id"], ["task_attempts.project_id", "task_attempts.attempt_id"], ondelete="CASCADE", name="fk_task_submissions_attempt"),
        Index("idx_task_submissions_project_task", "project_id", "task_id", "created_at"),
    )

    project_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    submission_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(256), nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(256), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[list[str]] = mapped_column("evidence", JSONB, nullable=False, server_default="[]")
    task_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TaskReview(Base):
    __tablename__ = "task_reviews"
    __table_args__ = (
        ForeignKeyConstraint(["project_id", "task_id"], ["tasks.project_id", "tasks.task_id"], ondelete="CASCADE", name="fk_task_reviews_task"),
        ForeignKeyConstraint(["project_id", "submission_id"], ["task_submissions.project_id", "task_submissions.submission_id"], ondelete="CASCADE", name="fk_task_reviews_submission"),
        Index("idx_task_reviews_project_task", "project_id", "task_id", "created_at"),
    )

    project_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    review_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(256), nullable=False)
    submission_id: Mapped[str] = mapped_column(String(256), nullable=False)
    reviewer: Mapped[str] = mapped_column(String(256), nullable=False)
    decision: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ContextUsageDaily(Base):
    __tablename__ = "context_usage_daily"

    project_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    usage_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    returned_event_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    returned_brief_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    user_brief_requests: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    project_brief_requests: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    same_task_requests: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    other_agents_requests: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    other_tasks_requests: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    project_activity_requests: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())