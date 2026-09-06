"""
Database connection and session management.

Async throughout. The reason is FR-AI-001: the scan pipeline awaits a network
call to the vision provider in the middle of a request. With synchronous
sessions, that request would hold a database connection open for the whole
provider round trip -- several seconds of a connection doing nothing. Supabase's
free tier allows 60 connections, so that runs out fast.

Nothing here connects at import time. The engine is built on first use, which
keeps `import app.db.session` cheap and lets tests substitute their own.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


# Supabase's connection pooler runs pgbouncer in transaction mode: a physical
# connection is handed to a different client between statements. asyncpg caches
# prepared statements per connection by default, so the next request reuses a
# statement name the new backend has never seen, and Postgres reports a
# duplicate. Disabling the cache is the documented fix. The cost is one extra
# parse per statement, which is not measurable at this scale.
#
# Harmless on a direct (non-pooler) connection, so it stays on either way.
PGBOUNCER_SAFE_CONNECT_ARGS = {
    "statement_cache_size": 0,
    "prepared_statement_cache_size": 0,
}


def _async_url(url: str) -> str:
    """
    Supabase hands out a `postgresql://` URL, which SQLAlchemy reads as the
    synchronous psycopg2 driver. Swapping the scheme selects asyncpg instead.

    Doing this here rather than asking you to edit `.env` means the value you
    paste from the Supabase dashboard works unchanged.
    """
    if url.startswith("postgresql+"):
        return url  # a driver is already specified; leave it alone
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def get_engine() -> AsyncEngine:
    """Built once per process, on first use."""
    global _engine
    if _engine is None:
        settings = get_settings()
        if not settings.database_url:
            raise RuntimeError("DATABASE_URL is not set")

        _engine = create_async_engine(
            _async_url(settings.database_url),
            # Supabase closes idle connections. Without pre_ping, the first
            # query after an idle period fails on a dead connection.
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            # Never turn this on. Echoed SQL puts row values -- including dates
            # of birth -- into the log.
            echo=False,
            connect_args=PGBOUNCER_SAFE_CONNECT_ARGS,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            class_=AsyncSession,
            # Keeps attributes readable after commit. Without this, touching
            # `user.auth_id` after a commit triggers a fresh query.
            expire_on_commit=False,
        )
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """
    FastAPI dependency. Commits if the request succeeded, rolls back if it
    raised.

    The rollback matters more than it looks. Without it, a request that fails
    halfway through onboarding could leave a User with a confirmed date of
    birth but no age band -- a half-written record that FR-ONB-004 says should
    not exist.
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def check_connection() -> tuple[bool, str | None]:
    """
    Used by /readyz. Returns (ok, error type name).

    Runs the cheapest possible query. This is a connectivity check, not a
    performance check -- if the database is merely slow, that is a monitoring
    concern, not a reason to report the service as unready.

    Only the exception TYPE is returned, never its message. A connection error
    message can contain the host and sometimes the credentials, and /readyz is
    often exposed more widely than the rest of the API.
    """
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True, None
    except Exception as exc:
        logger.warning("database connectivity check failed: %s", type(exc).__name__)
        return False, type(exc).__name__


async def dispose_engine() -> None:
    """Called on shutdown so connections close cleanly rather than timing out."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None