"""
Data retention pass. DR-003, DR-004.

    python scripts/retention.py                      # report only
    python scripts/retention.py --apply              # delete old scan logs
    python scripts/retention.py --apply --accounts   # ...and inactive accounts

Deletes scan logs older than 24 months (except those behind a routine), and
lists accounts with no sign-in for 24 months. DR-004 requires those users to be
notified first: email them, wait, then run with --accounts to delete them --
database rows and Supabase sign-in both.

Dry run by default: DATABASE_URL is the live database.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

from app.core.config import get_settings  # noqa: E402
from app.db.session import dispose_engine, get_session_factory  # noqa: E402
from app.services.auth_admin import AuthAdmin  # noqa: E402
from app.services.retention import run_retention  # noqa: E402


async def main(apply: bool, accounts: bool) -> None:
    settings = get_settings()
    if not settings.database_url:
        raise SystemExit("DATABASE_URL is not set in .env")
    if accounts and not apply:
        raise SystemExit("--accounts needs --apply")

    factory = get_session_factory()
    async with factory() as session:
        report = await run_retention(
            session,
            datetime.now(timezone.utc),
            delete_accounts=accounts,
            auth_admin=AuthAdmin(settings) if accounts else None,
        )
        if apply:
            await session.commit()
        else:
            await session.rollback()
    await dispose_engine()

    mode = "applied" if apply else "dry run"
    print(f"[{mode}] scan logs deleted: {report.scan_logs_deleted}")
    print(f"[{mode}] accounts inactive 24+ months: {len(report.inactive_accounts)}")
    for auth_id in report.inactive_accounts:
        print(f"    {auth_id}")
    if accounts:
        print(f"[{mode}] accounts deleted: {report.accounts_deleted}")
    elif report.inactive_accounts:
        print("Notify these users (DR-004), then re-run with --apply --accounts.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the changes")
    parser.add_argument("--accounts", action="store_true", help="also delete inactive accounts")
    args = parser.parse_args()
    asyncio.run(main(args.apply, args.accounts))
