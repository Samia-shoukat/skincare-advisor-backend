"""
Review catalogue candidates and approve the safe ones into the catalogue.

    python scripts/review_candidates.py
    python scripts/review_candidates.py --show-ineligible   # also browse the blocked ones
    python scripts/review_candidates.py --all               # revisit ones already decided

Run scripts/build_catalogue_candidates.py first. For each approvable product
it shows the routine step, the matrix ingredient and strength, every
ingredient with its kind, anything unrecognised, and warnings. Then:

    [a]pprove  [r]eject  [e]dit  [s]kip  [q]uit

[e]dit takes corrections from the packaging -- a strength the website left
out, an SPF, the right step, the price tier. Approving adds the product to
app/clinical/catalogue/catalogue.v0.json (validated against the matrix first)
and records the decision in app/clinical/catalogue/review_log.json.

The database is not touched. To publish approved products to the live
products table that the app reads, run the existing seed step:

    python scripts/seed_catalogue.py           # what would change
    python scripts/seed_catalogue.py --apply   # write it

See app/services/catalogue_review.py for why approval writes the file rather
than the table.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, ".")

from app.core.config import get_settings  # noqa: E402
from app.services.catalogue_review import DEFAULT_LOG, CatalogueFile, ReviewLog, run_review  # noqa: E402
from app.services.matrix_loader import load_matrix_file  # noqa: E402


def git_user() -> str:
    try:
        name = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        name = ""
    return name or os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--candidates", type=Path, default=Path("ml/catalogue/candidates.json"))
    parser.add_argument("--reviewer", default=None, help="name recorded on decisions (default: git user)")
    parser.add_argument("--all", action="store_true", help="include candidates already decided")
    parser.add_argument("--show-ineligible", action="store_true", help="also show blocked candidates, read-only")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if not args.candidates.exists():
        raise SystemExit(f"{args.candidates} not found. Run scripts/build_catalogue_candidates.py first.")

    settings = get_settings()
    matrix_json = json.loads(Path(settings.rules_matrix_path).read_text(encoding="utf-8"))
    catalogue = CatalogueFile(Path(settings.catalogue_path), load_matrix_file(Path(settings.rules_matrix_path)))
    built = json.loads(args.candidates.read_text(encoding="utf-8"))
    reviewer = args.reviewer or git_user()
    print(f"Reviewer: {reviewer}   catalogue {catalogue.version}   "
          f"candidates built {built['generatedAt']} (lexicon {built['lexiconVersion']})")

    counts = run_review(
        built["candidates"],
        matrix=matrix_json,
        catalogue=catalogue,
        log=ReviewLog(DEFAULT_LOG),
        reviewer=reviewer,
        lexicon_version=built.get("lexiconVersion"),
        include_decided=args.all,
        show_ineligible=args.show_ineligible,
    )

    print(f"\n{counts['approved']} approved, {counts['rejected']} rejected, {counts['skipped']} skipped.")
    if counts["approved"]:
        print(f"Catalogue is now {catalogue.version}. To publish to the live products table:")
        print("  python scripts/seed_catalogue.py           # check what would change")
        print("  python scripts/seed_catalogue.py --apply   # write it")


if __name__ == "__main__":
    main()
