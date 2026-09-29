"""
Load the product catalogue into the database. FR-REC-005, DR-005, DR-006.

    python scripts/seed_catalogue.py            # show what would change
    python scripts/seed_catalogue.py --apply    # write it

Reads app/clinical/catalogue/catalogue.v0.json, validates it against the rules
matrix, and makes the products table match: new products are added, existing
ones updated, and products no longer in the file are DEACTIVATED -- never
deleted, because past routines refer to them (DR-005).

Run after `alembic upgrade head`, and again whenever the catalogue file changes.
Safe to re-run: it converges rather than duplicating.

Dry run by default, because this writes to whatever DATABASE_URL points at, and
in this project that is the live Supabase database.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, ".")

from app.core.config import get_settings  # noqa: E402
from app.db.session import dispose_engine, get_session_factory  # noqa: E402
from app.services.catalogue import load_catalogue_file, sync_catalogue  # noqa: E402
from app.services.matrix_loader import load_matrix_file  # noqa: E402


async def main(apply: bool) -> None:
    settings = get_settings()
    if not settings.database_url:
        raise SystemExit("DATABASE_URL is not set in .env")

    matrix = load_matrix_file(Path(settings.rules_matrix_path))
    catalogue = load_catalogue_file(Path(settings.catalogue_path), matrix)
    print(f"catalogue {catalogue.version}: {len(catalogue.entries)} products, "
          f"validated against matrix {matrix.version}")

    factory = get_session_factory()
    async with factory() as session:
        counts = await sync_catalogue(session, catalogue)
        if apply:
            await session.commit()
        else:
            await session.rollback()

    await dispose_engine()

    verb = "applied" if apply else "would apply (dry run; pass --apply to write)"
    print(f"{verb}: {counts['created']} created, {counts['updated']} updated, "
          f"{counts['deactivated']} deactivated")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the changes")
    asyncio.run(main(parser.parse_args().apply))
