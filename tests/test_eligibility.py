"""
Scan eligibility. FR-SUB-002, FR-TRI-001, FR-ONB-003.

These test the function rather than the endpoint, because that is where the
logic lives — and because the same function is called from the scan pipeline,
so testing it once covers both callers.

The ordering tests matter more than they look. A user can easily be blocked for
several reasons at once, and which one is reported decides what they see and
whether it is anything they can act on.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.core.enums import AgeBand, ScanIneligibilityReason, SkinType
from app.db.models.user import User
from app.services.eligibility import evaluate_eligibility


def make_user(**overrides) -> User:
    """A fully onboarded, eligible user. Tests break one thing at a time."""
    now = datetime.now(timezone.utc)
    defaults = dict(
        auth_id="test",
        auth_provider="email",
        date_of_birth=date(2000, 1, 1),
        dob_confirmed_at=now,
        age_band=AgeBand.ADULT,
        scan_access_blocked=False,
        is_pregnant=False,
        on_prescription_treatment=False,
        referral_flag=False,
        safety_answers_completed=True,
        skin_type=SkinType.COMBINATION,
        skin_type_set_at=now,
        consent_version="1.2",
        consent_at=now,
        scans_remaining=1,
    )
    return User(**{**defaults, **overrides})


# ---------------------------------------------------------------------------


def test_fully_onboarded_user_may_scan():
    assert evaluate_eligibility(make_user(), quota_enforced=True) is None


@pytest.mark.parametrize(
    "missing",
    [
        {"dob_confirmed_at": None},
        {"safety_answers_completed": False},
        {"skin_type": None},
        {"consent_at": None},
    ],
)
def test_incomplete_onboarding_blocks_scanning(missing):
    """SRS 4.3: every onboarding step is satisfied before a scan is permitted."""
    user = make_user(**missing)
    assert (
        evaluate_eligibility(user, quota_enforced=True)
        is ScanIneligibilityReason.ONBOARDING_INCOMPLETE
    )


def test_restricted_account_cannot_scan():
    """FR-ONB-003."""
    user = make_user(scan_access_blocked=True)
    assert (
        evaluate_eligibility(user, quota_enforced=True)
        is ScanIneligibilityReason.SCAN_ACCESS_BLOCKED
    )


def test_referral_flag_blocks_scanning():
    """
    FR-TRI-001. This is the reason the check exists at all: the requirement is
    that no image is captured or transmitted while the flag is set, which can
    only hold if the answer is available before the camera opens.
    """
    user = make_user(referral_flag=True)
    assert (
        evaluate_eligibility(user, quota_enforced=True)
        is ScanIneligibilityReason.REFERRAL_REQUIRED
    )


def test_exhausted_quota_blocks_scanning():
    """FR-SUB-001."""
    user = make_user(scans_remaining=0)
    assert (
        evaluate_eligibility(user, quota_enforced=True)
        is ScanIneligibilityReason.QUOTA_EXHAUSTED
    )


def test_quota_can_be_disabled_for_testing():
    """
    FR-SUB-004. Note the narrowness: turning enforcement off lifts the quota and
    nothing else. A restricted or flagged user is still refused.
    """
    user = make_user(scans_remaining=0)
    assert evaluate_eligibility(user, quota_enforced=False) is None


def test_disabled_quota_does_not_lift_the_referral_flag():
    user = make_user(scans_remaining=0, referral_flag=True)
    assert (
        evaluate_eligibility(user, quota_enforced=False)
        is ScanIneligibilityReason.REFERRAL_REQUIRED
    )


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_referral_outranks_quota():
    """
    A flagged user with no scans left is told to see a doctor, not told to come
    back next month. Reporting the quota here would suggest that waiting fixes
    it, which is the opposite of the intended message.
    """
    user = make_user(referral_flag=True, scans_remaining=0)
    assert (
        evaluate_eligibility(user, quota_enforced=True)
        is ScanIneligibilityReason.REFERRAL_REQUIRED
    )


def test_restriction_outranks_referral():
    """
    FR-ONB-003 is permanent; a referral is not. Reporting the referral would
    imply that seeing a doctor restores access, and it does not.
    """
    user = make_user(scan_access_blocked=True, referral_flag=True)
    assert (
        evaluate_eligibility(user, quota_enforced=True)
        is ScanIneligibilityReason.SCAN_ACCESS_BLOCKED
    )


def test_incomplete_onboarding_outranks_everything():
    """Nothing below it is meaningful on a profile that cannot produce a routine."""
    user = make_user(
        skin_type=None,
        scan_access_blocked=True,
        referral_flag=True,
        scans_remaining=0,
    )
    assert (
        evaluate_eligibility(user, quota_enforced=True)
        is ScanIneligibilityReason.ONBOARDING_INCOMPLETE
    )


def test_only_one_reason_is_returned():
    """
    A user shown four problems at once acts on none. The return type being a
    single value rather than a list is the design; this asserts it stays that
    way.
    """
    user = make_user(referral_flag=True, scans_remaining=0)
    result = evaluate_eligibility(user, quota_enforced=True)
    assert not isinstance(result, (list, tuple, set))