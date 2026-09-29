"""
The Product entity. SRS 6.2, FR-REC-005, DR-005, DR-006.

A curated catalogue of locally available products, seeded from
`app/clinical/catalogue/catalogue.v0.json` by `scripts/seed_catalogue.py`.

## Ingredients are keys into the matrix, not a table

SRS 6.1 lists Ingredient as its own entity. It is not a table here, and that is
a deliberate deviation (ADR-020): Appendix F already defines every ingredient
with its restriction flags, and CON-003 puts all clinical logic in that file. A
second copy of `pregnancyRestricted` in a database table is a second place for
it to be wrong, and the two would drift the first time one was edited without
the other. A product lists the matrix keys it contains; the flags are always
read from the matrix that is actually in force.

## Never deleted

DR-005: a product referenced by a routine is never hard-deleted. `is_active` is
the soft delete, and the routine_products link table (routine.py) refuses a
delete at the database level with ON DELETE RESTRICT, so the rule holds even for
someone deleting rows by hand in the Supabase console.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum as SAEnum, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import ProductTier
from app.db.base import Base
from app.db.models.user import JSONType


class Product(Base):
    __tablename__ = "products"

    # Stable slug from the seed file, e.g. "cerave-hydrating-cleanser". A slug
    # rather than a UUID so that re-running the seed updates the same row
    # instead of creating a duplicate.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    brand: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))

    # SRS 6.2: BUDGET or PREMIUM.
    price_tier: Mapped[ProductTier] = mapped_column(SAEnum(ProductTier, name="product_tier"))

    # [{"ingredient": "niacinamide", "percent": 5}]. First entry is the primary
    # ingredient -- the routine step this product serves. DR-006: never empty.
    ingredients: Mapped[list] = mapped_column(JSONType)

    # DR-005 soft delete. Public, per 6.2.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    # Which seed file last wrote this row. FR-ONB-008's review record binds to
    # a catalogue version as well as a matrix version.
    catalogue_version: Mapped[str] = mapped_column(String(32))

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @property
    def primary_ingredient(self) -> str:
        return self.ingredients[0]["ingredient"]
