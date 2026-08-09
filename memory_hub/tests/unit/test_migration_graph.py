from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_hub_migration_graph_has_one_current_head() -> None:
    hub_root = Path(__file__).resolve().parents[2]
    config = Config(str(hub_root / "alembic.ini"))
    config.set_main_option("script_location", str(hub_root / "migrations"))

    script = ScriptDirectory.from_config(config)

    assert tuple(script.get_heads()) == ("0010_task_graph",)