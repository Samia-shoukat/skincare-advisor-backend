"""
Appendix H resolution. FR-AI-007, FR-AI-008, DR-009.

Two requirements govern this file and they point in opposite directions, which
is the whole design problem.

FR-AI-007 permits the system to name conditions, as a differential of two or
more, for a signal returned at HIGH confidence, drawn from the closed table.
FR-AI-008 requires suppression whenever confidence is below HIGH, whenever more
than two signals are present, or whenever the table has no entry -- and its
rationale states the priority plainly: "Suppression is the default; naming is
the exception."

So this module is written as a suppression check that occasionally declines to
suppress, rather than as a lookup that occasionally refuses. The difference is
not stylistic. A lookup with guards has a default of "return the names", and
every future edit is one missing guard away from naming a condition. A function
whose every early return is a suppression has a default of silence.

## Why the table lives in JSON

CON-003: no clinical judgement in application code. A practitioner reviewing
what this app is willing to say about someone's skin should be able to read one
file and see all of it, without reading Python. That file is
`app/clinical/associations/associations.v0.json`, and this module is an
interpreter for it -- there is no condition name anywhere in this source file,
and there must never be one.

## The enabled flag

DR-009 requires every enabled row to carry a source reference and a recorded
precision measurement under NFR-SAFE-006. That is enforced at load: a row
claiming `enabled: true` without a measurement is rejected and the whole table
refuses to load, rather than being quietly disabled.

Failing loudly is deliberate. A silently disabled row looks identical to a row
that was never enabled, so the failure mode of the quiet version is that
someone enables a row, sees no error, sees no names, and concludes the feature
is broken rather than that the precondition is unmet.

In release 1.0 every row is disabled, so every path through here suppresses.
That is the intended state and the reasoning is in the table file itself.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.enums import ClinicalSignal, Confidence, SuppressionReason
from app.schemas.analysis import SignalFinding

logger = logging.getLogger(__name__)


# FR-AI-008. More than two concurrent signals suppresses every association,
# because the appearances overlap and the differential stops being meaningful.
# Stated as a named constant rather than a literal `> 2` so that the threshold
# is searchable from the requirement.
MAX_SIGNALS_FOR_ASSOCIATION = 2

# NFR-SAFE-006. A row may be enabled only at or above this measured precision.
MIN_PRECISION = 0.80


class AssociationTableInvalid(ValueError):
    """The table file is malformed, or a row claims more than it can support."""


@dataclass(frozen=True)
class AssociationRow:
    signal: ClinicalSignal
    associations: tuple[str, ...]
    source: str
    precision_measured: float | None
    enabled: bool


@dataclass(frozen=True)
class AssociationTable:
    version: str
    rows: dict[ClinicalSignal, AssociationRow]

    def row_for(self, signal: ClinicalSignal) -> AssociationRow | None:
        return self.rows.get(signal)


@dataclass(frozen=True)
class AssociationOutcome:
    """
    The decision for one signal.

    `associations` is empty whenever `suppression_reason` is set, and the two
    are never both populated. Callers are expected to render `associations`
    only when it is non-empty rather than branching on the reason, so that a
    reason added later cannot accidentally become a display path.
    """

    signal: ClinicalSignal
    associations: tuple[str, ...] = ()
    suppression_reason: SuppressionReason | None = None

    @property
    def suppressed(self) -> bool:
        return self.suppression_reason is not None


def load_association_table(path: Path) -> AssociationTable:
    payload = json.loads(path.read_text(encoding="utf-8"))

    version = payload.get("version")
    if not version:
        raise AssociationTableInvalid("association table has no version")

    rows: dict[ClinicalSignal, AssociationRow] = {}

    for raw in payload.get("rows", []):
        signal = _coerce_signal(raw.get("signalId"))
        if signal is None:
            raise AssociationTableInvalid(
                f"row names {raw.get('signalId')!r}, which is not a ClinicalSignal"
            )

        if signal in rows:
            raise AssociationTableInvalid(f"duplicate row for {signal.value}")

        names = tuple(str(n).strip() for n in raw.get("associations", []) if str(n).strip())

        # FR-AI-007: "a row shall never contain a single name". A single name
        # functions as a diagnosis and anchors both the user and any clinician
        # they go on to consult, which is the harm the minimum exists to
        # prevent. Rejected at load rather than filtered at display, so the
        # table cannot be edited into that state without the app refusing to
        # start.
        if len(names) < 2:
            raise AssociationTableInvalid(
                f"row {signal.value} has fewer than two associations; "
                "a single name functions as a diagnosis"
            )

        source = str(raw.get("source") or "").strip()
        if not source:
            # DR-009, completeness threshold 100%.
            raise AssociationTableInvalid(f"row {signal.value} has no source reference")

        precision = raw.get("precisionMeasured")
        enabled = bool(raw.get("enabled", False))

        if enabled:
            # NFR-SAFE-006 and DR-009 together. Both halves are checked because
            # a row can fail either way: no measurement at all, or a
            # measurement that does not clear the bar.
            if not isinstance(precision, (int, float)):
                raise AssociationTableInvalid(
                    f"row {signal.value} is enabled with no recorded precision "
                    "measurement; DR-009 requires one"
                )
            if float(precision) < MIN_PRECISION:
                raise AssociationTableInvalid(
                    f"row {signal.value} is enabled at precision {precision}, "
                    f"below the NFR-SAFE-006 bar of {MIN_PRECISION}"
                )

        rows[signal] = AssociationRow(
            signal=signal,
            associations=names,
            source=source,
            precision_measured=float(precision) if isinstance(precision, (int, float)) else None,
            enabled=enabled,
        )

    enabled_count = sum(1 for r in rows.values() if r.enabled)
    logger.info(
        "association table %s loaded: %d rows, %d enabled", version, len(rows), enabled_count
    )

    return AssociationTable(version=version, rows=rows)


@lru_cache
def get_association_table(path_str: str) -> AssociationTable:
    """Cached per path. Read once per process, like the questionnaire."""
    return load_association_table(Path(path_str))


def resolve_associations(
    table: AssociationTable, signals: tuple[SignalFinding, ...]
) -> tuple[AssociationOutcome, ...]:
    """
    FR-AI-007 and FR-AI-008, applied to one scan's signals.

    Returns one outcome per signal, in the order given. Every outcome is either
    suppressed with a recorded reason or carries two or more names; there is no
    third state, and no outcome carries one name.

    The signal-count check is evaluated once for the whole set rather than per
    signal, because FR-AI-008 makes it a property of the scan: more than two
    concurrent findings make association unreliable for all of them, not for
    the third one onwards.
    """
    # Checked first, and outside the loop, because it applies to every signal
    # regardless of individual confidence.
    if len(signals) > MAX_SIGNALS_FOR_ASSOCIATION:
        return tuple(
            AssociationOutcome(
                signal=finding.signal,
                suppression_reason=SuppressionReason.MULTIPLE_SIGNALS,
            )
            for finding in signals
        )

    outcomes: list[AssociationOutcome] = []

    for finding in signals:
        # FR-AI-007: no association list below HIGH confidence. FR-AI-008 makes
        # this the first per-signal check, so that a table lookup does not even
        # happen for a finding that could not display names anyway.
        if finding.confidence is not Confidence.HIGH:
            outcomes.append(
                AssociationOutcome(
                    signal=finding.signal,
                    suppression_reason=SuppressionReason.CONFIDENCE_BELOW_HIGH,
                )
            )
            continue

        row = table.row_for(finding.signal)
        if row is None:
            outcomes.append(
                AssociationOutcome(
                    signal=finding.signal,
                    suppression_reason=SuppressionReason.NO_TABLE_ENTRY,
                )
            )
            continue

        if not row.enabled:
            # Distinguished from NO_TABLE_ENTRY on purpose. "There is no row"
            # and "there is a row we are not willing to show yet" are different
            # facts about the system, and only one of them is resolved by
            # writing a row.
            outcomes.append(
                AssociationOutcome(
                    signal=finding.signal,
                    suppression_reason=SuppressionReason.ENTRY_DISABLED,
                )
            )
            continue

        outcomes.append(
            AssociationOutcome(signal=finding.signal, associations=row.associations)
        )

    return tuple(outcomes)


def _coerce_signal(value: Any) -> ClinicalSignal | None:
    if not isinstance(value, str):
        return None
    try:
        return ClinicalSignal(value.strip().upper())
    except ValueError:
        return None
