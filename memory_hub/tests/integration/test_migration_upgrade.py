"""End-to-end Alembic upgrade coverage for an empty PostgreSQL database."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError


pytestmark = pytest.mark.skipif(not os.getenv("MEMORY_HUB_DATABASE_URL"), reason="requires PostgreSQL")


def _alembic_config(database_url: str) -> Config:
    hub_root = Path(__file__).resolve().parents[2]
    config = Config(str(hub_root / "alembic.ini"))
    config.set_main_option("script_location", str(hub_root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def test_fresh_postgresql_database_upgrades_to_head(monkeypatch) -> None:
    configured_url = make_url(os.environ["MEMORY_HUB_DATABASE_URL"])
    database_name = f"memory_hub_migration_test_{uuid4().hex}"
    admin_engine = create_engine(configured_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    test_engine = None
    test_url = configured_url.set(database=database_name)
    escaped_test_url = test_url.render_as_string(hide_password=False).replace("%", "%%")

    try:
        try:
            with admin_engine.connect() as connection:
                connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        except ProgrammingError as exc:
            pytest.skip(f"requires PostgreSQL CREATE DATABASE permission: {exc.orig}")

        monkeypatch.setenv("MEMORY_HUB_DATABASE_URL", escaped_test_url)
        command.upgrade(_alembic_config(escaped_test_url), "head")

        test_engine = create_engine(test_url)
        with test_engine.connect() as connection:
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
            task_tables = set(connection.scalars(text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name LIKE 'task_%'"
            )))
            projection_state = connection.scalar(text("SELECT to_regclass('public.graph_projection_states')"))

        assert revision == "0011_remove_project_graph"
        assert task_tables == {
            "task_agents",
            "task_attempts",
            "task_events",
            "task_reviews",
            "task_submissions",
            "tasks",
        }
        assert projection_state is None
    finally:
        if test_engine is not None:
            test_engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'))
        admin_engine.dispose()