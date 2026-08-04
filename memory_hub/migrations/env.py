from __future__ import annotations

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from memory_hub.db.models import Base

config = context.config
database_url = os.getenv("MEMORY_HUB_DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()