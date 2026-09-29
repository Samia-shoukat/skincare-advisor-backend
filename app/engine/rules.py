"""
The rules engine. FR-REC-001, FR-REC-002, FR-REC-003.

Turns a skin type, a set of cosmetic concerns and the user's safety flags into
an AM/PM routine, using only the Appendix F matrix. No machine learning, no
randomness, no clock, no database: the same input always gives the same
routine, byte for byte (FR-REC-001).

## How it decides, in order

1. **Exclude.** Build the set of ingredients this user may not have:
   - everything prohibited by ANY detected concern (this is how barrier repair
     outranks active treatment -- a priority-1 dryness concern bans harsh
     actives before the acne rules are even looked at);
   - `minorRestricted` ingredients for a MINOR user (FR-REC-002);
   - `pregnancyRestricted` ingredients when `isPregnant` is set (FR-REC-003);
   - the prescription exclusions when the user is already on a prescription
     acne treatment.
2. **Base steps.** Cleanse, moisturise, and sunscreen in the morning. Every
   routine gets these; which cleanser and moisturiser depends on skin type.
3. **Treat.** Walk the concerns from highest priority to lowest. For each, take
   the first rule in matrix order whose ingredient is not excluded, whose time
   slot still has room under the active limit, and which does not clash with an
   ingredient already chosen. If the ingredient is already in the routine, the
   concern is simply added to that step.
4. **Record omissions.** A concern that got no step is recorded with the reason
   (FR-REC-001, FR-REC-002, FR-REC-003 all require this).

This module contains no clinical judgement of its own (CON-003). Every choice
above is driven by values in the matrix; the engine only defines the order in
which they are applied.

## What never reaches here

Clinical signals. FR-AI-006 requires that no signal value appears in the rules
engine input, and `RoutineInput` has no field that could carry one. A scan with
a signal never calls this module at all -- the pipeline refers it first.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.core.enums import AgeBand, Concern, SkinType
from app.services.matrix_loader import Matrix

_STEP_ORDER = {"CLEANSE": 0, "TREAT": 1, "MOISTURISE": 2, "PROTECT": 3}
_CONCERN_ORDER = {c: i for i, c in enumerate(Concern)}


class ReferralApplies(RuntimeError):
    """
    FR-REC-004 invariant 4: no routine is produced when a referral applies.

    The pipeline never calls the engine for a referred user. This exists so that
    a future caller that forgets gets an exception, not a routine.
    """


# ---------------------------------------------------------------------------
# Input and output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoutineInput:
    """
    Everything the engine is allowed to know. Note what is absent: no clinical
    signals (FR-AI-006) and no image.

    `concerns` is a frozenset so that order and duplicates in the caller's list
    cannot change the result.
    """

    skin_type: SkinType
    age_band: AgeBand
    is_pregnant: bool
    on_prescription_treatment: bool
    concerns: frozenset[Concern]
    referral_flag: bool = False


@dataclass
class RoutineStep:
    step: str
    ingredient: str
    label: str
    frequency: str
    rule_id: str
    max_percent: float | None = None
    concerns: list[Concern] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "step": self.step,
            "ingredient": self.ingredient,
            "label": self.label,
            "maxPercent": self.max_percent,
            "frequency": self.frequency,
            "ruleId": self.rule_id,
            "concerns": [c.value for c in self.concerns],
        }


@dataclass
class Routine:
    matrix_version: str
    am: list[RoutineStep]
    pm: list[RoutineStep]
    # [{"concern": "ACNE", "reason": "..."}]
    omitted: list[dict[str, str]]

    def steps(self) -> list[RoutineStep]:
        return self.am + self.pm

    def ingredients(self) -> set[str]:
        return {s.ingredient for s in self.steps()}

    def as_dict(self) -> dict:
        return {
            "matrixVersion": self.matrix_version,
            "am": [s.as_dict() for s in self.am],
            "pm": [s.as_dict() for s in self.pm],
            "omitted": self.omitted,
        }

    def to_json(self) -> str:
        """Canonical form. Two equal routines give byte-identical output."""
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


# Omission reasons. Recorded on the routine, so a missing step can always be
# traced to why.
OMITTED_ALL_EXCLUDED = "NO_PERMITTED_INGREDIENT"
OMITTED_NO_ROOM = "ACTIVE_LIMIT_OR_CONFLICT"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def excluded_ingredients(matrix: Matrix, data: RoutineInput) -> dict[str, str]:
    """
    Ingredient -> why it is excluded for this user.

    Returned with reasons rather than as a bare set because the invariant
    checks and the tests both need to know which rule removed what.
    """
    excluded: dict[str, str] = {}

    # Highest priority first, so the recorded reason is the most important one.
    for concern in _sorted_concerns(matrix, data.concerns):
        for ingredient in sorted(matrix.concerns[concern].prohibits):
            excluded.setdefault(ingredient, f"PROHIBITED_BY_{concern.value}")

    for key, ingredient in sorted(matrix.ingredients.items()):
        if data.age_band is AgeBand.MINOR and ingredient.minor_restricted:
            excluded.setdefault(key, "MINOR_RESTRICTED")
        if data.is_pregnant and ingredient.pregnancy_restricted:
            excluded.setdefault(key, "PREGNANCY_RESTRICTED")

    if data.on_prescription_treatment:
        for key in sorted(matrix.prescription_exclusions):
            excluded.setdefault(key, "ON_PRESCRIPTION_TREATMENT")

    return excluded


def generate_routine(matrix: Matrix, data: RoutineInput) -> Routine:
    """FR-REC-001. Deterministic: same matrix and input, same routine."""
    if data.referral_flag:
        raise ReferralApplies("no routine is generated while a referral applies")

    excluded = excluded_ingredients(matrix, data)
    slots: dict[str, list[RoutineStep]] = {"AM": [], "PM": []}
    limits = {"AM": matrix.max_actives_am, "PM": matrix.max_actives_pm}

    # --- base steps -----------------------------------------------------
    for base in matrix.base_steps:
        key = base.ingredient_by_skin_type[data.skin_type]
        for timing in base.timing:
            slots[timing].append(
                RoutineStep(
                    step=base.step,
                    ingredient=key,
                    label=matrix.ingredients[key].label,
                    frequency=base.frequency,
                    rule_id=base.rule_id,
                )
            )

    # --- treatment steps ------------------------------------------------
    chosen: dict[str, RoutineStep] = {}
    omitted: list[dict[str, str]] = []

    for concern in _sorted_concerns(matrix, data.concerns):
        rules = matrix.rules_for(concern)
        addressed = False

        for rule in rules:
            if rule.ingredient in chosen:
                # Already in the routine for a higher-priority concern: one
                # step serves both.
                chosen[rule.ingredient].concerns.append(concern)
                addressed = True
                break

            if rule.ingredient in excluded:
                continue

            actives_in_slot = sum(
                1 for s in slots[rule.timing] if matrix.ingredients[s.ingredient].is_active
            )
            if actives_in_slot >= limits[rule.timing]:
                continue

            clashes = matrix.ingredients[rule.ingredient].incompatible_with
            if clashes & chosen.keys():
                continue

            step = RoutineStep(
                step="TREAT",
                ingredient=rule.ingredient,
                label=matrix.ingredients[rule.ingredient].label,
                frequency=rule.frequency,
                rule_id=rule.rule_id,
                max_percent=rule.max_percent,
                concerns=[concern],
            )
            slots[rule.timing].append(step)
            chosen[rule.ingredient] = step
            addressed = True
            break

        if not addressed:
            all_excluded = all(r.ingredient in excluded for r in rules)
            omitted.append(
                {
                    "concern": concern.value,
                    "reason": OMITTED_ALL_EXCLUDED if all_excluded else OMITTED_NO_ROOM,
                }
            )

    for timing in slots:
        slots[timing].sort(key=lambda s: _STEP_ORDER[s.step])

    return Routine(
        matrix_version=matrix.version,
        am=slots["AM"],
        pm=slots["PM"],
        omitted=omitted,
    )


def _sorted_concerns(matrix: Matrix, concerns: frozenset[Concern]) -> list[Concern]:
    """Priority first (1 is highest), then SRS enumeration order as tie-break."""
    return sorted(concerns, key=lambda c: (matrix.concerns[c].priority, _CONCERN_ORDER[c]))
