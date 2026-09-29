"""
Product catalogue: seeding and matching. FR-REC-005, DR-005, DR-006.

Two jobs.

**Seeding** reads the versioned catalogue file, validates it against the rules
matrix (DR-006: every product maps to at least one known ingredient and carries
a price tier), and upserts it into the products table. A product that has left
the file is deactivated, never deleted (DR-005).

**Matching** takes a generated routine and, for each step, picks one BUDGET
product, one PREMIUM product, and writes the generic pharmacy alternative
(FR-REC-005).

## The matching filter is a safety check, not a preference

FR-REC-005's rationale: "The product catalogue never overrides a safety
exclusion." The engine chose a safe ingredient; a product is a bottle that
contains that ingredient *and other things*. So a product is shown only if:

  1. it is active;
  2. its primary ingredient is the step's ingredient;
  3. its primary strength is at or below the rule's maxPercent (where both are
     stated) -- a 10% serum is not the 5% the rule permits;
  4. NONE of its ingredients is excluded for this user (minor, pregnancy,
     prescription, or prohibited by a detected concern);
  5. none of its ingredients clashes with anything else in the routine;
  6. it adds no second active ingredient -- which would silently break the
     FR-REC-004 active-count limit the engine just enforced.

A missing tier is not an error. The step still shows the generic alternative,
and the gap is recorded (FR-REC-005: "the missing tier is recorded in metrics").
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clinical.copy import strings
from app.core.enums import ProductTier
from app.db.models.catalogue import Product
from app.engine.rules import Routine, RoutineInput, RoutineStep, excluded_ingredients
from app.services.matrix_loader import Matrix

logger = logging.getLogger(__name__)


class CatalogueInvalid(ValueError):
    """The seed file is malformed or names an ingredient the matrix lacks."""


# ---------------------------------------------------------------------------
# Seed file
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogueEntry:
    id: str
    brand: str
    name: str
    price_tier: ProductTier
    ingredients: list[dict[str, Any]]


@dataclass(frozen=True)
class CatalogueFile:
    version: str
    entries: tuple[CatalogueEntry, ...]


def load_catalogue_file(path: Path, matrix: Matrix) -> CatalogueFile:
    """Read and validate the seed file. DR-006 at 100%."""
    payload = json.loads(path.read_text(encoding="utf-8"))

    version = str(payload.get("version") or "").strip()
    if not version:
        raise CatalogueInvalid("catalogue has no version")

    entries: list[CatalogueEntry] = []
    seen: set[str] = set()

    for raw in payload.get("products", []):
        product_id = str(raw.get("id") or "").strip()
        if not product_id:
            raise CatalogueInvalid("a product has no id")
        if product_id in seen:
            raise CatalogueInvalid(f"duplicate product id {product_id}")
        seen.add(product_id)

        try:
            tier = ProductTier(raw.get("priceTier"))
        except ValueError:
            # DR-006: every product carries a priceTier.
            raise CatalogueInvalid(f"{product_id}: priceTier must be BUDGET or PREMIUM") from None

        ingredients = raw.get("ingredients") or []
        if not ingredients:
            # DR-006: every product maps to at least one ingredient.
            raise CatalogueInvalid(f"{product_id}: no ingredients")

        cleaned: list[dict[str, Any]] = []
        for item in ingredients:
            key = item.get("ingredient")
            if key not in matrix.ingredients:
                raise CatalogueInvalid(f"{product_id}: unknown ingredient {key!r}")
            percent = item.get("percent")
            if percent is not None and (not isinstance(percent, (int, float)) or percent <= 0):
                raise CatalogueInvalid(f"{product_id}: percent must be a positive number or null")
            cleaned.append({"ingredient": key, "percent": percent})

        entries.append(
            CatalogueEntry(
                id=product_id,
                brand=str(raw.get("brand") or "").strip(),
                name=str(raw.get("name") or "").strip(),
                price_tier=tier,
                ingredients=cleaned,
            )
        )

    if not entries:
        raise CatalogueInvalid("catalogue has no products")

    return CatalogueFile(version=version, entries=tuple(entries))


async def sync_catalogue(session: AsyncSession, catalogue: CatalogueFile) -> dict[str, int]:
    """
    Make the products table match the file.

    Upserts every entry and deactivates every product not in the file. Nothing
    is deleted (DR-005). Returns counts, for the seed script to print.
    """
    existing = {p.id: p for p in (await session.execute(select(Product))).scalars()}
    in_file = {e.id for e in catalogue.entries}
    counts = {"created": 0, "updated": 0, "deactivated": 0}

    for entry in catalogue.entries:
        product = existing.get(entry.id)
        if product is None:
            product = Product(id=entry.id)
            session.add(product)
            counts["created"] += 1
        else:
            counts["updated"] += 1
        product.brand = entry.brand
        product.name = entry.name
        product.price_tier = entry.price_tier
        product.ingredients = entry.ingredients
        product.is_active = True
        product.catalogue_version = catalogue.version

    for product_id, product in existing.items():
        if product_id not in in_file and product.is_active:
            product.is_active = False
            counts["deactivated"] += 1

    await session.flush()
    return counts


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def generic_alternative(label: str, percent: float | None) -> str:
    """
    FR-REC-005's generic option, e.g.
    "Ask your pharmacy for a niacinamide serum at 5% or lower".
    """
    text = label[:1].lower() + label[1:]
    article = "an" if text[:1] in "aeiou" else "a"
    if percent is None:
        return strings.GENERIC_WITHOUT_STRENGTH.format(article=article, label=text)
    shown = f"{percent:g}"  # 5.0 -> "5", 0.1 -> "0.1"
    return strings.GENERIC_WITH_STRENGTH.format(article=article, label=text, percent=shown)


def product_permitted(
    product: Product,
    step: RoutineStep,
    matrix: Matrix,
    excluded: set[str],
    routine_ingredients: set[str],
) -> bool:
    """The six conditions in the module docstring. All must hold."""
    if not product.is_active:
        return False
    if product.primary_ingredient != step.ingredient:
        return False

    primary_percent = product.ingredients[0].get("percent")
    if (
        step.max_percent is not None
        and primary_percent is not None
        and primary_percent > step.max_percent
    ):
        return False

    others_in_routine = routine_ingredients - {step.ingredient}
    for item in product.ingredients:
        key = item["ingredient"]
        if key in excluded:
            return False
        if matrix.ingredients[key].incompatible_with & others_in_routine:
            return False
        if key != step.ingredient and matrix.ingredients[key].is_active:
            return False

    return True


def _product_payload(product: Product | None) -> dict[str, Any] | None:
    if product is None:
        return None
    return {"id": product.id, "brand": product.brand, "name": product.name}


async def match_products(
    session: AsyncSession, matrix: Matrix, data: RoutineInput, routine: Routine
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """
    Product options for every step, and the tiers that could not be filled.

    Returns (routine payload with products attached, missing tiers). Ordering
    within a tier is by product id, so the same catalogue always yields the
    same pick -- FR-REC-001's determinism extends to what the user is shown.
    """
    products = list(
        (
            await session.execute(
                select(Product).where(Product.is_active.is_(True)).order_by(Product.id)
            )
        ).scalars()
    )
    excluded = set(excluded_ingredients(matrix, data))
    routine_ingredients = routine.ingredients()
    missing: list[dict[str, str]] = []

    def options_for(step: RoutineStep, timing: str) -> dict[str, Any]:
        picks: dict[ProductTier, Product | None] = {}
        for tier in (ProductTier.BUDGET, ProductTier.PREMIUM):
            picks[tier] = next(
                (
                    p
                    for p in products
                    if p.price_tier is tier
                    and product_permitted(p, step, matrix, excluded, routine_ingredients)
                ),
                None,
            )
            if picks[tier] is None:
                missing.append(
                    {"timing": timing, "step": step.step, "ingredient": step.ingredient, "tier": tier.value}
                )
        return {
            "budget": _product_payload(picks[ProductTier.BUDGET]),
            "premium": _product_payload(picks[ProductTier.PREMIUM]),
            "generic": generic_alternative(step.label, step.max_percent),
        }

    payload = routine.as_dict()
    for timing, steps, out in (("AM", routine.am, payload["am"]), ("PM", routine.pm, payload["pm"])):
        for step, step_out in zip(steps, out):
            step_out["products"] = options_for(step, timing)

    if missing:
        # FR-REC-005: "the missing tier is recorded in metrics".
        logger.info("catalogue has no product for %d step tiers: %s", len(missing), missing)

    return payload, missing
