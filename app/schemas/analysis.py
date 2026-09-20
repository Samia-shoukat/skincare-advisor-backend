"""
Appendix G -- the analysis response schema. Closes OI-003.

This is the contract between the backend and whatever analysed the image, and
it is the narrowest point in the system. Everything upstream of it is a model
that can say anything; everything downstream of it is code that can only see
values from a closed enumeration. FR-AI-001's rationale puts it plainly: a
closed schema prevents the system from emitting a clinical condition name and
bounds the rules engine input.

So this module is deliberately unforgiving about shape and deliberately patient
about content.

## Two kinds of failure, two different answers

**A malformed response** -- not an object, `imageUsable` missing or not a
boolean, `cosmeticConcerns` not a list -- means the provider did not answer the
question that was asked. That raises, the scan returns ANALYSIS_INVALID, and
FR-AI-001 requires the quota to survive untouched.

**An unrecognised identifier** inside a well-formed response is different. The
response is usable; one item in it is not. FR-AI-002 and FR-AI-005 both say the
same thing about this case: discard the item, record the discard in metrics,
carry on. A provider inventing a tenth concern should cost that concern, not the
user's scan.

The distinction matters because the second case is routine. Vision models
paraphrase enum values, and a scan failing outright every time one does would
make the product unusable while telling us nothing.

## Determinism

FR-REC-001 requires byte-identical routines from identical input, and the rules
engine reads what comes out of here. A provider returning the same concerns in a
different order on two calls would break that claim through no fault of the
engine, so findings are sorted into enumeration order and duplicates are
collapsed before they leave this module. `parse_provider_response` is a pure
function of its argument: no clock, no database, no configuration.

## Observation text

`observation` is the only free-text field Appendix G permits, and DR-008
requires it to be validated against a prohibited-terms list before storage or
display. Validation happens here rather than at the display layer, so that a
name cannot reach the scan log either.

A failing string is replaced, not dropped, and the signal survives. The
reasoning is in `app/clinical/copy/observations.py`: the signal is what causes
the referral, and losing a referral to a wording problem is the one outcome
FR-AI-006 is shaped to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.clinical.copy.observations import observation_for
from app.clinical.copy.prohibited_terms import find_prohibited
from app.core.enums import (
    ClinicalSignal,
    Concern,
    Confidence,
    ObservationSource,
    Severity,
)


class AnalysisSchemaError(ValueError):
    """
    The response is not shaped like Appendix G.

    Raised only for structural problems. An unrecognised identifier is not one
    of these -- it is a discard, and the scan continues.
    """


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConcernFinding:
    """One cosmetic concern with its grade. Input to the rules engine."""

    concern: Concern
    severity: Severity


@dataclass(frozen=True)
class SignalFinding:
    """
    One finding outside cosmetic scope.

    `observation` is what the user reads. `observation_source` records whether
    that text came from the provider or was substituted under DR-008, which is
    stored on the scan log and is the only way to notice a prompt regression
    before it shows up as something worse.
    """

    signal: ClinicalSignal
    observation: str
    confidence: Confidence
    observation_source: ObservationSource


@dataclass(frozen=True)
class Discards:
    """
    Everything the provider returned that this module refused.

    FR-AI-002 and FR-AI-005 require discards to be recorded in metrics, and
    DR-008 requires the same of a substituted observation. Counting them is not
    enough on its own -- the values are kept, because "three concerns discarded"
    says nothing actionable and "discarded ACNE_SCARS" points straight at the
    prompt that produced it.

    No image data and no user identifier reaches here: only the identifiers the
    provider used and the prohibited terms that were found in its text.
    """

    unknown_concerns: tuple[str, ...] = ()
    unknown_signals: tuple[str, ...] = ()
    invalid_severities: tuple[str, ...] = ()
    invalid_confidences: tuple[str, ...] = ()
    # Signal identifiers whose observation failed DR-008, with the terms that
    # failed it.
    substituted_observations: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def any(self) -> bool:
        return bool(
            self.unknown_concerns
            or self.unknown_signals
            or self.invalid_severities
            or self.invalid_confidences
            or self.substituted_observations
        )

    def as_metrics(self) -> dict[str, Any]:
        """Flat shape for the scan log. Empty keys are omitted, not nulled."""
        payload: dict[str, Any] = {}
        if self.unknown_concerns:
            payload["unknownConcerns"] = list(self.unknown_concerns)
        if self.unknown_signals:
            payload["unknownSignals"] = list(self.unknown_signals)
        if self.invalid_severities:
            payload["invalidSeverities"] = list(self.invalid_severities)
        if self.invalid_confidences:
            payload["invalidConfidences"] = list(self.invalid_confidences)
        if self.substituted_observations:
            payload["substitutedObservations"] = [
                {"signalId": sid, "terms": list(terms)}
                for sid, terms in self.substituted_observations
            ]
        return payload

    def merged_with(self, other: "Discards") -> "Discards":
        """
        Combine two records.

        Needed because FR-AI-004 permits a scan to be served by more than one
        task, and a discard from the concerns task and a discard from the
        signals task belong on the same scan log entry.
        """
        return Discards(
            unknown_concerns=self.unknown_concerns + other.unknown_concerns,
            unknown_signals=self.unknown_signals + other.unknown_signals,
            invalid_severities=self.invalid_severities + other.invalid_severities,
            invalid_confidences=self.invalid_confidences + other.invalid_confidences,
            substituted_observations=(
                self.substituted_observations + other.substituted_observations
            ),
        )


@dataclass(frozen=True)
class AnalysisResult:
    """
    A validated Appendix G response.

    Past this point nothing in the system can hold a value outside the closed
    enumerations, which is the property the rest of the pipeline is built on.
    """

    image_usable: bool
    concerns: tuple[ConcernFinding, ...] = ()
    signals: tuple[SignalFinding, ...] = ()

    @property
    def has_clinical_signal(self) -> bool:
        """FR-TRI-002 triggers on presence, not on severity or confidence."""
        return bool(self.signals)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


# Enumeration order, used to sort findings. Declaration order in `enums.py` is
# the SRS 4.2 order, so a routine's inputs read the way the specification lists
# them rather than the way a provider happened to emit them.
_CONCERN_ORDER = {c: i for i, c in enumerate(Concern)}
_SIGNAL_ORDER = {s: i for i, s in enumerate(ClinicalSignal)}
_SEVERITY_RANK = {Severity.MILD: 0, Severity.MODERATE: 1, Severity.PRONOUNCED: 2}
_CONFIDENCE_RANK = {Confidence.LOW: 0, Confidence.MODERATE: 1, Confidence.HIGH: 2}


def parse_provider_response(raw: Any) -> tuple[AnalysisResult, Discards]:
    """
    Validate an Appendix G response and reduce it to closed vocabulary.

    Raises `AnalysisSchemaError` where the response is structurally wrong.
    Returns the surviving findings and a record of everything refused.
    """
    if not isinstance(raw, dict):
        raise AnalysisSchemaError("response is not a JSON object")

    if "imageUsable" not in raw:
        raise AnalysisSchemaError("imageUsable is missing")

    image_usable = raw["imageUsable"]
    if not isinstance(image_usable, bool):
        # Not coerced from a string or an integer. A provider that answers
        # "true" instead of true has not followed the contract, and guessing at
        # its intent on the one field that decides whether a face was visible
        # is not a guess worth making.
        raise AnalysisSchemaError("imageUsable is not a boolean")

    concerns, concern_discards = _parse_concerns(raw.get("cosmeticConcerns"))
    signals, signal_discards = _parse_signals(raw.get("clinicalSignals"))

    result = AnalysisResult(
        image_usable=image_usable,
        concerns=concerns,
        signals=signals,
    )
    return result, concern_discards.merged_with(signal_discards)


def _parse_concerns(raw: Any) -> tuple[tuple[ConcernFinding, ...], Discards]:
    """FR-AI-002. Nine values, everything else discarded."""
    if raw is None:
        return (), Discards()
    if not isinstance(raw, list):
        raise AnalysisSchemaError("cosmeticConcerns is not an array")

    unknown: list[str] = []
    bad_severity: list[str] = []
    # Keyed by concern so a duplicate collapses rather than being counted twice.
    best: dict[Concern, Severity] = {}

    for item in raw:
        if not isinstance(item, dict):
            unknown.append(_describe(item))
            continue

        concern = _coerce(Concern, item.get("concernId"))
        if concern is None:
            unknown.append(_describe(item.get("concernId")))
            continue

        severity = _coerce(Severity, item.get("severity"))
        if severity is None:
            # The concern is real and the grade is not. Recording MILD rather
            # than discarding keeps the concern in the routine at the lowest
            # grade, which is the conservative reading -- dropping it would
            # silently remove something the provider did see.
            bad_severity.append(_describe(item.get("severity")))
            severity = Severity.MILD

        existing = best.get(concern)
        if existing is None or _SEVERITY_RANK[severity] > _SEVERITY_RANK[existing]:
            best[concern] = severity

    findings = tuple(
        ConcernFinding(concern=c, severity=best[c])
        for c in sorted(best, key=lambda c: _CONCERN_ORDER[c])
    )

    return findings, Discards(
        unknown_concerns=tuple(unknown),
        invalid_severities=tuple(bad_severity),
    )


def _parse_signals(raw: Any) -> tuple[tuple[SignalFinding, ...], Discards]:
    """FR-AI-005, and DR-008 on the observation text."""
    if raw is None:
        return (), Discards()
    if not isinstance(raw, list):
        raise AnalysisSchemaError("clinicalSignals is not an array")

    unknown: list[str] = []
    bad_confidence: list[str] = []
    substituted: list[tuple[str, tuple[str, ...]]] = []
    # Keyed by signal, keeping the highest confidence. Two reports of the same
    # appearance are one finding; taking the higher confidence is the reading
    # that keeps FR-AI-007's HIGH threshold reachable rather than diluting it
    # with a duplicate.
    best: dict[ClinicalSignal, tuple[Confidence, str, ObservationSource]] = {}

    for item in raw:
        if not isinstance(item, dict):
            unknown.append(_describe(item))
            continue

        signal = _coerce(ClinicalSignal, item.get("signalId"))
        if signal is None:
            unknown.append(_describe(item.get("signalId")))
            continue

        confidence = _coerce(Confidence, item.get("confidence"))
        if confidence is None:
            # LOW, not HIGH. An unreadable confidence must not be the thing
            # that unlocks an association list under FR-AI-007.
            bad_confidence.append(_describe(item.get("confidence")))
            confidence = Confidence.LOW

        observation, source, terms = _validate_observation(signal, item.get("observation"))
        if terms:
            substituted.append((signal.value, terms))

        existing = best.get(signal)
        if existing is None or _CONFIDENCE_RANK[confidence] > _CONFIDENCE_RANK[existing[0]]:
            best[signal] = (confidence, observation, source)

    findings = tuple(
        SignalFinding(
            signal=s,
            confidence=best[s][0],
            observation=best[s][1],
            observation_source=best[s][2],
        )
        for s in sorted(best, key=lambda s: _SIGNAL_ORDER[s])
    )

    return findings, Discards(
        unknown_signals=tuple(unknown),
        invalid_confidences=tuple(bad_confidence),
        substituted_observations=tuple(substituted),
    )


def _validate_observation(
    signal: ClinicalSignal, raw: Any
) -> tuple[str, ObservationSource, tuple[str, ...]]:
    """
    DR-008. Return the text that may be shown, where it came from, and why.

    An empty or non-string observation is treated the same as a prohibited one:
    FR-AI-005 requires a non-empty observation on every signal, so there is no
    valid response that omits it, and the curated text is the answer either way.
    """
    if not isinstance(raw, str) or not raw.strip():
        return observation_for(signal), ObservationSource.CURATED, ()

    text = " ".join(raw.split())
    terms = find_prohibited(text)
    if terms:
        return observation_for(signal), ObservationSource.CURATED, tuple(terms)

    return text, ObservationSource.PROVIDER, ()


def _coerce(enum_cls: Any, value: Any) -> Any | None:
    """
    An enum member, or None where the value is not one.

    Case and surrounding whitespace are forgiven because they are transcription
    noise rather than a different answer. Nothing else is: a near-miss like
    "ACNE_SCARS" is a value outside the enumeration, and FR-AI-002 says to
    discard it, not to guess which of the nine was meant.
    """
    if not isinstance(value, str):
        return None
    try:
        return enum_cls(value.strip().upper())
    except ValueError:
        return None


def _describe(value: Any) -> str:
    """
    A short, safe rendering of a rejected value for the discard record.

    Truncated because this ends up in a log line, and a provider that returns a
    paragraph where an identifier belongs should not be able to write a
    paragraph into the log.
    """
    text = value if isinstance(value, str) else type(value).__name__
    return text[:64]
