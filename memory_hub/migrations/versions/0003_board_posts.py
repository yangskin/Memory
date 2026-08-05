"""Create board_posts table for Project Board collaboration.

Revision ID: 0003_board_posts
Revises: 0002_events_project_time_index
Create Date: 2026-08-05
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import DateTime, Index, String, Text, inspect
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "0003_board_posts"
down_revision = "0002_events_project_time_index"
branch_labels = None
depends_on = None


def _table_exists(bind, table_name: str) -> bool:
    return inspect(bind).has_table(table_name)


def _index_exists(bind, index_name: str, table_name: str) -> bool:
    return any(index["name"] == index_name for index in inspect(bind).get_indexes(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "board_posts"):
        op.create_table(
            "board_posts",
            sa.Column("post_id", UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("project_id", String(length=256), nullable=False),
            sa.Column("author_user_id", String(length=256), nullable=False),
            sa.Column("author_agent_id", String(length=256), nullable=True),
            sa.Column("author_agent_instance_id", String(length=256), nullable=True),
            sa.Column("post_type", String(length=64), nullable=False),
            sa.Column("content", Text(), nullable=False),
            sa.Column("task_id", String(length=256), nullable=True),
            sa.Column("thread_id", UUID(as_uuid=True), nullable=False),
            sa.Column("reply_to", UUID(as_uuid=True), nullable=True),
            sa.Column("references_json", JSONB(), nullable=False, server_default="[]"),
            sa.Column("status", String(length=64), nullable=False, server_default="open"),
            sa.Column("created_at", DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", DateTime(timezone=True), nullable=True),
        )

    if not _index_exists(bind, "idx_board_posts_project_created", "board_posts"):
        op.create_index("idx_board_posts_project_created", "board_posts", ["project_id", "created_at"])
    if not _index_exists(bind, "idx_board_posts_project_status", "board_posts"):
        op.create_index("idx_board_posts_project_status", "board_posts", ["project_id", "status", "created_at"])
    if not _index_exists(bind, "idx_board_posts_project_thread", "board_posts"):
        op.create_index("idx_board_posts_project_thread", "board_posts", ["project_id", "thread_id", "created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, "board_posts"):
        op.drop_table("board_posts")
