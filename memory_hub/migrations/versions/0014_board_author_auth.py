"""Board发帖身份由认证主体写入；旧记录保留NULL，不推断身份。"""
from alembic import op
import sqlalchemy as sa

revision = "0014_board_author_auth"
down_revision = "0013_device_enrollment"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("board_posts", sa.Column("author_auth_id", sa.String(64), nullable=True))


def downgrade():
    op.drop_column("board_posts", "author_auth_id")
