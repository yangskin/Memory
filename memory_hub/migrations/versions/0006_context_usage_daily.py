"""Add daily Context API usage aggregates.

Revision ID: 0006_context_usage_daily
Revises: 0005_brief_input_fingerprint
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_context_usage_daily"
down_revision = "0005_brief_input_fingerprint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "context_usage_daily",
        sa.Column("project_id", sa.String(256), primary_key=True),
        sa.Column("usage_date", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("returned_event_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("returned_brief_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("user_brief_requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("project_brief_requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("same_task_requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("other_agents_requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("other_tasks_requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("project_activity_requests", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("context_usage_daily")