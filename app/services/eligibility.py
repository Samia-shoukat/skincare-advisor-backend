"""
Scan eligibility.

One function, deliberately. The same evaluation answers two questions — "should
the capture control be enabled" and "may this scan proceed" — and the only way
to be sure those never drift apart is for there to be one implementation of it.

FR-SUB-002 states that a client-side gate is a display convenience. That is
exactly what the eligibility endpoint serves, and it is why this module is
called from both places.
"""

from __future__ import annotations

from app.core.enums import ScanIneligibilityReason
from app.db.models.user import User


def evaluate_eligibility(
    user: User, *, quota_enforced: bool
) -> ScanIneligibilityReason | None:
    """
    Return the first reason this user may not scan, or None.

    The order is not arbitrary; each check is meaningless unless the ones above
    it have passed.

    1. Onboarding. A profile without a skin type or safety answers cannot
       produce a routine at all, so nothing below is worth evaluating.
    2. Age restriction (FR-ONB-003). Withdrawn access is permanent, and no
       later condition can restore it.
    3. Referral flag (FR-TRI-001). The requirement is that no image is captured
       or transmitted while this is set — which means this must be answerable
       before the camera opens, not after a photograph exists.
    4. Quota (FR-SUB-001). Last, because it is the only reason that is
       temporary and the only one a user can do something about.

    Returning one reason rather than all of them is deliberate. A user shown
    four problems at once acts on none.
    """
    if not user.onboarding_complete:
        return ScanIneligibilityReason.ONBOARDING_INCOMPLETE

    if user.scan_access_blocked:
        return ScanIneligibilityReason.SCAN_ACCESS_BLOCKED

    if user.referral_flag:
        return ScanIneligibilityReason.REFERRAL_REQUIRED

    # FR-SUB-004 allows enforcement to be switched off so end-to-end tests can
    # run repeatedly. Production keeps it on, and /readyz complains if it does
    # not.
    if quota_enforced and user.scans_remaining <= 0:
        return ScanIneligibilityReason.QUOTA_EXHAUSTED

    return None