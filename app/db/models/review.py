"""
Clinical review records. FR-ONB-008.

The requirement has two halves and the second is the one that is easy to lose:

  * Where a signed record exists for the active matrix version, the app may
    state that routines are reviewed by a licensed practitioner, and must show
    the reviewer's name, licence number, and review date.
  * Where no such record exists, NO review claim appears anywhere in the
    application.

The second half is a requirement, not a gap. It is implementable today, and it
is implemented here: the resolver returns the unsubstantiated claim unless it
finds a matching signed record. There is no code path that produces a review
claim without a record backing it, which is what makes the guarantee hold
rather than depend on someone remembering.

`matrix_version` is unique, so one record binds to one matrix version. A rules
change ships a new matrix version, which orphans the old record and drops the
claim back to unsubstantiated until the new matrix is reviewed. That is correct:
the review was of the old rules.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, String, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.clinical.copy.strings import (
    REVIEW_CLAIM_SUBSTANTIATED,
    REVIEW_CLAIM_UNSUBSTANTIATED,
)
from app.db.base import Base, new_uuid


class ReviewRecord(Base):
    """SRS 6.2. Created only by an operator, never by the application."""

    __tablename__ = "review_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)

    reviewer_name: Mapped[str] = mapped_column(String(200))
    licence_number: Mapped[str] = mapped_column(String(100))
    reviewed_at: Mapped[date] = mapped_column(Date)

    # One record per matrix version. The uniqueness is the binding.
    matrix_version: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    catalogue_version: Mapped[str] = mapped_column(String(32))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


async def resolve_review_claim(
    session: AsyncSession, matrix_version: str
) -> dict[str, object]:
    """
    What the app is permitted to say about clinical review, right now.

    `substantiated` is false unless a record is found. Every caller must branch
    on it rather than on the presence of a name, because a client that renders
    whatever fields it receives would show an empty reviewer block and imply a
    review that did not happen.
    """
    result = await session.execute(
        select(ReviewRecord).where(ReviewRecord.matrix_version == matrix_version)
    )
    record = result.scalar_one_or_none()

    if record is None:
        return {
            "substantiated": False,
            "claim": REVIEW_CLAIM_UNSUBSTANTIATED,
            "reviewer": None,
        }

    return {
        "substantiated": True,
        "claim": REVIEW_CLAIM_SUBSTANTIATED,
        "reviewer": {
            "name": record.reviewer_name,
            "licenceNumber": record.licence_number,
            "reviewedAt": record.reviewed_at.isoformat(),
            "matrixVersion": record.matrix_version,
        },
    }