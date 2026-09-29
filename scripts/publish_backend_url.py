"""
Publish the backend's current public address. DEVELOPMENT AND TESTING ONLY.

    python scripts/publish_backend_url.py https://abc123.lhr.life
    python scripts/publish_backend_url.py --watch <tunnel-log-file>

A test build on a phone talks to the laptop through a temporary HTTPS tunnel,
and those addresses change on every reconnect. Rebuilding the app for each new
address takes minutes, and typing it by hand gets old fast.

So the address is written to the `app_config` table in Supabase, which has a
permanent address the app already knows. On startup a test build asks Supabase
where the backend is and follows it (see lib/apiBase.ts).

Nothing secret is published: it is the public URL of a development server.
Reads are open to the anon key; writes need the service role, i.e. this script.

Release builds ignore all of this -- `EXPO_PUBLIC_ALLOW_SERVER_OVERRIDE` is
not set for them, so they use only the address they were built with.
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")

from sqlalchemy import text  # noqa: E402

from app.db.session import dispose_engine, get_session_factory  # noqa: E402

KEY = "backend_url"
URL_PATTERN = re.compile(r"https://[a-z0-9.-]+\.(?:lhr\.life|trycloudflare\.com|serveousercontent\.com|onrender\.com)")


async def publish(url: str) -> None:
    if not url.startswith("https://"):
        raise SystemExit(f"refusing to publish a non-HTTPS address: {url}")
    url = url.rstrip("/")
    factory = get_session_factory()
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO app_config (key, value, updated_at) "
                "VALUES (:k, :v, now()) "
                "ON CONFLICT (key) DO UPDATE SET value = :v, updated_at = now()"
            ),
            {"k": KEY, "v": url},
        )
        await session.commit()
    await dispose_engine()
    print(f"published {KEY} = {url}")


def latest_url_in(log: Path) -> str | None:
    try:
        found = URL_PATTERN.findall(log.read_text(encoding="utf-8", errors="ignore"))
    except FileNotFoundError:
        return None
    return found[-1] if found else None


async def watch(log: Path, interval: float = 5.0) -> None:
    """Re-publish whenever the tunnel log shows a new address."""
    current: str | None = None
    print(f"watching {log} for address changes (Ctrl+C to stop)")
    while True:
        found = latest_url_in(log)
        if found and found != current:
            await publish(found)
            current = found
        time.sleep(interval)


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__.strip().splitlines()[2])
    if args[0] == "--watch":
        asyncio.run(watch(Path(args[1])))
    else:
        asyncio.run(publish(args[0]))
