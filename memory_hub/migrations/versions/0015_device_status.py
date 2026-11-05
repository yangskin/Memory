"""设备状态改用独立表覆盖更新，并删除 Board 里遗留的心跳快照。

hub-health / codex-execution 原先每分钟在 Board 新建一条 note；迁移后每台设备每个通道
只占一行。降级只删除新表，已删除的历史快照不恢复（它们只是过期的可见性信息）。
"""
from alembic import op
import sqlalchemy as sa

revision = "0015_device_status"
down_revision = "0014_board_author_auth"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "device_status",
        sa.Column("project_id", sa.String(256), primary_key=True),
        sa.Column("channel", sa.String(64), primary_key=True),
        sa.Column("author_auth_id", sa.String(64), primary_key=True),
        sa.Column("device_name", sa.String(96), primary_key=True),
        sa.Column("author_user_id", sa.String(256), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_device_status_project_channel_device", "device_status", ["project_id", "channel", "device_name"])
    op.create_index("idx_device_status_project_updated", "device_status", ["project_id", "updated_at"])
    # 只删机器心跳 note；人类留言、回帖及其它机器通道（原窗口续办收据）保持不变。
    op.execute(
        "DELETE FROM board_posts WHERE post_type = 'note' "
        "AND author_agent_id IN ('hub-health', 'codex-execution')"
    )


def downgrade():
    op.drop_index("idx_device_status_project_updated", table_name="device_status")
    op.drop_index("idx_device_status_project_channel_device", table_name="device_status")
    op.drop_table("device_status")
