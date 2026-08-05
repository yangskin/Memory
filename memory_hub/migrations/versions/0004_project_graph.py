"""Add deterministic project graph projection tables.

Revision ID: 0004_project_graph
Revises: 0003_board_posts
Create Date: 2026-08-05
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "0004_project_graph"
down_revision = "0003_board_posts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
            "graph_nodes",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("project_id", sa.String(256), nullable=False),
            sa.Column("node_type", sa.String(64), nullable=False),
            sa.Column("node_key", sa.String(1024), nullable=False),
            sa.Column("name", sa.String(1024), nullable=False),
            sa.Column("metadata", JSONB(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("project_id", "node_type", "node_key", name="uq_graph_nodes_project_type_key"),
            sa.UniqueConstraint("project_id", "id", name="uq_graph_nodes_project_id"),
        )
    op.create_index("idx_graph_nodes_project_type", "graph_nodes", ["project_id", "node_type"])
    op.create_table(
            "graph_edges",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("project_id", sa.String(256), nullable=False),
            sa.Column("source_node_id", UUID(as_uuid=True), nullable=False),
            sa.Column("target_node_id", UUID(as_uuid=True), nullable=False),
            sa.Column("relation_type", sa.String(64), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="1"),
            sa.Column("source_event_ids", JSONB(), nullable=False, server_default="[]"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("project_id", "source_node_id", "target_node_id", "relation_type", name="uq_graph_edges_project_relation"),
            sa.ForeignKeyConstraint(["project_id", "source_node_id"], ["graph_nodes.project_id", "graph_nodes.id"], ondelete="CASCADE", name="fk_graph_edges_source_project_node"),
            sa.ForeignKeyConstraint(["project_id", "target_node_id"], ["graph_nodes.project_id", "graph_nodes.id"], ondelete="CASCADE", name="fk_graph_edges_target_project_node"),
        )
    op.create_index("idx_graph_edges_project_source", "graph_edges", ["project_id", "source_node_id"])
    op.create_index("idx_graph_edges_project_target", "graph_edges", ["project_id", "target_node_id"])
    op.create_table(
            "graph_projection_states",
            sa.Column("project_id", sa.String(256), primary_key=True),
            sa.Column("covers_through_seq", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )


def downgrade() -> None:
    op.drop_table("graph_projection_states")
    op.drop_table("graph_edges")
    op.drop_table("graph_nodes")