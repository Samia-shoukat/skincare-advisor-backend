"""
Approve every catalogue candidate that passed all of the parser's checks.

    python scripts/auto_approve_candidates.py --dry-run   # list what would be approved
    python scripts/auto_approve_candidates.py             # approve them

The non-interactive form of scripts/review_candidates.py. A candidate is
approved only when its status is "eligible": every check passed and nothing
needs a person -- the same bar the manual [a]pprove applies. Each approval is
validated against the matrix before it is written (DR-006) and logged with
"auto" in the reviewer field, so the log shows how it got in.

## What it never does: invent a strength

A candidate marked "needs strength" is SKIPPED, not given a default. The
matrix caps every active (niacinamide 5%, salicylic acid 2%, adapalene 0.1%,
...), and the cap is a safety rule. Writing a default strength would record a
number nobody checked: a 10% serum would enter the catalogue labelled 5% and be
recommended as if it were within the rule. Read the strength off the packaging
and approve those with scripts/review_candidates.py ([e]dit -> [s]trength).

Then publish with scripts/seed_catalogue.py --apply, as for manual approvals.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")

from app.core.config import get_settings  # noqa: E402
from app.services.catalogue_review import DEFAULT_LOG, CatalogueFile, ReviewLog  # noqa: E402
from app.services.ingredient_parser import Proposal  # noqa: E402
from app.services.matrix_loader import load_matrix_file  # noqa: E402
from scripts.review_candidates import git_user  # noqa: E402


def auto_approve(candidates: list[dict], catalogue: CatalogueFile, log: ReviewLog, *,
                 reviewer: str, lexicon_version: str | None = None, dry_run: bool = False):
    """Approve the eligible; skip the rest. Returns (approved, skipped, failed)."""
    decided, in_catalogue = log.decided(), catalogue.ids()
    approved, skipped, failed = [], [], []
    for raw in candidates:
        p = Proposal.from_dict(raw)
        if p.status == "ineligible" or p.catalogue_id() in in_catalogue or p.source_id in decided:
            continue
        if p.status == "needs_input":
            skipped.append((p, ", ".join(p.needs)))
            continue
        if dry_run:
            approved.append(p)
            continue
        try:
            catalogue.add(p.catalogue_entry())
        except Exception as exc:  # noqa: BLE001 -- reported; the file is left as it was
            failed.append((p, str(exc)))
            continue
        log.record(p, "approved", reviewer=reviewer, catalogue_version=catalogue.version,
                   lexicon_version=lexicon_version)
        approved.append(p)
    return approved, skipped, failed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--candidates", type=Path, default=Path("ml/catalogue/candidates.json"))
    parser.add_argument("--dry-run", action="store_true", help="list, write nothing")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not args.candidates.exists():
        raise SystemExit(f"{args.candidates} not found. Run scripts/build_catalogue_candidates.py first.")

    settings = get_settings()
    built = json.loads(args.candidates.read_text(encoding="utf-8"))
    catalogue = CatalogueFile(Path(settings.catalogue_path), load_matrix_file(Path(settings.rules_matrix_path)))
    approved, skipped, failed = auto_approve(
        built["candidates"], catalogue, ReviewLog(DEFAULT_LOG),
        reviewer=f"{git_user()} (auto: all parser checks passed)",
        lexicon_version=built.get("lexiconVersion"), dry_run=args.dry_run,
    )

    verb = "Would approve" if args.dry_run else "Approved"
    print(f"{verb} {len(approved)}:")
    for p in approved:
        strength = f" {p.percent:g}%" if p.percent else ""
        print(f"  {p.brand} -- {p.name} -> {p.primary}{strength} [{p.price_tier}]")
    if skipped:
        print(f"\nSkipped {len(skipped)} that need a value read off the packaging "
              f"(approve with scripts/review_candidates.py):")
        for p, needs in skipped:
            print(f"  {p.brand} -- {p.name} -> {p.primary}: needs {needs}")
    for p, error in failed:
        print(f"\nNOT added: {p.brand} -- {p.name}: {error}")
    if approved and not args.dry_run:
        print(f"\nCatalogue is now {catalogue.version}. Publish: python scripts/seed_catalogue.py --apply")


if __name__ == "__main__":
    main()
