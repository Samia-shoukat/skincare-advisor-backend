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

Changing any string here requires bumping CLAIMS_VERSION. Changing the
LIMITATIONS_STATEMENT additionally requires bumping the consent version setting,
which is what makes FR-ONB-007 re-present the screen to every user.
"""

from __future__ import annotations

from typing import Any, Final

# Version of this whole bundle, for client caching. Bump on any wording change.
#
# NOT the consent version. FR-ONB-007 re-presents the limitations screen when
# the *limitations statement* changes, and that is tracked separately by
# Settings.consent_statement_version, sent in the bundle as `consentVersion`.
# They used to be the same value, which meant fixing a typo on the routine
# screen would have made every user's consent fail.
CLAIMS_VERSION: Final[str] = "1.3"


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

SUMMARY_TITLE: Final[str] = "Skin screening summary"
SUMMARY_DATE_LABEL: Final[str] = "Date"
SUMMARY_OBSERVED_LABEL: Final[str] = "What the photo showed"
SUMMARY_NO_PHOTO: Final[str] = "No photo was taken. Referral was based on the answers below."
SUMMARY_ANSWERS_LABEL: Final[str] = "Answers given in the app"
SUMMARY_YES: Final[str] = "Yes"
SUMMARY_NO: Final[str] = "No"

# The four safety answers, relabelled for the summary.
#
# FR-TRI-005 requires the summary to include the declared safety answers AND to
# contain no condition name. The questions themselves name conditions ("acne",
# "eczema, psoriasis, or rosacea"), so quoting them verbatim would break the
# second half. These labels carry the same answer without the names. The
# clinician reading the summary gets a yes or no on each and can ask the
# follow-up in person, which is where it belongs.
SUMMARY_ANSWER_LABELS: Final[dict[str, str]] = {
    "SQ1": "Pregnant or breastfeeding",
    "SQ2": "Currently using a prescription skin treatment",
    "SQ3": "Open wounds, sores, swelling, or a mole that has changed recently",
    "SQ4": "Has an existing skin diagnosis from a doctor",
}


# ---------------------------------------------------------------------------
# FR-REC-007 -- persistent routine disclaimer
# ---------------------------------------------------------------------------

ROUTINE_DISCLAIMER: Final[str] = "These are cosmetic suggestions, not medical advice."

# FR-REC-005. The generic pharmacy alternative shown on every step. The SRS
# gives the exact form: "Ask your pharmacy for a niacinamide serum at 5% or
# lower". `{article}` is "a" or "an", `{label}` is the matrix label lowercased.
GENERIC_WITH_STRENGTH: Final[str] = "Ask your pharmacy for {article} {label} at {percent}% or lower"
GENERIC_WITHOUT_STRENGTH: Final[str] = "Ask your pharmacy for {article} {label}"

# Routine screen. Concerns are shown with these labels rather than their
# identifiers: "ACNE" on a routine screen reads as a diagnosis, and "acne" is on
# the DR-008 condition-name list. These describe what is visible, which is all
# a concern is (SRS 1.3).
CONCERN_LABELS: Final[dict[str, str]] = {
    "ACNE": "Breakouts",
    "EXCESS_OIL": "Shine",
    "DRYNESS": "Dryness",
    "DEHYDRATION": "Tightness",
    "POST_ACNE_MARKS": "Marks left by blemishes",
    "UNEVEN_TONE": "Uneven tone",
    "ENLARGED_PORES": "Visible pores",
    "MILD_REDNESS": "Mild redness",
    "FINE_LINES": "Fine lines",
}

FREQUENCY_LABELS: Final[dict[str, str]] = {
    "DAILY": "Every day",
    "ALTERNATE_DAYS": "Every other day",
    "TWICE_WEEKLY": "Twice a week",
}

# Why each step is in the routine, in one line the user can act on.
#
# Copy, not clinical logic, so it lives here rather than in the matrix (IF-UI-001,
# CON-003). Keyed by matrix ingredient: a matrix that adds an ingredient without
# a line here fails the test suite rather than showing a blank card.
INGREDIENT_PURPOSE: Final[dict[str, str]] = {
    "gentle_cleanser": "Lifts oil and sunscreen without stripping your skin",
    "moisturiser_light": "Hydrates without heaviness, so skin makes less oil",
    "moisturiser_barrier": "Rebuilds the barrier that keeps moisture in",
    "sunscreen_spf30": "Stops marks darkening and protects the work below",
    "niacinamide": "Calms oil, evens tone, strengthens the barrier",
    "azelaic_acid": "Fades marks and settles redness, gently",
    "salicylic_acid": "Clears out pores and smooths rough texture",
    "benzoyl_peroxide": "Targets the bacteria behind inflamed spots",
    "adapalene": "Keeps pores from clogging in the first place",
    "glycolic_acid": "Resurfaces dullness and softens fine lines",
    "vitamin_c": "Brightens dullness and evens out tone",
}

ROUTINE_HEADING: Final[str] = "Your routine"
ROUTINE_MORNING: Final[str] = "Morning"

# Skin profile card. The skin type is shown in plain words rather than as the
# enum value: "COMBINATION" is an identifier, "Combination" is a description.
ROUTINE_PROFILE_HEADING: Final[str] = "Your skin profile"
ROUTINE_SKIN_TYPE_LABEL: Final[str] = "Skin type"
ROUTINE_CONCERNS_LABEL: Final[str] = "What we noticed"
ROUTINE_STEPS_LABEL: Final[str] = "Steps"
ROUTINE_AFFORDABLE: Final[str] = "Affordable"
ROUTINE_PREMIUM_TIER: Final[str] = "Premium"
ROUTINE_PHARMACY: Final[str] = "At the pharmacy"

SKIN_TYPE_LABELS: Final[dict[str, str]] = {
    "OILY": "Oily",
    "DRY": "Dry",
    "COMBINATION": "Combination",
    "NORMAL": "Normal",
}
ROUTINE_EVENING: Final[str] = "Evening"
ROUTINE_BUDGET: Final[str] = "Budget"
ROUTINE_PREMIUM: Final[str] = "Premium"
ROUTINE_FOR: Final[str] = "For"
ROUTINE_OMITTED: Final[str] = (
    "Some things we noticed aren't covered here, because no over-the-counter "
    "option was suitable alongside the rest of your routine or your answers."
)
# Start slowly: the SRS rules use alternate-day frequencies for retinoids, and
# the first weeks are where irritation happens.
ROUTINE_START_SLOWLY: Final[str] = (
    "Introduce one new product at a time, a week apart. Stop any product that "
    "stings or burns."
)
ROUTINE_OFFLINE: Final[str] = "Showing your saved routine. You're offline."


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


# ---------------------------------------------------------------------------
# Scan flow screens
# ---------------------------------------------------------------------------

# Home dashboard.
#
# "Glow seeker" is a status, not a claim about anyone's skin. Nothing here
# describes a condition or promises a result.
HOME_GREETING: Final[str] = "Hello"
HOME_STATUS: Final[str] = "Glow seeker"
HOME_SCAN_TITLE: Final[str] = "Scan my face"
HOME_SCAN_SUBTITLE: Final[str] = "One photo. Analysed, then discarded."
HOME_ROUTINE_TILE: Final[str] = "My routine"
HOME_ACCOUNT_TILE: Final[str] = "Account"
HOME_NO_ROUTINE: Final[str] = "No routine yet"

# Skin vitals. These are the severities the analysis reported, shown as levels.
# Not measurements: the system measures no hydration, oil or barrier value, and
# a number here would be invented.
VITALS_HEADING: Final[str] = "Skin vitals"
VITALS_CAPTION: Final[str] = "What the scan observed, and how strongly"
SEVERITY_LABELS: Final[dict[str, str]] = {
    "MILD": "Mild",
    "MODERATE": "Moderate",
    "PRONOUNCED": "Pronounced",
}

SCAN_READY_HEADING: Final[str] = "Ready for your scan"
SCAN_READY_BODY: Final[str] = (
    "Take one photo of your face in good light. The photo is analysed and then "
    "discarded; it is never saved."
)
SCAN_START: Final[str] = "Start scan"

# FR-AI-003. Shown after an unusable result, and the guidance after three.
SCAN_RETAKE: Final[str] = "We couldn't read that photo clearly. Please take another."
SCAN_RETAKE_GUIDANCE: Final[str] = (
    "Face a window or a bright lamp, remove glasses, pull hair back from your "
    "face, and hold the phone at arm's length at eye level. You can also stop "
    "here and try again later - this hasn't used your scan."
)

REFERRAL_HEADING: Final[str] = "Please see a healthcare professional"
REFERRAL_OBSERVED_INTRO: Final[str] = "In your photo we noticed:"
REFERRAL_SUMMARY_HEADING: Final[str] = "Summary for your appointment"
REFERRAL_SHARE: Final[str] = "Copy or share summary"

# FR-ONB-003. The support address appears; the age threshold does not, because
# stating it tells the user exactly what to enter next time.
SCAN_BLOCKED_SUPPORT: Final[str] = (
    "This account can't use the scan feature. If you think that's a mistake, "
    "contact {email}."
)

# Account screen.
ACCOUNT_HEADING: Final[str] = "Account & privacy"
ACCOUNT_DELETE_HEADING: Final[str] = "Delete your account"
ACCOUNT_DELETE_BODY: Final[str] = (
    "This permanently deletes your account, your answers, your scan history and "
    "your routine. It can't be undone. Photos are never stored, so there are "
    "none to delete."
)
ACCOUNT_DELETE_CONFIRM: Final[str] = "Yes, delete everything"
ACCOUNT_DELETED: Final[str] = "Your account has been deleted."
ACCOUNT_CONTACT: Final[str] = "Questions or problems? Email {email}."


def bundle(review_claim: str, *, consent_version: str, support_email: str) -> dict[str, Any]:
    """
    The payload GET /v1/content/strings returns.

    `review_claim` depends on whether a signed ReviewRecord exists for the
    matrix in force, which is a database question. `consent_version` and
    `support_email` are deployment settings. Everything else is static.
    """
    return {
        "version": CLAIMS_VERSION,
        # FR-ONB-007. What the consent screen sends back when acknowledging.
        "consentVersion": consent_version,
        "supportEmail": support_email,
        # Public pages served by this backend (Google Play needs URLs).
        "legal": {
            "privacyPath": "/legal/privacy",
            "termsPath": "/legal/terms",
            "accountDeletionPath": "/legal/account-deletion",
        },
        "account": {
            "heading": ACCOUNT_HEADING,
            "deleteHeading": ACCOUNT_DELETE_HEADING,
            "deleteBody": ACCOUNT_DELETE_BODY,
            "deleteConfirm": ACCOUNT_DELETE_CONFIRM,
            "deleted": ACCOUNT_DELETED,
            "contact": ACCOUNT_CONTACT.format(email=support_email),
        },
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
        "scan": {
            "readyHeading": SCAN_READY_HEADING,
            "readyBody": SCAN_READY_BODY,
            "start": SCAN_START,
            "retake": SCAN_RETAKE,
            "retakeGuidance": SCAN_RETAKE_GUIDANCE,
        },
        "routineScreen": {
            "heading": ROUTINE_HEADING,
            "morning": ROUTINE_MORNING,
            "evening": ROUTINE_EVENING,
            "budget": ROUTINE_BUDGET,
            "premium": ROUTINE_PREMIUM,
            "for": ROUTINE_FOR,
            "omitted": ROUTINE_OMITTED,
            "startSlowly": ROUTINE_START_SLOWLY,
            "offline": ROUTINE_OFFLINE,
            "concernLabels": dict(CONCERN_LABELS),
            "frequencyLabels": dict(FREQUENCY_LABELS),
            "ingredientPurpose": dict(INGREDIENT_PURPOSE),
            "profileHeading": ROUTINE_PROFILE_HEADING,
            "skinTypeLabel": ROUTINE_SKIN_TYPE_LABEL,
            "concernsLabel": ROUTINE_CONCERNS_LABEL,
            "stepsLabel": ROUTINE_STEPS_LABEL,
            "affordable": ROUTINE_AFFORDABLE,
            "premiumTier": ROUTINE_PREMIUM_TIER,
            "pharmacy": ROUTINE_PHARMACY,
            "skinTypeLabels": dict(SKIN_TYPE_LABELS),
            "vitalsHeading": VITALS_HEADING,
            "vitalsCaption": VITALS_CAPTION,
            "severityLabels": dict(SEVERITY_LABELS),
        },
        "home": {
            "greeting": HOME_GREETING,
            "status": HOME_STATUS,
            "scanTitle": HOME_SCAN_TITLE,
            "scanSubtitle": HOME_SCAN_SUBTITLE,
            "routineTile": HOME_ROUTINE_TILE,
            "accountTile": HOME_ACCOUNT_TILE,
            "noRoutine": HOME_NO_ROUTINE,
        },
        "referralScreen": {
            "heading": REFERRAL_HEADING,
            "observedIntro": REFERRAL_OBSERVED_INTRO,
            "summaryHeading": REFERRAL_SUMMARY_HEADING,
            "share": REFERRAL_SHARE,
        },
        "scanBlockedSupport": SCAN_BLOCKED_SUPPORT.format(email=support_email),
    }