"""
Rules engine. FR-REC-001 to FR-REC-004.

The centrepiece is the exhaustive test: every combination of skin type, concern
set, age band, pregnancy flag and prescription flag -- 16,384 routines -- is
generated and checked against all seven invariants. That is FR-REC-004's
acceptance criterion taken literally: "the suite reports zero violations".
"""

from __future__ import annotations

import ast
import itertools
from pathlib import Path

import pytest

from app.core.enums import AgeBand, Concern, SkinType
from app.engine.invariants import check_routine
from app.engine.rules import (
    OMITTED_ALL_EXCLUDED,
    OMITTED_NO_ROOM,
    ReferralApplies,
    RoutineInput,
    generate_routine,
)
from app.services.matrix_loader import load_matrix_file

MATRIX = load_matrix_file(Path("app/clinical/matrix/rules_matrix.v0.json"))


def make(*concerns, skin=SkinType.NORMAL, age=AgeBand.ADULT, pregnant=False, rx=False, referral=False):
    return RoutineInput(
        skin_type=skin,
        age_band=age,
        is_pregnant=pregnant,
        on_prescription_treatment=rx,
        concerns=frozenset(concerns),
        referral_flag=referral,
    )


def ingredients(routine) -> set[str]:
    return routine.ingredients()


# ---------------------------------------------------------------------------
# FR-REC-004 -- every combination, zero violations
# ---------------------------------------------------------------------------


def all_concern_sets():
    concerns = list(Concern)
    for size in range(len(concerns) + 1):
        yield from itertools.combinations(concerns, size)


def test_every_combination_satisfies_every_invariant():
    checked = 0
    failures = []
    for concern_set in all_concern_sets():
        for skin, age, pregnant, rx in itertools.product(
            SkinType, AgeBand, (False, True), (False, True)
        ):
            data = make(*concern_set, skin=skin, age=age, pregnant=pregnant, rx=rx)
            routine = generate_routine(MATRIX, data)
            violations = check_routine(MATRIX, data, routine)
            if violations:
                failures.append((data, violations))
            checked += 1

    assert checked == 512 * 4 * 2 * 2 * 2
    assert failures == [], failures[:5]


# ---------------------------------------------------------------------------
# FR-REC-001 -- determinism and priority
# ---------------------------------------------------------------------------


def test_same_input_gives_byte_identical_routine():
    data = make(Concern.ACNE, Concern.UNEVEN_TONE, Concern.DRYNESS, skin=SkinType.COMBINATION)
    assert generate_routine(MATRIX, data).to_json() == generate_routine(MATRIX, data).to_json()


def test_concern_order_does_not_matter():
    a = make(Concern.FINE_LINES, Concern.ACNE, Concern.MILD_REDNESS)
    b = make(Concern.MILD_REDNESS, Concern.ACNE, Concern.FINE_LINES)
    assert generate_routine(MATRIX, a).to_json() == generate_routine(MATRIX, b).to_json()


def test_barrier_concern_prohibitions_beat_acne_permissions():
    """The SRS's own example: priority-1 barrier + acne -> no barrier-prohibited ingredient."""
    routine = generate_routine(MATRIX, make(Concern.DRYNESS, Concern.ACNE))
    banned = MATRIX.concerns[Concern.DRYNESS].prohibits
    assert not (ingredients(routine) & banned)
    # Acne is still treated, with something gentler.
    assert any(Concern.ACNE in s.concerns for s in routine.steps())


def test_acne_alone_gets_the_first_choice_retinoid():
    routine = generate_routine(MATRIX, make(Concern.ACNE))
    assert "adapalene" in ingredients(routine)


def test_engine_invokes_no_model():
    """FR-REC-001: 'no machine-learning model is invoked'. The engine cannot even import one."""
    source = Path("app/engine/rules.py").read_text(encoding="utf-8")
    imported = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any(m.startswith("app.services.analysis") for m in imported)


# ---------------------------------------------------------------------------
# FR-REC-002 / FR-REC-003 -- restrictions
# ---------------------------------------------------------------------------


def test_minor_never_gets_a_minor_restricted_ingredient():
    """UC-005."""
    routine = generate_routine(MATRIX, make(*Concern, age=AgeBand.MINOR))
    for key in ingredients(routine):
        assert not MATRIX.ingredients[key].minor_restricted


def test_pregnant_user_never_gets_a_pregnancy_restricted_ingredient():
    routine = generate_routine(MATRIX, make(Concern.ACNE, Concern.FINE_LINES, pregnant=True))
    for key in ingredients(routine):
        assert not MATRIX.ingredients[key].pregnancy_restricted
    assert "adapalene" not in ingredients(routine)


def test_prescription_user_gets_no_otc_acne_active():
    routine = generate_routine(MATRIX, make(Concern.ACNE, rx=True))
    assert not (ingredients(routine) & MATRIX.prescription_exclusions)


def test_omitted_step_is_recorded_with_reason():
    """
    Redness + acne + on prescription: the barrier rules and the prescription
    exclusion together remove almost everything. Whatever cannot be treated
    must be recorded, not silently dropped.
    """
    data = make(Concern.MILD_REDNESS, Concern.ACNE, rx=True)
    routine = generate_routine(MATRIX, data)
    treated = {c for s in routine.steps() for c in s.concerns}
    omitted = {o["concern"] for o in routine.omitted}
    assert treated | {Concern(c) for c in omitted} == data.concerns


def test_concern_with_every_ingredient_banned_is_omitted():
    """
    Redness bans vitamin C; the prescription rule bans azelaic acid. Those are
    the only two treatments for uneven tone, so it is omitted -- and says why.
    """
    routine = generate_routine(MATRIX, make(Concern.UNEVEN_TONE, Concern.MILD_REDNESS, rx=True))
    assert {"concern": "UNEVEN_TONE", "reason": OMITTED_ALL_EXCLUDED} in routine.omitted


def test_concern_crowded_out_by_the_active_limit_is_omitted():
    """
    Pregnant, acne and oil. Adapalene is banned in pregnancy, so acne takes
    benzoyl peroxide -- the one AM active slot. Oil's first choice (niacinamide)
    is also AM, so there is no room; its second (salicylic acid) is banned in
    pregnancy. Oil is omitted, and the reason says it was crowded out.
    """
    routine = generate_routine(MATRIX, make(Concern.ACNE, Concern.EXCESS_OIL, pregnant=True))
    assert {"concern": "EXCESS_OIL", "reason": OMITTED_NO_ROOM} in routine.omitted


# ---------------------------------------------------------------------------
# Base steps and referral
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("skin", list(SkinType))
def test_no_concerns_still_gives_a_complete_base_routine(skin):
    routine = generate_routine(MATRIX, make(skin=skin))
    assert [s.step for s in routine.am] == ["CLEANSE", "MOISTURISE", "PROTECT"]
    assert [s.step for s in routine.pm] == ["CLEANSE", "MOISTURISE"]
    assert routine.omitted == []


def test_dry_skin_gets_the_barrier_moisturiser():
    routine = generate_routine(MATRIX, make(skin=SkinType.DRY))
    assert "moisturiser_barrier" in ingredients(routine)


def test_referral_produces_no_routine():
    """FR-REC-004 invariant 4."""
    with pytest.raises(ReferralApplies):
        generate_routine(MATRIX, make(Concern.ACNE, referral=True))


def test_invariant_checker_catches_a_bad_routine():
    """The checker is independent of the engine -- prove it can fail."""
    data = make(Concern.DRYNESS, pregnant=True)
    routine = generate_routine(MATRIX, make(Concern.ACNE))  # built for the wrong user
    violations = check_routine(MATRIX, data, routine)
    assert any(v.startswith("INV3") for v in violations)
    assert any(v.startswith("INV5") for v in violations)
