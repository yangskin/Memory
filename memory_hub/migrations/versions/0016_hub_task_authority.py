"""Hub 成为任务唯一状态机：设备绑定、命令存档、任务关系，删除 Graph 存储、task_agents 与 task_sync 记忆事件。

不可降级：Graph 节点/边与 task_sync 外层事件被删除后无法从现有数据还原，
回滚只能恢复迁移前的 pg_dump 备份。
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB

revision = "0016_hub_task_authority"
down_revision = "0015_device_status"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())

    op.create_table(
        "token_devices",
        sa.Column("token_id", sa.String(128), primary_key=True),
        sa.Column("project_id", sa.String(256), nullable=False),
        sa.Column("device_name", sa.String(96), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_token_devices_project_device", "token_devices", ["project_id", "device_name"])
    op.create_table(
        "task_commands",
        sa.Column("project_id", sa.String(256), primary_key=True),
        sa.Column("command_id", sa.String(256), primary_key=True),
        sa.Column("request_hash", sa.String(80), nullable=False),
        sa.Column("actor", sa.String(256), nullable=False),
        sa.Column("task_id", sa.String(256), nullable=False),
        sa.Column("result", JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_task_commands_project_task", "task_commands", ["project_id", "task_id"])
    op.create_table(
        "task_links",
        sa.Column("project_id", sa.String(256), primary_key=True),
        sa.Column("task_id", sa.String(256), primary_key=True),
        sa.Column("relation", sa.String(32), primary_key=True),
        sa.Column("target", sa.String(1024), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id", "task_id"], ["tasks.project_id", "tasks.task_id"], ondelete="CASCADE", name="fk_task_links_task"),
    )
    op.create_index("idx_task_links_project_target", "task_links", ["project_id", "relation", "target"])

    # 不从 device_enrollments 回填设备绑定：那里的名字来自注册时的主机名，可能与计算机名不同。
    # 旧凭据由客户端在 Hub 声明 task_commands 后，以本机计算机名自动完成一次性绑定。

    op.add_column("task_events", sa.Column("task_seq", sa.Integer(), nullable=True))
    op.add_column("task_events", sa.Column("agent", sa.String(64), nullable=True))
    op.alter_column("task_events", "expected_version", existing_type=sa.Integer(), nullable=True)
    op.execute(
        """
        UPDATE task_events AS e SET task_seq = s.rn
        FROM (
            SELECT task_event_seq,
                   row_number() OVER (PARTITION BY project_id, task_id ORDER BY task_event_seq) AS rn
            FROM task_events
        ) AS s
        WHERE e.task_event_seq = s.task_event_seq
        """
    )
    op.alter_column("task_events", "task_seq", existing_type=sa.Integer(), nullable=False)
    op.create_unique_constraint("uq_task_events_project_task_seq", "task_events", ["project_id", "task_id", "task_seq"])
    op.drop_constraint("uq_task_events_project_source_event", "task_events", type_="unique")
    op.drop_column("task_events", "source_event_id")

    op.add_column("tasks", sa.Column("last_task_seq", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("tasks", sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        """
        UPDATE tasks AS t SET last_task_seq = s.max_seq, last_activity_at = s.last_at
        FROM (
            SELECT project_id, task_id, max(task_seq) AS max_seq, max(occurred_at) AS last_at
            FROM task_events GROUP BY project_id, task_id
        ) AS s
        WHERE t.project_id = s.project_id AND t.task_id = s.task_id
        """
    )
    op.execute("UPDATE tasks SET last_activity_at = updated_at WHERE last_activity_at IS NULL")

    # 历史关系按创建事件回填；不做目标存在校验（规格要求），只排除空值与自引用。
    for relation, key in (("depends_on", "depends_on"), ("produced_memory", "produced_memory")):
        op.execute(
            f"""
            INSERT INTO task_links(project_id, task_id, relation, target, created_at)
            SELECT DISTINCT e.project_id, e.task_id, '{relation}', btrim(v.value), e.occurred_at
            FROM task_events AS e
            JOIN tasks AS t ON t.project_id = e.project_id AND t.task_id = e.task_id
            CROSS JOIN LATERAL jsonb_array_elements_text(
                CASE WHEN jsonb_typeof(e.payload -> '{key}') = 'array' THEN e.payload -> '{key}' ELSE '[]'::jsonb END
            ) AS v(value)
            WHERE e.event_type = 'TaskCreated'
              AND btrim(v.value) <> ''
              AND length(btrim(v.value)) <= 1024
              AND ('{relation}' <> 'depends_on' OR btrim(v.value) <> e.task_id)
            ON CONFLICT DO NOTHING
            """
        )
    op.execute(
        """
        INSERT INTO task_links(project_id, task_id, relation, target, created_at)
        SELECT e.project_id, e.task_id, 'parent', btrim(e.payload ->> 'parent_task_id'), e.occurred_at
        FROM task_events AS e
        JOIN tasks AS t ON t.project_id = e.project_id AND t.task_id = e.task_id
        WHERE e.event_type = 'TaskCreated'
          AND jsonb_typeof(e.payload -> 'parent_task_id') = 'string'
          AND btrim(e.payload ->> 'parent_task_id') <> ''
          AND btrim(e.payload ->> 'parent_task_id') <> e.task_id
        ON CONFLICT DO NOTHING
        """
    )

    op.execute("DELETE FROM memory_events WHERE operation = 'task_sync'")
    if "graph_edges" in tables:
        op.drop_table("graph_edges")
    if "graph_nodes" in tables:
        op.drop_table("graph_nodes")
    # task_agents 只写不读；命令事务写它会跨任务共享行，造成锁顺序死锁与误报冲突。
    if "task_agents" in tables:
        op.drop_table("task_agents")


def downgrade():
    raise RuntimeError(
        "0016_hub_task_authority cannot be downgraded; restore the pre-migration pg_dump backup instead"
    )
