"""Add a project time index for recent-event queries.

Revision ID: 0002_events_project_time_index
Revises: 0001_initial
Create Date: 2026-08-04
"""

from alembic import op
from sqlalchemy import inspect


revision = "0002_events_project_time_index"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def _index_exists(bind, index_name: str, table_name: str) -> bool:
    return any(index["name"] == index_name for index in inspect(bind).get_indexes(table_name))


def upgrade() -> None:
    # 0001 uses Base.metadata.create_all, which already creates every index
    # declared in the model's __table_args__. Keep this migration idempotent so
    # a fresh database (where the index is created during 0001) does not fail
    # with a duplicate-index error, while older databases still get the index.
    bind = op.get_bind()
    if not _index_exists(bind, "idx_events_project_time", "memory_events"):
        op.create_index("idx_events_project_time", "memory_events", ["project_id", "occurred_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if _index_exists(bind, "idx_events_project_time", "memory_events"):
        op.drop_index("idx_events_project_time", table_name="memory_events")