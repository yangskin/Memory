"""Remove legacy Project Graph state while preserving Task Graph storage.

Revision ID: 0011_remove_project_graph
Revises: 0010_task_graph
Create Date: 2026-08-09
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB


revision = "0011_remove_project_graph"
down_revision = "0010_task_graph"
branch_labels = None
depends_on = None


_LEGACY_NODE_TYPES = "'source', 'file', 'class', 'module', 'blueprint', 'map', 'plugin', 'system'"


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())

    if "brief_jobs" in tables:
        op.execute(sa.text("DELETE FROM brief_jobs WHERE brief_type = 'project_graph'"))
    if "brief_heads" in tables:
        op.execute(sa.text("DELETE FROM brief_heads WHERE brief_type = 'project_graph'"))
    if "brief_snapshots" in tables:
        op.execute(sa.text("DELETE FROM brief_snapshots WHERE brief_type = 'project_graph'"))
    if "memory_events" in tables:
        op.execute(sa.text("UPDATE memory_events SET metadata = metadata - 'graph_delta' WHERE metadata ? 'graph_delta'"))

    if {"graph_nodes", "graph_edges"}.issubset(tables):
        op.execute(
            sa.text(
                """
                DELETE FROM graph_edges AS edge
                USING graph_nodes AS source, graph_nodes AS target
                WHERE edge.project_id = source.project_id
                  AND edge.project_id = target.project_id
                  AND edge.source_node_id = source.id
                  AND edge.target_node_id = target.id
                  AND NOT (
                    (edge.relation_type = 'depends_on' AND source.node_type = 'task' AND target.node_type = 'task')
                    OR (edge.relation_type = 'parent_of' AND source.node_type = 'task' AND target.node_type = 'task')
                    OR (edge.relation_type = 'produced_memory' AND source.node_type = 'task' AND target.node_type = 'asset')
                    OR (edge.relation_type = 'current_attempt' AND source.node_type = 'task' AND target.node_type = 'attempt')
                    OR (edge.relation_type = 'assigned_to' AND source.node_type = 'attempt' AND target.node_type = 'agent')
                    OR (edge.relation_type = 'assigned_by' AND source.node_type = 'attempt' AND target.node_type = 'agent')
                    OR (edge.relation_type = 'has_submission' AND source.node_type = 'attempt' AND target.node_type = 'submission')
                    OR (edge.relation_type = 'has_review' AND source.node_type = 'submission' AND target.node_type = 'review')
                    OR (edge.relation_type = 'reviewed_by' AND source.node_type = 'review' AND target.node_type = 'agent')
                  )
                """
            )
        )
        op.execute(sa.text(f"DELETE FROM graph_nodes WHERE node_type IN ({_LEGACY_NODE_TYPES})"))
        op.execute(
            sa.text(
                """
                DELETE FROM graph_nodes AS asset
                WHERE asset.node_type = 'asset'
                  AND NOT EXISTS (
                    SELECT 1
                    FROM graph_edges AS edge
                    JOIN graph_nodes AS task ON task.project_id = edge.project_id AND task.id = edge.source_node_id
                    WHERE edge.project_id = asset.project_id
                      AND edge.target_node_id = asset.id
                      AND edge.relation_type = 'produced_memory'
                      AND task.node_type = 'task'
                  )
                """
            )
        )
    if "graph_projection_states" in tables:
        op.drop_table("graph_projection_states")


def downgrade() -> None:
    if not inspect(op.get_bind()).has_table("graph_projection_states"):
        op.create_table(
            "graph_projection_states",
            sa.Column("project_id", sa.String(256), primary_key=True),
            sa.Column("covers_through_seq", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )