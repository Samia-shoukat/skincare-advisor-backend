"""
The single versioned string resource required by IF-UI-001.

Every user-facing claim, disclaimer, and piece of referral copy lives here and
nowhere else. No screen component holds a claim, and no provider prompt does
either. FR-TRI-003 gives the reason directly: the framing around a differential
is fixed rather than generated, so the system cannot present an association as
a conclusion.

The client fetches these at runtime rather than shipping them, so a wording
correction does not wait on app store review -- the same argument FR-REC-006
makes for the rules matrix.

Changing any string here requires bumping CLAIMS_VERSION. FR-ONB-007
re-presents the limitations screen whenever the consent version increments, and
that only works if the version actually moves.
"""

from __future__ import annotations

from typing import Any, Final

CLAIMS_VERSION: Final[str] = "1.2"


# ---------------------------------------------------------------------------
# FR-ONB-007 -- limitations acknowledgement
# ---------------------------------------------------------------------------
# Reproduced verbatim from the SRS. Do not paraphrase: FR-ONB-007's rationale
# notes that a consent statement misdescribing system behaviour provides no
# protection, which is why version 1.2 rewrote it in the first place.

LIMITATIONS_STATEMENT: Final[str] = (
    "This app is not a medical device. It cannot diagnose you. It suggests "
    "over-the-counter cosmetic products based on your answers and what is "
    "visible in your photo. If it sees something outside what cosmetics can "
    "help with, it will describe what it saw and may list conditions that "
    "sometimes look similar - this is not a diagnosis and may be wrong. Only a "
    "doctor can tell you what you have. If your skin is painful, bleeding, "
    "spreading, or changing quickly, see a healthcare professional."
)


# ---------------------------------------------------------------------------
# FR-ONB-005 -- the four safety questions
# ---------------------------------------------------------------------------
# Exactly four, in order, verbatim. `field` names the request field the answer
# populates, so the client never has to guess the mapping.

SAFETY_QUESTIONS: Final[tuple[dict[str, str], ...]] = (
    {
        "id": "SQ1",
        "text": "Are you pregnant or breastfeeding?",
        "field": "pregnantOrBreastfeeding",
    },
    {
        "id": "SQ2",
        "text": "Are you currently using a prescription acne treatment?",
        "field": "prescriptionAcneTreatment",
    },
    {
        "id": "SQ3",
        "text": (
            "Do you have any open wounds, sores, swelling, or a mole that has "
            "changed recently?"
        ),
        "field": "openWoundsOrChangingMole",
    },
    {
        "id": "SQ4",
        "text": "Have you been diagnosed with eczema, psoriasis, or rosacea?",
        "field": "diagnosedEczemaPsoriasisRosacea",
    },
)


# ---------------------------------------------------------------------------
# FR-TRI-003 -- referral screen
# ---------------------------------------------------------------------------

REFERRAL_GENERAL: Final[str] = (
    "Based on what you told us, we're not able to suggest a routine. Please see "
    "a healthcare professional for an in-person assessment."
)

# Fixed framing around an association list, opened and closed by these two
# strings and no others. The closing line is not decoration -- it is what stops
# the list reading as a conclusion.
ASSOCIATION_INTRO: Final[str] = "This appearance is commonly associated with conditions such as"
ASSOCIATION_OUTRO: Final[str] = "Only a dermatologist can determine which, if any, applies to you."

# Shown on every referral screen, association list or not. FR-TRI-003's
# rationale: a user given no routine and no direction is likely to self-select
# products, which is the outcome the referral exists to prevent.
REFERRAL_INTERIM_GUIDANCE: Final[str] = (
    "Until you've been seen, avoid starting any new active skincare products."
)

# FR-TRI-005 -- consultation summary.
SUMMARY_NOT_A_DIAGNOSIS: Final[str] = (
    "This is an automated cosmetic screening result, not a diagnosis."
)


# ---------------------------------------------------------------------------
# FR-REC-007 -- persistent routine disclaimer
# ---------------------------------------------------------------------------

ROUTINE_DISCLAIMER: Final[str] = "These are cosmetic suggestions, not medical advice."


# ---------------------------------------------------------------------------
# FR-ONB-008 -- clinical review claim
# ---------------------------------------------------------------------------
# The default state is unclaimed. UNSUBSTANTIATED is what shows while no signed
# ReviewRecord matches the active matrix version, and it makes no review claim
# at all -- it describes where the rules came from, which is true either way.

REVIEW_CLAIM_UNSUBSTANTIATED: Final[str] = (
    "Routines are based on published clinical guidelines."
)
REVIEW_CLAIM_SUBSTANTIATED: Final[str] = (
    "Routines are reviewed by a licensed practitioner."
)


# ---------------------------------------------------------------------------
# FR-CAM-001 -- capture gate prompts
# ---------------------------------------------------------------------------

CAPTURE_TOO_DARK: Final[str] = "Move to brighter light"
CAPTURE_BLURRY: Final[str] = "Hold the phone steady"
CAPTURE_NO_FACE: Final[str] = "Center your face in the frame"


# ---------------------------------------------------------------------------
# FR-SUB-001 / FR-SUB-005 / FR-ONB-003
# ---------------------------------------------------------------------------

QUOTA_EXHAUSTED: Final[str] = "You've used your scan. Your saved routine stays available here."

# FR-ONB-003. The support address appears; the age threshold does not, because
# stating it tells the user exactly what to enter next time.
SCAN_BLOCKED_SUPPORT: Final[str] = (
    "This account can't use the scan feature. If you think that's a mistake, "
    "contact support@example.com."  # TODO: real address before release
)


def bundle(review_claim: str) -> dict[str, Any]:
    """
    The payload GET /v1/content/strings returns.

    `review_claim` is passed in rather than read here because it depends on
    whether a signed ReviewRecord exists for the active matrix version, which
    is a database question. Everything else is static.
    """
    return {
        "version": CLAIMS_VERSION,
        "limitationsStatement": LIMITATIONS_STATEMENT,
        "safetyQuestions": [dict(q) for q in SAFETY_QUESTIONS],
        "referral": {
            "general": REFERRAL_GENERAL,
            "associationIntro": ASSOCIATION_INTRO,
            "associationOutro": ASSOCIATION_OUTRO,
            "interimGuidance": REFERRAL_INTERIM_GUIDANCE,
            "summaryNotADiagnosis": SUMMARY_NOT_A_DIAGNOSIS,
        },
        "routineDisclaimer": ROUTINE_DISCLAIMER,
        "reviewClaim": review_claim,
        "capture": {
            "tooDark": CAPTURE_TOO_DARK,
            "blurry": CAPTURE_BLURRY,
            "noFace": CAPTURE_NO_FACE,
        },
        "quotaExhausted": QUOTA_EXHAUSTED,
        "scanBlockedSupport": SCAN_BLOCKED_SUPPORT,
    }