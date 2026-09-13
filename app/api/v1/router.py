"""
Version 1 router.

Every route lives under /v1. The prefix is not decoration -- IF-COMM-001 asks
for a versioned REST API, and a version in the path is what lets a v2 with a
different scan response ship while v1 clients keep working.

New route modules are mounted here and nowhere else, so this file is the index
of what the API actually exposes.
"""

from fastapi import APIRouter

from app.api.v1.routes import content, onboarding, scan

api_router = APIRouter(prefix="/v1")
api_router.include_router(onboarding.router)
api_router.include_router(content.router)
api_router.include_router(scan.router)