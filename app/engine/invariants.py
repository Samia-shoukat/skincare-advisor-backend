"""
Routine safety invariants. FR-REC-004.

Seven properties every generated routine must have. This module checks them
independently of the engine -- it does not reuse the engine's exclusion logic --
so that a bug in the engine cannot also hide itself here.

The FR-REC-004 rationale is worth keeping in view: passing these proves the
engine enforces its rule set correctly. It does not prove the rule set is
clinically complete. That needs a practitioner (OI-001).

Used two ways:
  * the test suite runs every input combination through the engine and checks
    each routine here (FR-REC-004's acceptance criterion);
  * the scan pipeline checks every real routine here before showing it, and
    refuses to display one that fails.
"""

from __future__ import annotations

from app.core.enums import AgeBand
from app.engine.rules import Routine, RoutineInput
from app.services.matrix_loader import Matrix


def check_routine(
    matrix: Matrix, data: RoutineInput, routine: Routine | None
) -> list[str]:
    """Every invariant the routine breaks. An empty list means it is safe."""
    # 4. No routine when a referral applies.
    if data.referral_flag:
        return ["INV4_ROUTINE_DESPITE_REFERRAL"] if routine is not None else []
    if routine is None:
        return []

    violations: list[str] = []
    ingredients = routine.ingredients()
    info = matrix.ingredients

    # 1. No incompatible pair anywhere in the routine.
    for key in sorted(ingredients):
        clash = info[key].incompatible_with & ingredients
        if clash:
            violations.append(f"INV1_INCOMPATIBLE:{key}+{','.join(sorted(clash))}")

    # 2. No age-restricted ingredient for a MINOR user.
    if data.age_band is AgeBand.MINOR:
        for key in sorted(ingredients):
            if info[key].minor_restricted:
                violations.append(f"INV2_MINOR_RESTRICTED:{key}")

    # 3. No pregnancy-restricted ingredient when flagged.
    if data.is_pregnant:
        for key in sorted(ingredients):
            if info[key].pregnancy_restricted:
                violations.append(f"INV3_PREGNANCY_RESTRICTED:{key}")

    # 5. Barrier repair outranks active treatment: nothing prohibited by any
    #    detected concern may appear.
    for concern in data.concerns:
        banned = matrix.concerns[concern].prohibits & ingredients
        for key in sorted(banned):
            violations.append(f"INV5_PROHIBITED_BY_{concern.value}:{key}")

    # 6. Active count within the Appendix F limit, per time of day.
    for timing, steps, limit in (
        ("AM", routine.am, matrix.max_actives_am),
        ("PM", routine.pm, matrix.max_actives_pm),
    ):
        actives = sum(1 for s in steps if info[s.ingredient].is_active)
        if actives > limit:
            violations.append(f"INV6_TOO_MANY_ACTIVES_{timing}:{actives}>{limit}")

    # 7. A sunscreen step in every AM routine.
    if not any(s.step == "PROTECT" for s in routine.am):
        violations.append("INV7_NO_AM_SUNSCREEN")

    # Not one of the seven, but checked for the same reason: a user already on
    # a prescription acne treatment gets no OTC acne active on top of it.
    if data.on_prescription_treatment:
        for key in sorted(ingredients & matrix.prescription_exclusions):
            violations.append(f"PRESCRIPTION_EXCLUDED:{key}")

    return violations
