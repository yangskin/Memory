"""Add a project time index for recent-event queries.

Revision ID: 0002_events_project_time_index
Revises: 0001_initial
Create Date: 2026-08-04
"""

from alembic import op


revision = "0002_events_project_time_index"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("idx_events_project_time", "memory_events", ["project_id", "occurred_at"])


def downgrade() -> None:
    op.drop_index("idx_events_project_time", table_name="memory_events")