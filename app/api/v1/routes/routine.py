"""
Routine endpoints. FR-SUB-005, UC-007.

"Exhausting the allowance restricts new analysis, not access to results the
user already received." So this route does not check eligibility or quota at
all -- only authentication. A user whose one scan is used still reads their
routine here, in full, every time they open the app.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUser, SessionDep
from app.core.errors import NoRoutine
from app.services.routine_service import latest_routine, routine_response

router = APIRouter(prefix="/routines", tags=["routine"])


@router.get("/latest")
async def get_latest_routine(user: CurrentUser, session: SessionDep) -> dict:
    """The most recent routine, with its product options. 404 if none yet."""
    row = await latest_routine(session, user)
    if row is None:
        raise NoRoutine()
    return routine_response(row)
