"""Add the missing Context usage update timestamp.

Revision ID: 0010_context_usage_updated_at
Revises: 0009_graph_edge_evidence
Create Date: 2026-08-07
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0010_context_usage_updated_at"
down_revision = "0009_graph_edge_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("context_usage_daily")}
    if "updated_at" not in columns:
        op.add_column("context_usage_daily", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("context_usage_daily")}
    if "updated_at" in columns:
        op.drop_column("context_usage_daily", "updated_at")