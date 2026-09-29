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

import base64
import binascii
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, File, Header, UploadFile
from sqlalchemy import select

from app.api.deps import CurrentUser, PipelineDep, SessionDep, SettingsDep
from app.core.enums import ScanIneligibilityReason, ScanOutcome
from app.core.errors import (
    ImageUnusable,
    NotReferred,
    OnboardingIncomplete,
    QuotaExceeded,
    ReferralRequired,
    ScanAccessBlocked,
)
from app.schemas.scan import (
    EligibilityResponse,
    Referral,
    ReferralSignal,
    ScanBase64Request,
    ScanResponse,
)
from app.services.eligibility import evaluate_eligibility
from app.services.quota import find_completed_scan
from app.db.models.routine import Routine
from app.services.referral_summary import build_summary, latest_safety_answers
from app.services.routine_service import routine_response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scans", tags=["scan"])

# Generous enough for a prepared photograph, tight enough that a mistake or an
# abuse attempt is refused before it is read into memory.
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# FR-CAM-003 prepares a JPEG. PNG and WebP are accepted because the web build
# of the client may not re-encode, and all three are formats the provider reads.
ALLOWED_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})


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


@router.get(
    "/referral",
    response_model=Referral,
    response_model_exclude_none=True,
)
async def get_declared_referral(user: CurrentUser, session: SessionDep) -> Referral:
    """
    FR-TRI-001, UC-003. The referral for a user flagged by their own answers.

    Served without a scan, because FR-TRI-001 requires that no image is captured
    or transmitted while `referral_flag` is set. The client reaches this when
    eligibility returns REFERRAL_REQUIRED, instead of opening the camera.

    Refused for an unflagged user. A referral screen shown to someone with no
    referral condition would be telling them to see a doctor for no reason.
    """
    if not user.referral_flag:
        raise NotReferred()

    answers = await latest_safety_answers(session, user)
    return Referral(
        kind="DECLARED",
        signals=[],
        summary=build_summary(
            observations=[],
            scanned_at=datetime.now(timezone.utc),
            answers=answers,
        ),
    )


# Registered with and without the trailing slash. Without the second route,
# POST /v1/scans gets a 307 redirect before the upload is read, and most HTTP
# clients re-send a multipart body badly on redirect -- the user sees a
# confusing 400 instead of their scan.
@router.post("", response_model=ScanResponse, response_model_exclude_none=True, include_in_schema=False)
@router.post("/", response_model=ScanResponse, response_model_exclude_none=True)
async def create_scan(
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    pipeline: PipelineDep,
    image: UploadFile = File(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ScanResponse:
    """
    Run a scan. SRS 4.2 processing order, stages 1 through 8.

    Stages 1 and 2 (quota, referral flag) are evaluated here, before the file
    is read -- an ineligible request should not cost a byte of image transfer,
    let alone a provider call (FR-SUB-002). Everything after that is the
    pipeline's.
    """
    # FR-SUB-003. A retry of a scan that already completed returns the same
    # result instead of running again -- no second provider call, no second
    # decrement.
    #
    # Checked BEFORE eligibility, deliberately. The scan being retried may be
    # the one that used the last of the allowance, and a retry that then failed
    # the quota check would tell a user who received a routine that they have
    # none. A replay is not a new scan, and it is scoped to this user's own
    # earlier result, so it grants nothing eligibility would have refused.
    previous = await find_completed_scan(session, user, idempotency_key)
    if previous is not None and previous.outcome is ScanOutcome.ROUTINE:
        saved = await session.scalar(select(Routine).where(Routine.scan_log_id == previous.id))
        return ScanResponse(
            scan_id=previous.id,
            outcome=previous.outcome,
            concerns=previous.concerns,
            routine=routine_response(saved) if saved else None,
            scans_remaining=user.scans_remaining,
        )

    # --- stages 1 and 2 ---------------------------------------------------
    reason = evaluate_eligibility(user, quota_enforced=settings.quota_enforced)
    if reason is not None:
        raise _to_error(reason)

    # --- stage 3 (server half) --------------------------------------------
    # The capture quality gate itself runs on the device (FR-CAM-001). What the
    # server can check is that something image-shaped and sensibly sized
    # arrived.
    contents = await image.read()
    await image.close()
    content_type = image.content_type or "image/jpeg"

    return await _run_scan(
        session, user, settings, pipeline, contents, content_type, idempotency_key
    )


@router.post(
    "/base64",
    response_model=ScanResponse,
    response_model_exclude_none=True,
)
async def create_scan_base64(
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    pipeline: PipelineDep,
    payload: ScanBase64Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ScanResponse:
    """
    The same scan, with the image as base64 in a JSON body.

    Exists because multipart uploads from React Native proved unreliable in the
    field: the request failed on the device before reaching the server, with no
    server-side trace to diagnose. A JSON body is one code path in the client
    and one in okhttp, with no streamed file handle and no boundary.

    The cost is about 33% more bytes on the wire than multipart, which is why
    this is the fallback and not the default. Everything after the decode is
    the same pipeline, so the Zero-Save Policy and the processing order are
    unchanged -- the bytes live in memory and are never written anywhere.
    """
    previous = await find_completed_scan(session, user, idempotency_key)
    if previous is not None and previous.outcome is ScanOutcome.ROUTINE:
        saved = await session.scalar(select(Routine).where(Routine.scan_log_id == previous.id))
        return ScanResponse(
            scan_id=previous.id,
            outcome=previous.outcome,
            concerns=previous.concerns,
            routine=routine_response(saved) if saved else None,
            scans_remaining=user.scans_remaining,
        )

    reason = evaluate_eligibility(user, quota_enforced=settings.quota_enforced)
    if reason is not None:
        raise _to_error(reason)

    try:
        contents = base64.b64decode(payload.image_base64, validate=True)
    except (binascii.Error, ValueError):
        raise ImageUnusable("That photo couldn't be read.") from None

    return await _run_scan(
        session, user, settings, pipeline, contents, payload.content_type, idempotency_key
    )


async def _run_scan(
    session,
    user,
    settings,
    pipeline,
    contents: bytes,
    content_type: str,
    idempotency_key: str | None,
) -> ScanResponse:
    """
    Stages 3 (server half) through 8, shared by both upload shapes.

    Kept in one function so the two entry points cannot drift: a size limit or
    a referral rule added for one would otherwise silently miss the other.
    """
    if len(contents) > MAX_IMAGE_BYTES:
        raise ImageUnusable("That photo is too large.")
    if not contents:
        raise ImageUnusable("No image was received.")
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise ImageUnusable("That file isn't a photo we can use.")

    try:
        result = await pipeline.run(
            session,
            user,
            contents,
            content_type=content_type,
            idempotency_key=idempotency_key,
        )
    finally:
        # CON-001. Dropped explicitly rather than left to go out of scope at
        # the end of the function, so that nothing added below this line later
        # can hold on to it by accident.
        del contents

    if result.outcome is ScanOutcome.REFERRAL:
        answers = await latest_safety_answers(session, user)
        return ScanResponse(
            scan_id=result.scan_id,
            outcome=result.outcome,
            referral=Referral(
                kind="OBSERVED",
                signals=[ReferralSignal(**s) for s in result.referral_signals],
                summary=build_summary(
                    observations=[s["observation"] for s in result.referral_signals],
                    scanned_at=datetime.now(timezone.utc),
                    answers=answers,
                ),
            ),
            scans_remaining=result.scans_remaining,
        )

    return ScanResponse(
        scan_id=result.scan_id,
        outcome=result.outcome,
        concerns=result.concerns,
        routine=result.routine,
        scans_remaining=result.scans_remaining,
    )


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
        # the image is refused here before it is read, and the distinct code
        # lets the client route to the referral screen rather than a setup one.
        ScanIneligibilityReason.REFERRAL_REQUIRED: ReferralRequired(),
        ScanIneligibilityReason.QUOTA_EXHAUSTED: QuotaExceeded(),
    }[reason]