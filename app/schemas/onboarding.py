"""
Request and response shapes for onboarding.

Two conventions run through this file.

**camelCase on the wire, snake_case in Python.** The client is JavaScript and
expects camelCase; Python code reads better in snake_case. `alias` gives both,
so neither side has to translate.

**Responses say as little as possible.** `SessionResponse` and
`DateOfBirthResponse` in particular carry no age, no date, and no threshold.
FR-ONB-003 requires the minimum age never to be stated, and a response field is
just as visible to a curious user as a screen label -- more so, since anyone can
open the network tab.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import AgeBand, SkinType


class _Base(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        # Reject unexpected fields rather than ignoring them. A client sending
        # `dateOfBirthh` should get a clear 422, not a silently empty profile.
        extra="forbid",
    )


# ---------------------------------------------------------------------------
# POST /v1/auth/session -- FR-ONB-001
# ---------------------------------------------------------------------------


class SessionResponse(_Base):
    """
    Returned on sign-in. Tells the client which onboarding step to show next.

    `isNewUser` is for the client's routing only. It carries no privilege --
    every gate below is evaluated server-side on every request.
    """

    is_new_user: bool = Field(alias="isNewUser")
    onboarding_complete: bool = Field(alias="onboardingComplete")

    # Which steps remain. The client walks these in order.
    needs_date_of_birth: bool = Field(alias="needsDateOfBirth")
    needs_safety_answers: bool = Field(alias="needsSafetyAnswers")
    needs_skin_type: bool = Field(alias="needsSkinType")
    needs_consent: bool = Field(alias="needsConsent")

    # FR-ONB-003. A plain boolean with no explanation attached.
    scan_access_blocked: bool = Field(alias="scanAccessBlocked")


# ---------------------------------------------------------------------------
# POST /v1/me/date-of-birth -- FR-ONB-002, 003, 004
# ---------------------------------------------------------------------------


class DateOfBirthRequest(_Base):
    """
    `confirmed` exists because FR-ONB-002 requires an explicit confirmation
    step before the value becomes immutable. The client shows the computed age
    and asks the user to confirm; this flag is the record that it did.

    Without it, a mis-typed year would be frozen on first submit with no way
    back short of contacting support.
    """

    date_of_birth: date = Field(alias="dateOfBirth")
    confirmed: bool = Field(
        description="True only after the user has seen and accepted the confirmation screen"
    )


class DateOfBirthResponse(_Base):
    """
    Deliberately thin.

    No age, no date echo, no threshold, no reason. `scanAccessBlocked` is the
    only signal, and when it is true the client shows the support message from
    the string resource -- which also names no age.
    """

    age_band: AgeBand | None = Field(alias="ageBand")
    scan_access_blocked: bool = Field(alias="scanAccessBlocked")


# ---------------------------------------------------------------------------
# POST /v1/me/safety-answers -- FR-ONB-005
# ---------------------------------------------------------------------------


class SafetyAnswersRequest(_Base):
    """
    All four answers, every time. None is optional.

    The requirement says all four are presented and answered; a partial submit
    would leave a profile that looks screened but isn't. Booleans rather than
    nullable booleans make that structurally impossible.
    """

    pregnant_or_breastfeeding: bool = Field(alias="pregnantOrBreastfeeding")
    prescription_acne_treatment: bool = Field(alias="prescriptionAcneTreatment")
    open_wounds_or_changing_mole: bool = Field(alias="openWoundsOrChangingMole")
    diagnosed_eczema_psoriasis_rosacea: bool = Field(alias="diagnosedEczemaPsoriasisRosacea")

    @property
    def sets_referral_flag(self) -> bool:
        """
        Questions 3 and 4 set the referral flag. Questions 1 and 2 set
        ingredient restrictions instead and do not block scanning.
        """
        return self.open_wounds_or_changing_mole or self.diagnosed_eczema_psoriasis_rosacea

    def as_record(self) -> dict[str, bool]:
        """Shape stored in the SafetyAnswerChange audit rows."""
        return {
            "SQ1": self.pregnant_or_breastfeeding,
            "SQ2": self.prescription_acne_treatment,
            "SQ3": self.open_wounds_or_changing_mole,
            "SQ4": self.diagnosed_eczema_psoriasis_rosacea,
        }


class SafetyAnswersResponse(_Base):
    """
    `referralFlag` is returned so the client can disable the capture control
    before a photograph is taken -- FR-TRI-001 requires that no image is
    captured or transmitted while the flag is set.

    That is a display convenience. The server re-checks it on every scan.
    """

    referral_flag: bool = Field(alias="referralFlag")
    onboarding_complete: bool = Field(alias="onboardingComplete")


# ---------------------------------------------------------------------------
# POST /v1/me/skin-type -- FR-ONB-006
# ---------------------------------------------------------------------------


class SkinTypeRequest(_Base):
    """
    Raw answers, not a computed type.

    Scoring happens server-side for two reasons: FR-ONB-006 requires the same
    answers to always produce the same type, and CON-003 keeps clinical
    judgement out of application code -- the scoring table is configuration
    under app/clinical/questionnaire/.
    """

    answers: dict[str, str] = Field(
        description="questionId -> selected optionId, from GET /v1/content/questionnaire"
    )


class SkinTypeResponse(_Base):
    skin_type: SkinType = Field(alias="skinType")
    onboarding_complete: bool = Field(alias="onboardingComplete")


# ---------------------------------------------------------------------------
# POST /v1/me/consent -- FR-ONB-007
# ---------------------------------------------------------------------------


class ConsentRequest(_Base):
    """
    The version the user actually saw.

    Sending it back rather than trusting the server's current value catches a
    real race: the statement is updated between the screen rendering and the
    user tapping accept. Recording what they read, not what is current, is what
    makes the consent record mean anything.
    """

    acknowledged_version: str = Field(alias="acknowledgedVersion")


class ConsentResponse(_Base):
    consent_version: str = Field(alias="consentVersion")
    onboarding_complete: bool = Field(alias="onboardingComplete")


# ---------------------------------------------------------------------------
# GET /v1/me
# ---------------------------------------------------------------------------


class ProfileResponse(_Base):
    """
    Current onboarding state. No date of birth, no age -- the client has no use
    for either, and not returning them is one fewer place they can leak.
    """

    age_band: AgeBand | None = Field(alias="ageBand")
    skin_type: SkinType | None = Field(alias="skinType")
    referral_flag: bool = Field(alias="referralFlag")
    scan_access_blocked: bool = Field(alias="scanAccessBlocked")
    onboarding_complete: bool = Field(alias="onboardingComplete")
    scans_remaining: int = Field(alias="scansRemaining")
    consent_version: str | None = Field(alias="consentVersion")