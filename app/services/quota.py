"""
Scan allowance. FR-SUB-002, FR-SUB-003, FR-SUB-004.

Eligibility -- may this user scan at all -- lives in `app.services.eligibility`
and is evaluated before a byte of image is read. This module handles the other
half: what happens to the allowance *after* a scan has run.

The two are deliberately separate. Eligibility is a pure function of the user
row and answers a question the client also asks, so it must be callable without
a database write. Decrementing is a write, happens once, and happens at the very
end of the processing order in SRS 4.2.

## One rule, stated three times in the SRS

FR-SUB-003: decrement only when a scan produces a displayed routine.
FR-TRI-004: a referral does not decrement, because a user should never be
charged, in quota or money, for being told to see a doctor.
FR-AI-003: an unusable image does not decrement either.

All three are the same rule seen from different angles, and the way to keep them
consistent is for there to be one place that decides. `should_decrement` is that
place, and it takes an outcome rather than a set of flags so that a fifth reason
to skip the decrement cannot be added without appearing here.

## Idempotency

FR-SUB-003's third criterion: a retry with the same idempotency key decrements
at most once. A mobile client on a poor connection retries, and the SRS
processing order puts the decrement after the provider call -- so the window
between "the routine was generated" and "the client received it" is exactly the
window where a retry is most likely and most expensive.

The key is looked up against completed scan logs. A hit returns the earlier log
rather than running the scan again, which also avoids a second provider call.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ScanOutcome
from app.db.models.scan_log import ScanLog
from app.db.models.user import User

logger = logging.getLogger(__name__)


def should_decrement(outcome: ScanOutcome) -> bool:
    """
    FR-SUB-003. The single decision point.

    Written as an explicit membership test rather than `outcome is ROUTINE` so
    that the three outcomes that must not decrement are visible here, next to
    the requirements that say so. A reader checking FR-TRI-004 should be able
    to confirm it from this function without following a chain of negations.
    """
    return outcome is ScanOutcome.ROUTINE


async def find_completed_scan(
    session: AsyncSession, user: User, idempotency_key: str | None
) -> ScanLog | None:
    """
    An earlier scan for this user under the same key, if there is one.

    Scoped to the user, not global. Two clients colliding on a key -- a
    plausible accident with a short or client-generated key -- must not let one
    user read another's scan result, and scoping the lookup makes that
    structurally impossible rather than dependent on key entropy.
    """
    if not idempotency_key:
        return None

    result = await session.execute(
        select(ScanLog)
        .where(ScanLog.user_auth_id == user.auth_id)
        .where(ScanLog.idempotency_key == idempotency_key)
        .order_by(ScanLog.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def apply_decrement(user: User, *, quota_enforced: bool) -> bool:
    """
    Take one scan off the allowance. Returns whether it was taken.

    Does nothing when enforcement is off (FR-SUB-004), and nothing when the
    count is already at zero. That second guard should be unreachable, because
    eligibility refuses the scan earlier -- but a negative allowance would make
    `scans_remaining` meaningless to every caller that reads it, and the cost of
    the check is one comparison.
    """
    if not quota_enforced:
        logger.info("quota enforcement off; allowance unchanged for %s", user.auth_id)
        return False

    if user.scans_remaining <= 0:
        logger.warning(
            "decrement requested for %s with no allowance remaining; ignoring",
            user.auth_id,
        )
        return False

    user.scans_remaining -= 1
    return True
