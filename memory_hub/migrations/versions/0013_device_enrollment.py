"""待审批设备申请；服务端不保存任何凭据明文。"""
from alembic import op
import sqlalchemy as sa

revision = "0013_device_enrollment"
down_revision = "0012_brief_token_budget"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "device_enrollments",
        sa.Column("enrollment_id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(256), nullable=False),
        sa.Column("device_name", sa.String(96), nullable=False),
        sa.Column("token_id", sa.String(128), nullable=False, unique=True),
        sa.Column("token_secret_hash", sa.String(64), nullable=False),
        sa.Column("poll_secret_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("approved_by", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_enrollment_project_created", "device_enrollments", ["project_id", "created_at"])


def downgrade():
    op.drop_table("device_enrollments")
