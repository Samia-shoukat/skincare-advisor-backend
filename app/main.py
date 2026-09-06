"""
Application entry point.

"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.errors import AppError, app_error_handler, unhandled_error_handler
from app.db.session import check_connection, dispose_engine

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
    )

    logger.info("starting in %s mode", settings.environment)
    logger.info("quota enforcement: %s", "on" if settings.quota_enforced else "OFF")

    if not settings.quota_enforced and settings.is_production:
        # FR-SUB-004 allows this toggle for testing. In production it would
        # give every user unlimited scans.
        logger.error("quota enforcement is OFF in production -- this is a misconfiguration")

    yield

    await dispose_engine()
    logger.info("shutting down")


app = FastAPI(
    title="Skincare Advisor API",
    version="0.1.0",
    description=(
        "Backend for SRS-AISA-001 v1.3. Not a medical device. Cosmetic screening only."
    ),
    lifespan=lifespan,
)

# IF-COMM-003. Registered before any route runs, so nothing can return a raw
# traceback even if a handler forgets to catch something.
app.add_exception_handler(AppError, app_error_handler)
app.add_exception_handler(Exception, unhandled_error_handler)

app.include_router(api_router)


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict[str, str]:
    """
    Liveness. Answers one question: is this process running?

    Deliberately checks nothing else. A liveness probe that fails when the
    database is slow causes the orchestrator to restart a perfectly healthy
    process, which makes the outage worse.
    """
    return {"status": "ok"}


@app.get("/readyz", tags=["ops"])
async def readyz() -> dict[str, object]:
    """
    Readiness. Answers a different question: can this process do its job?

    Each check names the requirement it protects, so a failing check tells you
    what is broken rather than just that something is.
    """
    settings = get_settings()

    db_ok, db_error = await check_connection()

    checks: dict[str, dict[str, object]] = {
        "database": {
            "ok": db_ok,
            "requirement": "IF-SW-002",
            "detail": db_error,
        },
        "auth": {
            "ok": bool(settings.jwt_secret),
            "requirement": "IF-COMM-002",
            "detail": None if settings.jwt_secret else "JWT_SECRET is not set",
        },
        "vision_provider": {
            "ok": bool(settings.gemini_api_key),
            "requirement": "DEP-001",
            "detail": None if settings.gemini_api_key else "GEMINI_API_KEY is not set",
        },
        "provider_retention_disabled": {
            "ok": settings.provider_retention_disabled,
            "requirement": "FR-CAM-004",
            "detail": (
                None
                if settings.provider_retention_disabled
                else "the Zero-Save Policy is not satisfied while the provider may retain inputs"
            ),
        },
    }

    return {"ready": all(c["ok"] for c in checks.values()), "checks": checks}