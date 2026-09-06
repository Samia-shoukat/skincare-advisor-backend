"""
Onboarding endpoints. FR-ONB-001 through FR-ONB-007.

Every route here is POST, and none is PATCH or PUT. That is not a REST style
preference -- FR-ONB-002's rationale states that an editable date of birth makes
the FR-ONB-003 age gate trivially bypassable, so there is simply no route that
can change one. The model raises if you try; the API gives you nothing to try
with. FR-ONB-006 gets the same treatment: "completed once during onboarding" is
read strictly.

The one exception is safety answers, which are updatable by decision -- a user
whose wound has healed should not be locked out permanently. Every change is
written to `SafetyAnswerChange` first, because `referral_flag` is what
FR-TRI-001 checks before an image is ever captured, and a change to it with no
record would leave no way to tell a correction from a bypass.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from fastapi import APIRouter, status

from app.api.deps import CurrentUser, SessionDep, SettingsDep, TokenDep
from app.core.errors import (
    ConsentVersionMismatch,
    ImmutableField,
    InvalidDateOfBirth,
    InvalidQuestionnaireAnswers,
)
from app.db.models.user import SafetyAnswerChange, User
from app.schemas.onboarding import (
    ConsentRequest,
    ConsentResponse,
    DateOfBirthRequest,
    DateOfBirthResponse,
    ProfileResponse,
    SafetyAnswersRequest,
    SafetyAnswersResponse,
    SessionResponse,
    SkinTypeRequest,
    SkinTypeResponse,
)
from app.services.questionnaire import IncompleteAnswers, get_questionnaire, score

logger = logging.getLogger(__name__)

router = APIRouter(tags=["onboarding"])


def _needs(user: User, required_consent_version: str) -> dict[str, bool]:
    """Which onboarding steps remain. Shared by the session and profile responses."""
    return {
        "needs_date_of_birth": user.dob_confirmed_at is None,
        "needs_safety_answers": not user.safety_answers_completed,
        "needs_skin_type": user.skin_type is None,
        "needs_consent": not user.consent_current(required_consent_version),
    }


# ---------------------------------------------------------------------------
# FR-ONB-001
# ---------------------------------------------------------------------------


@router.post("/auth/session", response_model=SessionResponse)
async def create_session(
    claims: TokenDep,
    session: SessionDep,
    settings: SettingsDep,
) -> SessionResponse:
    """
    Sign in, creating the account on first arrival.

    Idempotent by construction. `auth_id` is the primary key and holds the
    provider's subject claim, so a returning user of the same provider account
    resolves to the existing row -- FR-ONB-001's acceptance criterion is
    satisfied by the schema rather than by a check that could be forgotten.

    No password is read, stored, or verified anywhere on this path. Supabase
    performed the sign-in; this endpoint only trusts a signature.
    """
    user = await session.get(User, claims.subject)
    is_new = user is None

    if user is None:
        user = User(
            auth_id=claims.subject,
            auth_provider=claims.provider,
            # Placeholder until the date-of-birth screen. The column is NOT NULL
            # because a confirmed user always has one, and `dob_confirmed_at` --
            # not this field -- is what marks the step complete.
            date_of_birth=date(1900, 1, 1),
            scans_remaining=settings.default_scan_allowance,
        )
        session.add(user)
        logger.info("created account for provider=%s", claims.provider)

    user.last_sign_in_at = datetime.now(timezone.utc)
    await session.flush()

    return SessionResponse(
        is_new_user=is_new,
        onboarding_complete=user.onboarding_complete,
        scan_access_blocked=user.scan_access_blocked,
        **_needs(user, settings.consent_statement_version),
    )


# ---------------------------------------------------------------------------
# FR-ONB-002, 003, 004
# ---------------------------------------------------------------------------


@router.post("/me/date-of-birth", response_model=DateOfBirthResponse)
async def set_date_of_birth(
    payload: DateOfBirthRequest,
    user: CurrentUser,
    session: SessionDep,
) -> DateOfBirthResponse:
    """
    Confirm the date of birth. Once. There is no route to change it afterwards.

    Read the under-13 branch carefully, because it is the whole point of
    FR-ONB-003 and it is easy to "fix" into a bug:

      * The request SUCCEEDS. Status 200, not 403.
      * The account is created and kept.
      * `scanAccessBlocked` comes back true, with no age and no reason.

    Rejecting the date instead would tell the user what the threshold is and
    hand them a second attempt at clearing it. FR-ONB-003 requires the minimum
    age never to be stated, and this is where that is either honoured or quietly
    thrown away.
    """
    if user.dob_confirmed_at is not None:
        raise ImmutableField()

    if not payload.confirmed:
        # FR-ONB-002 wants an explicit confirmation step before the value
        # freezes. A mis-typed year should be catchable before it is permanent.
        raise InvalidDateOfBirth("Please confirm the date before continuing.")

    try:
        user.confirm_date_of_birth(payload.date_of_birth, date.today())
    except ValueError as exc:
        # Reached only for a future date or an age above the upper bound.
        # Never for under-13 -- that path sets the flag and returns normally.
        raise InvalidDateOfBirth(detail={"reason": str(exc)}) from exc

    await session.flush()

    if user.scan_access_blocked:
        # Logged without the date and without the age.
        logger.info("scan access withdrawn on age at confirmation")

    return DateOfBirthResponse(
        age_band=user.age_band,
        scan_access_blocked=user.scan_access_blocked,
    )


# ---------------------------------------------------------------------------
# FR-ONB-005
# ---------------------------------------------------------------------------


@router.post("/me/safety-answers", response_model=SafetyAnswersResponse)
async def set_safety_answers(
    payload: SafetyAnswersRequest,
    user: CurrentUser,
    session: SessionDep,
) -> SafetyAnswersResponse:
    """
    Record all four safety answers. Updatable, and audited on every change.

    Questions 1 and 2 restrict which ingredients the rules engine may select
    (FR-REC-003). Questions 3 and 4 set `referral_flag`, which stops a scan
    before any image is captured (FR-TRI-001).
    """
    is_first_submission = not user.safety_answers_completed

    previous_answers = (
        None
        if is_first_submission
        else {
            "SQ1": user.is_pregnant,
            "SQ2": user.on_prescription_treatment,
            "referralFlag": user.referral_flag,
        }
    )
    referral_before = None if is_first_submission else user.referral_flag

    user.is_pregnant = payload.pregnant_or_breastfeeding
    user.on_prescription_treatment = payload.prescription_acne_treatment
    user.referral_flag = payload.sets_referral_flag
    user.safety_answers_completed = True

    session.add(
        SafetyAnswerChange(
            user_auth_id=user.auth_id,
            previous_answers=previous_answers,
            new_answers=payload.as_record(),
            referral_flag_before=referral_before,
            referral_flag_after=user.referral_flag,
        )
    )

    if referral_before and not user.referral_flag:
        # The direction that matters: a user moving from "cannot scan" to "can".
        # This is precisely the transition the audit table exists to make
        # visible, so it is logged at warning rather than info.
        logger.warning("referral flag cleared by user edit auth_id=%s", user.auth_id)

    await session.flush()

    return SafetyAnswersResponse(
        referral_flag=user.referral_flag,
        onboarding_complete=user.onboarding_complete,
    )


# ---------------------------------------------------------------------------
# FR-ONB-006
# ---------------------------------------------------------------------------


@router.post("/me/skin-type", response_model=SkinTypeResponse)
async def set_skin_type(
    payload: SkinTypeRequest,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
) -> SkinTypeResponse:
    """
    Score the questionnaire and store the result. Once.

    The client sends raw answers, not a chosen type. Scoring happens here for
    two reasons: FR-ONB-006 requires the same answers to always produce the same
    type, which a client cannot guarantee across versions; and CON-003 keeps
    clinical judgement out of application code, so the weights live in
    `app/clinical/questionnaire/` where a reviewer can read them.

    Nothing about the image contributes. Skin type reflects sebum production
    over time; a photograph captures one moment confounded by lighting and
    recent cleansing.
    """
    if user.skin_type is not None:
        raise ImmutableField()

    questionnaire = get_questionnaire(settings.questionnaire_path)

    try:
        skin_type, totals = score(questionnaire, payload.answers)
    except IncompleteAnswers as exc:
        # Usually a stale client running against a newer questionnaire version.
        # Partial scoring is not offered: a type derived from four of six
        # answers is indistinguishable afterwards from one derived from all six.
        raise InvalidQuestionnaireAnswers(detail={"reason": str(exc)}) from exc

    user.set_skin_type(skin_type)
    await session.flush()

    # Totals are logged, never returned. A client that can see the weights can
    # work backwards from a desired type to the answers producing it.
    logger.info("skin type determined totals=%s version=%s", totals, questionnaire.version)

    return SkinTypeResponse(
        skin_type=user.skin_type,
        onboarding_complete=user.onboarding_complete,
    )


# ---------------------------------------------------------------------------
# FR-ONB-007
# ---------------------------------------------------------------------------


@router.post("/me/consent", response_model=ConsentResponse)
async def record_consent(
    payload: ConsentRequest,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
) -> ConsentResponse:
    """
    Record the limitations acknowledgement, with the version the user saw.

    Storing the acknowledged version rather than the current one is what makes
    re-acknowledgement work: when the statement is rewritten and the version
    increments, `consent_current` returns false and the screen reappears.
    """
    if payload.acknowledged_version != settings.consent_statement_version:
        # The statement changed between the screen rendering and the tap. The
        # user consented to text that is no longer current, so ask again rather
        # than record a consent to something they did not read.
        raise ConsentVersionMismatch(
            detail={
                "acknowledged": payload.acknowledged_version,
                "current": settings.consent_statement_version,
            }
        )

    user.consent_version = payload.acknowledged_version
    user.consent_at = datetime.now(timezone.utc)
    await session.flush()

    return ConsentResponse(
        consent_version=user.consent_version,
        onboarding_complete=user.onboarding_complete,
    )


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


@router.get("/me", response_model=ProfileResponse)
async def get_profile(user: CurrentUser) -> ProfileResponse:
    """Current state. Returns no date of birth and no age."""
    return ProfileResponse(
        age_band=user.age_band,
        skin_type=user.skin_type,
        referral_flag=user.referral_flag,
        scan_access_blocked=user.scan_access_blocked,
        onboarding_complete=user.onboarding_complete,
        scans_remaining=user.scans_remaining,
        consent_version=user.consent_version,
    )


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(user: CurrentUser, session: SessionDep) -> None:
    """
    DR-002. Immediate hard delete, by decision.

    Scan logs, routines, and safety answer audit rows go with it via
    `ondelete="CASCADE"`. Nothing is retained and nothing is anonymised and
    kept -- "delete" means the row is gone.
    """
    await session.delete(user)
    logger.info("account deleted")