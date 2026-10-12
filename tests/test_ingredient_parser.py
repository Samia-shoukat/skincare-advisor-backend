"""
The ingredient parser and catalogue proposals. FR-REC-005, DR-006, CON-003.

The examples are lifted from what the stores actually print -- "(and)" chains,
"Active: ... - Other ingredients:" headers, missing commas, misspellings --
because a parser tested only on tidy lists fails on the first real one.

The safety direction is the one that matters: every test that a product is
*blocked* pins a way an unsafe or unverifiable bottle could otherwise reach a
routine.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.catalogue import load_catalogue_file
from app.services.ingredient_parser import (
    apply_edit,
    assign_price_tiers,
    classify_type,
    load_lexicon,
    parse_ingredient,
    parse_price,
    propose,
    split_ingredients,
)
from app.services.matrix_loader import load_matrix_file

MATRIX_PATH = Path("app/clinical/matrix/rules_matrix.v0.json")
MATRIX = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
LEX = load_lexicon()

# A full list with a base, a preservative and more than six entries.
BASE = "Aqua, Glycerin, Propanediol, Xanthan Gum, Phenoxyethanol, Ethylhexylglycerin, Sodium Hyaluronate"


def row(name: str, ingredients: str, price: str = "Rs. 1,200", **extra) -> dict:
    return {"id": "uuid-1", "brand": "Brand", "name": name, "price": price,
            "image_url": "https://cdn.example/p.jpg", "skin_type": None,
            "ingredients": ingredients, **extra}


# ---------------------------------------------------------------------------
# Splitting and matching
# ---------------------------------------------------------------------------


def test_splits_what_stores_actually_print():
    text = ("Active: 5% Niacinamide, 0.5% Witch Hazel - Other ingredients: Aqua, "
            "Titanium Dioxide (and) Alumina; Glycerin and Sodium PCA")
    assert split_ingredients(text) == [
        "5% Niacinamide", "0.5% Witch Hazel", "Aqua", "Titanium Dioxide", "Alumina",
        "Glycerin", "Sodium PCA",
    ]


def test_vitamin_e_and_c_is_two_vitamins_not_a_lost_c():
    tokens = split_ingredients("Aqua, Vitamin E and C, Glycerin")
    assert "vitamin C" in tokens and "vitamin E" in tokens
    assert parse_ingredient("vitamin C", LEX).canonical == "vitamin_c"


def test_matches_on_word_boundaries():
    assert parse_ingredient("Sodium Lactate", LEX).kind == "excipient"   # not lactic acid
    assert parse_ingredient("Lactic Acid", LEX).kind == "non_matrix_active"
    assert parse_ingredient("Ethylhexyl Salicylate", LEX).kind == "uv_filter"   # not salicylic acid


def test_a_missing_comma_does_not_hide_an_active():
    item = parse_ingredient("Salicylic Acid Sodium lactate", LEX)
    assert (item.kind, item.canonical) == ("active", "salicylic_acid")


def test_strength_takes_the_higher_of_two_printed():
    assert parse_ingredient("Niacinamide (5% / 10%)", LEX).percent == 10.0
    assert parse_ingredient("Lactic acid 5%", LEX).percent == 5.0


def test_misspellings_are_matched_and_marked():
    item = parse_ingredient("Proplene Glycoll", LEX)
    assert item.kind == "excipient" and item.fuzzy


def test_peptides_are_caught_inside_longer_names():
    assert parse_ingredient("Palmitoyl Tripeptide-1", LEX).kind == "non_matrix_active"


def test_ingredient_families_are_recognised():
    assert parse_ingredient("PEG-40 Hydrogenated Castor Oil", LEX).kind == "excipient"
    assert parse_ingredient("CI 77491", LEX).kind == "excipient"


@pytest.mark.parametrize("name, expected", [
    ("Barrier Bestie Ceramide Moisturiser", "MOISTURISE"),
    ("Daily Purifying Rice Cleanser Face Wash", "CLEANSE"),
    ("SunSlay Sunscreen Fluid SPF 50+++", "PROTECT"),
    ("NiaRonic Serum (Pore Minimizing)", "TREAT"),
    ("AHA/BHA Body Wash", "OTHER"),          # body before wash
    ("Bright-C Under Eye Cream", "OTHER"),    # eye before cream
    ("Lip Glaze (Strawberry)", "OTHER"),
    ("Skin Charisma", "UNKNOWN"),
])
def test_classifies_the_routine_step_from_the_name(name, expected):
    assert classify_type(name)[0] == expected


def test_parses_store_prices():
    assert parse_price("Rs. 1,450") == 1450.0
    assert parse_price("Rs. 898.20") == 898.2
    assert parse_price(None) is None


# ---------------------------------------------------------------------------
# Proposals: what can be approved
# ---------------------------------------------------------------------------


def test_a_single_active_serum_within_the_rule_is_eligible():
    p = propose(row("Niacinamide Serum", f"{BASE}, Niacinamide (5%)"), MATRIX)
    assert p.status == "eligible"
    assert (p.product_type, p.primary, p.percent) == ("TREAT", "niacinamide", 5.0)


def test_strength_printed_only_in_the_name_is_used():
    p = propose(row("Clear Serum (4% Niacinamide)", f"{BASE}, Niacinamide"), MATRIX)
    assert p.percent == 4.0 and p.status == "eligible"


def test_an_unstated_strength_needs_the_reviewer():
    p = propose(row("Niacinamide Serum", f"{BASE}, Niacinamide"), MATRIX)
    assert p.status == "needs_input" and p.needs == ["strength"]
    apply_edit(p, MATRIX, percent=4)
    assert p.status == "eligible"


def test_a_plain_cleanser_maps_to_the_gentle_cleanser_step():
    p = propose(row("Hydrating Facial Cleanser", f"{BASE}, Decyl Glucoside"), MATRIX)
    assert (p.status, p.primary) == ("eligible", "gentle_cleanser")


def test_a_sunscreen_at_spf_30_or_more_is_eligible():
    p = propose(row("Daily Sunscreen SPF 50", f"{BASE}, Zinc Oxide, Octocrylene"), MATRIX)
    assert (p.status, p.primary, p.spf) == ("eligible", "sunscreen_spf30", 50)


def test_a_sunscreen_is_recognised_from_its_filters_when_the_name_is_vague():
    p = propose(row("Daily Defence", f"{BASE}, Zinc Oxide, Octocrylene, Avobenzone"), MATRIX)
    assert p.product_type == "PROTECT" and p.needs == ["spf"]


def test_moisturiser_kind_follows_the_ingredients_and_name():
    barrier = propose(row("Ceramide Moisturiser", f"{BASE}, Ceramide NP, Shea Butter"), MATRIX)
    light = propose(row("Oil Free Gel Moisturizer", f"{BASE}, Dimethicone"), MATRIX)
    assert barrier.primary == "moisturiser_barrier"
    assert light.primary == "moisturiser_light"


# ---------------------------------------------------------------------------
# Proposals: what must be blocked
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name, ingredients, reason", [
    ("Niacinamide 10% Serum", f"{BASE}, Niacinamide (10%)", "above the 5% maximum"),
    ("Brightening Serum", f"{BASE}, Niacinamide, Alpha Arbutin", "does not cover yet"),
    ("Clear Serum", f"{BASE}, Niacinamide (5%), Salicylic Acid (2%)", "two actives"),
    ("Retinol Serum", f"{BASE}, Retinol", "does not cover yet"),
    ("Peptide Serum", f"{BASE}, Palmitoyl Tripeptide-1", "does not cover yet"),
    ("Dermive Moisturizer", "Ceramides, Hyaluronic Acid, Glycerin", "only key ingredients"),
    ("Rose Face Wash", f"{BASE}, Decyl Glucoside, Parfum", "fragrance-free cleanser"),
    ("Anti-Acne Cleanser", f"{BASE}, Salicylic Acid", "active cleanser"),
    ("Daily Sunscreen SPF 15", f"{BASE}, Zinc Oxide", "below 30"),
    ("Tinted Sunscreen SPF 50", f"{BASE}, Zinc Oxide, Niacinamide", "one active per step"),
    ("Hydrating Toner", BASE, "no active the matrix uses"),
    ("Lip Glaze", f"{BASE}, Shea Butter", "not a facial routine product"),
    ("Anything Serum", "", "no ingredient list"),
])
def test_blocks(name, ingredients, reason):
    p = propose(row(name, ingredients), MATRIX)
    assert p.status == "ineligible"
    assert any(reason in b for b in p.blockers), p.blockers


def test_a_vitamin_c_derivative_maps_with_a_warning():
    p = propose(row("Vitamin C Serum", f"{BASE}, Ascorbyl Glucoside (10%)"), MATRIX)
    assert p.primary == "vitamin_c"
    assert any("form of vitamin_c" in w for w in p.warnings)


def test_an_edited_strength_above_the_rule_blocks():
    p = propose(row("Niacinamide Serum", f"{BASE}, Niacinamide"), MATRIX)
    apply_edit(p, MATRIX, percent=10)
    assert p.status == "ineligible"


def test_placing_an_unknown_product_applies_that_steps_checks():
    p = propose(row("Skin Charisma", f"{BASE}, Parfum"), MATRIX)
    assert p.needs == ["type"]
    apply_edit(p, MATRIX, primary="gentle_cleanser")
    assert p.status == "ineligible"   # fragrance, on the fragrance-free cleanser step
    with pytest.raises(ValueError):
        apply_edit(p, MATRIX, primary="retinol")


# ---------------------------------------------------------------------------
# Price tiers and the catalogue shape
# ---------------------------------------------------------------------------


def test_price_tiers_split_at_the_median_of_the_same_kind():
    proposals = [propose(row(f"Plain Moisturizer {n}", f"{BASE}, Dimethicone", price=f"Rs. {price}"), MATRIX)
                 for n, price in enumerate([800, 1000, 1500, 2500])]
    assign_price_tiers(proposals)
    assert [p.price_tier for p in proposals] == ["BUDGET", "BUDGET", "PREMIUM", "PREMIUM"]


def test_an_approved_entry_passes_the_seed_loader(tmp_path):
    p = propose(row("Niacinamide Serum", f"{BASE}, Niacinamide (5%)"), MATRIX)
    assign_price_tiers([p])
    catalogue = json.loads(Path("app/clinical/catalogue/catalogue.v0.json").read_text(encoding="utf-8"))
    catalogue["products"].append(p.catalogue_entry())
    path = tmp_path / "catalogue.json"
    path.write_text(json.dumps(catalogue), encoding="utf-8")

    loaded = load_catalogue_file(path, load_matrix_file(MATRIX_PATH))   # DR-006
    entry = next(e for e in loaded.entries if e.id == p.catalogue_id())
    assert entry.ingredients == [{"ingredient": "niacinamide", "percent": 5.0}]
    assert p.catalogue_entry()["imageUrl"] == "https://cdn.example/p.jpg"
