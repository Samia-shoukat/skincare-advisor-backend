"""
The Routine entity. SRS 6.2, FR-REC-006, FR-SUB-005, DR-005.

The AM/PM plan generated for a user from one scan. Stored because FR-SUB-005
requires the routine and its products to stay available after the scan
allowance is used -- the user paid their one scan for this, and exhausting the
allowance restricts new analysis, not access to results already received.

## What is stored

`steps` is the full payload the client renders: AM and PM steps, each with its
ingredient, strength, frequency, the concerns it addresses, and its budget,
premium and generic options. Stored whole rather than rebuilt on read, because a
rebuilt routine would use today's matrix and catalogue, and FR-REC-006 requires
the routine to carry the matrix version that actually produced it.

`omitted_steps` is SRS 6.2's omittedSteps: concerns that got no step, and why.

## DR-005, enforced by the database

`routine_products` links each routine to every product it shows. Its foreign
key to products is ON DELETE RESTRICT, so a product that appears in any routine
cannot be hard-deleted -- not by the app, not by a script, not by hand in the
Supabase console. Deactivation (`is_active = false`) is the only way out.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, Table, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, new_uuid
from app.db.models.catalogue import Product
from app.db.models.user import JSONType

routine_products = Table(
    "routine_products",
    Base.metadata,
    Column(
        "routine_id",
        String(36),
        ForeignKey("routines.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "product_id",
        String(64),
        # DR-005. RESTRICT, not CASCADE: deleting a product must fail, not
        # silently strip it out of every routine that showed it.
        ForeignKey("products.id", ondelete="RESTRICT"),
        primary_key=True,
    ),
)


class Routine(Base):
    __tablename__ = "routines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)

    # DR-002: hard-deleted with the account.
    user_auth_id: Mapped[str] = mapped_column(
        ForeignKey("users.auth_id", ondelete="CASCADE"), index=True
    )

    # The scan that produced it. Deleted with the scan log; both go with the
    # account.
    scan_log_id: Mapped[str] = mapped_column(
        ForeignKey("scan_logs.id", ondelete="CASCADE"), unique=True
    )

    # FR-REC-006: the matrix version is recorded on every generated routine.
    matrix_version: Mapped[str] = mapped_column(String(32))
    catalogue_version: Mapped[str | None] = mapped_column(String(32))

    # SRS 6.2 `steps`: {"am": [...], "pm": [...]} with product options.
    steps: Mapped[dict] = mapped_column(JSONType)

    # SRS 6.2 `omittedSteps`: [{"concern": "...", "reason": "..."}].
    omitted_steps: Mapped[list | None] = mapped_column(JSONType)

    # FR-REC-005: tiers the catalogue could not fill.
    missing_tiers: Mapped[list | None] = mapped_column(JSONType)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    products: Mapped[list[Product]] = relationship(secondary=routine_products)
