"""Database tables for append-only shared Memory events and derived briefs."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, func
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


class DeviceEnrollment(Base):
    """待审批设备仅保存凭据哈希；明文始终留在申请设备。"""

    __tablename__ = "device_enrollments"
    __table_args__ = (Index("idx_enrollment_project_created", "project_id", "created_at"),)

    enrollment_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(256), nullable=False)
    device_name: Mapped[str] = mapped_column(String(96), nullable=False)
    token_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    token_secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    poll_secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    approved_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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


class BriefTokenUsageDaily(Base):
    """Per-project reservations for external Brief generation."""

    __tablename__ = "brief_token_usage_daily"

    project_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    usage_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


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
    author_auth_id: Mapped[str | None] = mapped_column(String(64))
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


class TokenDevice(Base):
    """凭据到设备身份的不可变绑定；任务执行者身份只从这里推导。"""

    __tablename__ = "token_devices"
    __table_args__ = (Index("idx_token_devices_project_device", "project_id", "device_name"),)

    token_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(256), nullable=False)
    device_name: Mapped[str] = mapped_column(String(96), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    bound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TaskEvent(Base):
    __tablename__ = "task_events"
    __table_args__ = (
        UniqueConstraint("project_id", "command_id", name="uq_task_events_project_command"),
        UniqueConstraint("project_id", "task_id", "task_seq", name="uq_task_events_project_task_seq"),
        Index("idx_task_events_project_task_seq", "project_id", "task_id", "task_event_seq"),
        Index("idx_task_events_project_time", "project_id", "occurred_at"),
    )

    task_event_seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(256), nullable=False)
    task_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    command_id: Mapped[str] = mapped_column(String(256), nullable=False)
    task_id: Mapped[str] = mapped_column(String(256), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(256), nullable=False)
    agent: Mapped[str | None] = mapped_column(String(64))
    expected_version: Mapped[int | None] = mapped_column(Integer)
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
    last_task_seq: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskCommand(Base):
    """已接受任务命令的幂等存档；被拒绝的命令不写入，因此可修正后复用 command_id。"""

    __tablename__ = "task_commands"
    __table_args__ = (Index("idx_task_commands_project_task", "project_id", "task_id"),)

    project_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    command_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    actor: Mapped[str] = mapped_column(String(256), nullable=False)
    task_id: Mapped[str] = mapped_column(String(256), nullable=False)
    result_json: Mapped[dict[str, Any]] = mapped_column("result", JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TaskLink(Base):
    """创建时写入且不可修改的任务关系；不修改被引用任务。"""

    __tablename__ = "task_links"
    __table_args__ = (
        ForeignKeyConstraint(["project_id", "task_id"], ["tasks.project_id", "tasks.task_id"], ondelete="CASCADE", name="fk_task_links_task"),
        Index("idx_task_links_project_target", "project_id", "relation", "target"),
    )

    project_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    relation: Mapped[str] = mapped_column(String(32), primary_key=True)
    target: Mapped[str] = mapped_column(String(1024), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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


class DeviceStatus(Base):
    """设备状态独立通道：每个(项目, 通道, 发布凭据, 设备名)只保留一行并覆盖更新。

    状态是 advisory 可见性信息，不表示设备在线，也不承载任务生命周期；它不进入 Board。
    """

    __tablename__ = "device_status"
    __table_args__ = (
        Index("idx_device_status_project_channel_device", "project_id", "channel", "device_name"),
        Index("idx_device_status_project_updated", "project_id", "updated_at"),
    )

    project_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    channel: Mapped[str] = mapped_column(String(64), primary_key=True)
    author_auth_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    device_name: Mapped[str] = mapped_column(String(96), primary_key=True)
    author_user_id: Mapped[str] = mapped_column(String(256), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)