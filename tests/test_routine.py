"""
Catalogue, product matching, and the full routine path. FR-REC-005, FR-REC-006,
FR-SUB-001, FR-SUB-003, FR-SUB-005, DR-005, DR-006, UC-002, UC-007.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.enums import AgeBand, Concern, ProductTier, SkinType
from app.db.base import Base
from app.db.models import Product, Routine, ScanLog
from app.engine.rules import RoutineInput, generate_routine
from app.services import routine_service
from app.services.catalogue import (
    CatalogueInvalid,
    generic_alternative,
    load_catalogue_file,
    match_products,
    sync_catalogue,
)
from app.services.matrix_loader import load_matrix_file
from tests.test_scan import RoutineSpy, StubProvider, clear, onboard, scan, use_pipeline

MATRIX = load_matrix_file(Path("app/clinical/matrix/rules_matrix.v0.json"))
CATALOGUE_PATH = Path("app/clinical/catalogue/catalogue.v0.json")


async def seed(session_factory) -> None:
    async with session_factory() as db:
        await sync_catalogue(db, load_catalogue_file(CATALOGUE_PATH, MATRIX))
        await db.commit()


def make(*concerns, skin=SkinType.OILY, age=AgeBand.ADULT, pregnant=False, rx=False):
    return RoutineInput(skin, age, pregnant, rx, frozenset(concerns))


def step_for(payload: dict, ingredient: str) -> dict:
    return next(s for s in payload["am"] + payload["pm"] if s["ingredient"] == ingredient)


# ---------------------------------------------------------------------------
# Catalogue file -- DR-006
# ---------------------------------------------------------------------------


def test_shipped_catalogue_is_valid():
    catalogue = load_catalogue_file(CATALOGUE_PATH, MATRIX)
    assert catalogue.entries


def test_every_matrix_ingredient_has_a_budget_product():
    """ASM-003: products at more than one price point. Budget at minimum."""
    catalogue = load_catalogue_file(CATALOGUE_PATH, MATRIX)
    budget = {e.ingredients[0]["ingredient"] for e in catalogue.entries if e.price_tier is ProductTier.BUDGET}
    assert set(MATRIX.ingredients) <= budget


@pytest.mark.parametrize(
    ("product", "message"),
    [
        ({"id": "x", "priceTier": "CHEAP", "ingredients": [{"ingredient": "niacinamide"}]}, "priceTier"),
        ({"id": "x", "priceTier": "BUDGET", "ingredients": []}, "no ingredients"),
        ({"id": "x", "priceTier": "BUDGET", "ingredients": [{"ingredient": "snail_mucin"}]}, "unknown"),
        ({"id": "", "priceTier": "BUDGET", "ingredients": [{"ingredient": "niacinamide"}]}, "no id"),
    ],
)
def test_invalid_product_is_refused(tmp_path, product, message):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"version": "t", "products": [product]}), encoding="utf-8")
    with pytest.raises(CatalogueInvalid, match=message):
        load_catalogue_file(path, MATRIX)


async def test_removed_product_is_deactivated_not_deleted(session_factory, tmp_path):
    """DR-005."""
    await seed(session_factory)
    path = tmp_path / "c.json"
    path.write_text(
        json.dumps(
            {
                "version": "t2",
                "products": [
                    {"id": "benzac-ac-5", "brand": "B", "name": "N", "priceTier": "BUDGET",
                     "ingredients": [{"ingredient": "benzoyl_peroxide", "percent": 5}]}
                ],
            }
        ),
        encoding="utf-8",
    )
    async with session_factory() as db:
        counts = await sync_catalogue(db, load_catalogue_file(path, MATRIX))
        await db.commit()
        all_rows = list((await db.execute(select(Product))).scalars())

    assert counts["deactivated"] == len(all_rows) - 1
    assert len(all_rows) > 1  # nothing deleted
    assert [p.id for p in all_rows if p.is_active] == ["benzac-ac-5"]


# ---------------------------------------------------------------------------
# Matching -- FR-REC-005
# ---------------------------------------------------------------------------


def test_generic_text_matches_the_srs_example():
    assert (
        generic_alternative("Niacinamide serum", 5.0)
        == "Ask your pharmacy for a niacinamide serum at 5% or lower"
    )
    assert generic_alternative("Adapalene gel (retinoid)", 0.1).startswith("Ask your pharmacy for an adapalene")


async def _match(session_factory, data):
    await seed(session_factory)
    routine = generate_routine(MATRIX, data)
    async with session_factory() as db:
        return await match_products(db, MATRIX, data, routine)


async def test_every_step_has_a_generic_option(session_factory):
    payload, _ = await _match(session_factory, make(Concern.ACNE, Concern.EXCESS_OIL))
    for step in payload["am"] + payload["pm"]:
        assert step["products"]["generic"].startswith("Ask your pharmacy for")


async def test_product_above_the_rule_strength_is_never_shown(session_factory):
    """
    Niacinamide is capped at 5%. The 10% products must not appear at either
    tier, however well known they are -- the catalogue never overrides a limit.
    """
    payload, _ = await _match(session_factory, make(Concern.EXCESS_OIL))
    options = step_for(payload, "niacinamide")["products"]
    shown = {options["budget"]["id"], options["premium"]["id"]}

    # The Ordinary's 10% serum is in the catalogue and is the best known
    # niacinamide product sold here. It is still never shown, because 10% is
    # above every niacinamide rule in the matrix.
    assert "the-ordinary-niacinamide-10-zinc-1" not in shown
    # What is shown states no strength on the pack, so there is nothing to
    # breach -- the cap check applies only where both numbers are known.
    assert options["budget"]["id"] == "ponds-bright-beauty-spotless-glow-cream"
    assert options["premium"]["id"] == "cetaphil-bright-healthy-radiance-night-cream"


async def test_product_with_a_second_active_is_never_shown(session_factory):
    """
    A bottle carrying two matrix actives is rejected, because showing it would
    add an active the engine did not choose and break the FR-REC-004 count.

    The product is inserted here rather than taken from the catalogue: the
    Pakistan catalogue deliberately contains only single-active products, so
    there is nothing in it that exercises this filter. Owning the fixture also
    means the test keeps testing the rule when the catalogue is next replaced.
    """
    await seed(session_factory)
    async with session_factory() as db:
        db.add(
            Product(
                id="test-two-active-vitamin-c",
                brand="Test",
                name="Vitamin C with salicylic acid",
                price_tier=ProductTier.PREMIUM,
                ingredients=[
                    {"ingredient": "vitamin_c", "percent": 10},
                    {"ingredient": "salicylic_acid", "percent": 2},
                ],
                is_active=True,
                catalogue_version="test",
            )
        )
        await db.commit()

    data = make(Concern.UNEVEN_TONE)
    routine = generate_routine(MATRIX, data)
    async with session_factory() as db:
        payload, _ = await match_products(db, MATRIX, data, routine)

    options = step_for(payload, "vitamin_c")["products"]
    shown = {o["id"] for o in (options["budget"], options["premium"]) if o}
    assert "test-two-active-vitamin-c" not in shown


async def test_no_product_contains_an_excluded_ingredient(session_factory):
    """FR-REC-005: the catalogue never overrides a safety exclusion."""
    data = make(*Concern, pregnant=True, age=AgeBand.MINOR, rx=True)
    await seed(session_factory)
    routine = generate_routine(MATRIX, data)
    async with session_factory() as db:
        payload, _ = await match_products(db, MATRIX, data, routine)
        shown = [
            o["id"]
            for s in payload["am"] + payload["pm"]
            for o in (s["products"]["budget"], s["products"]["premium"])
            if o
        ]
        products = {p.id: p for p in (await db.execute(select(Product))).scalars()}

    for pid in shown:
        for item in products[pid].ingredients:
            info = MATRIX.ingredients[item["ingredient"]]
            assert not info.pregnancy_restricted, pid
            assert not info.minor_restricted, pid
            assert item["ingredient"] not in MATRIX.prescription_exclusions, pid


async def test_inactive_product_is_never_shown(session_factory):
    await seed(session_factory)
    async with session_factory() as db:
        (await db.get(Product, "benzique-cream-4")).is_active = False
        await db.commit()
    data = make(Concern.ACNE, pregnant=True)  # pregnancy moves acne to benzoyl peroxide
    routine = generate_routine(MATRIX, data)
    async with session_factory() as db:
        payload, _ = await match_products(db, MATRIX, data, routine)
    assert step_for(payload, "benzoyl_peroxide")["products"]["budget"] is None


# ---------------------------------------------------------------------------
# End to end -- UC-002, UC-007
# ---------------------------------------------------------------------------


async def test_first_scan_gives_a_full_routine_with_products(client, settings, session_factory):
    """UC-002 and FR-SUB-001: the full result, no paywall, allowance used once."""
    await seed(session_factory)
    use_pipeline(settings, StubProvider(clear()), routine=RoutineSpy(settings))
    headers = await onboard(client)

    r = await scan(client, headers)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "ROUTINE"
    assert body["scansRemaining"] == 0
    routine = body["routine"]
    assert routine["matrixVersion"] == MATRIX.version
    assert [s["step"] for s in routine["am"]][-1] == "PROTECT"
    assert all("products" in s for s in routine["am"] + routine["pm"])


async def test_routine_is_saved_with_matrix_version_and_scan(client, settings, session_factory):
    """FR-REC-006 and SRS 6.2."""
    await seed(session_factory)
    use_pipeline(settings, StubProvider(clear()), routine=RoutineSpy(settings))
    headers = await onboard(client)

    body = (await scan(client, headers)).json()

    async with session_factory() as db:
        row = (await db.execute(select(Routine))).scalar_one()
        log = await db.get(ScanLog, body["scanId"])
        products = (await db.execute(select(Routine).where(Routine.id == row.id))).scalar_one()
        await db.refresh(products, ["products"])
    assert row.scan_log_id == body["scanId"]
    assert row.matrix_version == MATRIX.version
    assert log.matrix_version == MATRIX.version
    assert log.quota_decremented is True
    assert products.products  # linked for DR-005


async def test_routine_stays_available_after_allowance_is_used(client, settings, session_factory):
    """FR-SUB-005, UC-007."""
    await seed(session_factory)
    use_pipeline(settings, StubProvider(clear()), routine=RoutineSpy(settings))
    headers = await onboard(client)
    scanned = (await scan(client, headers)).json()

    eligibility = (await client.get("/v1/scans/eligibility", headers=headers)).json()
    latest = await client.get("/v1/routines/latest", headers=headers)

    assert eligibility["canScan"] is False
    assert latest.status_code == 200
    assert latest.json()["am"] == scanned["routine"]["am"]


async def test_no_routine_yet_is_404(client, settings):
    headers = await onboard(client)
    r = await client.get("/v1/routines/latest", headers=headers)
    assert r.status_code == 404
    assert r.json()["errorCode"] == "NO_ROUTINE"


async def test_clear_skin_gets_the_base_routine_not_a_retake(client, settings, session_factory):
    """No concerns found is a valid answer, not an unusable photo."""
    await seed(session_factory)
    use_pipeline(settings, StubProvider(clear([])), routine=RoutineSpy(settings))
    headers = await onboard(client)

    r = await scan(client, headers)

    assert r.json()["outcome"] == "ROUTINE"
    assert [s["step"] for s in r.json()["routine"]["am"]] == ["CLEANSE", "MOISTURISE", "PROTECT"]


async def test_retry_returns_the_saved_routine(client, settings, session_factory):
    await seed(session_factory)
    use_pipeline(settings, StubProvider(clear()), routine=RoutineSpy(settings))
    headers = await onboard(client)

    first = (await scan(client, headers, key="k1")).json()
    second = (await scan(client, headers, key="k1")).json()

    assert second["routine"]["am"] == first["routine"]["am"]


async def test_unsafe_routine_is_never_shown_or_charged(client, settings, session_factory, monkeypatch):
    """The runtime invariant check: a routine that fails it is withheld."""
    monkeypatch.setattr(routine_service, "check_routine", lambda *a: ["INV7_NO_AM_SUNSCREEN"])
    await seed(session_factory)
    use_pipeline(settings, StubProvider(clear()), routine=RoutineSpy(settings))
    headers = await onboard(client)

    r = await scan(client, headers)

    assert r.status_code == 503
    async with session_factory() as db:
        assert (await db.execute(select(Routine))).first() is None
        log = (await db.execute(select(ScanLog))).scalar_one()
    assert log.outcome.value == "ERROR"
    assert log.discards["pendingStage"] == "INVARIANT_CHECK"
    assert log.quota_decremented is False


# ---------------------------------------------------------------------------
# DR-005 at the database level
# ---------------------------------------------------------------------------


async def test_product_in_a_routine_cannot_be_hard_deleted():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(conn, _):  # SQLite enforces foreign keys only when asked
        conn.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    from app.db.models import User
    from datetime import date

    async with factory() as db:
        db.add(User(auth_id="u", auth_provider="google", date_of_birth=date(1990, 1, 1)))
        product = Product(id="p", brand="b", name="n", price_tier=ProductTier.BUDGET,
                          ingredients=[{"ingredient": "niacinamide", "percent": 5}],
                          catalogue_version="t")
        db.add(product)
        db.add(ScanLog(id="s", user_auth_id="u", outcome="ROUTINE", matrix_version="m"))
        await db.flush()
        db.add(Routine(user_auth_id="u", scan_log_id="s", matrix_version="m",
                       steps={"am": [], "pm": []}, products=[product]))
        await db.commit()

    with pytest.raises(IntegrityError):
        async with factory() as db:
            from sqlalchemy import delete
            await db.execute(delete(Product).where(Product.id == "p"))
            await db.commit()

    await engine.dispose()


def test_excluded_secondary_ingredient_blocks_the_product():
    """
    Defence in depth. With today's matrix every restricted ingredient is also an
    active, so the second-active rule blocks such products first. A future
    matrix could restrict a NON-active ingredient; this check is what stops a
    product carrying it.
    """
    from app.engine.rules import RoutineStep
    from app.services.catalogue import product_permitted

    step = RoutineStep(step="TREAT", ingredient="niacinamide", label="x", frequency="DAILY",
                       rule_id="r", max_percent=5)
    product = Product(id="p", brand="b", name="n", price_tier=ProductTier.BUDGET, is_active=True,
                      ingredients=[{"ingredient": "niacinamide", "percent": 5},
                                   {"ingredient": "moisturiser_barrier", "percent": None}])
    ok = product_permitted(product, step, MATRIX, set(), {"niacinamide"})
    blocked = product_permitted(product, step, MATRIX, {"moisturiser_barrier"}, {"niacinamide"})
    assert ok and not blocked


async def test_routine_keeps_concern_severity_for_the_dashboard(client, settings, session_factory):
    """The skin-vitals display needs severity, and it must survive a reload."""
    await seed(session_factory)
    use_pipeline(
        settings,
        StubProvider(
            clear([
                {"concernId": "ACNE", "severity": "PRONOUNCED"},
                {"concernId": "EXCESS_OIL", "severity": "MILD"},
            ])
        ),
        routine=RoutineSpy(settings),
    )
    headers = await onboard(client)

    scanned = (await scan(client, headers)).json()["routine"]
    reloaded = (await client.get("/v1/routines/latest", headers=headers)).json()

    expected = [
        {"concernId": "ACNE", "severity": "PRONOUNCED"},
        {"concernId": "EXCESS_OIL", "severity": "MILD"},
    ]
    assert scanned["concerns"] == expected
    assert reloaded["concerns"] == expected
