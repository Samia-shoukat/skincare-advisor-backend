"""
Product ranking: which permitted product to suggest for a routine step.
FR-REC-001, FR-REC-005, ADR-030.

## Where it sits

The rules matrix decides each step -- ingredient, strength, timing (CON-003).
app/services/catalogue.py filters the catalogue down to the products that are
safe for that step and this user (its six conditions). Of what remains, this
module chooses the order. Before it existed the order was the product id --
an alphabetical accident.

It cannot make a step less safe. It never sees a product the filter refused,
and the one filter it adds -- a declared fragrance allergy -- only removes.

## How a product is scored

1. **Fragrance allergy is a filter, not a score.** A product with fragrance,
   or whose fragrance is unknown, is not offered to a user who reports an
   allergy. An allergy is not a preference to be outweighed by a good match.
2. **Ingredient match -- the NLP part.** Each product's printed ingredient
   list is read through the INCI lexicon into normalised terms (synonyms
   folded: "sodium hyaluronate" is "hyaluronic acid"), and weighted by TF-IDF
   across the catalogue: a term nearly every product has, like glycerin, says
   little; a distinctive one says more. The user's concerns and skin type
   become a query in the same space -- ceramides and squalane for dryness,
   centella and panthenol for redness. The score is their cosine similarity.
3. **Small, named adjustments** (weights in ranking.v0.json):
   the store lists the user's skin type or "All" (+), lists only other types
   (-); for sensitive skin, fragrance (-), unknown fragrance (- less), known
   irritants (-, capped); an ingredient list on file at all (+).
4. **Ties break by product id**, so the same catalogue and the same user
   always produce the same routine (FR-REC-001).

Products with no ingredient list on file -- the curated catalogue, for now --
score zero on 2 and 3. Among themselves they keep the old id order, which is
why switching ranking on changed none of the existing routine tests.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.services.ingredient_parser import load_lexicon, parse_ingredients

RANKER_VERSION = "1.0.0"
DEFAULT_PROFILE = Path(__file__).resolve().parents[1] / "clinical" / "ingredients" / "ranking.v0.json"

_SKIN_TYPE_WORDS = ("oily", "dry", "combination", "normal")


@dataclass(frozen=True)
class Preferences:
    """What ranking may know about the user. No safety flags: those are the filter's."""

    skin_type: str | None = None              # SkinType value, e.g. "COMBINATION"
    concerns: frozenset[str] = frozenset()    # Concern values from the scan
    sensitive_skin: bool | None = None
    fragrance_allergy: bool | None = None


@dataclass(frozen=True)
class RankingProfile:
    version: str
    synonyms: dict[str, str]
    concern_terms: dict[str, tuple[str, ...]]
    skin_type_terms: dict[str, tuple[str, ...]]
    irritants: tuple[str, ...]
    weights: dict[str, float]


@lru_cache(maxsize=4)
def load_profile(path: Path = DEFAULT_PROFILE) -> RankingProfile:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return RankingProfile(
        version=raw["version"],
        synonyms={k.lower(): v.lower() for k, v in raw["synonyms"].items()},
        concern_terms={k: tuple(v) for k, v in raw["concernTerms"].items()},
        skin_type_terms={k: tuple(v) for k, v in raw["skinTypeTerms"].items()},
        irritants=tuple(raw["irritants"]),
        weights={k: float(v) for k, v in raw["weights"].items()},
    )


def preference_permitted(product: Any, prefs: Preferences | None) -> bool:
    """The one filter ranking adds. Removes only; see the module docstring."""
    if prefs is None or not prefs.fragrance_allergy:
        return True
    return product.fragrance is False


@lru_cache(maxsize=512)
def ingredient_terms(inci: str | None, profile_version: str = "") -> frozenset[str]:
    """
    The normalised ingredient terms of one printed list. Lexicon names where
    the lexicon knows the ingredient, the cleaned printed name where it does
    not, synonyms folded. Cached: a catalogue's lists do not change per scan.
    """
    if not inci:
        return frozenset()
    profile = load_profile()
    terms = set()
    for item in parse_ingredients(inci, load_lexicon()):
        name = item.matched or re.sub(r"\([^)]*\)|\d+(\.\d+)?\s*%", " ", item.raw.lower())
        name = re.sub(r"\s+", " ", name).strip(" .*-")
        if name:
            terms.add(profile.synonyms.get(name, name))
    return frozenset(terms)


class ProductRanker:
    """Built once per routine over the active catalogue, then asked per step."""

    def __init__(self, catalogue: Sequence[Any], profile: RankingProfile | None = None) -> None:
        self.profile = profile or load_profile()
        self._terms = {p.id: ingredient_terms(getattr(p, "inci", None), self.profile.version) for p in catalogue}
        documents = [t for t in self._terms.values() if t]
        self._n = len(documents)
        df: dict[str, int] = {}
        for terms in documents:
            for term in terms:
                df[term] = df.get(term, 0) + 1
        # Smoothed IDF: a term in every document still weighs a little, and
        # one in none (a query term no product has) weighs the most.
        self._idf = {term: math.log((self._n + 1) / (count + 1)) + 1 for term, count in df.items()}
        self._idf_unseen = math.log(self._n + 1) + 1

    def idf(self, term: str) -> float:
        return self._idf.get(term, self._idf_unseen)

    # -- the query ------------------------------------------------------------

    def query(self, prefs: Preferences) -> dict[str, float]:
        """The user's profile as weighted ingredient terms."""
        wanted: dict[str, float] = {}
        for concern in sorted(prefs.concerns):
            for term in self.profile.concern_terms.get(concern, ()):
                wanted[term] = wanted.get(term, 0.0) + 1.0
        for term in self.profile.skin_type_terms.get(prefs.skin_type or "", ()):
            wanted[term] = wanted.get(term, 0.0) + 0.5
        return {term: weight * self.idf(term) for term, weight in wanted.items()}

    # -- scoring ----------------------------------------------------------------

    def similarity(self, product: Any, query: dict[str, float]) -> float:
        terms = self._terms.get(product.id) or ingredient_terms(getattr(product, "inci", None))
        if not terms or not query:
            return 0.0
        vector = {term: self.idf(term) for term in terms}       # binary TF x IDF
        dot = sum(weight * vector.get(term, 0.0) for term, weight in query.items())
        norm = math.sqrt(sum(v * v for v in vector.values())) * math.sqrt(sum(v * v for v in query.values()))
        return dot / norm if norm else 0.0

    def score(self, product: Any, prefs: Preferences, query: dict[str, float] | None = None) -> tuple[float, list[str]]:
        """The score and the named reasons that produced it."""
        w = self.profile.weights
        query = self.query(prefs) if query is None else query
        reasons: list[str] = []

        match = self.similarity(product, query)
        total = w["ingredientMatch"] * match
        if match > 0:
            reasons.append(f"ingredient match {match:.2f}")

        fit = _skin_type_fit(getattr(product, "skin_types", None), prefs.skin_type)
        if fit > 0:
            total += w["skinTypeMatch"]
            reasons.append("suits this skin type")
        elif fit < 0:
            total += w["skinTypeMismatch"]
            reasons.append("listed for other skin types")

        if prefs.sensitive_skin:
            if product.fragrance is True:
                total += w["fragranceSensitive"]
                reasons.append("fragrance (sensitive skin)")
            elif product.fragrance is None:
                total += w["fragranceUnknownSensitive"]
            text = (getattr(product, "inci", None) or "").lower()
            hits = [i for i in self.profile.irritants if re.search(r"(?<![a-z])" + re.escape(i) + r"(?![a-z])", text)]
            if hits:
                total += max(w["irritantCap"], w["irritant"] * len(hits))
                reasons.append(f"irritants: {', '.join(hits)}")

        if self._terms.get(product.id):
            total += w["ingredientListOnFile"]
        return total, reasons

    def rank(self, products: Iterable[Any], prefs: Preferences) -> list[Any]:
        """Best first; ties by product id (FR-REC-001)."""
        query = self.query(prefs)
        scored = [(round(self.score(p, prefs, query)[0], 9), p) for p in products]
        scored.sort(key=lambda item: (-item[0], item[1].id))
        return [p for _, p in scored]


def _skin_type_fit(listed: str | None, skin_type: str | None) -> int:
    """+1 the store lists this type or "All"; -1 it lists only others; 0 unknown."""
    if not listed or not skin_type:
        return 0
    text = listed.lower()
    if "all" in re.findall(r"[a-z]+", text) or skin_type.lower() in text:
        return 1
    return -1 if any(word in text for word in _SKIN_TYPE_WORDS) else 0
