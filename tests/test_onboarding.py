"""
Feature 1 verification. FR-ONB-001 through FR-ONB-008.

Each test names the requirement and the acceptance criterion it covers, so this
file doubles as the evidence trail for SRS Section 7 rather than needing a
separate document to explain what was checked.

What is NOT covered here, and must be verified on the UI:

  * FR-ONB-002 -- that a confirmation screen is presented before the value is
    stored. The API enforces `confirmed: true`; whether a screen actually shows
    the computed age is a Demonstration item.
  * FR-ONB-003 -- that the date of birth screen does not state the minimum age.
    No backend test can see a screen label.
  * FR-ONB-005 -- that all four questions are presented. The API requires four
    answers; presentation is a UI concern.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.core.enums import AgeBand, SkinType
from app.db.models.user import SafetyAnswerChange, User, compute_age
from tests.conftest import (
    ANSWERS_COMBINATION,
    ANSWERS_DRY,
    ANSWERS_OILY,
    TEST_CONSENT_VERSION,
    auth,
)

# ---------------------------------------------------------------------------
# IF-COMM-002 -- authentication
# ---------------------------------------------------------------------------


async def test_protected_route_requires_a_token(client):
    """Unauthenticated requests are rejected."""
    response = await client.get("/v1/me")
    assert response.status_code == 401
    assert response.json()["errorCode"] == "UNAUTHORISED"


async def test_malformed_token_is_rejected(client):
    response = await client.get("/v1/me", headers={"Authorization": "Bearer not-a-token"})
    assert response.status_code == 401


async def test_token_error_does_not_leak_the_reason(client):
    """
    IF-COMM-003. The response distinguishes nothing about why the token failed.
    Telling a prober that a signature was valid but expired is more use to them
    than to a legitimate client, which re-authenticates either way.
    """
    response = await client.get("/v1/me", headers={"Authorization": "Bearer aaa.bbb.ccc"})
    body = response.json()
    assert set(body) == {"errorCode", "message"}
    assert "signature" not in body["message"].lower()


# ---------------------------------------------------------------------------
# FR-ONB-001 -- account creation
# ---------------------------------------------------------------------------


async def test_first_sign_in_creates_an_account(client):
    response = await client.post("/v1/auth/session", headers=auth("new-user"))
    assert response.status_code == 200

    body = response.json()
    assert body["isNewUser"] is True
    assert body["onboardingComplete"] is False
    assert body["needsDateOfBirth"] is True
    assert body["needsSafetyAnswers"] is True
    assert body["needsSkinType"] is True
    assert body["needsConsent"] is True


async def test_returning_user_resolves_to_the_same_account(client, session_factory):
    """
    FR-ONB-001: a returning user of the same provider account resolves to the
    existing record. Verified two ways -- the response says so, and the table
    holds exactly one row.
    """
    first = await client.post("/v1/auth/session", headers=auth("repeat-user"))
    second = await client.post("/v1/auth/session", headers=auth("repeat-user"))

    assert first.json()["isNewUser"] is True
    assert second.json()["isNewUser"] is False

    async with session_factory() as db:
        rows = (await db.execute(select(User))).scalars().all()
        assert len(rows) == 1


async def test_different_subjects_are_different_accounts(client, session_factory):
    await client.post("/v1/auth/session", headers=auth("user-a"))
    await client.post("/v1/auth/session", headers=auth("user-b"))

    async with session_factory() as db:
        rows = (await db.execute(select(User))).scalars().all()
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# FR-ONB-002 -- date of birth
# ---------------------------------------------------------------------------


async def test_date_of_birth_requires_confirmation(client):
    await client.post("/v1/auth/session", headers=auth("dob-user"))
    response = await client.post(
        "/v1/me/date-of-birth",
        headers=auth("dob-user"),
        json={"dateOfBirth": "2000-01-01", "confirmed": False},
    )
    assert response.status_code == 422
    assert response.json()["errorCode"] == "INVALID_DATE_OF_BIRTH"


async def test_date_of_birth_is_immutable_after_confirmation(client):
    """
    The acceptance criterion. An editable date of birth makes the FR-ONB-003
    age gate bypassable, so the second attempt must fail regardless of what it
    contains.
    """
    await client.post("/v1/auth/session", headers=auth("immutable-user"))
    first = await client.post(
        "/v1/me/date-of-birth",
        headers=auth("immutable-user"),
        json={"dateOfBirth": "2000-01-01", "confirmed": True},
    )
    assert first.status_code == 200

    second = await client.post(
        "/v1/me/date-of-birth",
        headers=auth("immutable-user"),
        json={"dateOfBirth": "1995-01-01", "confirmed": True},
    )
    assert second.status_code == 409
    assert second.json()["errorCode"] == "IMMUTABLE_FIELD"


async def test_future_date_is_rejected(client):
    await client.post("/v1/auth/session", headers=auth("future-user"))
    tomorrow = date.today() + timedelta(days=1)
    response = await client.post(
        "/v1/me/date-of-birth",
        headers=auth("future-user"),
        json={"dateOfBirth": tomorrow.isoformat(), "confirmed": True},
    )
    assert response.status_code == 422


async def test_age_above_upper_bound_is_rejected(client):
    """FR-ONB-002 sets an upper bound of 80. This is a range check, not a gate."""
    await client.post("/v1/auth/session", headers=auth("old-user"))
    response = await client.post(
        "/v1/me/date-of-birth",
        headers=auth("old-user"),
        json={"dateOfBirth": "1900-01-01", "confirmed": True},
    )
    assert response.status_code == 422


async def test_no_route_exists_to_change_date_of_birth(client):
    """
    Structural. PATCH and PUT are not merely guarded -- they are not routed at
    all, which is a stronger guarantee than a check that could be removed.
    """
    for method in (client.patch, client.put):
        response = await method(
            "/v1/me/date-of-birth",
            headers=auth("no-patch-user"),
            json={"dateOfBirth": "1990-01-01"},
        )
        assert response.status_code == 405


# ---------------------------------------------------------------------------
# FR-ONB-003 -- under-13
# ---------------------------------------------------------------------------


async def test_under_thirteen_succeeds_and_blocks_scan_access(client):
    """
    THE test for FR-ONB-003, and the one most likely to be broken by a
    well-meaning change.

    The request SUCCEEDS. Rejecting it would state the threshold by implication
    and hand the user a second attempt at clearing it. The account is created,
    kept, and silently barred from scanning.
    """
    await client.post("/v1/auth/session", headers=auth("child-user"))

    child_dob = date.today().replace(year=date.today().year - 12)
    response = await client.post(
        "/v1/me/date-of-birth",
        headers=auth("child-user"),
        json={"dateOfBirth": child_dob.isoformat(), "confirmed": True},
    )

    assert response.status_code == 200, "under-13 must not be rejected at input"
    body = response.json()
    assert body["scanAccessBlocked"] is True
    assert body["ageBand"] is None


async def test_under_thirteen_response_states_no_age_or_threshold(client):
    """
    The response body is as visible to a curious user as a screen label. It
    carries the flag and nothing that would reveal what the threshold is.
    """
    await client.post("/v1/auth/session", headers=auth("child-quiet"))
    child_dob = date.today().replace(year=date.today().year - 10)
    response = await client.post(
        "/v1/me/date-of-birth",
        headers=auth("child-quiet"),
        json={"dateOfBirth": child_dob.isoformat(), "confirmed": True},
    )

    body = response.json()
    assert set(body) == {"ageBand", "scanAccessBlocked"}
    assert "13" not in response.text


async def test_scan_block_persists_across_requests(client):
    """FR-ONB-003: the restriction persists across application restarts."""
    await client.post("/v1/auth/session", headers=auth("child-persist"))
    child_dob = date.today().replace(year=date.today().year - 11)
    await client.post(
        "/v1/me/date-of-birth",
        headers=auth("child-persist"),
        json={"dateOfBirth": child_dob.isoformat(), "confirmed": True},
    )

    later = await client.post("/v1/auth/session", headers=auth("child-persist"))
    assert later.json()["scanAccessBlocked"] is True


# ---------------------------------------------------------------------------
# FR-ONB-004 -- age band
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("years_ago", "expected"),
    [
        (13, AgeBand.MINOR),
        (17, AgeBand.MINOR),
        (18, AgeBand.ADULT),
        (40, AgeBand.ADULT),
    ],
)
async def test_age_band_is_derived_from_date_of_birth(client, years_ago, expected):
    subject = f"band-{years_ago}"
    await client.post("/v1/auth/session", headers=auth(subject))
    dob = date.today().replace(year=date.today().year - years_ago)

    response = await client.post(
        "/v1/me/date-of-birth",
        headers=auth(subject),
        json={"dateOfBirth": dob.isoformat(), "confirmed": True},
    )
    assert response.json()["ageBand"] == expected.value


def test_age_calculation_before_and_after_birthday():
    """The boundary the age band depends on. Pure function, no HTTP needed."""
    dob = date(2010, 6, 15)
    assert compute_age(dob, date(2026, 6, 14)) == 15
    assert compute_age(dob, date(2026, 6, 15)) == 16


# ---------------------------------------------------------------------------
# FR-ONB-005 -- safety screening
# ---------------------------------------------------------------------------


async def test_all_four_answers_are_required(client):
    await client.post("/v1/auth/session", headers=auth("partial-user"))
    response = await client.post(
        "/v1/me/safety-answers",
        headers=auth("partial-user"),
        json={"pregnantOrBreastfeeding": False, "prescriptionAcneTreatment": False},
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("field", "expected_flag"),
    [
        ("pregnantOrBreastfeeding", False),
        ("prescriptionAcneTreatment", False),
        ("openWoundsOrChangingMole", True),
        ("diagnosedEczemaPsoriasisRosacea", True),
    ],
)
async def test_only_questions_three_and_four_set_the_referral_flag(
    client, field, expected_flag
):
    """
    Questions 1 and 2 restrict ingredient selection (FR-REC-003). Questions 3
    and 4 stop the scan entirely (FR-TRI-001). Confusing the two would either
    block users needlessly or let a wound through to a cosmetic routine.
    """
    subject = f"safety-{field}"
    await client.post("/v1/auth/session", headers=auth(subject))

    answers = {
        "pregnantOrBreastfeeding": False,
        "prescriptionAcneTreatment": False,
        "openWoundsOrChangingMole": False,
        "diagnosedEczemaPsoriasisRosacea": False,
    }
    answers[field] = True

    response = await client.post(
        "/v1/me/safety-answers", headers=auth(subject), json=answers
    )
    assert response.json()["referralFlag"] is expected_flag


async def test_clearing_the_referral_flag_is_audited(client, session_factory):
    """
    The transition the audit table exists for. A user moving from "cannot scan"
    to "can scan" is the one edit with a safety consequence, and it must leave
    a record that distinguishes it from an honest correction.
    """
    subject = "audit-user"
    await client.post("/v1/auth/session", headers=auth(subject))

    answers = {
        "pregnantOrBreastfeeding": False,
        "prescriptionAcneTreatment": False,
        "openWoundsOrChangingMole": True,
        "diagnosedEczemaPsoriasisRosacea": False,
    }
    await client.post("/v1/me/safety-answers", headers=auth(subject), json=answers)

    answers["openWoundsOrChangingMole"] = False
    cleared = await client.post(
        "/v1/me/safety-answers", headers=auth(subject), json=answers
    )
    assert cleared.json()["referralFlag"] is False

    async with session_factory() as db:
        rows = (
            (await db.execute(select(SafetyAnswerChange).order_by(SafetyAnswerChange.changed_at)))
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert rows[1].referral_flag_before is True
        assert rows[1].referral_flag_after is False


# ---------------------------------------------------------------------------
# FR-ONB-006 -- skin type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        (ANSWERS_COMBINATION, SkinType.COMBINATION),
        (ANSWERS_OILY, SkinType.OILY),
        (ANSWERS_DRY, SkinType.DRY),
    ],
)
async def test_questionnaire_resolves_to_a_skin_type(client, answers, expected):
    subject = f"skin-{expected.value.lower()}"
    await client.post("/v1/auth/session", headers=auth(subject))
    response = await client.post(
        "/v1/me/skin-type", headers=auth(subject), json={"answers": answers}
    )
    assert response.status_code == 200
    assert response.json()["skinType"] == expected.value


async def test_scoring_is_deterministic(client):
    """
    FR-ONB-006's acceptance criterion. Run against two accounts because the
    endpoint refuses a second submission on one.
    """
    results = []
    for i in range(3):
        subject = f"determinism-{i}"
        await client.post("/v1/auth/session", headers=auth(subject))
        response = await client.post(
            "/v1/me/skin-type",
            headers=auth(subject),
            json={"answers": ANSWERS_COMBINATION},
        )
        results.append(response.json()["skinType"])

    assert len(set(results)) == 1


async def test_skin_type_is_immutable(client):
    """"Completed once during onboarding" is read strictly: no retakes."""
    await client.post("/v1/auth/session", headers=auth("skin-immutable"))
    await client.post(
        "/v1/me/skin-type",
        headers=auth("skin-immutable"),
        json={"answers": ANSWERS_OILY},
    )
    second = await client.post(
        "/v1/me/skin-type",
        headers=auth("skin-immutable"),
        json={"answers": ANSWERS_DRY},
    )
    assert second.status_code == 409
    assert second.json()["errorCode"] == "IMMUTABLE_FIELD"


async def test_incomplete_answers_are_rejected(client):
    """
    Partial scoring is not offered. A type from four of six answers is
    indistinguishable afterwards from one derived from all six.
    """
    await client.post("/v1/auth/session", headers=auth("skin-partial"))
    response = await client.post(
        "/v1/me/skin-type",
        headers=auth("skin-partial"),
        json={"answers": {"Q1": "Q1A", "Q2": "Q2A"}},
    )
    assert response.status_code == 422
    assert response.json()["errorCode"] == "INVALID_QUESTIONNAIRE_ANSWERS"


async def test_unknown_option_id_is_rejected(client):
    await client.post("/v1/auth/session", headers=auth("skin-bogus"))
    bogus = dict(ANSWERS_OILY, Q1="Q1Z")
    response = await client.post(
        "/v1/me/skin-type", headers=auth("skin-bogus"), json={"answers": bogus}
    )
    assert response.status_code == 422


async def test_questionnaire_does_not_expose_scoring_weights(client):
    """
    A client that can see the weights can work backwards from a desired skin
    type to the answers producing it -- and the entire routine is built on that
    value.
    """
    response = await client.get("/v1/content/questionnaire")
    assert response.status_code == 200
    assert "scores" not in response.text
    assert len(response.json()["questions"]) == 6


# ---------------------------------------------------------------------------
# FR-ONB-007 -- consent
# ---------------------------------------------------------------------------


async def test_consent_is_recorded_with_its_version(client):
    await client.post("/v1/auth/session", headers=auth("consent-user"))
    response = await client.post(
        "/v1/me/consent",
        headers=auth("consent-user"),
        json={"acknowledgedVersion": TEST_CONSENT_VERSION},
    )
    assert response.status_code == 200
    assert response.json()["consentVersion"] == TEST_CONSENT_VERSION


async def test_stale_consent_version_is_refused(client):
    """
    Recording this would store agreement to text the user never read, which is
    exactly what the version field exists to prevent.
    """
    await client.post("/v1/auth/session", headers=auth("stale-consent"))
    response = await client.post(
        "/v1/me/consent",
        headers=auth("stale-consent"),
        json={"acknowledgedVersion": "1.0"},
    )
    assert response.status_code == 409
    assert response.json()["errorCode"] == "CONSENT_VERSION_MISMATCH"


async def test_limitations_statement_is_served_from_the_string_resource(client):
    """
    IF-UI-001. It comes from the server, and it says what the system actually
    does -- a consent statement that misdescribes behaviour provides no
    protection.
    """
    response = await client.get("/v1/content/strings")
    body = response.json()
    statement = body["limitationsStatement"]

    # The consent version is its own field, separate from the bundle version.
    assert body["consentVersion"] == TEST_CONSENT_VERSION
    assert "not a medical device" in statement
    assert "cannot diagnose" in statement


# ---------------------------------------------------------------------------
# FR-ONB-008 -- clinical review claim
# ---------------------------------------------------------------------------


async def test_no_review_claim_without_a_signed_record(client):
    """
    The default state, and a requirement in its own right: where no signed
    record exists, no review claim appears anywhere in the application.
    """
    response = await client.get("/v1/content/review-claim")
    body = response.json()

    assert body["substantiated"] is False
    assert body["reviewer"] is None
    assert "reviewed by" not in body["claim"].lower()


async def test_strings_bundle_carries_the_unsubstantiated_claim(client):
    response = await client.get("/v1/content/strings")
    assert "reviewed by a licensed practitioner" not in response.text


# ---------------------------------------------------------------------------
# SRS 4.3 -- the gate
# ---------------------------------------------------------------------------


async def test_onboarding_completes_only_after_all_four_steps(client):
    """
    Every step must be satisfied before a scan is permitted. Asserted after each
    one so a regression names the step that broke it.
    """
    subject = "full-flow"
    session = await client.post("/v1/auth/session", headers=auth(subject))
    assert session.json()["onboardingComplete"] is False

    await client.post(
        "/v1/me/date-of-birth",
        headers=auth(subject),
        json={"dateOfBirth": "2000-01-01", "confirmed": True},
    )
    assert (await client.get("/v1/me", headers=auth(subject))).json()["onboardingComplete"] is False

    await client.post(
        "/v1/me/safety-answers",
        headers=auth(subject),
        json={
            "pregnantOrBreastfeeding": False,
            "prescriptionAcneTreatment": False,
            "openWoundsOrChangingMole": False,
            "diagnosedEczemaPsoriasisRosacea": False,
        },
    )
    assert (await client.get("/v1/me", headers=auth(subject))).json()["onboardingComplete"] is False

    await client.post(
        "/v1/me/skin-type", headers=auth(subject), json={"answers": ANSWERS_COMBINATION}
    )
    assert (await client.get("/v1/me", headers=auth(subject))).json()["onboardingComplete"] is False

    await client.post(
        "/v1/me/consent",
        headers=auth(subject),
        json={"acknowledgedVersion": TEST_CONSENT_VERSION},
    )

    profile = (await client.get("/v1/me", headers=auth(subject))).json()
    assert profile["onboardingComplete"] is True
    assert profile["skinType"] == SkinType.COMBINATION.value
    assert profile["ageBand"] == AgeBand.ADULT.value
    assert profile["scansRemaining"] == 1


async def test_profile_returns_no_date_of_birth_or_age(client):
    """One fewer place for a restricted field to leak."""
    subject = "no-leak"
    await client.post("/v1/auth/session", headers=auth(subject))
    await client.post(
        "/v1/me/date-of-birth",
        headers=auth(subject),
        json={"dateOfBirth": "1995-03-20", "confirmed": True},
    )

    response = await client.get("/v1/me", headers=auth(subject))
    assert "1995" not in response.text
    assert "dateOfBirth" not in response.json()


# ---------------------------------------------------------------------------
# DR-002 -- deletion
# ---------------------------------------------------------------------------


async def test_account_deletion_removes_the_user_and_its_audit_rows(client, session_factory):
    """
    Immediate hard delete, by decision. Cascade covers the audit rows, so
    "delete" means the row is gone rather than hidden.
    """
    subject = "delete-me"
    await client.post("/v1/auth/session", headers=auth(subject))
    await client.post(
        "/v1/me/safety-answers",
        headers=auth(subject),
        json={
            "pregnantOrBreastfeeding": True,
            "prescriptionAcneTreatment": False,
            "openWoundsOrChangingMole": False,
            "diagnosedEczemaPsoriasisRosacea": False,
        },
    )

    deleted = await client.delete("/v1/me", headers=auth(subject))
    assert deleted.status_code == 204

    async with session_factory() as db:
        assert (await db.execute(select(User))).scalars().all() == []
        assert (await db.execute(select(SafetyAnswerChange))).scalars().all() == []


async def test_a_live_token_for_a_deleted_account_is_unauthorised(client):
    """
    Not a 404. The account is gone and the correct client behaviour is to sign
    in again, not to show a missing-resource screen.
    """
    subject = "deleted-token"
    await client.post("/v1/auth/session", headers=auth(subject))
    await client.delete("/v1/me", headers=auth(subject))

    response = await client.get("/v1/me", headers=auth(subject))
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# CON-001 / DR-001 -- Zero-Save Policy, structural half
# ---------------------------------------------------------------------------


def test_no_table_has_an_image_shaped_column():
    """
    SRS 6.2 lists ScanLog.imagePayload as an attribute that must NOT exist as a
    persisted column. Section 7 verifies this by inspection; an inspection done
    once is worth less than an assertion that runs on every commit.
    """
    from sqlalchemy import LargeBinary

    from app.db.base import Base

    image_words = {"image", "photo", "picture", "payload", "img", "selfie", "frame"}
    offenders = []

    for table in Base.metadata.tables.values():
        for column in table.columns:
            if any(word in column.name.lower() for word in image_words):
                offenders.append(f"{table.name}.{column.name}")
            if isinstance(column.type, LargeBinary):
                offenders.append(f"{table.name}.{column.name} (binary)")

    assert offenders == [], f"image-shaped columns found: {offenders}"