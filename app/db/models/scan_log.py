"""
The ScanLog entity. SRS 6.2, DR-003, DR-008.

An audit record of a scan attempt, and the evidence trail for most of the
safety requirements in Sections 4.5 and 4.6. FR-TRI-002 requires a referral to
record its triggering signal identifiers; FR-AI-008 requires a suppression to
record its reason; FR-AI-004 requires the backend that served each task;
FR-AI-009 requires the model version where one served it; NFR-SAFE-007 requires
every condition name ever displayed to a user to be recoverable afterwards.
None of those are satisfiable without a row per scan.

## The column that is not here

SRS 6.2 lists `imagePayload` with the constraint "Must not exist as a persisted
column". It is the only row in the data dictionary that specifies an absence,
and this is the table it is absent from.

That absence is load-bearing rather than incidental. CON-001 and DR-001 require
that no facial image is written to any persistent medium on any device or
server, and the strongest available form of that guarantee is a schema with no
column capable of holding one. `tests/test_zero_save.py` asserts it against
`Base.metadata` rather than against this file, so adding a binary column
anywhere in the package fails the suite, not just adding one here.

## What is safe to store

Identifiers from closed enumerations, and the one free-text field Appendix G
permits. DR-008 requires observation strings to be validated against the
prohibited-terms list *before storage*, not merely before display -- so the
text that lands in `clinical_signals` has already passed
`app.clinical.copy.prohibited_terms`, and a string that failed was replaced
with curated text upstream in `app.schemas.analysis`.

`displayed_associations` is the exception that proves the rule: it is the only
column in the database permitted to contain a condition name, it is written
only from the Appendix H table, and it exists because NFR-SAFE-007 requires
knowing what the system actually said to whom. In release 1.0 every association
row is disabled, so this column is empty on every row -- which is itself the
evidence that naming is off.

## Retention

DR-003: 24 months, or immediately on account deletion. The cascade is declared
on the foreign key so that DR-002's hard delete cannot leave orphans behind,
and so that it holds without an application-level sweep remembering to run.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import AnalysisBackend, ScanOutcome
from app.db.base import Base, new_uuid
from app.db.models.user import JSONType, User


class ScanLog(Base):
    """SRS 6.2. Metadata only. One row per scan attempt, including failures."""

    __tablename__ = "scan_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)

    user_auth_id: Mapped[str] = mapped_column(
        ForeignKey("users.auth_id", ondelete="CASCADE"), index=True
    )

    # --- Outcome ----------------------------------------------------------
    outcome: Mapped[ScanOutcome] = mapped_column(SAEnum(ScanOutcome, name="scan_outcome"))

    # --- Analysis findings ------------------------------------------------
    # [{"concernId": "ACNE", "severity": "MODERATE"}]. Closed vocabulary only,
    # no free text, no condition names (DR-008).
    concerns: Mapped[list | None] = mapped_column(JSONType)

    # [{"signalId": "...", "observation": "...", "confidence": "HIGH",
    #   "observationSource": "PROVIDER"}]. Written where outcome is REFERRAL.
    # `observationSource` records whether the provider's own wording survived
    # DR-008 validation; a run of CURATED values is a prompt regression.
    clinical_signals: Mapped[list | None] = mapped_column(JSONType)

    # NFR-SAFE-007. The condition names actually shown, with the signal and
    # confidence that produced them. The only column permitted to hold one.
    displayed_associations: Mapped[list | None] = mapped_column(JSONType)

    # FR-AI-008. [{"signalId": "...", "reason": "ENTRY_DISABLED"}]. Recorded
    # whenever an association list was withheld, which in release 1.0 is every
    # signal on every referral.
    suppression_reasons: Mapped[list | None] = mapped_column(JSONType)

    # FR-AI-002, FR-AI-005, DR-008. Identifiers the provider returned that were
    # refused, and observation text that was substituted.
    discards: Mapped[dict | None] = mapped_column(JSONType)

    # --- Provenance -------------------------------------------------------
    # FR-REC-006. The matrix version every routine is stamped with. Recorded on
    # referral and error rows too, so that a scan can be placed against the
    # rules that were active even when no routine was produced.
    matrix_version: Mapped[str] = mapped_column(String(32))

    # FR-AI-004. The backend for the scan as a whole. Where a plan splits
    # across backends this holds INTERNAL_MODEL only if every task did; the
    # per-task detail lives in `routing`.
    analysis_backend: Mapped[AnalysisBackend | None] = mapped_column(
        SAEnum(AnalysisBackend, name="analysis_backend")
    )

    # FR-AI-004. [{"task": "COMBINED", "backend": "HOSTED_PROVIDER",
    #              "fallbackReason": "..."}]. One entry per task.
    routing: Mapped[list | None] = mapped_column(JSONType)

    # FR-AI-009. {"acne-detector": "1.2.0"}. Null where no trained model served
    # any task, which is every scan in release 1.0.
    model_versions: Mapped[dict | None] = mapped_column(JSONType)

    # --- Quota ------------------------------------------------------------
    # FR-SUB-003. Client-supplied, so a retried request is recognised as a
    # repeat rather than a new scan. Indexed because it is looked up on every
    # scan that carries one.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), index=True)

    # Recorded rather than inferred from `outcome`. FR-SUB-003 ties the
    # decrement to a displayed routine, and inferring it later from the outcome
    # would mean re-deriving a past decision from a rule that may since have
    # changed.
    quota_decremented: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    user: Mapped[User] = relationship()
