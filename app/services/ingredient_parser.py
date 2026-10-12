"""
Ingredient-list parsing and catalogue proposals. FR-REC-005, DR-006, CON-003.

Turns a scraped product -- name plus the ingredient text its store printed --
into a *proposal* for the product catalogue: which routine step it could fill,
which matrix ingredient it carries, at what strength, and every reason it
cannot be proposed at all. A person approves proposals (scripts/review_
candidates.py); nothing here writes to the catalogue.

## What it decides, and what it never does

It reads. It does not choose ingredients, timings or strengths for anyone --
that is the rules matrix (CON-003). It only answers "is this bottle one of the
things the matrix already asks for?", and answers no whenever it is unsure:

  * an active the matrix does not cover yet (arbutin, retinol, ...): no
  * two actives in one bottle: no -- FR-REC-004 allows one per step
  * a printed strength above every rule's maximum: no -- it could never be
    matched, and approving it would only add a dead entry
  * only "key ingredients" listed: no -- the rest of the bottle is unknown
  * a cleanser with fragrance or an active: no -- the step is a gentle,
    fragrance-free cleanser
  * a sunscreen below SPF 30, or carrying an active: no

Some gaps a reviewer can close from the packaging -- a strength that was not
printed online, an SPF, which kind of moisturiser it is. Those come back as
`needs` rather than blockers.

## Names are matched to the lexicon, never guessed

app/clinical/ingredients/inci_lexicon.v0.json holds every name. Matching is on
word boundaries ("lactic acid" never matches "sodium lactate"). A name close
to a known one -- the scraped data has "Proplene Glycol" and "Stearinc Acid" --
is matched fuzzily and marked as such, so the reviewer sees the guess. A name
matching nothing is listed as unrecognised, for the reviewer to check.
"""

from __future__ import annotations

import difflib
import json
import re
import statistics
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

PARSER_VERSION = "0.1.0"
DEFAULT_LEXICON = Path(__file__).resolve().parents[1] / "clinical" / "ingredients" / "inci_lexicon.v0.json"

# Ingredient kinds, most decisive first. A token naming several things takes
# the most decisive kind -- "Salicylic Acid Sodium lactate" (a missing comma
# in the scraped data) is an active, not an excipient.
KINDS = ("active", "derivative", "non_matrix_active", "fragrance", "uv_filter",
         "supportive", "excipient", "botanical", "unknown")

# Routine steps a product can fill. TREAT carries one matrix active; the other
# three map to the matrix's base-step keys.
CLEANSE, MOISTURISE, PROTECT, TREAT, OTHER, UNKNOWN = (
    "CLEANSE", "MOISTURISE", "PROTECT", "TREAT", "OTHER", "UNKNOWN")

_TYPE_RULES: list[tuple[str, re.Pattern[str]]] = [
    # Not part of a facial routine at all. First, so "body wash" and
    # "hand cream" never reach CLEANSE or MOISTURISE.
    (OTHER, re.compile(r"\b(lip|lips|body|hands?|feet|foot|underarm|patch(es)?|soap|scrub|"
                       r"mask|mist|eye|eyes|pads?|cotton|case|holder)\b")),
    (PROTECT, re.compile(r"\b(sunscreen|sun ?screen|sun ?block|sunblock|spf|uv)\b")),
    (CLEANSE, re.compile(r"\b(cleanser|cleansing|face ?wash|facewash|wash|foam|micellar)\b")),
    (TREAT, re.compile(r"\b(serum|toner|essence|ampoule|treatment|exfoliant|exfoliating|peel|"
                       r"solution|drops|booster|spot)\b")),
    (MOISTURISE, re.compile(r"\b(moisturi[sz]er|moisturi[sz]ing|cream|lotion|emulsion|gel)\b")),
]

BASE_KEY_STEPS = {
    "gentle_cleanser": CLEANSE,
    "moisturiser_light": MOISTURISE,
    "moisturiser_barrier": MOISTURISE,
    "sunscreen_spf30": PROTECT,
}

_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_SPF = re.compile(r"\bspf\s*-?\s*(\d{2,3})", re.IGNORECASE)


# ---------------------------------------------------------------------------
# The lexicon
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Lexicon:
    version: str
    terms: dict[str, tuple[str, str]]   # name -> (kind, canonical)
    patterns: tuple[tuple[re.Pattern[str], str, str, str], ...]  # (regex, name, kind, canonical)
    botanical: re.Pattern[str]
    base_ingredients: frozenset[str]
    barrier_markers: tuple[str, ...]
    light_markers: tuple[str, ...]


def _word(term: str) -> re.Pattern[str]:
    return re.compile(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])")


@lru_cache(maxsize=4)
def load_lexicon(path: Path = DEFAULT_LEXICON) -> Lexicon:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    terms: dict[str, tuple[str, str]] = {}

    def add(names, kind, canonical):
        for name in names:
            terms.setdefault(name.lower(), (kind, canonical))

    for key, names in raw["matrixActives"].items():
        add(names, "active", key)
    for key, names in raw.get("matrixDerivatives", {}).items():
        add(names, "derivative", key)
    for label, names in raw["nonMatrixActives"].items():
        add(names, "non_matrix_active", label)
    add(raw["fragrance"], "fragrance", "fragrance")
    add(raw["uvFilters"], "uv_filter", "uv filter")
    add(raw["supportive"], "supportive", "supportive")
    add(raw["excipients"], "excipient", "excipient")
    add(raw.get("botanicals", []), "botanical", "botanical")

    # Longest names first, so "ethylhexyl salicylate" is found before any
    # shorter name could claim part of it. Families named by pattern (PEG-40,
    # CI 77491, ...) come last.
    patterns = tuple(
        (_word(name), name, kind, canonical)
        for name, (kind, canonical) in sorted(terms.items(), key=lambda t: -len(t[0]))
    ) + tuple(
        (re.compile(pattern), pattern, "excipient", "excipient")
        for pattern in raw.get("excipientPatterns", [])
    )
    return Lexicon(
        version=raw["version"],
        terms=terms,
        patterns=patterns,
        botanical=re.compile(raw["botanicalPattern"]),
        base_ingredients=frozenset(n.lower() for n in raw["baseIngredients"]),
        barrier_markers=tuple(raw["barrierMarkers"]),
        light_markers=tuple(raw["lightMarkers"]),
    )


# ---------------------------------------------------------------------------
# Parsing one ingredient list
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedIngredient:
    raw: str
    kind: str                     # one of KINDS
    canonical: str | None         # matrix key, non-matrix label, or None
    percent: float | None
    matched: str | None = None    # the lexicon name that matched
    fuzzy: bool = False
    also: tuple[str, ...] = ()    # further matrix/non-matrix actives in the same token


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("​", "")
    return re.sub(r"\s+", " ", text.lower()).strip(" .*;:-")


def split_ingredients(text: str) -> list[str]:
    """
    One entry per ingredient. Handles what stores actually print: "(and)"
    chains, "Active: ... - Other ingredients: ..." headers, "and"/"&"
    separators, bullets and semicolons.
    """
    if not text:
        return []
    text = unicodedata.normalize("NFKC", text)
    # "Vitamin E and C" names two vitamins. Split on "and" as-is, it leaves a
    # lone "C" that is then dropped -- hiding an active.
    text = re.sub(r"(?i)\bvit(?:amin)?\.?\s*([a-e])\s*(?:and|&|\+)\s*([a-e])\b", r"vitamin \1, vitamin \2", text)
    text = re.sub(r"(?i)\b(key\s+|active\s+|other\s+|full\s+|inactive\s+)?ingredients?\s*:", ",", text)
    text = re.sub(r"(?i)\b(actives?|others?)\s*:", ",", text)
    text = re.sub(r"(?i)\(\s*and\s*\)", ",", text)
    text = re.sub(r"\s+[-–]\s+", ",", text)              # " - Other ..." (not "PEG-40")
    parts = re.split(r"[,;•·\n]|\s+and\s+|\s+&\s+", text, flags=re.IGNORECASE)
    tokens = []
    for part in parts:
        token = part.strip(" .*\t\r")
        if len(re.sub(r"[^a-zA-Z]", "", token)) >= 2:     # drops "C", "%", stray digits
            tokens.append(token)
    return tokens


def _percent(text: str) -> float | None:
    values = [float(v) for v in _PERCENT.findall(text)]
    # "Niacinamide (5% / 10%)" is sold in two strengths: the higher one is
    # the one a maxPercent rule has to hold for.
    return max(values) if values else None


def parse_ingredient(token: str, lexicon: Lexicon) -> ParsedIngredient:
    text = _normalise(_PERCENT.sub(" ", token))
    found: list[tuple[str, str, str]] = []           # (kind, canonical, name)
    claimed = text
    for pattern, name, kind, canonical in lexicon.patterns:
        if pattern.search(claimed):
            found.append((kind, canonical, name))
            claimed = pattern.sub(" ", claimed)      # a span counts once

    percent = _percent(token)
    if found:
        found.sort(key=lambda f: KINDS.index(f[0]))
        kind, canonical, name = found[0]
        also = tuple(
            c for k, c, _ in found[1:]
            if k in ("active", "derivative", "non_matrix_active") and c != canonical
        )
        return ParsedIngredient(token, kind, canonical, percent, name, False, also)

    # Close to a known name? Only whole entries of a useful length, at a high
    # cutoff: a fuzzy guess at an active is shown to the reviewer as a guess.
    bare = re.sub(r"\([^)]*\)", " ", text).strip()
    if len(bare) >= 6:
        close = difflib.get_close_matches(bare, lexicon.terms.keys(), n=1, cutoff=0.88)
        if close:
            kind, canonical = lexicon.terms[close[0]]
            return ParsedIngredient(token, kind, canonical, percent, close[0], True)

    if lexicon.botanical.search(text):
        return ParsedIngredient(token, "botanical", None, percent)
    return ParsedIngredient(token, "unknown", None, percent)


def parse_ingredients(text: str, lexicon: Lexicon) -> list[ParsedIngredient]:
    return [parse_ingredient(token, lexicon) for token in split_ingredients(text)]


# ---------------------------------------------------------------------------
# A catalogue proposal for one product
# ---------------------------------------------------------------------------


@dataclass
class Proposal:
    """A scraped product, read against the matrix. Mutable: review edits it."""

    source_id: str
    brand: str
    name: str
    price_text: str | None
    price_value: float | None
    image_url: str | None
    skin_type: str | None
    inci: str | None
    product_type: str
    type_evidence: str
    ingredients: list[ParsedIngredient]
    primary: str | None = None            # matrix ingredient key
    percent: float | None = None          # strength of the primary active
    spf: int | None = None
    fragrance: bool = False
    price_tier: str | None = None
    blockers: list[str] = field(default_factory=list)   # cannot be approved
    needs: list[str] = field(default_factory=list)      # approvable once the reviewer supplies it
    warnings: list[str] = field(default_factory=list)   # approvable; worth a look

    @property
    def status(self) -> str:
        if self.blockers:
            return "ineligible"
        return "needs_input" if self.needs else "eligible"

    @property
    def unrecognised(self) -> list[str]:
        return [i.raw for i in self.ingredients if i.kind == "unknown"]

    def catalogue_id(self) -> str:
        return slugify(f"{self.brand} {self.name}")

    def catalogue_entry(self) -> dict[str, Any]:
        """The shape app/services/catalogue.py loads (DR-006), plus provenance."""
        return {
            "id": self.catalogue_id(),
            "brand": self.brand,
            "name": self.name,
            "priceTier": self.price_tier,
            "ingredients": [{"ingredient": self.primary, "percent": self.percent}],
            # Not read by the loader yet. Kept so the app-display phase has the
            # image and price, and the fragrance-allergy filter has its flag,
            # without scraping again.
            "imageUrl": self.image_url,
            "price": self.price_text,
            "fragrance": self.fragrance,
            # For the product ranker: what else is in the bottle, and which
            # skin types the store says it is for (a keyword guess).
            "inci": self.inci,
            "skinType": self.skin_type,
            "source": {"table": "scraped_products", "id": self.source_id,
                       "parserVersion": PARSER_VERSION},
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "sourceId": self.source_id, "brand": self.brand, "name": self.name,
            "price": self.price_text, "priceValue": self.price_value, "imageUrl": self.image_url,
            "skinType": self.skin_type, "inci": self.inci,
            "productType": self.product_type, "typeEvidence": self.type_evidence,
            "ingredients": [
                {"raw": i.raw, "kind": i.kind, "canonical": i.canonical, "percent": i.percent,
                 "matched": i.matched, "fuzzy": i.fuzzy, "also": list(i.also)}
                for i in self.ingredients
            ],
            "primary": self.primary, "percent": self.percent, "spf": self.spf,
            "fragrance": self.fragrance, "priceTier": self.price_tier,
            "status": self.status, "blockers": self.blockers, "needs": self.needs,
            "warnings": self.warnings,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Proposal":
        return cls(
            source_id=d["sourceId"], brand=d["brand"], name=d["name"], price_text=d.get("price"),
            price_value=d.get("priceValue"), image_url=d.get("imageUrl"), skin_type=d.get("skinType"),
            inci=d.get("inci"), product_type=d["productType"], type_evidence=d.get("typeEvidence", ""),
            ingredients=[ParsedIngredient(i["raw"], i["kind"], i.get("canonical"), i.get("percent"),
                                          i.get("matched"), i.get("fuzzy", False), tuple(i.get("also", ())))
                         for i in d.get("ingredients", [])],
            primary=d.get("primary"), percent=d.get("percent"), spf=d.get("spf"),
            fragrance=d.get("fragrance", False), price_tier=d.get("priceTier"),
            blockers=list(d.get("blockers", [])), needs=list(d.get("needs", [])),
            warnings=list(d.get("warnings", [])),
        )


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text[:64].rstrip("-")


def parse_price(text: str | None) -> float | None:
    if not text:
        return None
    match = re.search(r"(\d[\d,]*(?:\.\d+)?)", text)
    return float(match.group(1).replace(",", "")) if match else None


def classify_type(name: str) -> tuple[str, str]:
    """The routine step a product's name says it is, and the word that said so."""
    text = _normalise(name)
    for product_type, pattern in _TYPE_RULES:
        match = pattern.search(text)
        if match:
            return product_type, match.group(0)
    return UNKNOWN, ""


def max_percent_for(matrix: dict[str, Any], key: str) -> float | None:
    """The highest strength any rule allows for `key`. None if no rule caps it."""
    caps = [r["maxPercent"] for r in matrix.get("rules", []) if r["ingredient"] == key and r.get("maxPercent")]
    return max(caps) if caps else None


def propose(row: dict[str, Any], matrix: dict[str, Any], lexicon: Lexicon | None = None) -> Proposal:
    """
    Read one scraped_products row against the matrix.

    `matrix` is the parsed rules_matrix JSON. `row` has the scraped table's
    columns: id, name, brand, price, image_url, skin_type, ingredients.
    """
    lexicon = lexicon or load_lexicon()
    name, inci = (row.get("name") or "").strip(), row.get("ingredients") or ""
    product_type, evidence = classify_type(name)
    ingredients = parse_ingredients(inci, lexicon)
    # A name like "Daily Defence" says nothing; a list of UV filters does.
    if product_type == UNKNOWN and sum(i.kind == "uv_filter" for i in ingredients) >= 2:
        product_type, evidence = PROTECT, "UV filters in the ingredient list"

    p = Proposal(
        source_id=str(row.get("id") or ""), brand=(row.get("brand") or "").strip(), name=name,
        price_text=row.get("price"), price_value=parse_price(row.get("price")),
        image_url=row.get("image_url"), skin_type=row.get("skin_type"), inci=inci or None,
        product_type=product_type, type_evidence=evidence, ingredients=ingredients,
    )

    # --- what is in the bottle --------------------------------------------
    actives: dict[str, float | None] = {}
    others: set[str] = set()
    for item in ingredients:
        found = [(item.kind, item.canonical)] + [
            ("non_matrix_active" if c not in matrix["ingredients"] else "active", c) for c in item.also
        ]
        for kind, canonical in found:
            if kind in ("active", "derivative") and canonical:
                actives[canonical] = _max(actives.get(canonical), item.percent)
                if kind == "derivative":
                    p.warnings.append(f"{item.raw.strip()} is a form of {canonical}, mapped to it: check the rule applies")
                if item.fuzzy:
                    p.warnings.append(f"'{item.raw.strip()}' read as {item.matched} (close spelling)")
            elif kind == "non_matrix_active":
                others.add(canonical)
        if item.kind == "fragrance":
            p.fragrance = True

    # The name often states the strength the list leaves out:
    # "Peachcinamide TXA Serum (6% Niacinamide, 4% TXA)".
    for item in parse_ingredients(re.sub(r"[()+]", ",", name), lexicon):
        if item.kind in ("active", "derivative") and item.canonical in actives and item.percent:
            actives[item.canonical] = _max(actives[item.canonical], item.percent)
        if item.kind == "non_matrix_active":
            others.add(item.canonical)

    spf_values = [int(v) for v in _SPF.findall(f"{name} {inci}")]
    p.spf = max(spf_values) if spf_values else None

    # --- why it cannot be proposed ------------------------------------------
    if product_type == OTHER:
        p.blockers.append(f"not a facial routine product ('{evidence}')")
    if not ingredients:
        p.blockers.append("no ingredient list")
    elif len(ingredients) < 6 or not any(
        (i.matched or "").lower() in lexicon.base_ingredients for i in ingredients
    ):
        # A full ingredient list has a base (water, an oil, glycerin...) and
        # rarely fewer than six entries. "Ceramides, Hyaluronic Acid,
        # Glycerin" is a highlights reel, and what it leaves out could be an
        # active.
        p.blockers.append("only key ingredients are listed: the rest of the bottle is unknown")
    if others:
        p.blockers.append(
            f"contains {', '.join(sorted(others))}, which the rules matrix does not cover yet")
    if len(actives) > 1:
        p.blockers.append(
            f"contains two actives ({', '.join(sorted(actives))}): a routine step allows one")

    single = next(iter(actives)) if len(actives) == 1 else None
    if product_type == CLEANSE:
        if actives:
            p.blockers.append(f"an active cleanser ({', '.join(sorted(actives))}): the step is a gentle cleanser")
        if p.fragrance:
            p.blockers.append("contains fragrance: the step is a fragrance-free cleanser")
        p.primary = "gentle_cleanser"
    elif product_type == PROTECT:
        if actives:
            p.blockers.append(f"also contains {', '.join(sorted(actives))}, an active: one active per step")
        if p.spf is None:
            p.needs.append("spf")
        elif p.spf < 30:
            p.blockers.append(f"SPF {p.spf} is below 30")
        p.primary = "sunscreen_spf30"
    elif single and product_type in (TREAT, MOISTURISE, UNKNOWN):
        p.primary, p.percent = single, actives[single]
        if product_type != TREAT:
            p.warnings.append(
                f"named as a {product_type.lower()} product but carries {single}: "
                f"it would fill the {single} step, not the moisturiser")
        _check_strength(p, matrix)
    elif product_type == MOISTURISE and not actives:
        text = f"{name} {inci}".lower()
        barrier = [m for m in lexicon.barrier_markers if m in text]
        light = [m for m in lexicon.light_markers if m in name.lower()]
        p.primary = "moisturiser_barrier" if barrier and not light else "moisturiser_light"
        p.warnings.append(
            f"{p.primary.replace('_', ' ')} chosen from "
            f"{', '.join(barrier or light) or 'no clear sign'}: check the texture")
    elif product_type == TREAT and not actives:
        p.blockers.append("a treatment with no active the matrix uses: there is no step for it")
    elif product_type == UNKNOWN:
        # The reviewer can place it: [e]dit -> step.
        p.needs.append("type")

    if p.fragrance and product_type != CLEANSE:
        p.warnings.append("contains fragrance")
    if p.unrecognised:
        p.warnings.append(f"{len(p.unrecognised)} unrecognised ingredient(s): check them")
    return p


def _check_strength(p: Proposal, matrix: dict[str, Any]) -> None:
    cap = max_percent_for(matrix, p.primary)
    if p.percent is None:
        if cap is not None:
            p.needs.append("strength")
    elif cap is not None and p.percent > cap:
        p.blockers.append(
            f"{p.percent:g}% {p.primary} is above the {cap:g}% maximum every rule allows")


def _max(a: float | None, b: float | None) -> float | None:
    values = [v for v in (a, b) if v is not None]
    return max(values) if values else None


def apply_edit(p: Proposal, matrix: dict[str, Any], *, percent: float | None = None,
               spf: int | None = None, primary: str | None = None, tier: str | None = None) -> None:
    """A reviewer's correction from the packaging. Re-checks what it touches."""
    if primary is not None:
        if primary not in matrix["ingredients"]:
            raise ValueError(f"{primary!r} is not a matrix ingredient")
        p.primary = primary
        p.needs = [n for n in p.needs if n != "type"]
        step = BASE_KEY_STEPS.get(primary)
        if step:
            # Placing a product on a base step applies that step's checks.
            p.product_type = step
            if step == CLEANSE and p.fragrance:
                p.blockers.append("contains fragrance: the step is a fragrance-free cleanser")
            if step == PROTECT and p.spf is None and "spf" not in p.needs:
                p.needs.append("spf")
        else:
            p.product_type = TREAT
            if p.percent is None and "strength" not in p.needs:
                _check_strength(p, matrix)
    if percent is not None:
        p.percent = percent
        p.needs = [n for n in p.needs if n != "strength"]
        p.blockers = [b for b in p.blockers if "maximum every rule allows" not in b]
        _check_strength(p, matrix)
    if spf is not None:
        p.spf = spf
        p.needs = [n for n in p.needs if n != "spf"]
        if spf < 30:
            p.blockers.append(f"SPF {spf} is below 30")
    if tier is not None:
        if tier not in ("BUDGET", "PREMIUM"):
            raise ValueError("tier must be BUDGET or PREMIUM")
        p.price_tier = tier


def assign_price_tiers(proposals: list[Proposal]) -> None:
    """
    BUDGET at or below the median price of the same kind of product, PREMIUM
    above it. Per kind, because a sunscreen and a serum are not priced alike.
    Ineligible proposals are left out, so they do not move the median.
    """
    groups: dict[str, list[Proposal]] = {}
    for p in proposals:
        if p.status != "ineligible" and p.price_value is not None:
            groups.setdefault(p.primary or p.product_type, []).append(p)
    everyone = [p.price_value for g in groups.values() for p in g]
    overall = statistics.median(everyone) if everyone else None
    for members in groups.values():
        prices = [p.price_value for p in members]
        median = statistics.median(prices) if len(prices) > 1 else overall
        for p in members:
            p.price_tier = "BUDGET" if median is None or p.price_value <= median else "PREMIUM"
