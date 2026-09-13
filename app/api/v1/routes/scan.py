"""
Scan endpoints.

Only eligibility for now. `POST /v1/scans` arrives with the analysis pipeline;
it is deliberately absent rather than stubbed, because a stub that returns a
plausible routine is worse than no endpoint at all.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from app.api.deps import CurrentUser, SettingsDep
from app.schemas.scan import EligibilityResponse
from app.services.eligibility import evaluate_eligibility

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scans", tags=["scan"])


@router.get("/eligibility", response_model=EligibilityResponse)
async def get_eligibility(
    user: CurrentUser,
    settings: SettingsDep,
) -> EligibilityResponse:
    """
    Whether the capture control should be offered.

    The client calls this before opening the camera. FR-TRI-001 requires that a
    user with the referral flag set never has an image captured at all, and a
    check that runs after the shutter cannot satisfy that — the photograph
    already exists by then.

    Note what this endpoint does NOT do: it does not decrement anything, does
    not create a scan record, and grants no permission. The same evaluation
    runs again inside the scan pipeline, so a client calling this and then
    ignoring the answer achieves nothing (FR-SUB-002).
    """
    reason = evaluate_eligibility(user, quota_enforced=settings.quota_enforced)

    return EligibilityResponse(
        can_scan=reason is None,
        reason=reason,
        scans_remaining=user.scans_remaining,
    )