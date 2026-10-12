"""
Reviewing catalogue candidates. Used by scripts/review_candidates.py.

A person approves every product that enters the catalogue. This module is the
mechanics of that: showing a candidate, applying the reviewer's corrections,
and -- on approval -- adding it to the catalogue file and recording the
decision.

## The file, not the table

The live products table is made to match app/clinical/catalogue/
catalogue.v0.json by scripts/seed_catalogue.py, which DEACTIVATES anything not
in the file. So approval writes to the file. A row written straight into the
table would be switched off by the next seed; the file is the source of truth,
in git, versioned, reviewed like the matrix.

Every write is validated by the same loader the seed script uses (DR-006)
before it replaces the file, so an approval can never leave the catalogue in a
state the seed would reject.

## The log

app/clinical/catalogue/review_log.json records every decision -- who, when,
what was approved or rejected and why, what the reviewer corrected, and which
parser, lexicon and catalogue versions were involved. A product's presence in
the catalogue can always be traced to the person who let it in.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.services.catalogue import load_catalogue_file
from app.services.ingredient_parser import PARSER_VERSION, Proposal, apply_edit, max_percent_for

DEFAULT_LOG = Path("app/clinical/catalogue/review_log.json")

# Kinds worth naming in the ingredient line; plain excipients are listed bare.
_TAGS = {
    "active": "ACTIVE", "derivative": "ACTIVE, derivative", "non_matrix_active": "NOT IN MATRIX",
    "fragrance": "fragrance", "uv_filter": "UV filter", "supportive": "supportive",
    "botanical": "botanical", "unknown": "unrecognised",
}


def bump_version(version: str) -> str:
    """'1.0.0-pk' -> '1.1.0-pk'. Adding products is a minor change."""
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(.*)", version)
    if not match:
        return f"{version}.1"
    major, minor, _patch, suffix = match.groups()
    return f"{major}.{int(minor) + 1}.0{suffix}"


class CatalogueFile:
    """The catalogue JSON, appended to one approval at a time."""

    def __init__(self, path: Path, matrix) -> None:
        self.path = path
        self.matrix = matrix  # the loaded Matrix, for DR-006 validation
        self.data = json.loads(path.read_text(encoding="utf-8"))
        self.bumped = False

    @property
    def version(self) -> str:
        return self.data["version"]

    def ids(self) -> set[str]:
        return {p["id"] for p in self.data["products"]}

    def add(self, entry: dict[str, Any]) -> None:
        if entry["id"] in self.ids():
            raise ValueError(f"{entry['id']} is already in the catalogue")
        previous_version = self.data["version"]
        self.data["products"].append(entry)
        if not self.bumped:
            self.data["version"] = bump_version(previous_version)
        try:
            self._write()
        except Exception:
            self.data["products"].pop()
            self.data["version"] = previous_version
            raise
        self.bumped = True

    def _write(self) -> None:
        text = json.dumps(self.data, indent=2, ensure_ascii=False) + "\n"
        staged = self.path.with_name(self.path.name + ".staged")
        staged.write_text(text, encoding="utf-8")
        try:
            load_catalogue_file(staged, self.matrix)   # what the seed script will check
        except Exception:
            staged.unlink(missing_ok=True)
            raise
        staged.replace(self.path)


class ReviewLog:
    def __init__(self, path: Path = DEFAULT_LOG) -> None:
        self.path = path
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))
        else:
            self.data = {
                "_about": [
                    "Every catalogue review decision, newest last. Written by",
                    "scripts/review_candidates.py; do not edit by hand.",
                ],
                "decisions": [],
            }

    def decided(self) -> dict[str, dict[str, Any]]:
        return {d["sourceId"]: d for d in self.data["decisions"]}

    def record(self, p: Proposal, decision: str, *, reviewer: str, reason: str | None = None,
               edits: list[str] | None = None, catalogue_version: str | None = None,
               lexicon_version: str | None = None) -> None:
        self.data["decisions"].append({
            "sourceId": p.source_id,
            "catalogueId": p.catalogue_id(),
            "brand": p.brand,
            "name": p.name,
            "decision": decision,
            "reason": reason or None,
            "primary": p.primary,
            "percent": p.percent,
            "priceTier": p.price_tier,
            "edits": edits or [],
            "reviewer": reviewer,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "catalogueVersion": catalogue_version,
            "parserVersion": PARSER_VERSION,
            "lexiconVersion": lexicon_version,
        })
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def describe(p: Proposal, matrix: dict[str, Any], position: str = "") -> list[str]:
    """What the reviewer sees for one candidate."""
    lines = ["-" * 78, f"{position}  {p.brand} -- {p.name}    {p.price_text or 'no price'}"]

    label = (matrix["ingredients"].get(p.primary) or {}).get("label") if p.primary else None
    step = f"{p.product_type} -> {p.primary}" if p.primary else p.product_type
    if label:
        step += f"  ({label})"
    lines.append(f"  Step         {step}")
    if p.primary and (cap := max_percent_for(matrix, p.primary)) is not None:
        stated = f"{p.percent:g}%" if p.percent is not None else "NOT STATED"
        lines.append(f"  Strength     {stated}   (rules allow up to {cap:g}%)")
    if p.product_type == "PROTECT" or p.primary == "sunscreen_spf30":
        lines.append(f"  SPF          {p.spf if p.spf is not None else 'NOT STATED'}")
    lines.append(f"  Price tier   {p.price_tier or 'not set'}")
    if p.skin_type:
        lines.append(f"  Skin type    {p.skin_type} (guessed from the store's text)")
    lines.append(f"  Fragrance    {'yes' if p.fragrance else 'none listed'}")
    if p.image_url:
        lines.append(f"  Image        {p.image_url}")

    listed = []
    for item in p.ingredients:
        tag = _TAGS.get(item.kind)
        text = item.raw.strip()
        if item.fuzzy:
            text += f" (read as {item.matched})"
        listed.append(f"{text} [{tag}]" if tag else text)
    lines.append("  Ingredients  " + "; ".join(listed) if listed else "  Ingredients  none")

    for title, items in (("Blocked", p.blockers), ("Needs", p.needs), ("Warnings", p.warnings)):
        for n, item in enumerate(items):
            lines.append(f"  {title if n == 0 else '':<12} - {item}")
    return lines


Ask = Callable[[str], str]
Say = Callable[[str], None]


def _edit(p: Proposal, matrix: dict[str, Any], ask: Ask, say: Say) -> list[str]:
    """One round of corrections. Returns what was changed, for the log."""
    changes: list[str] = []
    while True:
        choice = ask("  Edit: [s]trength  s[p]f  [i]ngredient/step  [t]ier  [d]one > ").strip().lower()
        try:
            if choice == "s":
                value = float(ask("    strength in % (from the packaging): ").strip().rstrip("%"))
                apply_edit(p, matrix, percent=value)
                changes.append(f"strength={value:g}%")
            elif choice == "p":
                value = int(ask("    SPF (from the packaging): ").strip())
                apply_edit(p, matrix, spf=value)
                changes.append(f"spf={value}")
            elif choice == "i":
                keys = list(matrix["ingredients"])
                for n, key in enumerate(keys, 1):
                    say(f"    {n:2}. {key:<20} {matrix['ingredients'][key]['label']}")
                index = int(ask("    number: ").strip()) - 1
                if not 0 <= index < len(keys):
                    raise ValueError("no such number")
                apply_edit(p, matrix, primary=keys[index])
                changes.append(f"primary={keys[index]}")
            elif choice == "t":
                value = ask("    tier [b]udget / [p]remium: ").strip().lower()
                tier = {"b": "BUDGET", "p": "PREMIUM"}.get(value[:1])
                if tier is None:
                    raise ValueError("enter b or p")
                apply_edit(p, matrix, tier=tier)
                changes.append(f"tier={tier}")
            elif choice in ("d", ""):
                return changes
            else:
                say("    ?")
        except ValueError as exc:
            say(f"    not changed: {exc}")


def run_review(
    candidates: list[dict[str, Any]],
    *,
    matrix: dict[str, Any],
    catalogue: CatalogueFile,
    log: ReviewLog,
    reviewer: str,
    lexicon_version: str | None = None,
    include_decided: bool = False,
    show_ineligible: bool = False,
    ask: Ask = input,
    say: Say = print,
) -> dict[str, int]:
    """The interactive loop. Returns counts of what happened."""
    decided = log.decided()
    in_catalogue = catalogue.ids()
    queue = []
    for raw in candidates:
        p = Proposal.from_dict(raw)
        if p.catalogue_id() in in_catalogue:
            continue
        if not include_decided and p.source_id in decided:
            continue
        if p.status == "ineligible" and not show_ineligible:
            continue
        queue.append(p)

    counts = {"approved": 0, "rejected": 0, "skipped": 0}
    if not queue:
        say("Nothing to review.")
        return counts

    for n, p in enumerate(queue, 1):
        for line in describe(p, matrix, f"[{n}/{len(queue)}]"):
            say(line)
        if p.status == "ineligible":
            if ask("  (blocked: read-only)  [n]ext  [q]uit > ").strip().lower() == "q":
                break
            continue

        edits: list[str] = []
        while True:
            choice = ask("  [a]pprove  [r]eject  [e]dit  [s]kip  [q]uit > ").strip().lower()
            if choice == "a":
                if p.status != "eligible" or not p.primary or not p.price_tier:
                    missing = p.blockers or [f"needs {need}" for need in p.needs] or ["no step or price tier"]
                    say(f"  Cannot approve yet: {'; '.join(missing)}. Use [e]dit.")
                    continue
                try:
                    catalogue.add(p.catalogue_entry())
                except Exception as exc:  # noqa: BLE001 -- shown to the reviewer, file untouched
                    say(f"  Not added: {exc}")
                    continue
                log.record(p, "approved", reviewer=reviewer, edits=edits,
                           catalogue_version=catalogue.version, lexicon_version=lexicon_version)
                counts["approved"] += 1
                say(f"  Approved -> {p.catalogue_id()} (catalogue {catalogue.version})")
                break
            if choice == "r":
                reason = ask("  reason (optional): ").strip()
                log.record(p, "rejected", reviewer=reviewer, reason=reason, edits=edits,
                           lexicon_version=lexicon_version)
                counts["rejected"] += 1
                break
            if choice == "e":
                edits += _edit(p, matrix, ask, say)
                for line in describe(p, matrix, f"[{n}/{len(queue)}]")[1:]:
                    say(line)
                continue
            if choice == "s":
                counts["skipped"] += 1
                break
            if choice == "q":
                return counts
            say("  ?")
    return counts
