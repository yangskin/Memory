"""Create Memory Hub V1 tables.

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-04
"""

from alembic import op

from memory_hub.db.models import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)