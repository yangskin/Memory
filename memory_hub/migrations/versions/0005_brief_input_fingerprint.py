"""Track brief input fingerprints and scheduler checks.

Revision ID: 0005_brief_input_fingerprint
Revises: 0004_project_graph
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0005_brief_input_fingerprint"
down_revision = "0004_project_graph"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "last_checked_at" not in {item["name"] for item in inspect(bind).get_columns("brief_jobs")}:
        op.add_column("brief_jobs", sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True))
    if "input_fingerprint" not in {item["name"] for item in inspect(bind).get_columns("brief_snapshots")}:
        op.add_column("brief_snapshots", sa.Column("input_fingerprint", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("brief_snapshots", "input_fingerprint")
    op.drop_column("brief_jobs", "last_checked_at")