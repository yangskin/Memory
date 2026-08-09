"""Create Memory Hub V1 tables.

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-04
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "access_tokens",
        sa.Column("token_id", sa.String(length=128), primary_key=True),
        sa.Column("token_secret_hash", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=256), nullable=False),
        sa.Column("project_id", sa.String(length=256), nullable=False),
        sa.Column("scopes", ARRAY(sa.String(length=64)), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "memory_events",
        sa.Column("server_seq", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("event_id", UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", sa.String(length=256), nullable=False),
        sa.Column("user_id", sa.String(length=256), nullable=False),
        sa.Column("source_node_id", sa.String(length=256), nullable=True),
        sa.Column("agent_id", sa.String(length=256), nullable=True),
        sa.Column("agent_instance_id", sa.String(length=256), nullable=True),
        sa.Column("task_id", sa.String(length=256), nullable=True),
        sa.Column("task_run_id", sa.String(length=256), nullable=True),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("record_kind", sa.String(length=128), nullable=True),
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("task_phase", sa.String(length=64), nullable=True),
        sa.Column("content_markdown", sa.Text(), nullable=True),
        sa.Column("metadata", JSONB(), nullable=False, server_default="{}"),
        sa.Column("source_record_id", sa.String(length=256), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("content_hash", sa.String(length=80), nullable=False),
        sa.Column("content_redacted", sa.Boolean(), nullable=False, server_default="false"),
        sa.UniqueConstraint("project_id", "event_id", name="uq_memory_events_project_event"),
    )
    op.create_index("idx_events_project_seq", "memory_events", ["project_id", "server_seq"])
    op.create_index("idx_events_project_user_time", "memory_events", ["project_id", "user_id", "occurred_at"])
    op.create_index("idx_events_project_task_time", "memory_events", ["project_id", "task_id", "occurred_at"])
    op.create_index("idx_events_project_agent_time", "memory_events", ["project_id", "agent_instance_id", "occurred_at"])
    op.create_table(
        "brief_jobs",
        sa.Column("job_key", sa.String(length=512), primary_key=True),
        sa.Column("project_id", sa.String(length=256), nullable=False),
        sa.Column("brief_type", sa.String(length=64), nullable=False),
        sa.Column("subject_user_id", sa.String(length=256), nullable=True),
        sa.Column("requested_through_seq", sa.BigInteger(), nullable=False),
        sa.Column("processed_through_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_id", sa.String(length=256), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "brief_snapshots",
        sa.Column("brief_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", sa.String(length=256), nullable=False),
        sa.Column("brief_type", sa.String(length=64), nullable=False),
        sa.Column("subject_user_id", sa.String(length=256), nullable=True),
        sa.Column("input_seq_from", sa.BigInteger(), nullable=True),
        sa.Column("input_seq_to", sa.BigInteger(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("structured_brief", JSONB(), nullable=False),
        sa.Column("rendered_markdown", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=256), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_event_ids", JSONB(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(length=64), nullable=False),
    )
    op.create_table(
        "brief_heads",
        sa.Column("project_id", sa.String(length=256), primary_key=True),
        sa.Column("brief_type", sa.String(length=64), primary_key=True),
        sa.Column("subject_user_id", sa.String(length=256), primary_key=True),
        sa.Column("current_brief_id", UUID(as_uuid=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("brief_heads")
    op.drop_table("brief_snapshots")
    op.drop_table("brief_jobs")
    op.drop_table("memory_events")
    op.drop_table("access_tokens")