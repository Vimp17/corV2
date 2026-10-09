"""Alembic environment. Uses the connection passed by app.db.init_db when present,
otherwise connects with MAI_DATABASE_URL from app.config."""
from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from app.config import settings

config = context.config


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        context.configure(connection=conn, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("Offline SQL generation is not supported; run migrations against a database.")
run_migrations_online()
