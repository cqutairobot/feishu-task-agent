"""Alembic environment for Feishu Task Agent."""

from __future__ import annotations

from alembic import context

from app.config import load_database_settings
from app.database.engine import create_database_engine
from app.database.models import Base


config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    override = config.attributes.get("database_url")
    if isinstance(override, str) and override:
        return override
    return load_database_settings().url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_database_engine(_database_url())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
