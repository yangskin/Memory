"""Create Task Graph event and projection tables.

Revision ID: 0010_task_graph
Revises: 0010_context_usage_updated_at
Create Date: 2026-08-09
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "0010_task_graph"
down_revision = "0010_context_usage_updated_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_events",
        sa.Column("task_event_seq", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.String(length=256), nullable=False),
        sa.Column("source_event_id", UUID(as_uuid=True), nullable=False),
        sa.Column("command_id", sa.String(length=256), nullable=False),
        sa.Column("task_id", sa.String(length=256), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.String(length=256), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("expected_assignment_epoch", sa.Integer(), nullable=True),
        sa.Column("task_version", sa.Integer(), nullable=False),
        sa.Column("assignment_epoch", sa.Integer(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False, server_default="{}"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("project_id", "command_id", name="uq_task_events_project_command"),
        sa.UniqueConstraint("project_id", "source_event_id", name="uq_task_events_project_source_event"),
    )
    op.create_index("idx_task_events_project_task_seq", "task_events", ["project_id", "task_id", "task_event_seq"])
    op.create_index("idx_task_events_project_time", "task_events", ["project_id", "occurred_at"])
    op.create_table(
        "tasks",
        sa.Column("project_id", sa.String(length=256), primary_key=True),
        sa.Column("task_id", sa.String(length=256), primary_key=True),
        sa.Column("title", sa.String(length=1024), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False, server_default=""),
        sa.Column("acceptance", sa.Text(), nullable=False, server_default=""),
        sa.Column("priority", sa.String(length=64), nullable=False, server_default="normal"),
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("assignment_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("current_attempt_id", sa.String(length=256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_tasks_project_state", "tasks", ["project_id", "state", "updated_at"])
    op.create_index("idx_tasks_project_updated", "tasks", ["project_id", "updated_at"])
    op.create_table(
        "task_agents",
        sa.Column("project_id", sa.String(length=256), primary_key=True),
        sa.Column("agent_id", sa.String(length=256), primary_key=True),
        sa.Column("role", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("capabilities", JSONB(), nullable=False, server_default="[]"),
        sa.Column("owner", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=64), nullable=False, server_default="available"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_task_agents_project_status", "task_agents", ["project_id", "status"])
    op.create_table(
        "task_attempts",
        sa.Column("project_id", sa.String(length=256), primary_key=True),
        sa.Column("attempt_id", sa.String(length=256), primary_key=True),
        sa.Column("task_id", sa.String(length=256), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("assignee", sa.String(length=256), nullable=False),
        sa.Column("assigned_by", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id", "task_id"], ["tasks.project_id", "tasks.task_id"], ondelete="CASCADE", name="fk_task_attempts_task"),
        sa.UniqueConstraint("project_id", "task_id", "epoch", name="uq_task_attempts_project_task_epoch"),
    )
    op.create_index("idx_task_attempts_project_assignee", "task_attempts", ["project_id", "assignee", "status"])
    op.create_table(
        "task_submissions",
        sa.Column("project_id", sa.String(length=256), primary_key=True),
        sa.Column("submission_id", sa.String(length=256), primary_key=True),
        sa.Column("task_id", sa.String(length=256), nullable=False),
        sa.Column("attempt_id", sa.String(length=256), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("evidence", JSONB(), nullable=False, server_default="[]"),
        sa.Column("task_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id", "task_id"], ["tasks.project_id", "tasks.task_id"], ondelete="CASCADE", name="fk_task_submissions_task"),
        sa.ForeignKeyConstraint(["project_id", "attempt_id"], ["task_attempts.project_id", "task_attempts.attempt_id"], ondelete="CASCADE", name="fk_task_submissions_attempt"),
    )
    op.create_index("idx_task_submissions_project_task", "task_submissions", ["project_id", "task_id", "created_at"])
    op.create_table(
        "task_reviews",
        sa.Column("project_id", sa.String(length=256), primary_key=True),
        sa.Column("review_id", sa.String(length=256), primary_key=True),
        sa.Column("task_id", sa.String(length=256), nullable=False),
        sa.Column("submission_id", sa.String(length=256), nullable=False),
        sa.Column("reviewer", sa.String(length=256), nullable=False),
        sa.Column("decision", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id", "task_id"], ["tasks.project_id", "tasks.task_id"], ondelete="CASCADE", name="fk_task_reviews_task"),
        sa.ForeignKeyConstraint(["project_id", "submission_id"], ["task_submissions.project_id", "task_submissions.submission_id"], ondelete="CASCADE", name="fk_task_reviews_submission"),
    )
    op.create_index("idx_task_reviews_project_task", "task_reviews", ["project_id", "task_id", "created_at"])


def downgrade() -> None:
    op.drop_table("task_reviews")
    op.drop_table("task_submissions")
    op.drop_table("task_attempts")
    op.drop_table("task_agents")
    op.drop_table("tasks")
    op.drop_table("task_events")