"""
The prohibited-terms list required by DR-008. Closes OI-009.

CON-002 permits the system to name a condition in exactly one place: an
association list drawn from the Appendix H table, at HIGH confidence, as a
differential of two or more. Everywhere else -- and in particular in the
`observation` string, the only free-text field Appendix G permits -- a
condition name must not appear.

Appendix G's prompt guidance is the first defence: ask for observable
characteristics, not interpretations. This list is the second. A model asked to
describe will usually describe, but "usually" is not a property a safety control
can be built on, and the cost of being wrong here is the app telling someone
they have something.

## Two kinds of entry

**Condition names.** Anything that names a disease, however colloquially. These
are matched on word boundaries so that a name embedded in an ordinary word does
not trigger.

**Hedges.** "Looks like", "consistent with", "suggestive of". These matter as
much as the names, and arguably more. CON-002 is explicit that a hedged form is
still a condition claim, and FR-ONB-007's rationale records that the consent
statement had to be rewritten once already because the previous wording no
longer described what the system did. A sentence that hedges toward a diagnosis
has made one.

## On false positives

This list is deliberately broad, and it will occasionally reject a description
that was fine. That costs the reviewed sentence from `observations.py` instead
of the provider's, which is a cost of nothing -- the user reads a true statement
either way and the referral still happens. A narrow list that let a condition
name through would cost considerably more.

The one thing to avoid is a term so common that every observation is rejected,
which would make substitution the norm and hide a real prompt regression in the
noise. "Mole" is the clearest example and is deliberately absent: it names an
ordinary feature of skin, it appears in safety question 3, and a description of
where a mark sits relative to one is legitimate.
"""

from __future__ import annotations

import re
from typing import Final


# Condition names, matched whole-word. Lowercase; matching is case-insensitive.
CONDITION_TERMS: Final[frozenset[str]] = frozenset(
    {
        # Inflammatory and papulosquamous
        "acne",
        "rosacea",
        "eczema",
        "psoriasis",
        "dermatitis",
        "atopic",
        "seborrheic",
        "seborrhoeic",
        "lichen",
        "ichthyosis",
        "urticaria",
        "hives",
        # Pigmentary
        "melasma",
        "vitiligo",
        "chloasma",
        "hyperpigmentation",
        "hypopigmentation",
        "albinism",
        # Neoplastic -- the highest-consequence group in the list
        "cancer",
        "cancerous",
        "precancerous",
        "carcinoma",
        "melanoma",
        "malignant",
        "malignancy",
        "benign",
        "tumour",
        "tumor",
        "keratosis",
        "naevus",
        "nevus",
        "dysplastic",
        # Infective
        "impetigo",
        "cellulitis",
        "folliculitis",
        "herpes",
        "shingles",
        "zoster",
        "warts",
        "verruca",
        "molluscum",
        "ringworm",
        "tinea",
        "candida",
        "fungal",
        "bacterial",
        "viral",
        "infection",
        "infected",
        "abscess",
        "staphylococcal",
        "streptococcal",
        "scabies",
        # Autoimmune and systemic
        "lupus",
        "pemphigus",
        "pemphigoid",
        "vasculitis",
        "sarcoidosis",
        "alopecia",
        # Generic clinical framing. "Lesion" and "cyst" are clinical vocabulary
        # rather than descriptions a reader can check against the photograph,
        # which is the line FR-AI-005 draws.
        "lesion",
        "lesions",
        "cyst",
        "cystic",
        "pathology",
        "pathological",
        "syndrome",
        "disease",
        "disorder",
        "condition",
        "diagnosis",
        "diagnose",
        "diagnosed",
        "diagnostic",
        "symptom",
        "symptoms",
        "prognosis",
    }
)


# Hedges, matched as phrases. A sentence containing one of these has stopped
# describing and started concluding, whatever it concludes about.
HEDGE_PHRASES: Final[tuple[str, ...]] = (
    "looks like",
    "look like",
    "looking like",
    "consistent with",
    "suggestive of",
    "suggests",
    "indicative of",
    "indicates",
    "characteristic of",
    "typical of",
    "typically seen in",
    "may be a",
    "could be a",
    "appears to be a",
    "resembles",
    "resembling",
    "compatible with",
    "in keeping with",
    "differential",
    "likely to be",
    "probably a",
)


# Built once. A single alternation is both faster than a loop over 90 terms and
# easier to reason about: one pattern, one place where the boundary rules live.
_CONDITION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(" + "|".join(sorted(re.escape(t) for t in CONDITION_TERMS)) + r")\b",
    re.IGNORECASE,
)

_HEDGE_PATTERN: Final[re.Pattern[str]] = re.compile(
    "(" + "|".join(sorted(re.escape(p) for p in HEDGE_PHRASES)) + ")",
    re.IGNORECASE,
)


def find_prohibited(text: str) -> list[str]:
    """
    Every prohibited term in `text`, lowercased and deduplicated.

    Returns a list rather than a boolean because the caller records what was
    found. DR-008 is a validity requirement with a 100% threshold, and "an
    observation was rejected" is not evidence of anything; "an observation was
    rejected for containing 'melasma'" points at the prompt that produced it.
    """
    if not text:
        return []

    found = {m.group(0).lower() for m in _CONDITION_PATTERN.finditer(text)}
    found |= {m.group(0).lower() for m in _HEDGE_PATTERN.finditer(text)}
    return sorted(found)


def is_clean(text: str) -> bool:
    """True when `text` may be stored and displayed as written."""
    return not find_prohibited(text)
