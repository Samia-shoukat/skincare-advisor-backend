"""
Curated observation text, one per ClinicalSignal identifier. Closes OI-008.

FR-AI-005 requires every returned signal to carry a non-empty `observation`
describing appearance only. The provider writes that string, and the prompt in
the analysis backend asks it to describe rather than interpret -- but a prompt
is a request, not a guarantee. DR-008 requires the result to be validated
against a prohibited-terms list before storage or display.

This file is what happens when validation fails.

## Why substitute rather than discard

The obvious response to an observation containing a condition name is to throw
the signal away. That would be wrong, and dangerously so.

A clinical signal is the trigger for a referral (FR-TRI-002). Discarding the
signal because its accompanying text was badly worded would suppress the
referral -- and FR-AI-006 constrains signal handling to one direction precisely
so that no detection or handling error can result in a routine being generated
where one should not be. Losing the referral is the one outcome this whole
subsystem exists to prevent; losing the provider's phrasing costs nothing,
because the text below says the same thing in wording that has been reviewed.

So the signal survives and its text is replaced. `ObservationSource.CURATED` is
recorded on the scan log when that happens, because a run of substitutions is a
prompt regression worth seeing rather than a per-user problem.

## What these strings may and may not say

Each one describes what a photograph shows: shape, colour relative to
surrounding skin, texture, distribution, location. None names a condition, none
hedges toward one ("looks like", "consistent with"), and none suggests a
treatment, an ingredient, or a product -- FR-TRI-003 forbids all three on the
referral screen, and this text renders there.

Every string here is validated against the prohibited-terms list by the test
suite, so this file cannot drift into naming something by accident.
"""

from __future__ import annotations

from typing import Final

from app.core.enums import ClinicalSignal


OBSERVATIONS: Final[dict[ClinicalSignal, str]] = {
    ClinicalSignal.PIGMENT_PATCHES: (
        "Areas of skin that look darker than the skin around them."
    ),
    ClinicalSignal.INFLAMED_PATCHES: (
        "Raised areas with a defined edge, looking redder or darker than the "
        "skin around them."
    ),
    ClinicalSignal.SCALING_PLAQUES: (
        "Raised patches with visible flaking on the surface."
    ),
    ClinicalSignal.PERSISTENT_REDNESS: (
        "Redness spread across a broad area rather than limited to individual "
        "spots."
    ),
    ClinicalSignal.NODULAR_LESIONS: (
        "Firm lumps sitting under the surface of the skin."
    ),
    ClinicalSignal.UNIFORM_PAPULES: (
        "Many small bumps of a similar size, spread evenly across an area."
    ),
    ClinicalSignal.BLISTERS: (
        "Raised bumps that appear to be filled with fluid."
    ),
    ClinicalSignal.OPEN_WOUND: (
        "An area where the surface of the skin is broken."
    ),
    ClinicalSignal.INFECTION_SIGNS: (
        "Crusting, weeping, or a yellow film on the surface of the skin."
    ),
    ClinicalSignal.IRREGULAR_LESION: (
        "A mark with an uneven edge, or with more than one colour within it."
    ),
}


# A signal identifier with no curated text is a gap in this file, not a reason
# to fail a referral. It should be impossible -- the test suite asserts the
# mapping is total over the enumeration -- but if the enumeration gains a member
# without a string, the user still gets a referral and a sentence that is true.
FALLBACK_OBSERVATION: Final[str] = (
    "Something visible on the skin that over-the-counter products are not "
    "suitable for."
)


def observation_for(signal: ClinicalSignal) -> str:
    """The reviewed description of what this signal looks like."""
    return OBSERVATIONS.get(signal, FALLBACK_OBSERVATION)
