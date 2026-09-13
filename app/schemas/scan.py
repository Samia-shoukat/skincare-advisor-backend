"""
Request and response shapes for scanning.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import ScanIneligibilityReason


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