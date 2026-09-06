"""
Alembic environment.

Three things are wired up here that the generated template does not do for you:

  1. The database URL comes from `app.core.config`, not from `alembic.ini`.
     `alembic.ini` is committed to git; the URL contains the database password.
     Keeping it in `.env` is the only reason `.gitignore` protects anything.

  2. That URL is passed through `_async_url`, and the same pgbouncer-safe
     connect args the application uses are applied here. Alembic builds its own
     engine, so a fix applied only in `session.py` would leave migrations
     failing against the Supabase pooler while the app worked fine -- a
     confusing split worth avoiding by importing both from one place.

  3. `target_metadata` points at the real model registry, so autogenerate can
     see every table. If a model is missing from `app/db/models/__init__.py`,
     Alembic will not know about it and will quietly never create its table.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

from app.core.config import get_settings
from app.db.models import Base  # noqa: F401 -- imports every model as a side effect
from app.db.session import PGBOUNCER_SAFE_CONNECT_ARGS, _async_url

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Inject the URL from settings rather than reading it from alembic.ini.
# The `%` doubling is required because ConfigParser treats `%` as an escape
# character, and a Supabase password can legitimately contain one.
settings = get_settings()
if not settings.database_url:
    raise RuntimeError("DATABASE_URL is not set; check your .env file")

config.set_main_option(
    "sqlalchemy.url", _async_url(settings.database_url).replace("%", "%%")
)

target_metadata = Base.metadata


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Without this, a column changing from String(32) to String(64), or an
        # enum gaining a value, produces an empty migration.
        compare_type=True,
        # Catches a `server_default` being added or removed -- easy to change in
        # a model and easy to forget in a migration.
        compare_server_default=True,
    )


def run_migrations_offline() -> None:
    """Generate SQL without connecting. Useful for reviewing before applying."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # migrations are short-lived; no pool needed
        connect_args=PGBOUNCER_SAFE_CONNECT_ARGS,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()