"""
Product ranking. FR-REC-001, FR-REC-005, ADR-030.

What these pin down:

  * a fragrance allergy removes fragranced AND unknown-fragrance products, and
    no score can bring one back;
  * the ingredient match prefers what suits the user's concerns, with
    synonyms folded;
  * sensitive skin weighs against fragrance and irritants;
  * the order is deterministic and ties break by id -- so products with no
    ingredient list on file (the curated catalogue) keep their old order;
  * end to end, ranking only ever reorders what the safety filter permitted,
    and the chosen product's imageUrl reaches the routine payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.core.enums import Concern, ProductTier, SkinType
from app.db.models import Product
from app.engine.rules import generate_routine
from app.services.catalogue import match_products, product_permitted
from app.services.product_ranking import Preferences, ProductRanker, preference_permitted
from tests.test_routine import MATRIX, make, seed, step_for

BASE = "Aqua, Glycerin, Propanediol, Phenoxyethanol, Ethylhexylglycerin, Xanthan Gum"


@dataclass(frozen=True)
class P:
    id: str
    inci: str | None = None
    fragrance: bool | None = None
    skin_types: str | None = None
    price_tier: ProductTier = ProductTier.BUDGET


def order(products, prefs) -> list[str]:
    return [p.id for p in ProductRanker(products).rank(products, prefs)]


# ---------------------------------------------------------------------------
# Unit
# ---------------------------------------------------------------------------


def test_a_fragrance_allergy_removes_fragranced_and_unknown_products():
    allergic = Preferences(fragrance_allergy=True)
    assert preference_permitted(P("a", fragrance=False), allergic)
    assert not preference_permitted(P("b", fragrance=True), allergic)
    assert not preference_permitted(P("c", fragrance=None), allergic)   # unknown is not "free"
    assert preference_permitted(P("d", fragrance=True), Preferences(fragrance_allergy=False))
    assert preference_permitted(P("e", fragrance=True), None)


def test_dryness_prefers_barrier_ingredients():
    rich = P("z-rich", f"{BASE}, Ceramide NP, Squalane, Shea Butter, Cholesterol")
    plain = P("a-plain", f"{BASE}, Dimethicone, Cetearyl Alcohol")
    prefs = Preferences(skin_type="DRY", concerns=frozenset({"DRYNESS"}))
    assert order([plain, rich], prefs) == ["z-rich", "a-plain"]


def test_synonyms_are_folded_before_matching():
    # The profile asks for "hyaluronic acid"; the label says sodium hyaluronate.
    hydrating = P("z-hydrating", f"{BASE}, Sodium Hyaluronate, Panthenol, Betaine")
    plain = P("a-plain", f"{BASE}, Dimethicone, Cetearyl Alcohol")
    prefs = Preferences(concerns=frozenset({"DEHYDRATION"}))
    assert order([plain, hydrating], prefs) == ["z-hydrating", "a-plain"]


def test_redness_prefers_soothing_ingredients():
    soothing = P("z-soothing", f"{BASE}, Centella Asiatica Extract, Allantoin, Panthenol")
    plain = P("a-plain", f"{BASE}, Dimethicone, Cetearyl Alcohol")
    assert order([plain, soothing], Preferences(concerns=frozenset({"MILD_REDNESS"}))) == ["z-soothing", "a-plain"]


def test_sensitive_skin_weighs_against_fragrance_and_irritants():
    scented = P("a-scented", f"{BASE}, Parfum, Menthol", fragrance=True)
    calm = P("z-calm", f"{BASE}, Allantoin", fragrance=False)
    sensitive = Preferences(sensitive_skin=True)
    assert order([scented, calm], sensitive) == ["z-calm", "a-scented"]
    _, reasons = ProductRanker([scented]).score(scented, sensitive)
    assert "fragrance (sensitive skin)" in reasons and any("menthol" in r for r in reasons)


def test_skin_type_listed_by_the_store_counts():
    for_oily = P("z-oily", BASE, skin_types="Oily, Acne-prone")
    for_dry = P("a-dry", BASE, skin_types="Dry")
    for_all = P("m-all", BASE, skin_types="All")
    assert order([for_dry, for_all, for_oily], Preferences(skin_type="OILY")) == ["m-all", "z-oily", "a-dry"]


def test_products_with_no_ingredient_list_keep_id_order():
    curated = [P("c-third"), P("a-first"), P("b-second")]
    prefs = Preferences(skin_type="OILY", concerns=frozenset({"ACNE"}), sensitive_skin=True)
    assert order(curated, prefs) == ["a-first", "b-second", "c-third"]


def test_the_order_is_deterministic_whatever_the_input_order():
    products = [P("b", f"{BASE}, Ceramide NP"), P("a", f"{BASE}, Ceramide NP"), P("c", BASE)]
    prefs = Preferences(concerns=frozenset({"DRYNESS"}))
    first = order(products, prefs)
    assert first == order(list(reversed(products)), prefs) == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# End to end through match_products
# ---------------------------------------------------------------------------


def _scraped(product_id, tier, ingredient, *, inci, fragrance=False, image=None, percent=None, skin=None):
    return Product(
        id=product_id, brand="Test", name=product_id, price_tier=tier,
        ingredients=[{"ingredient": ingredient, "percent": percent}], is_active=True,
        catalogue_version="test", inci=inci, fragrance=fragrance,
        image_url=image or f"https://cdn.example/{product_id}.jpg", skin_types=skin,
    )


async def _payload(session_factory, data, prefs, extra=(), ranking="ranked"):
    await seed(session_factory)
    async with session_factory() as db:
        db.add_all(extra)
        await db.commit()
    routine = generate_routine(MATRIX, data)
    async with session_factory() as db:
        return await match_products(db, MATRIX, data, routine, prefs, ranking=ranking)


async def test_ranking_picks_the_better_match_and_sends_its_image(session_factory):
    data = make(Concern.DRYNESS, skin=SkinType.DRY)
    prefs = Preferences(skin_type="DRY", concerns=frozenset({"DRYNESS"}))
    rich = _scraped("zz-barrier-cream", ProductTier.BUDGET, "moisturiser_barrier",
                    inci=f"{BASE}, Ceramide NP, Squalane, Shea Butter", skin="Dry")
    payload, _ = await _payload(session_factory, data, prefs, [rich])

    option = step_for(payload, "moisturiser_barrier")["products"]["budget"]
    # Alphabetically last; first on merit.
    assert option == {"id": "zz-barrier-cream", "brand": "Test", "name": "zz-barrier-cream",
                      "imageUrl": "https://cdn.example/zz-barrier-cream.jpg"}

    # The way back: first_by_id ignores the match and picks alphabetically.
    payload, _ = await _payload(session_factory, data, prefs, ranking="first_by_id")
    assert step_for(payload, "moisturiser_barrier")["products"]["budget"]["id"] != "zz-barrier-cream"


async def test_a_fragrance_allergy_leaves_only_known_fragrance_free_products(session_factory):
    data = make(Concern.DRYNESS, skin=SkinType.DRY)
    prefs = Preferences(skin_type="DRY", concerns=frozenset({"DRYNESS"}), fragrance_allergy=True)
    free = _scraped("zz-free", ProductTier.BUDGET, "moisturiser_barrier", inci=BASE, fragrance=False)
    scented = _scraped("aa-scented", ProductTier.BUDGET, "moisturiser_barrier",
                       inci=f"{BASE}, Parfum, Ceramide NP", fragrance=True)
    payload, missing = await _payload(session_factory, data, prefs, [free, scented])

    from sqlalchemy import select

    async with session_factory() as db:
        known_free = set((await db.execute(select(Product.id).where(Product.fragrance.is_(False)))).scalars())

    # Everything shown to an allergic user is KNOWN to be fragrance-free: not
    # the scented product, and none of the curated products whose fragrance
    # was never recorded.
    shown = [o["id"] for s in payload["am"] + payload["pm"]
             for o in (s["products"]["budget"], s["products"]["premium"]) if o]
    assert shown and set(shown) <= known_free
    assert "aa-scented" not in shown
    assert step_for(payload, "moisturiser_barrier")["products"]["budget"]["id"] == "zz-free"
    # A tier with nothing known to be free is left empty and recorded, not
    # filled with an unknown.
    assert missing, "expected some tiers to be left empty for an allergic user"


async def test_ranking_never_shows_what_the_safety_filter_refused(session_factory):
    """However well it matches, a product above the rule's strength stays out."""
    data = make(Concern.EXCESS_OIL)
    prefs = Preferences(skin_type="OILY", concerns=frozenset({"EXCESS_OIL"}))
    strong = _scraped("aa-strong-nia", ProductTier.BUDGET, "niacinamide", percent=10,
                      inci=f"{BASE}, Niacinamide, Zinc PCA", skin="Oily")
    payload, _ = await _payload(session_factory, data, prefs, [strong])
    options = step_for(payload, "niacinamide")["products"]
    assert "aa-strong-nia" not in {o["id"] for o in (options["budget"], options["premium"]) if o}
    step = next(s for s in generate_routine(MATRIX, data).am + generate_routine(MATRIX, data).pm
                if s.ingredient == "niacinamide")
    assert not product_permitted(strong, step, MATRIX, set(), {"niacinamide"})
