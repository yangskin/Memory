"""Store runtime identity details for board attribution."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0008_board_runtime_identity"
down_revision = "0007_event_runtime_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {item["name"] for item in inspect(op.get_bind()).get_columns("board_posts")}
    for name in ("runtime_node_id", "source_node_name", "workspace_id", "agent_session_id"):
        if name not in existing:
            op.add_column("board_posts", sa.Column(name, sa.String(256), nullable=True))
    if "transport_id" not in existing:
        op.add_column("board_posts", sa.Column("transport_id", sa.String(128), nullable=True))


def downgrade() -> None:
    for name in ("transport_id", "agent_session_id", "workspace_id", "source_node_name", "runtime_node_id"):
        op.drop_column("board_posts", name)