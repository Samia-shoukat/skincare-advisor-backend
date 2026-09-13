"""
Closed vocabularies defined in SRS Section 4.2.

Every value the system may emit is declared here. Nothing outside these
enumerations is accepted from a provider, stored in a ScanLog, or shown to a
user (FR-AI-002, FR-AI-005, DR-008).

Do not add or rename a value without a corresponding SRS amendment. These
lists being closed is the mechanism that keeps the system's vocabulary
bounded and reviewable; a value added here quietly is a requirement changed
quietly.

Every enum inherits `str` so that members serialise to their own value in JSON
and in Postgres without an explicit `.value` at each call site.
"""

from enum import Enum


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


class SkinType(str, Enum):
    """
    FR-ONB-006. Determined once, from the questionnaire, and never from the
    image.

    The rationale is worth keeping in view: skin type reflects sebum production
    over time, while a photograph captures one moment and is confounded by
    lighting, time of day, and recent cleansing.
    """

    OILY = "OILY"
    DRY = "DRY"
    COMBINATION = "COMBINATION"
    NORMAL = "NORMAL"


class AgeBand(str, Enum):
    """
    FR-ONB-004. MINOR is 13-17, ADULT is 18 and above. Derived from the
    confirmed date of birth, never entered directly, and included in every
    routine request.
    """

    MINOR = "MINOR"
    ADULT = "ADULT"


# ---------------------------------------------------------------------------
# Analysis output -- cosmetic
# ---------------------------------------------------------------------------


class Concern(str, Enum):
    """
    The nine cosmetic concerns. This list is closed.

    FR-AI-002: any identifier a provider returns outside this set is discarded
    and the discard recorded in metrics. Where every returned concern is
    discarded, the scan is treated as unusable rather than producing an empty
    routine.
    """

    ACNE = "ACNE"
    EXCESS_OIL = "EXCESS_OIL"
    DRYNESS = "DRYNESS"
    DEHYDRATION = "DEHYDRATION"
    POST_ACNE_MARKS = "POST_ACNE_MARKS"
    UNEVEN_TONE = "UNEVEN_TONE"
    ENLARGED_PORES = "ENLARGED_PORES"
    MILD_REDNESS = "MILD_REDNESS"
    FINE_LINES = "FINE_LINES"


class Severity(str, Enum):
    """
    Grade attached to a cosmetic concern.

    Not to be confused with `Confidence`, which attaches to a clinical signal.
    Both have three members and it is easy to reach for the wrong one.
    """

    MILD = "MILD"
    MODERATE = "MODERATE"
    PRONOUNCED = "PRONOUNCED"


# ---------------------------------------------------------------------------
# Analysis output -- clinical
# ---------------------------------------------------------------------------


class ClinicalSignal(str, Enum):
    """
    Findings outside the scope of over-the-counter cosmetic products.

    Two properties of these identifiers matter more than the list itself:

      * Each names an APPEARANCE, not a condition. PIGMENT_PATCHES describes
        what is visible; it does not assert melasma, or vitiligo, or anything
        else. Condition names come only from the Appendix H table under
        FR-AI-007, never from an identifier here.

      * They are INTERNAL. They appear in logs and metrics and are never
        rendered to a user. What the user sees is the `observation` string that
        accompanies the signal, which describes appearance in plain language.

    FR-AI-006 constrains these one way: a signal can only cause a referral. It
    can never suppress a referral raised elsewhere, and never contributes to
    routine generation.
    """

    PIGMENT_PATCHES = "PIGMENT_PATCHES"
    INFLAMED_PATCHES = "INFLAMED_PATCHES"
    SCALING_PLAQUES = "SCALING_PLAQUES"
    PERSISTENT_REDNESS = "PERSISTENT_REDNESS"
    NODULAR_LESIONS = "NODULAR_LESIONS"
    UNIFORM_PAPULES = "UNIFORM_PAPULES"
    BLISTERS = "BLISTERS"
    OPEN_WOUND = "OPEN_WOUND"
    INFECTION_SIGNS = "INFECTION_SIGNS"
    IRREGULAR_LESION = "IRREGULAR_LESION"


class Confidence(str, Enum):
    """
    Confidence attached to a clinical signal.

    FR-AI-007 permits an association list only at HIGH. FR-AI-008 makes
    suppression the default and naming the exception.
    """

    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"


class SuppressionReason(str, Enum):
    """
    FR-AI-008. Why an association list was withheld. Recorded on the scan log
    whenever suppression occurs, so that a referral screen showing only an
    observation can be traced to a reason.
    """

    CONFIDENCE_BELOW_HIGH = "CONFIDENCE_BELOW_HIGH"
    MULTIPLE_SIGNALS = "MULTIPLE_SIGNALS"
    NO_TABLE_ENTRY = "NO_TABLE_ENTRY"
    ENTRY_DISABLED = "ENTRY_DISABLED"


# ---------------------------------------------------------------------------
# Scan lifecycle
# ---------------------------------------------------------------------------


class ScanOutcome(str, Enum):
    """
    The four terminal states of a scan attempt. Recorded on every ScanLog.

    Only ROUTINE decrements the scan allowance (FR-SUB-003). REFERRAL in
    particular does not: a user should never be charged, in quota or money, for
    being told to see a doctor (FR-TRI-004).
    """

    ROUTINE = "ROUTINE"
    REFERRAL = "REFERRAL"
    UNUSABLE = "UNUSABLE"
    ERROR = "ERROR"


class ErrorCode(str, Enum):
    """
    Machine-readable codes returned to the client under IF-COMM-003, alongside
    a human-readable message and without internal implementation detail.
    """

    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    ANALYSIS_INVALID = "ANALYSIS_INVALID"
    IMAGE_UNUSABLE = "IMAGE_UNUSABLE"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"


# ---------------------------------------------------------------------------
# Catalogue and access
# ---------------------------------------------------------------------------


class ProductTier(str, Enum):
    """FR-REC-005. Each routine step offers one of each, plus a generic option."""

    BUDGET = "BUDGET"
    PREMIUM = "PREMIUM"


class SubscriptionTier(str, Enum):
    """
    Present in the data model but NOT enforced in release 1.0 (SRS 4.8). Every
    account is FREE and gets one scan under FR-SUB-001.

    Safe to extend later -- this is one of the few enums here that is not a
    closed clinical vocabulary.
    """

    FREE = "FREE"
    LIMITED = "LIMITED"
    PREMIUM = "PREMIUM"


# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------


class ReviewStatus(str, Enum):
    """
    DR-007. Every rule in the active matrix carries one of these, plus a
    non-empty source field.

    Values are lowercase because the SRS writes them that way and they appear
    verbatim in the matrix JSON. Do not normalise them to uppercase to match
    the other enums here -- that would silently break matrix validation.

    NEEDS_REVIEW rules are the priority items for clinical review under
    DEP-003, and three of the seven FR-REC-004 invariants currently carry it.
    """

    GUIDELINE_SOURCED = "guideline_sourced"
    NEEDS_REVIEW = "needs_review"
    EXPERT_CONFIRMED = "expert_confirmed"


class AnalysisBackend(str, Enum):
    """
    FR-AI-004. Recorded per task on every scan, so that any past result can be
    attributed to whatever served it.
    """

    INTERNAL_MODEL = "INTERNAL_MODEL"
    HOSTED_PROVIDER = "HOSTED_PROVIDER"


class ScanIneligibilityReason(str, Enum):
    """
    Why the capture control is unavailable.

    The client maps these to copy from the string resource; the reasons
    themselves carry no user-facing text (IF-UI-001).

    Order matters where more than one applies — see `evaluate_eligibility`.
    """

    ONBOARDING_INCOMPLETE = "ONBOARDING_INCOMPLETE"
    SCAN_ACCESS_BLOCKED = "SCAN_ACCESS_BLOCKED"
    REFERRAL_REQUIRED = "REFERRAL_REQUIRED"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"