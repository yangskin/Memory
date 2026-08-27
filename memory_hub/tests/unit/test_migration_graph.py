from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_hub_migration_graph_has_one_current_head() -> None:
    hub_root = Path(__file__).resolve().parents[2]
    config = Config(str(hub_root / "alembic.ini"))
    config.set_main_option("script_location", str(hub_root / "migrations"))

    script = ScriptDirectory.from_config(config)

    assert tuple(script.get_heads()) == ("0012_brief_token_budget",)

    migration = (hub_root / "migrations" / "versions" / "0011_remove_project_graph.py").read_text(encoding="utf-8")
    assert "graph_projection_states" in migration
    assert 'drop_table("graph_nodes")' not in migration
    assert 'drop_table("graph_edges")' not in migration

    initial = (hub_root / "migrations" / "versions" / "0001_initial.py").read_text(encoding="utf-8")
    assert "memory_hub.db.models import Base" not in initial
    assert 'op.create_table(\n        "memory_events"' in initial