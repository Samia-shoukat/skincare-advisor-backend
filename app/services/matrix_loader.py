"""
Appendix F loader. FR-REC-006, DR-007, CON-003.

The rules matrix is the only place clinical judgement lives (CON-003). This
module reads it, refuses it if it is malformed, and hands the rules engine a
typed, validated object. It makes no clinical decision of its own.

## Validation happens at load, not at use

A matrix with a rule pointing at an ingredient that does not exist, or a rule
with no source, is refused when it is loaded -- not discovered when the first
user with that concern scans. DR-007 sets a 100% completeness threshold on
`source` and `reviewStatus`, and the only way to hold a threshold of 100% is to
refuse anything below it.

## Remote matrix and fallback (FR-REC-006)

When `RULES_MATRIX_URL` is set, the matrix is fetched from it and cached for
`RULES_MATRIX_CACHE_TTL_SECONDS`. If a fetch fails -- network down, bad JSON, a
matrix that fails validation -- the last good matrix is used and the fallback is
recorded. With no URL (the development default) the local file is used.

A remote matrix that fails validation is treated exactly like an unreachable
one. Publishing a broken matrix must not take routine generation down; it must
leave the previous rules in force.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from app.core.config import Settings
from app.core.enums import Concern, ReviewStatus, SkinType

logger = logging.getLogger(__name__)

TIMINGS = ("AM", "PM")
FREQUENCIES = ("DAILY", "ALTERNATE_DAYS", "TWICE_WEEKLY")
STEPS = ("CLEANSE", "TREAT", "MOISTURISE", "PROTECT")


class MatrixInvalid(ValueError):
    """The matrix is malformed. Raised at load, never at scan time."""


# ---------------------------------------------------------------------------
# Typed matrix
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ingredient:
    key: str
    label: str
    is_active: bool
    minor_restricted: bool
    pregnancy_restricted: bool
    incompatible_with: frozenset[str]


@dataclass(frozen=True)
class BaseStep:
    """A step every routine gets: cleanse, moisturise, protect."""

    rule_id: str
    step: str
    timing: tuple[str, ...]
    ingredient_by_skin_type: dict[SkinType, str]
    frequency: str
    source: str
    review_status: ReviewStatus


@dataclass(frozen=True)
class ConcernPolicy:
    """
    Priority 1 is the highest. FR-REC-001: a higher-priority concern's
    prohibitions apply before any lower-priority concern's permissions, which is
    how barrier repair outranks active treatment.
    """

    concern: Concern
    priority: int
    prohibits: frozenset[str]


@dataclass(frozen=True)
class Rule:
    """One permitted treatment for one concern. Listed in preference order."""

    rule_id: str
    concern: Concern
    ingredient: str
    max_percent: float
    timing: str
    frequency: str
    source: str
    review_status: ReviewStatus


@dataclass(frozen=True)
class Matrix:
    version: str
    max_actives_am: int
    max_actives_pm: int
    ingredients: dict[str, Ingredient]
    base_steps: tuple[BaseStep, ...]
    concerns: dict[Concern, ConcernPolicy]
    prescription_exclusions: frozenset[str]
    rules: tuple[Rule, ...]

    def rules_for(self, concern: Concern) -> tuple[Rule, ...]:
        """Rules for one concern, in the order the matrix lists them."""
        return tuple(r for r in self.rules if r.concern is concern)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_matrix(payload: Any) -> Matrix:
    """Validate a decoded matrix and return it typed. Raises MatrixInvalid."""
    if not isinstance(payload, dict):
        raise MatrixInvalid("matrix is not a JSON object")

    version = str(payload.get("version") or "").strip()
    if not version:
        raise MatrixInvalid("matrix has no version")

    ingredients = _parse_ingredients(payload.get("ingredients"))
    limits = payload.get("limits") or {}

    matrix = Matrix(
        version=version,
        max_actives_am=_positive_int(limits.get("maxActivesAm"), "limits.maxActivesAm"),
        max_actives_pm=_positive_int(limits.get("maxActivesPm"), "limits.maxActivesPm"),
        ingredients=ingredients,
        base_steps=_parse_base_steps(payload.get("baseSteps"), ingredients),
        concerns=_parse_concerns(payload.get("concerns"), ingredients),
        prescription_exclusions=_parse_prescription(payload.get("prescriptionExclusions"), ingredients),
        rules=_parse_rules(payload.get("rules"), ingredients),
    )

    logger.info(
        "rules matrix %s loaded: %d ingredients, %d rules",
        matrix.version,
        len(matrix.ingredients),
        len(matrix.rules),
    )
    return matrix


def load_matrix_file(path: Path) -> Matrix:
    return parse_matrix(json.loads(path.read_text(encoding="utf-8")))


def _parse_ingredients(raw: Any) -> dict[str, Ingredient]:
    if not isinstance(raw, dict) or not raw:
        raise MatrixInvalid("matrix has no ingredients")

    ingredients: dict[str, Ingredient] = {}
    for key, item in raw.items():
        if not isinstance(item, dict):
            raise MatrixInvalid(f"ingredient {key!r} is not an object")
        for flag in ("isActiveIngredient", "minorRestricted", "pregnancyRestricted"):
            # Appendix F requires these on every ingredient. Not defaulted:
            # a missing pregnancyRestricted defaulting to false would quietly
            # permit an ingredient nobody decided was safe.
            if not isinstance(item.get(flag), bool):
                raise MatrixInvalid(f"ingredient {key!r} is missing boolean {flag}")
        ingredients[key] = Ingredient(
            key=key,
            label=str(item.get("label") or key),
            is_active=item["isActiveIngredient"],
            minor_restricted=item["minorRestricted"],
            pregnancy_restricted=item["pregnancyRestricted"],
            incompatible_with=frozenset(item.get("incompatibleWith") or []),
        )

    # Every incompatibility must name a real ingredient and be declared on
    # both sides. A one-sided pair is how "A with B" gets blocked while "B with
    # A" slips through, depending on which the engine picks first.
    for ing in ingredients.values():
        for other in ing.incompatible_with:
            if other not in ingredients:
                raise MatrixInvalid(f"{ing.key} is incompatible with unknown ingredient {other!r}")
            if ing.key not in ingredients[other].incompatible_with:
                raise MatrixInvalid(
                    f"incompatibility {ing.key} / {other} is declared on one side only"
                )

    return ingredients


def _parse_base_steps(raw: Any, ingredients: dict[str, Ingredient]) -> tuple[BaseStep, ...]:
    if not isinstance(raw, list) or not raw:
        raise MatrixInvalid("matrix has no base steps")

    steps: list[BaseStep] = []
    for item in raw:
        rule_id = _rule_id(item)
        step = item.get("step")
        if step not in STEPS:
            raise MatrixInvalid(f"{rule_id}: unknown step {step!r}")

        timing = tuple(item.get("timing") or [])
        if not timing or any(t not in TIMINGS for t in timing):
            raise MatrixInvalid(f"{rule_id}: timing must be a non-empty list of AM/PM")

        by_type_raw = item.get("ingredientBySkinType") or {}
        by_type: dict[SkinType, str] = {}
        for skin_type in SkinType:
            key = by_type_raw.get(skin_type.value)
            if key not in ingredients:
                raise MatrixInvalid(f"{rule_id}: no known ingredient for {skin_type.value}")
            if ingredients[key].is_active:
                # Base steps are for every user regardless of flags, so they
                # must never carry an active the restriction checks would need
                # to filter.
                raise MatrixInvalid(f"{rule_id}: base step uses active ingredient {key!r}")
            by_type[skin_type] = key

        steps.append(
            BaseStep(
                rule_id=rule_id,
                step=step,
                timing=timing,
                ingredient_by_skin_type=by_type,
                frequency=_frequency(item, rule_id),
                source=_source(item, rule_id),
                review_status=_review_status(item, rule_id),
            )
        )

    # FR-REC-004 invariant 7: every AM routine has sunscreen. Checked here too,
    # so a matrix that forgot it cannot load at all.
    if not any(s.step == "PROTECT" and "AM" in s.timing for s in steps):
        raise MatrixInvalid("matrix has no AM PROTECT base step; sunscreen is mandatory")

    return tuple(steps)


def _parse_concerns(raw: Any, ingredients: dict[str, Ingredient]) -> dict[Concern, ConcernPolicy]:
    if not isinstance(raw, dict):
        raise MatrixInvalid("matrix has no concerns section")

    policies: dict[Concern, ConcernPolicy] = {}
    for concern in Concern:
        item = raw.get(concern.value)
        # Total over the enumeration. A concern the analysis can return but the
        # matrix does not describe would have no priority to sort by.
        if not isinstance(item, dict):
            raise MatrixInvalid(f"concerns section has no entry for {concern.value}")
        prohibits = frozenset(item.get("prohibits") or [])
        unknown = prohibits - set(ingredients)
        if unknown:
            raise MatrixInvalid(f"{concern.value} prohibits unknown ingredients {sorted(unknown)}")
        policies[concern] = ConcernPolicy(
            concern=concern,
            priority=_positive_int(item.get("priority"), f"{concern.value}.priority"),
            prohibits=prohibits,
        )
    return policies


def _parse_prescription(raw: Any, ingredients: dict[str, Ingredient]) -> frozenset[str]:
    if not isinstance(raw, dict):
        raise MatrixInvalid("matrix has no prescriptionExclusions section")
    _source(raw, "prescriptionExclusions")
    _review_status(raw, "prescriptionExclusions")
    keys = frozenset(raw.get("ingredients") or [])
    unknown = keys - set(ingredients)
    if unknown:
        raise MatrixInvalid(f"prescriptionExclusions names unknown ingredients {sorted(unknown)}")
    return keys


def _parse_rules(raw: Any, ingredients: dict[str, Ingredient]) -> tuple[Rule, ...]:
    if not isinstance(raw, list) or not raw:
        raise MatrixInvalid("matrix has no rules")

    rules: list[Rule] = []
    seen: set[str] = set()
    for item in raw:
        rule_id = _rule_id(item)
        if rule_id in seen:
            raise MatrixInvalid(f"duplicate ruleId {rule_id}")
        seen.add(rule_id)

        try:
            concern = Concern(item.get("concern"))
        except ValueError:
            raise MatrixInvalid(f"{rule_id}: unknown concern {item.get('concern')!r}") from None

        ingredient = item.get("ingredient")
        if ingredient not in ingredients:
            raise MatrixInvalid(f"{rule_id}: unknown ingredient {ingredient!r}")
        if not ingredients[ingredient].is_active:
            raise MatrixInvalid(f"{rule_id}: treatment rule uses non-active {ingredient!r}")

        timing = item.get("timing")
        if timing not in TIMINGS:
            raise MatrixInvalid(f"{rule_id}: timing must be AM or PM")

        max_percent = item.get("maxPercent")
        if not isinstance(max_percent, (int, float)) or max_percent <= 0:
            raise MatrixInvalid(f"{rule_id}: maxPercent must be a positive number")

        rules.append(
            Rule(
                rule_id=rule_id,
                concern=concern,
                ingredient=ingredient,
                max_percent=float(max_percent),
                timing=timing,
                frequency=_frequency(item, rule_id),
                source=_source(item, rule_id),
                review_status=_review_status(item, rule_id),
            )
        )
    return tuple(rules)


def _rule_id(item: Any) -> str:
    if not isinstance(item, dict):
        raise MatrixInvalid("a rule is not an object")
    rule_id = str(item.get("ruleId") or "").strip()
    if not rule_id:
        raise MatrixInvalid("a rule has no ruleId")
    return rule_id


def _source(item: dict, where: str) -> str:
    # DR-007: every rule carries a non-empty source.
    source = str(item.get("source") or "").strip()
    if not source:
        raise MatrixInvalid(f"{where}: no source (DR-007)")
    return source


def _review_status(item: dict, where: str) -> ReviewStatus:
    # DR-007: and a reviewStatus from the defined enumeration.
    try:
        return ReviewStatus(item.get("reviewStatus"))
    except ValueError:
        raise MatrixInvalid(f"{where}: invalid reviewStatus (DR-007)") from None


def _frequency(item: dict, where: str) -> str:
    frequency = item.get("frequency")
    if frequency not in FREQUENCIES:
        raise MatrixInvalid(f"{where}: frequency must be one of {FREQUENCIES}")
    return frequency


def _positive_int(value: Any, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise MatrixInvalid(f"{where} must be a positive integer")
    return value


# ---------------------------------------------------------------------------
# Store -- FR-REC-006
# ---------------------------------------------------------------------------


Fetcher = Callable[[str], Awaitable[Any]]


async def _http_fetch(url: str) -> Any:
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()


@dataclass(frozen=True)
class LoadedMatrix:
    """The matrix to use, and whether it is a fallback (FR-REC-006)."""

    matrix: Matrix
    fell_back: bool = False


class MatrixStore:
    """
    Hands out the current matrix.

    `fetch` and `clock` are injectable so the fallback and the cache expiry can
    be tested without a network or a sleep.
    """

    def __init__(
        self,
        settings: Settings,
        fetch: Fetcher = _http_fetch,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._fetch = fetch
        self._clock = clock
        self._current: Matrix | None = None
        self._fetched_at: float | None = None

    async def get(self) -> LoadedMatrix:
        url = self._settings.rules_matrix_url.strip()

        if not url:
            # Development default: the local file, read once per process.
            if self._current is None:
                self._current = load_matrix_file(Path(self._settings.rules_matrix_path))
            return LoadedMatrix(self._current)

        fresh = (
            self._current is not None
            and self._fetched_at is not None
            and self._clock() - self._fetched_at < self._settings.rules_matrix_cache_ttl_seconds
        )
        if fresh:
            return LoadedMatrix(self._current)

        try:
            matrix = parse_matrix(await self._fetch(url))
        except Exception as exc:  # network, JSON, or validation -- all the same here
            logger.warning("rules matrix fetch failed, using fallback: %s", type(exc).__name__)
            if self._current is None:
                # Never fetched successfully. The bundled file is the last
                # known good matrix.
                self._current = load_matrix_file(Path(self._settings.rules_matrix_path))
            return LoadedMatrix(self._current, fell_back=True)

        self._current = matrix
        self._fetched_at = self._clock()
        return LoadedMatrix(matrix)