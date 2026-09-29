"""
Appendix G, DR-008, and Appendix H, as units. No HTTP, no database.

These are the closed-vocabulary guarantees the rest of the system is built on,
so they are tested directly as well as through the endpoint: a regression here
should fail with a message about the schema, not about a scan.
"""

from __future__ import annotations

import json

import pytest

from app.clinical.copy import strings
from app.clinical.copy.observations import OBSERVATIONS
from app.clinical.copy.prohibited_terms import (
    CONDITION_NAMES,
    find_condition_names,
    find_prohibited,
)
from app.core.enums import ClinicalSignal, Concern, Confidence, ObservationSource
from app.schemas.analysis import AnalysisSchemaError, parse_provider_response
from app.services.association_table import (
    AssociationTableInvalid,
    get_association_table,
    load_association_table,
)

TABLE_PATH = "app/clinical/associations/associations.v0.json"


# ---------------------------------------------------------------------------
# Structure -- FR-AI-001
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        "imageUsable: true",
        {},
        {"imageUsable": "true"},
        {"imageUsable": 1},
        {"imageUsable": True, "cosmeticConcerns": {}},
        {"imageUsable": True, "clinicalSignals": "none"},
    ],
)
def test_structurally_wrong_responses_raise(raw):
    with pytest.raises(AnalysisSchemaError):
        parse_provider_response(raw)


def test_missing_arrays_are_empty_not_errors():
    result, discards = parse_provider_response({"imageUsable": True})
    assert result.concerns == () and result.signals == ()
    assert not discards.any


# ---------------------------------------------------------------------------
# Closed vocabulary -- FR-AI-002, FR-AI-005
# ---------------------------------------------------------------------------


def test_unknown_concern_is_discarded_and_recorded():
    result, discards = parse_provider_response(
        {
            "imageUsable": True,
            "cosmeticConcerns": [
                {"concernId": "ACNE", "severity": "MILD"},
                {"concernId": "ACNE_SCARS", "severity": "MILD"},
            ],
        }
    )
    assert [c.concern for c in result.concerns] == [Concern.ACNE]
    assert discards.unknown_concerns == ("ACNE_SCARS",)


def test_unknown_signal_is_discarded_and_recorded():
    result, discards = parse_provider_response(
        {
            "imageUsable": True,
            "clinicalSignals": [
                {"signalId": "MELANOMA", "observation": "x", "confidence": "HIGH"}
            ],
        }
    )
    assert result.signals == ()
    assert discards.unknown_signals == ("MELANOMA",)


def test_case_and_whitespace_are_forgiven_but_near_misses_are_not():
    result, discards = parse_provider_response(
        {
            "imageUsable": True,
            "cosmeticConcerns": [
                {"concernId": " excess_oil ", "severity": "mild"},
                {"concernId": "EXCESS-OIL", "severity": "MILD"},
            ],
        }
    )
    assert [c.concern for c in result.concerns] == [Concern.EXCESS_OIL]
    assert discards.unknown_concerns == ("EXCESS-OIL",)


def test_unreadable_confidence_is_low_never_high():
    """An unreadable confidence must not be what unlocks FR-AI-007."""
    result, discards = parse_provider_response(
        {
            "imageUsable": True,
            "clinicalSignals": [
                {"signalId": "BLISTERS", "observation": "Raised bumps.", "confidence": "VERY"}
            ],
        }
    )
    assert result.signals[0].confidence is Confidence.LOW
    assert discards.invalid_confidences == ("VERY",)


def test_provider_paragraph_is_truncated_in_the_discard_record():
    _, discards = parse_provider_response(
        {"imageUsable": True, "cosmeticConcerns": [{"concernId": "A" * 500}]}
    )
    assert len(discards.unknown_concerns[0]) == 64


# ---------------------------------------------------------------------------
# Determinism -- FR-REC-001
# ---------------------------------------------------------------------------


def test_order_and_duplicates_do_not_change_the_result():
    a = {
        "imageUsable": True,
        "cosmeticConcerns": [
            {"concernId": "FINE_LINES", "severity": "MILD"},
            {"concernId": "ACNE", "severity": "MILD"},
            {"concernId": "ACNE", "severity": "PRONOUNCED"},
        ],
    }
    b = {
        "imageUsable": True,
        "cosmeticConcerns": [
            {"concernId": "ACNE", "severity": "PRONOUNCED"},
            {"concernId": "FINE_LINES", "severity": "MILD"},
        ],
    }
    assert parse_provider_response(a)[0] == parse_provider_response(b)[0]


# ---------------------------------------------------------------------------
# Observations -- DR-008, OI-008, OI-009
# ---------------------------------------------------------------------------


def test_every_signal_has_curated_text():
    assert set(OBSERVATIONS) == set(ClinicalSignal)


@pytest.mark.parametrize("signal", list(ClinicalSignal))
def test_curated_text_passes_its_own_filter(signal):
    assert find_prohibited(OBSERVATIONS[signal]) == []


@pytest.mark.parametrize(
    "text",
    [
        "Dark patches that look like melasma.",
        "Redness consistent with rosacea.",
        "A raised lesion on the jaw.",
        "This is suggestive of an infection.",
        "Scaling, possibly PSORIASIS.",
    ],
)
def test_observation_naming_or_hedging_is_substituted(text):
    result, discards = parse_provider_response(
        {
            "imageUsable": True,
            "clinicalSignals": [
                {"signalId": "INFLAMED_PATCHES", "observation": text, "confidence": "HIGH"}
            ],
        }
    )
    signal = result.signals[0]
    assert signal.observation == OBSERVATIONS[ClinicalSignal.INFLAMED_PATCHES]
    assert signal.observation_source is ObservationSource.CURATED
    assert discards.substituted_observations


def test_clean_provider_text_is_kept():
    result, _ = parse_provider_response(
        {
            "imageUsable": True,
            "clinicalSignals": [
                {
                    "signalId": "PERSISTENT_REDNESS",
                    "observation": "Redness across  both cheeks and the nose.",
                    "confidence": "MODERATE",
                }
            ],
        }
    )
    signal = result.signals[0]
    assert signal.observation == "Redness across both cheeks and the nose."
    assert signal.observation_source is ObservationSource.PROVIDER


def test_names_embedded_in_other_words_do_not_trigger():
    """Word boundaries: 'acne' inside another word is not a name."""
    assert find_condition_names("Pacnet and acnestis") == []


def test_mole_is_not_prohibited():
    """It appears in safety question 3 and names ordinary skin, not a disease."""
    assert find_prohibited("A mole near the left ear.") == []


def test_every_user_facing_string_is_free_of_condition_names():
    """
    Except the limitations statement's own disclaimers and the safety questions,
    which the SRS dictates verbatim.
    """
    exempt = {"LIMITATIONS_STATEMENT", "SAFETY_QUESTIONS"}
    for name in dir(strings):
        if name.isupper() and name not in exempt:
            value = getattr(strings, name)
            # For a mapping, only the values are displayed; keys are internal
            # identifiers like "ACNE" that never reach the screen.
            shown = " ".join(map(str, value.values())) if isinstance(value, dict) else str(value)
            assert find_condition_names(shown) == [], name


# ---------------------------------------------------------------------------
# Appendix H -- FR-AI-007, DR-009
# ---------------------------------------------------------------------------


def test_shipped_table_loads_with_every_row_disabled():
    table = get_association_table(TABLE_PATH)
    assert table.rows
    assert not any(r.enabled for r in table.rows.values())


def test_shipped_table_deliberately_omits_irregular_lesion_and_open_wound():
    table = get_association_table(TABLE_PATH)
    assert table.row_for(ClinicalSignal.IRREGULAR_LESION) is None
    assert table.row_for(ClinicalSignal.OPEN_WOUND) is None


def test_every_shipped_row_names_at_least_two_known_conditions():
    table = get_association_table(TABLE_PATH)
    for row in table.rows.values():
        assert len(row.associations) >= 2


def _table(tmp_path, row: dict):
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"version": "t", "rows": [row]}), encoding="utf-8")
    return path


def test_single_name_row_refuses_to_load(tmp_path):
    """FR-AI-007: a single name functions as a diagnosis."""
    path = _table(
        tmp_path,
        {"signalId": "BLISTERS", "associations": ["One"], "source": "x", "enabled": False},
    )
    with pytest.raises(AssociationTableInvalid, match="fewer than two"):
        load_association_table(path)


def test_enabled_row_without_measurement_refuses_to_load(tmp_path):
    """DR-009."""
    path = _table(
        tmp_path,
        {"signalId": "BLISTERS", "associations": ["A", "B"], "source": "x", "enabled": True},
    )
    with pytest.raises(AssociationTableInvalid, match="no recorded precision"):
        load_association_table(path)


def test_enabled_row_below_precision_bar_refuses_to_load(tmp_path):
    """NFR-SAFE-006."""
    path = _table(
        tmp_path,
        {
            "signalId": "BLISTERS",
            "associations": ["A", "B"],
            "source": "x",
            "precisionMeasured": 0.79,
            "enabled": True,
        },
    )
    with pytest.raises(AssociationTableInvalid, match="below"):
        load_association_table(path)


def test_row_without_source_refuses_to_load(tmp_path):
    path = _table(
        tmp_path,
        {"signalId": "BLISTERS", "associations": ["A", "B"], "source": "", "enabled": False},
    )
    with pytest.raises(AssociationTableInvalid, match="source"):
        load_association_table(path)


def test_a_measured_enabled_row_does_produce_names(tmp_path):
    """
    The mechanism works when permitted -- so that enabling a row later is a
    data change, and this test is the evidence it needs no code change.
    """
    from app.schemas.analysis import SignalFinding
    from app.services.association_table import resolve_associations

    path = _table(
        tmp_path,
        {
            "signalId": "BLISTERS",
            "associations": ["Alpha", "Beta"],
            "source": "x",
            "precisionMeasured": 0.9,
            "enabled": True,
        },
    )
    table = load_association_table(path)
    [outcome] = resolve_associations(
        table,
        (SignalFinding(ClinicalSignal.BLISTERS, "x", Confidence.HIGH, ObservationSource.CURATED),),
    )
    assert outcome.associations == ("Alpha", "Beta")
    assert not outcome.suppressed


def test_prohibited_list_covers_every_shipped_association_name():
    """
    A name in Appendix H must be caught if a provider ever writes it into an
    observation. Checks each name's words against the list.
    """
    table = get_association_table(TABLE_PATH)
    for row in table.rows.values():
        for name in row.associations:
            assert find_prohibited(name), f"{name!r} would pass DR-008 validation"
    assert "melanoma" in CONDITION_NAMES
