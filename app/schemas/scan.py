"""
Request and response shapes for scanning.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import ScanIneligibilityReason, ScanOutcome


class _Base(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class EligibilityResponse(_Base):
    """
    Whether the capture control should be offered.

    This is a **display convenience** and nothing more. FR-SUB-002 says so
    directly: the server re-evaluates every one of these conditions when a scan
    is actually submitted, and a client that ignores this response gains
    nothing. It exists so the user is told before they frame a photograph
    rather than after.

    `reason` is null when eligible. Where several conditions apply, only the
    first by the ordering in `evaluate_eligibility` is returned — a list of
    four reasons is a worse answer than the one the user can act on.
    """

    can_scan: bool = Field(alias="canScan")
    reason: ScanIneligibilityReason | None = None

    # FR-SUB-001. Shown alongside the control; the server holds the real count.
    scans_remaining: int = Field(alias="scansRemaining")

class ReferralSignal(_Base):
    """
    One finding on the referral screen. FR-TRI-003.

    Carries the observation and, only where FR-AI-007 permits it, the
    association list. The ClinicalSignal identifier is not here: SRS 4.2 says
    identifiers never reach the user, and a client that is never given one
    cannot render it by accident.
    """

    observation: str
    # Omitted from JSON when absent (see `response_model_exclude_none` on the
    # route), so a client cannot render the fixed framing around an empty list.
    associations: list[str] | None = None


class Referral(_Base):
    """
    FR-TRI-003 and FR-TRI-005.

    `kind` tells the client which screen variant to draw. DECLARED is the
    FR-TRI-001 case -- raised by the safety answers, no photo taken, general
    message only. OBSERVED is FR-TRI-002 -- raised by a clinical signal.

    The fixed copy (general message, association framing, interim guidance)
    is not repeated here. It comes from GET /v1/content/strings like every
    other claim (IF-UI-001), so there is exactly one copy of it.
    """

    kind: Literal["DECLARED", "OBSERVED"]
    signals: list[ReferralSignal] = Field(default_factory=list)
    summary: str


class ScanResponse(_Base):
    """
    POST /v1/scans. The result of a scan that reached a terminal outcome.

    Unusable, invalid and unavailable outcomes are not returned here -- they
    are IF-COMM-003 errors with their own codes, because the client handles
    each differently (retake, retry, retry later) and none of them carries a
    result.
    """

    scan_id: str = Field(alias="scanId")
    outcome: ScanOutcome
    referral: Referral | None = None
    concerns: list[dict[str, str]] | None = None
    # Null until the rules engine lands.
    routine: dict | None = None
    scans_remaining: int = Field(alias="scansRemaining")


class ScanBase64Request(_Base):
    """
    The JSON alternative to a multipart upload.

    `imageBase64` is the prepared JPEG, base64-encoded, without a data: prefix.
    The client sends this when multipart fails; see the route for why that
    fallback exists.
    """

    image_base64: str = Field(alias="imageBase64")
    content_type: str = Field(default="image/jpeg", alias="contentType")
