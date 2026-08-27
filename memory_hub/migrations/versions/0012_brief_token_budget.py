"""Persist daily token reservations for external Brief generation.

Revision ID: 0012_brief_token_budget
Revises: 0011_remove_project_graph
"""

from alembic import op
import sqlalchemy as sa


revision = "0012_brief_token_budget"
down_revision = "0011_remove_project_graph"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "brief_token_usage_daily",
        sa.Column("project_id", sa.String(length=256), primary_key=True),
        sa.Column("usage_date", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("brief_token_usage_daily")
