"""Store runtime identity details for event attribution."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0007_event_runtime_identity"
down_revision = "0006_context_usage_daily"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {item["name"] for item in inspect(op.get_bind()).get_columns("memory_events")}
    for name in ("runtime_node_id", "source_node_name", "workspace_id", "agent_session_id", "transport_id"):
        if name not in existing:
            op.add_column("memory_events", sa.Column(name, sa.String(256 if name != "transport_id" else 128), nullable=True))


def downgrade() -> None:
    for name in ("transport_id", "agent_session_id", "workspace_id", "source_node_name", "runtime_node_id"):
        op.drop_column("memory_events", name)