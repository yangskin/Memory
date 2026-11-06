from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory


def test_hub_migration_graph_has_one_current_head() -> None:
    hub_root = Path(__file__).resolve().parents[2]
    config = Config(str(hub_root / "alembic.ini"))
    config.set_main_option("script_location", str(hub_root / "migrations"))

    script = ScriptDirectory.from_config(config)

    assert tuple(script.get_heads()) == ("0016_hub_task_authority",)

    migration = (hub_root / "migrations" / "versions" / "0011_remove_project_graph.py").read_text(encoding="utf-8")
    assert "graph_projection_states" in migration

    initial = (hub_root / "migrations" / "versions" / "0001_initial.py").read_text(encoding="utf-8")
    assert "memory_hub.db.models import Base" not in initial
    assert 'op.create_table(\n        "memory_events"' in initial


def test_task_authority_migration_removes_graph_storage_and_cannot_downgrade() -> None:
    hub_root = Path(__file__).resolve().parents[2]
    source = (hub_root / "migrations" / "versions" / "0016_hub_task_authority.py").read_text(encoding="utf-8")
    for fragment in ('op.drop_table("graph_edges")', 'op.drop_table("graph_nodes")', 'op.drop_table("task_agents")',
                     "DELETE FROM memory_events WHERE operation = 'task_sync'",
                     'op.drop_column("task_events", "source_event_id")',
                     "uq_task_events_project_task_seq"):
        assert fragment in source
    # 设备绑定只能由客户端以计算机名建立，迁移不得从注册记录回填。
    assert "INSERT INTO token_devices" not in source
    namespace: dict[str, object] = {}
    exec(compile(source, "0016_hub_task_authority.py", "exec"), namespace)
    with pytest.raises(RuntimeError, match="cannot be downgraded"):
        namespace["downgrade"]()
