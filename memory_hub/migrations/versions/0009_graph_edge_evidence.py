"""Persist bounded record evidence on projected graph edges.

Revision ID: 0009_graph_edge_evidence
Revises: 0008_board_runtime_identity
Create Date: 2026-08-06
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB


revision = "0009_graph_edge_evidence"
down_revision = "0008_board_runtime_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("graph_edges")}
    if "evidence_ids" not in columns:
        op.add_column("graph_edges", sa.Column("evidence_ids", JSONB(), nullable=False, server_default="[]"))


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("graph_edges")}
    if "evidence_ids" in columns:
        op.drop_column("graph_edges", "evidence_ids")