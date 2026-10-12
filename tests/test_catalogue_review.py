"""
The catalogue review loop. Approvals go to the catalogue file -- validated
before it is written -- and every decision is logged.

The loop is driven with scripted answers in place of a keyboard.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.services.catalogue import load_catalogue_file
from app.services.catalogue_review import CatalogueFile, ReviewLog, bump_version, run_review
from app.services.ingredient_parser import assign_price_tiers, propose
from app.services.matrix_loader import load_matrix_file

MATRIX_PATH = Path("app/clinical/matrix/rules_matrix.v0.json")
MATRIX = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
BASE = "Aqua, Glycerin, Propanediol, Xanthan Gum, Phenoxyethanol, Ethylhexylglycerin, Sodium Hyaluronate"


def candidate(source_id: str, name: str, ingredients: str, price: str = "Rs. 1,200") -> dict:
    p = propose({"id": source_id, "brand": "Testbrand", "name": name, "price": price,
                 "image_url": f"https://cdn.example/{source_id}.jpg", "skin_type": None,
                 "ingredients": ingredients}, MATRIX)
    assign_price_tiers([p])
    return p.to_dict()


READY = candidate("s1", "Calm Niacinamide Serum", f"{BASE}, Niacinamide (5%)")
READY_TOO = candidate("s2", "Plain Gel Moisturizer", f"{BASE}, Dimethicone")
NEEDS_STRENGTH = candidate("s3", "Other Niacinamide Serum", f"{BASE}, Niacinamide")
BLOCKED = candidate("s4", "Glow Serum", f"{BASE}, Niacinamide, Alpha Arbutin")


@pytest.fixture
def files(tmp_path):
    catalogue_path = tmp_path / "catalogue.json"
    shutil.copy("app/clinical/catalogue/catalogue.v0.json", catalogue_path)
    matrix = load_matrix_file(MATRIX_PATH)
    return CatalogueFile(catalogue_path, matrix), ReviewLog(tmp_path / "review_log.json"), matrix


def scripted(*answers):
    queue = list(answers)
    return lambda _prompt: queue.pop(0)


def review(files, candidates, *answers, **options):
    catalogue, log, _ = files
    return run_review(candidates, matrix=MATRIX, catalogue=catalogue, log=log, reviewer="tester",
                      ask=scripted(*answers), say=lambda _line: None, **options)


def test_version_bump():
    assert bump_version("1.0.0-pk") == "1.1.0-pk"
    assert bump_version("2.3.9") == "2.4.0"


def test_approving_writes_a_valid_catalogue_and_logs_it(files):
    catalogue, log, matrix = files
    before = catalogue.version
    counts = review(files, [READY, READY_TOO], "a", "a")

    assert counts == {"approved": 2, "rejected": 0, "skipped": 0}
    loaded = load_catalogue_file(catalogue.path, matrix)      # what the seed will read
    assert loaded.version == bump_version(before)              # bumped once, not twice
    ids = {e.id for e in loaded.entries}
    assert {"testbrand-calm-niacinamide-serum", "testbrand-plain-gel-moisturizer"} <= ids

    saved = json.loads(catalogue.path.read_text(encoding="utf-8"))["products"][-2]
    assert saved["imageUrl"] == "https://cdn.example/s1.jpg"
    assert [d["decision"] for d in log.data["decisions"]] == ["approved", "approved"]
    assert log.data["decisions"][0]["reviewer"] == "tester"


def test_a_candidate_needing_input_cannot_be_approved_until_edited(files):
    catalogue, log, _ = files
    counts = review(files, [NEEDS_STRENGTH], "a", "e", "s", "4", "d", "a")
    assert counts["approved"] == 1
    entry = json.loads(catalogue.path.read_text(encoding="utf-8"))["products"][-1]
    assert entry["ingredients"] == [{"ingredient": "niacinamide", "percent": 4.0}]
    assert log.data["decisions"][0]["edits"] == ["strength=4%"]


def test_an_edit_above_the_rule_keeps_it_out(files):
    catalogue, _, _ = files
    before = catalogue.path.read_text(encoding="utf-8")
    counts = review(files, [NEEDS_STRENGTH], "e", "s", "10", "d", "a", "q")
    assert counts["approved"] == 0
    assert catalogue.path.read_text(encoding="utf-8") == before


def test_rejection_is_logged_with_its_reason(files):
    _, log, _ = files
    review(files, [READY], "r", "out of stock everywhere")
    [decision] = log.data["decisions"]
    assert (decision["decision"], decision["reason"]) == ("rejected", "out of stock everywhere")


def test_blocked_candidates_are_not_offered(files):
    counts = review(files, [BLOCKED])
    assert counts == {"approved": 0, "rejected": 0, "skipped": 0}
    # ...and with --show-ineligible they are shown read-only.
    assert review(files, [BLOCKED], "n", show_ineligible=True)["approved"] == 0


def test_decided_candidates_are_not_asked_again(files):
    review(files, [READY], "r", "")
    assert review(files, [READY]) == {"approved": 0, "rejected": 0, "skipped": 0}


def test_auto_approve_takes_the_eligible_and_never_invents_a_strength(files):
    from scripts.auto_approve_candidates import auto_approve

    catalogue, log, _ = files
    approved, skipped, failed = auto_approve(
        [READY, NEEDS_STRENGTH, BLOCKED], catalogue, log, reviewer="tester (auto)")
    assert [p.source_id for p in approved] == ["s1"]
    assert [p.source_id for p, _ in skipped] == ["s3"]        # needs strength: left out
    assert failed == []
    ids = catalogue.ids()
    assert "testbrand-calm-niacinamide-serum" in ids
    assert "testbrand-other-niacinamide-serum" not in ids     # no default strength
    assert log.data["decisions"][0]["reviewer"] == "tester (auto)"


def test_an_invalid_entry_leaves_the_file_untouched(files):
    catalogue, _, _ = files
    before = catalogue.path.read_text(encoding="utf-8")
    bad = {"id": "bad", "brand": "x", "name": "x", "priceTier": "BUDGET",
           "ingredients": [{"ingredient": "retinol", "percent": None}]}
    with pytest.raises(Exception):
        catalogue.add(bad)
    assert catalogue.path.read_text(encoding="utf-8") == before
    assert "bad" not in catalogue.ids()
