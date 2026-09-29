"""
Erasing one user's data. DR-002, DR-004.

Used by account deletion (the user asked) and by the retention job (24 months
inactive). Both mean the same thing: every row that belongs to this person is
gone, not hidden or anonymised.

The foreign keys already cascade on Postgres, so on the live database deleting
the user row alone would do it. The deletes here are explicit anyway, for two
reasons: the order is then visible in one place, and it does not depend on the
database enforcing foreign keys -- SQLite, which the test suite uses, does not
by default, so a test relying on the cascade would pass while proving nothing.

Products are not touched. They belong to the catalogue, not the user; only the
link rows between this user's routines and the products go.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.routine import Routine, routine_products
from app.db.models.scan_log import ScanLog
from app.db.models.user import SafetyAnswerChange, User


async def erase_user_data(session: AsyncSession, user: User) -> None:
    """Delete everything belonging to `user`, then the user. Flushes, does not commit."""
    routine_ids = select(Routine.id).where(Routine.user_auth_id == user.auth_id)

    await session.execute(delete(routine_products).where(routine_products.c.routine_id.in_(routine_ids)))
    await session.execute(delete(Routine).where(Routine.user_auth_id == user.auth_id))
    await session.execute(delete(ScanLog).where(ScanLog.user_auth_id == user.auth_id))
    await session.execute(
        delete(SafetyAnswerChange).where(SafetyAnswerChange.user_auth_id == user.auth_id)
    )
    # Expire the relationship so the ORM does not try to delete audit rows the
    # statement above already removed.
    session.expire(user, ["safety_answer_changes"])
    await session.delete(user)
    await session.flush()
