"""
Scan endpoints.

`POST /v1/scans` is where the Zero-Save Policy either holds or does not. The
image arrives as an upload, is read into memory, passed to the pipeline, and
goes out of scope. It is never written to disk, never cached, never logged, and
there is no column anywhere capable of storing it (CON-001, DR-001).

The eligibility endpoint exists so the client can disable the capture control
before a photograph is framed — FR-TRI-001 requires that a flagged user never
has an image captured at all, and a check that runs after the shutter cannot
deliver that.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, File, Form, Header, UploadFile

from app.api.deps import CurrentUser, SessionDep, SettingsDep
from app.core.enums import ScanIneligibilityReason
from app.core.errors import (
    AnalysisInvalid,
    ImageUnusable,
    OnboardingIncomplete,
    ProviderUnavailableError,
    QuotaExceeded,
    ScanAccessBlocked,
)
from app.schemas.scan import EligibilityResponse
from app.services.eligibility import evaluate_eligibility

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scans", tags=["scan"])

# Generous enough for a prepared photograph, tight enough that a mistake or an
# abuse attempt is refused before it is read into memory.
MAX_IMAGE_BYTES = 8 * 1024 * 1024


@router.get("/eligibility", response_model=EligibilityResponse)
async def get_eligibility(
    user: CurrentUser,
    settings: SettingsDep,
) -> EligibilityResponse:
    """
    Whether the capture control should be offered.

    A display convenience and nothing more (FR-SUB-002). The same evaluation
    runs again below, so a client that ignores this answer gains nothing.
    """
    reason = evaluate_eligibility(user, quota_enforced=settings.quota_enforced)

    return EligibilityResponse(
        can_scan=reason is None,
        reason=reason,
        scans_remaining=user.scans_remaining,
    )


@router.post("/")
async def create_scan(
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    image: UploadFile = File(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    """
    Run a scan.

    Eligibility is re-evaluated here rather than trusted from the client, and
    it happens before the file is read — an ineligible request should not cost
    a single byte of image transfer, let alone a provider call (FR-SUB-002).
    """
    reason = evaluate_eligibility(user, quota_enforced=settings.quota_enforced)
    if reason is not None:
        raise _to_error(reason)

    contents = await image.read()

    if len(contents) > MAX_IMAGE_BYTES:
        raise ImageUnusable("That photo is too large.")
    if not contents:
        raise ImageUnusable("No image was received.")

    logger.info("scan accepted: %d bytes", len(contents))

    # The pipeline is wired in the next step. Returning the size proves the
    # image arrived intact without storing it or echoing it back — a stub that
    # returned a plausible routine would be worse than no endpoint at all,
    # because it would look like it worked.
    return {
        "status": "received",
        "bytes": len(contents),
        "note": "Analysis pipeline not yet wired.",
    }


def _to_error(reason: ScanIneligibilityReason):
    """
    Map an ineligibility reason to its HTTP error.

    Kept as a mapping rather than branches inside the endpoint so that adding a
    reason forces a decision about what the client sees, instead of silently
    falling through to a generic refusal.
    """
    return {
        ScanIneligibilityReason.ONBOARDING_INCOMPLETE: OnboardingIncomplete(),
        ScanIneligibilityReason.SCAN_ACCESS_BLOCKED: ScanAccessBlocked(),
        # FR-TRI-001. The client should have prevented capture; if it did not,
        # the image is refused here and never reaches the provider.
        ScanIneligibilityReason.REFERRAL_REQUIRED: OnboardingIncomplete(
            "Please see a healthcare professional before scanning."
        ),
        ScanIneligibilityReason.QUOTA_EXHAUSTED: QuotaExceeded(),
    }[reason]