"""
Appendix F loader. FR-REC-006, DR-007.

Two groups: the shipped matrix is valid and complete, and a broken matrix is
refused at load rather than at scan time.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.enums import Concern, ReviewStatus
from app.services.matrix_loader import (
    MatrixInvalid,
    MatrixStore,
    load_matrix_file,
    parse_matrix,
)

MATRIX_PATH = Path("app/clinical/matrix/rules_matrix.v0.json")


@pytest.fixture
def raw() -> dict:
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The shipped matrix
# ---------------------------------------------------------------------------


def test_shipped_matrix_loads():
    matrix = load_matrix_file(MATRIX_PATH)
    assert matrix.version
    assert matrix.rules


def test_every_concern_has_a_policy_and_at_least_one_rule():
    matrix = load_matrix_file(MATRIX_PATH)
    for concern in Concern:
        assert concern in matrix.concerns
        assert matrix.rules_for(concern), f"no rule treats {concern.value}"


def test_every_rule_has_source_and_review_status():
    """DR-007, threshold 100%."""
    matrix = load_matrix_file(MATRIX_PATH)
    for rule in matrix.rules:
        assert rule.source
        assert isinstance(rule.review_status, ReviewStatus)


def test_nothing_claims_expert_confirmation():
    """No practitioner has reviewed this matrix (OI-001)."""
    matrix = load_matrix_file(MATRIX_PATH)
    statuses = {r.review_status for r in matrix.rules} | {s.review_status for s in matrix.base_steps}
    assert ReviewStatus.EXPERT_CONFIRMED not in statuses


def test_barrier_concerns_outrank_acne():
    """FR-REC-001: barrier repair outranks active treatment."""
    matrix = load_matrix_file(MATRIX_PATH)
    assert matrix.concerns[Concern.DRYNESS].priority < matrix.concerns[Concern.ACNE].priority


# ---------------------------------------------------------------------------
# Broken matrices are refused
# ---------------------------------------------------------------------------


def _break(raw: dict, mutate) -> dict:
    broken = copy.deepcopy(raw)
    mutate(broken)
    return broken


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda m: m.pop("version"), "no version"),
        (lambda m: m["rules"][0].update(source=""), "DR-007"),
        (lambda m: m["rules"][0].update(reviewStatus="approved"), "DR-007"),
        (lambda m: m["rules"][0].update(ingredient="snail_mucin"), "unknown ingredient"),
        (lambda m: m["rules"][0].update(concern="ACNE_SCARS"), "unknown concern"),
        (lambda m: m["rules"][0].update(timing="NOON"), "AM or PM"),
        (lambda m: m["rules"][1].update(ruleId=m["rules"][0]["ruleId"]), "duplicate"),
        (lambda m: m["concerns"].pop("FINE_LINES"), "FINE_LINES"),
        (lambda m: m["ingredients"]["adapalene"].pop("pregnancyRestricted"), "pregnancyRestricted"),
        (lambda m: m["ingredients"]["vitamin_c"].update(incompatibleWith=[]), "one side only"),
        (lambda m: m["baseSteps"].pop(2), "sunscreen"),
        (
            lambda m: m["baseSteps"][0]["ingredientBySkinType"].update(OILY="niacinamide"),
            "active ingredient",
        ),
    ],
)
def test_broken_matrix_is_refused(raw, mutate, message):
    with pytest.raises(MatrixInvalid, match=message):
        parse_matrix(_break(raw, mutate))


# ---------------------------------------------------------------------------
# Remote matrix and fallback -- FR-REC-006
# ---------------------------------------------------------------------------


def _settings(url: str = "") -> Settings:
    return Settings(
        rules_matrix_url=url,
        rules_matrix_path=str(MATRIX_PATH),
        rules_matrix_cache_ttl_seconds=300,
    )


async def test_no_url_uses_the_local_file():
    loaded = await MatrixStore(_settings()).get()
    assert loaded.matrix.version == load_matrix_file(MATRIX_PATH).version
    assert not loaded.fell_back


async def test_published_update_is_used_without_a_release(raw):
    """FR-REC-006: an updated matrix is picked up on the next fetch."""
    published = _break(raw, lambda m: m.update(version="9.9.9"))

    async def fetch(url):
        return published

    loaded = await MatrixStore(_settings("https://example.test/m.json"), fetch=fetch).get()
    assert loaded.matrix.version == "9.9.9"
    assert not loaded.fell_back


async def test_unreachable_resource_falls_back_to_last_good(raw):
    """FR-REC-006: the last retrieved matrix is used and the fallback recorded."""
    now = [0.0]
    responses = [_break(raw, lambda m: m.update(version="2.0.0")), ConnectionError()]

    async def fetch(url):
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    store = MatrixStore(_settings("https://example.test/m.json"), fetch=fetch, clock=lambda: now[0])
    first = await store.get()
    now[0] = 1000.0  # past the TTL, forces a refetch
    second = await store.get()

    assert first.matrix.version == "2.0.0" and not first.fell_back
    assert second.matrix.version == "2.0.0" and second.fell_back


async def test_broken_published_matrix_does_not_replace_the_good_one(raw):
    async def fetch(url):
        return {"version": "3.0.0"}  # fails validation

    loaded = await MatrixStore(_settings("https://example.test/m.json"), fetch=fetch).get()
    assert loaded.fell_back
    assert loaded.matrix.version == load_matrix_file(MATRIX_PATH).version


async def test_cache_avoids_refetching_within_ttl(raw):
    calls = []

    async def fetch(url):
        calls.append(url)
        return raw

    store = MatrixStore(_settings("https://example.test/m.json"), fetch=fetch, clock=lambda: 0.0)
    await store.get()
    await store.get()
    assert len(calls) == 1
