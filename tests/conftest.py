"""
Test fixtures.

Tests run against in-memory SQLite, not Supabase. Three reasons, and the third
is the one that matters most:

  * Speed. A full onboarding flow is a dozen round trips; over the network that
    is seconds per test.
  * Isolation. Each test gets a fresh database, so no test can see another
    test's rows and ordering never matters.
  * Honesty. A test that needs a live Supabase connection stops being run.

This works because `JSONType` in the models carries a SQLite variant. It does
mean the tests do not exercise Postgres-specific behaviour -- migrations and
JSONB indexing in particular still need checking against the real database.

Tokens are minted here with the same HS256 signature the application verifies,
which is the whole authentication surface: the backend never issues a token and
never sees a password (FR-ONB-001).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import jwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.db.base import Base
from app.db.models import ReviewRecord, SafetyAnswerChange, User  # noqa: F401
from app.db.session import get_session
from app.main import app as fastapi_app

TEST_SECRET = "test-secret-not-used-anywhere-real"
TEST_CONSENT_VERSION = "1.2"


@pytest.fixture
def settings() -> Settings:
    """
    Test settings. `quota_enforced` stays true -- FR-SUB-004 allows turning it
    off, but a test suite that runs with a safety control disabled proves less
    than one that runs with it on.
    """
    return Settings(
        environment="test",
        database_url="sqlite+aiosqlite:///:memory:",
        jwt_secret=TEST_SECRET,
        jwt_audience="authenticated",
        consent_statement_version=TEST_CONSENT_VERSION,
        questionnaire_path="app/clinical/questionnaire/skin_type.v0.json",
        active_matrix_version="0.0.0-empty",
        quota_enforced=True,
        default_scan_allowance=1,
    )


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """One fresh in-memory database per test."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        # SQLite's in-memory database is per-connection. A pool would hand out
        # a second, empty connection and the tables would appear to vanish.
        connect_args={"check_same_thread": False},
        poolclass=None,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    await engine.dispose()


@pytest_asyncio.fixture
async def client(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    """
    An HTTP client wired to the real application.

    Requests go through the actual routers, dependencies, and error handlers.
    Only the database and settings are substituted, so a test failure means the
    application is wrong rather than the harness.
    """

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    fastapi_app.dependency_overrides[get_session] = override_session
    fastapi_app.dependency_overrides[get_settings] = lambda: settings

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    fastapi_app.dependency_overrides.clear()


def make_token(subject: str = "test-user", provider: str = "google") -> str:
    """A token the application will accept, shaped like Supabase's."""
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": subject,
            "aud": "authenticated",
            "exp": now + timedelta(hours=1),
            "iat": now,
            "email": f"{subject}@example.com",
            "app_metadata": {"provider": provider},
        },
        TEST_SECRET,
        algorithm="HS256",
    )


def auth(subject: str = "test-user") -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(subject)}"}


# Answer sets that resolve to each skin type under skin_type.v0.json.
# Recomputing these by hand after a weight change is the point: if the config
# is edited, these fail and force a look at whether the change was intended.
ANSWERS_COMBINATION = {
    "Q1": "Q1C",  # shiny in places   -> oil 2, tzone 2
    "Q2": "Q2B",  # T-zone only       -> oil 2, tzone 3
    "Q3": "Q3B",  # flaking sometimes -> dry 2, cheek_dry 2
    "Q4": "Q4B",  # pores at nose     -> oil 1, tzone 2
    "Q5": "Q5A",  # drier in cold     -> dry 3, cheek_dry 2
    "Q6": "Q6B",  # occasional oil    -> oil 1
}

ANSWERS_OILY = {
    "Q1": "Q1D",
    "Q2": "Q2C",
    "Q3": "Q3A",
    "Q4": "Q4C",
    "Q5": "Q5C",
    "Q6": "Q6C",
}

ANSWERS_DRY = {
    "Q1": "Q1A",
    "Q2": "Q2A",
    "Q3": "Q3C",
    "Q4": "Q4A",
    "Q5": "Q5A",
    "Q6": "Q6A",
}