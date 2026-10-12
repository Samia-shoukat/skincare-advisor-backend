"""
Read scraped products against the rules matrix and write catalogue candidates.

    python scripts/build_catalogue_candidates.py
    python scripts/build_catalogue_candidates.py --from-json scraped_products.json

Reads the `scraped_products` table (or a JSON export of it), runs every row
through app/services/ingredient_parser.py, and writes ml/catalogue/
candidates.json for scripts/review_candidates.py. Prints what can be approved,
what needs a value from the packaging, and why the rest cannot.

Read-only: nothing is written to the database or the catalogue. Approving is
the review script's job, and a person's.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, ".")

from app.core.config import get_settings  # noqa: E402
from app.services.ingredient_parser import (  # noqa: E402
    PARSER_VERSION,
    assign_price_tiers,
    load_lexicon,
    propose,
)

DEFAULT_OUT = Path("ml/catalogue/candidates.json")
COLUMNS = "id, name, brand, price, image_url, skin_type, ingredients"


async def read_table() -> list[dict]:
    from sqlalchemy import text

    from app.db.session import dispose_engine, get_session_factory

    async with get_session_factory()() as session:
        rows = (await session.execute(text(f"select {COLUMNS} from scraped_products order by brand, name"))).mappings()
        result = [dict(r) for r in rows]
    await dispose_engine()
    return result


def build(rows: list[dict], matrix: dict, catalogue_ids: set[str]) -> dict:
    lexicon = load_lexicon()
    proposals = [propose({**row, "id": str(row["id"])}, matrix, lexicon) for row in rows]
    assign_price_tiers(proposals)

    candidates = []
    for p in proposals:
        entry = p.to_dict()
        entry["catalogueId"] = p.catalogue_id()
        entry["alreadyInCatalogue"] = p.catalogue_id() in catalogue_ids
        candidates.append(entry)
    order = {"eligible": 0, "needs_input": 1, "ineligible": 2}
    candidates.sort(key=lambda c: (order[c["status"]], c["brand"], c["name"]))

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "parserVersion": PARSER_VERSION,
        "lexiconVersion": lexicon.version,
        "matrixVersion": matrix.get("version"),
        "counts": dict(Counter(c["status"] for c in candidates)),
        "candidates": candidates,
    }


def report(result: dict) -> None:
    candidates = result["candidates"]
    print(f"\n{len(candidates)} scraped products read against matrix {result['matrixVersion']} "
          f"(parser {result['parserVersion']}, lexicon {result['lexiconVersion']})")
    for status in ("eligible", "needs_input", "ineligible"):
        print(f"  {status:<12} {result['counts'].get(status, 0)}")

    print("\nApprovable:")
    for c in candidates:
        if c["status"] == "ineligible":
            continue
        strength = f" {c['percent']:g}%" if c["percent"] else ""
        needs = f"  needs: {', '.join(c['needs'])}" if c["needs"] else ""
        done = "  (already in catalogue)" if c["alreadyInCatalogue"] else ""
        print(f"  {c['brand'][:16]:<16} {c['name'][:40]:<40} -> {c['primary']}{strength} "
              f"[{c['priceTier']}]{needs}{done}")

    reasons = Counter(
        b.split(":")[0].split(" (")[0] for c in candidates if c["status"] == "ineligible" for b in c["blockers"]
    )
    print("\nWhy the rest cannot be approved (a product can have several reasons):")
    for reason, n in reasons.most_common():
        print(f"  {n:3}  {reason}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--from-json", type=Path, help="read a JSON export instead of the database")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    settings = get_settings()
    matrix = json.loads(Path(settings.rules_matrix_path).read_text(encoding="utf-8"))
    catalogue = json.loads(Path(settings.catalogue_path).read_text(encoding="utf-8"))
    catalogue_ids = {p["id"] for p in catalogue["products"]}

    if args.from_json:
        rows = json.loads(args.from_json.read_text(encoding="utf-8"))
        rows = [{"id": r.get("id") or f"json-{i}", **r} for i, r in enumerate(rows)]
    else:
        rows = asyncio.run(read_table())

    result = build(rows, matrix, catalogue_ids)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    report(result)
    print(f"\nWrote {args.out}. Next: python scripts/review_candidates.py")


if __name__ == "__main__":
    main()
