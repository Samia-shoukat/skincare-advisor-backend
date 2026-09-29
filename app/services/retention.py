"""
Data retention. DR-003, DR-004.

DR-003: scan logs are deleted at 24 months.
DR-004: an account with no sign-in for 24 months is deleted.

Run by `scripts/retention.py`, on a schedule (daily is plenty).

## Two decisions

**Scan logs behind a routine are kept.** A routine belongs to its scan log, so
deleting the log deletes the routine -- and FR-SUB-005 says an active user keeps
access to their routine. A routine and its log are therefore kept as account
data while the account exists, and go with the account under DR-002 or DR-004.

**DR-004's notification is not automated.** The SRS says 24 months without
sign-in triggers a notification, then deletion. The app has no email channel, so
the script lists the accounts due; an operator notifies them, then re-runs with
--apply. Recorded as an open item.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, or_, and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.routine import Routine
from app.db.models.scan_log import ScanLog
from app.db.models.user import User
from app.services.account_deletion import erase_user_data

RETENTION = timedelta(days=730)  # 24 months


@dataclass
class RetentionReport:
    scan_logs_deleted: int
    inactive_accounts: list[str]
    accounts_deleted: int


async def purge_old_scan_logs(session: AsyncSession, now: datetime) -> int:
    """DR-003. Returns how many rows were deleted."""
    cutoff = now - RETENTION
    kept_for_routines = select(Routine.scan_log_id)
    result = await session.execute(
        delete(ScanLog)
        .where(ScanLog.created_at < cutoff)
        .where(ScanLog.id.not_in(kept_for_routines))
    )
    return result.rowcount or 0


async def inactive_accounts(session: AsyncSession, now: datetime) -> list[User]:
    """
    DR-004. Accounts with no sign-in for 24 months.

    An account that never recorded a sign-in time is judged by when it was
    created, so it cannot escape the rule by never having the field set.
    """
    cutoff = now - RETENTION
    result = await session.execute(
        select(User).where(
            or_(
                User.last_sign_in_at < cutoff,
                and_(User.last_sign_in_at.is_(None), User.created_at < cutoff),
            )
        )
    )
    return list(result.scalars())


async def run_retention(
    session: AsyncSession, now: datetime, *, delete_accounts: bool, auth_admin=None
) -> RetentionReport:
    """One retention pass. Flushes; the caller commits."""
    logs = await purge_old_scan_logs(session, now)

    due = await inactive_accounts(session, now)
    ids = [u.auth_id for u in due]
    deleted = 0
    if delete_accounts:
        for user in due:
            await erase_user_data(session, user)
            if auth_admin is not None:
                await auth_admin.delete_user(user.auth_id)
            deleted += 1

    await session.flush()
    return RetentionReport(scan_logs_deleted=logs, inactive_accounts=ids, accounts_deleted=deleted)
