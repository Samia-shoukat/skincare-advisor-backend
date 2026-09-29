"""
The scan pipeline. SRS 4.2 processing order.

The SRS states the order and one property of it: "Every scan runs these stages
in sequence. A stage that stops the flow prevents all later stages."

    1. quota check
    2. referral flag check
    3. capture quality gate
    4. image analysis
    5. clinical signal check
    6. rules engine
    7. product matching
    8. quota decrement

This module is that list, executed. The stages appear here in that order, once
each, with nothing between them -- because the ordering is a safety property,
not an implementation detail. FR-AI-006 requires that clinical signal
identification can only cause a referral, never suppress one, and that guarantee
depends on stage 5 sitting between the analysis and the rules engine rather than
running alongside it.

Stages 1 and 2 are evaluated by `app.services.eligibility`, before the request
body is read, so that FR-TRI-001 holds in its literal form: a flagged user never
has an image captured or transmitted. By the time this module runs, they have
already passed.

## Stages 6 and 7

`RoutineService` runs the rules engine, re-checks the FR-REC-004 invariants on
the result, matches products, and saves the routine. If no service is supplied
(a misconfiguration, never the production wiring) a cleared scan ends in
ScanOutcome.ERROR with `pendingStage` recorded, and nobody is charged.

A routine that fails the invariant check is never shown and never charged for.
That can only happen if a matrix published at runtime (FR-REC-006) contains an
unsafe combination the test suite never saw; it is logged at ERROR.

## What never leaves this module

The image. It arrives as bytes, is passed to the router, and goes out of scope
when this function returns. Nothing writes it, nothing logs it, and no return
value contains it (CON-001, FR-CAM-004). The `PreparedImage` type has no path
and no save method, and its repr is overridden so that an exception rendered
into a log line cannot carry a photograph with it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.enums import AnalysisBackend, ScanOutcome
from app.db.models.scan_log import ScanLog
from app.db.models.user import User
from app.schemas.analysis import AnalysisSchemaError
from app.services.analysis.base import PreparedImage, ProviderUnavailable
from app.services.analysis.router import AnalysisRouter, RoutedAnalysis
from app.services.association_table import (
    AssociationOutcome,
    get_association_table,
    resolve_associations,
)
from app.services.quota import apply_decrement, should_decrement
from app.services.routine_service import RoutineService, routine_response

logger = logging.getLogger(__name__)


@dataclass
class ScanResult:
    """
    The pipeline's answer, before it becomes an HTTP response.

    Deliberately not a Pydantic model. This crosses one internal boundary, and
    keeping it a plain dataclass means the route layer decides what the client
    sees -- so a field added here for logging cannot leak into a response by
    being serialised along with everything else.
    """

    outcome: ScanOutcome
    scan_id: str

    # REFERRAL only. One entry per signal, each with its observation and,
    # where FR-AI-007 permits it, its association list.
    referral_signals: list[dict[str, Any]] = field(default_factory=list)

    # ROUTINE only.
    routine: dict[str, Any] | None = None

    concerns: list[dict[str, str]] = field(default_factory=list)
    scans_remaining: int = 0
    quota_decremented: bool = False

    # Set where the outcome is ERROR because a stage is not implemented. Kept
    # off the client response; it exists so an operator reading the log can
    # tell an unbuilt stage from a broken one.
    pending_stage: str | None = None


class ScanPipeline:
    """
    One scan, start to finish.

    Constructed per application rather than per request, because the analysis
    router holds an HTTP client whose connection pool should outlive a single
    scan.
    """

    def __init__(
        self,
        settings: Settings,
        router: AnalysisRouter,
        routines: RoutineService | None = None,
    ) -> None:
        self._settings = settings
        self._router = router
        # Stages 6 and 7. See the module docstring.
        self._routines = routines

    async def run(
        self,
        session: AsyncSession,
        user: User,
        image_bytes: bytes,
        content_type: str = "image/jpeg",
        idempotency_key: str | None = None,
    ) -> ScanResult:
        """
        Stages 3 through 8. Stages 1 and 2 ran before the body was read.

        Raises the `AppError` subclasses the route layer already knows how to
        render, rather than returning an error shape of its own. There is one
        error taxonomy in this application and it lives in `app.core.errors`.
        """
        from app.core.errors import (
            AnalysisInvalid,
            ImageUnusable,
            ProviderUnavailableError,
        )

        prepared = PreparedImage(data=image_bytes, content_type=content_type)

        # --- stage 4: image analysis --------------------------------------
        try:
            analysis = await self._router.analyse(prepared)
        except AnalysisSchemaError as exc:
            # FR-AI-001: no result is displayed, ANALYSIS_INVALID is returned,
            # and the quota is not decremented. The log row is written first so
            # that a provider drifting out of schema is visible as a rate rather
            # than as scattered exceptions.
            logger.warning("analysis response failed Appendix G validation: %s", exc)
            scan = await self._record(
                session, user, ScanOutcome.ERROR, analysis=None, pending_stage=None
            )
            raise AnalysisInvalid(detail={"scanId": scan.id, "reason": str(exc)})
        except ProviderUnavailable as exc:
            logger.warning("analysis unavailable: %s", exc)
            scan = await self._record(
                session, user, ScanOutcome.ERROR, analysis=None, pending_stage=None
            )
            raise ProviderUnavailableError(detail={"scanId": scan.id})

        # --- stage 4b: unusable image -------------------------------------
        # FR-AI-003. Ahead of the clinical signal check on purpose: an image the
        # analysis could not read cannot have been screened either, and treating
        # its empty signal list as "nothing found" would convert an unreadable
        # photograph into a clean bill of health.
        if not analysis.result.image_usable:
            scan = await self._record(session, user, ScanOutcome.UNUSABLE, analysis)
            raise ImageUnusable(detail={"scanId": scan.id})

        # FR-AI-002: where every returned concern was DISCARDED, the scan is
        # treated as unusable rather than producing an empty routine.
        #
        # Only discarded, not merely absent. A provider that looked and found
        # no concerns has given a valid answer, and that user still gets the
        # base routine (cleanse, moisturise, sunscreen). Treating "none found"
        # as unusable would send someone with clear skin round the retake loop
        # forever. A signal also counts as a usable finding -- that is a
        # referral, not a retake.
        all_discarded = bool(analysis.discards.unknown_concerns) and not analysis.result.concerns
        if all_discarded and not analysis.result.signals:
            logger.info("no usable findings survived validation; treating as unusable")
            scan = await self._record(session, user, ScanOutcome.UNUSABLE, analysis)
            raise ImageUnusable(detail={"scanId": scan.id})

        # --- stage 5: clinical signal check -------------------------------
        # FR-TRI-002 triggers on presence, not on severity or confidence.
        # FR-AI-006: this can only stop the flow. There is no branch below that
        # can re-enable routine generation once a signal is present, and there
        # is no branch above where a signal removes a referral raised elsewhere
        # -- `referral_flag` was handled before the image was read.
        if analysis.result.has_clinical_signal:
            return await self._refer(session, user, analysis)

        # --- stages 6 and 7: rules engine and product matching ------------
        if self._routines is None:
            scan = await self._record(
                session,
                user,
                ScanOutcome.ERROR,
                analysis,
                pending_stage="RULES_ENGINE",
            )
            logger.info(
                "scan %s cleared triage but no routine generator is registered", scan.id
            )
            raise ProviderUnavailableError(detail={"scanId": scan.id, "stage": "RULES_ENGINE"})

        built = await self._routines.build(session, user, analysis)
        if built.violations:
            scan = await self._record(
                session,
                user,
                ScanOutcome.ERROR,
                analysis,
                pending_stage="INVARIANT_CHECK",
                matrix_version=built.matrix_version,
            )
            raise ProviderUnavailableError(detail={"scanId": scan.id, "stage": "INVARIANT_CHECK"})

        # --- stage 8: quota decrement -------------------------------------
        # Last, and only here. FR-SUB-003 ties it to a routine that was
        # produced, so it sits after generation rather than after analysis.
        decremented = apply_decrement(user, quota_enforced=self._settings.quota_enforced)

        scan = await self._record(
            session,
            user,
            ScanOutcome.ROUTINE,
            analysis,
            idempotency_key=idempotency_key,
            quota_decremented=decremented,
            matrix_version=built.matrix_version,
        )
        row = await self._routines.save(session, user, scan, built)

        return ScanResult(
            outcome=ScanOutcome.ROUTINE,
            scan_id=scan.id,
            routine=routine_response(row),
            concerns=_concerns_payload(analysis),
            scans_remaining=user.scans_remaining,
            quota_decremented=decremented,
        )

    async def aclose(self) -> None:
        await self._router.aclose()

    async def _current_matrix_version(self) -> str:
        if self._routines is None:
            return self._settings.active_matrix_version
        try:
            return await self._routines.matrix_version()
        except Exception:  # an unloadable matrix must not stop a referral being logged
            logger.exception("could not resolve matrix version for scan log")
            return self._settings.active_matrix_version

    # ------------------------------------------------------------------
    # Referral
    # ------------------------------------------------------------------

    async def _refer(
        self, session: AsyncSession, user: User, analysis: RoutedAnalysis
    ) -> ScanResult:
        """
        FR-TRI-002, FR-TRI-003, FR-TRI-004, FR-AI-007, FR-AI-008.

        The rules engine is not invoked and no concern reaches the response.
        FR-AI-006's acceptance criterion is that no clinical signal value
        appears in the rules engine input; the stronger form implemented here
        is that the engine is never called at all, so there is no input to
        inspect.
        """
        table = get_association_table(self._settings.associations_path)
        outcomes = resolve_associations(table, analysis.result.signals)
        by_signal = {o.signal: o for o in outcomes}

        referral_signals: list[dict[str, Any]] = []
        for finding in analysis.result.signals:
            outcome = by_signal.get(finding.signal) or AssociationOutcome(finding.signal)
            entry: dict[str, Any] = {
                # FR-TRI-003: the observation is what the screen displays. The
                # identifier is internal and is not in this payload -- SRS 4.2
                # says identifiers "appear in logs and metrics and are never
                # rendered to the user", so the client is never given one to
                # render by accident.
                "observation": finding.observation,
            }
            # Present only when FR-AI-007 permits it. Absent, rather than an
            # empty array, so that a client rendering `associations?.length`
            # cannot show the fixed framing text around nothing.
            if outcome.associations:
                entry["associations"] = list(outcome.associations)
            referral_signals.append(entry)

        scan = await self._record(
            session,
            user,
            ScanOutcome.REFERRAL,
            analysis,
            association_outcomes=outcomes,
        )

        # FR-TRI-004. Stated as a comment rather than a line of code because
        # there is no decrement here to point at, and its absence is the
        # requirement being met.
        return ScanResult(
            outcome=ScanOutcome.REFERRAL,
            scan_id=scan.id,
            referral_signals=referral_signals,
            scans_remaining=user.scans_remaining,
            quota_decremented=False,
        )

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    async def _record(
        self,
        session: AsyncSession,
        user: User,
        outcome: ScanOutcome,
        analysis: RoutedAnalysis | None,
        *,
        association_outcomes: tuple[AssociationOutcome, ...] = (),
        idempotency_key: str | None = None,
        quota_decremented: bool = False,
        pending_stage: str | None = None,
        matrix_version: str | None = None,
    ) -> ScanLog:
        """
        Write the audit row. SRS 6.2, and the evidence for most of Section 4.5.

        Written on every path including the failures, because a scan that
        errored is the one most worth being able to count later. Flushed rather
        than committed: the request's transaction is owned by the session
        dependency, and committing here would break the guarantee that a
        decrement and its log row land together or not at all.
        """
        scan = ScanLog(
            user_auth_id=user.auth_id,
            outcome=outcome,
            # FR-REC-006: the version actually in force. Resolved from the
            # matrix store even on referral and error rows, so every scan can
            # be placed against the rules that were active.
            matrix_version=matrix_version or await self._current_matrix_version(),
            idempotency_key=idempotency_key,
            quota_decremented=quota_decremented,
        )

        if analysis is not None:
            # FR-AI-002 requires the concern set to be recorded; DR-008 requires
            # it to hold closed-vocabulary identifiers and no free text.
            scan.concerns = [
                {"concernId": f.concern.value, "severity": f.severity.value}
                for f in analysis.result.concerns
            ] or None

            # FR-TRI-002 requires the triggering signal identifiers on a
            # referral. The observation is stored alongside because FR-TRI-005's
            # consultation summary is reproduced from this row, and it has
            # already passed DR-008 validation upstream.
            scan.clinical_signals = [
                {
                    "signalId": f.signal.value,
                    "observation": f.observation,
                    "confidence": f.confidence.value,
                    "observationSource": f.observation_source.value,
                }
                for f in analysis.result.signals
            ] or None

            scan.routing = analysis.routing_log() or None
            scan.model_versions = analysis.model_versions()
            scan.discards = analysis.discards.as_metrics() or None

            backends = analysis.backends_used
            # A single backend is named; a split plan records the hosted
            # provider, because "partly internal" is not one of the two values
            # FR-AI-004 defines and the per-task detail is in `routing` anyway.
            scan.analysis_backend = (
                next(iter(backends))
                if len(backends) == 1
                else AnalysisBackend.HOSTED_PROVIDER
            )

        if association_outcomes:
            # NFR-SAFE-007. Every condition name the user was shown, with what
            # produced it. Empty on every row while the table is disabled, which
            # is itself the evidence that naming is off.
            shown = [
                {
                    "signalId": o.signal.value,
                    "associations": list(o.associations),
                }
                for o in association_outcomes
                if o.associations
            ]
            scan.displayed_associations = shown or None

            # FR-AI-008: "Given suppression occurs, then the scan log records
            # the suppression reason."
            suppressed = [
                {"signalId": o.signal.value, "reason": o.suppression_reason.value}
                for o in association_outcomes
                if o.suppression_reason is not None
            ]
            scan.suppression_reasons = suppressed or None

        if pending_stage:
            scan.discards = {**(scan.discards or {}), "pendingStage": pending_stage}

        session.add(scan)

        if outcome in (ScanOutcome.UNUSABLE, ScanOutcome.ERROR):
            # These paths end in a raised AppError, and the session dependency
            # rolls back on any exception -- which would discard exactly the
            # rows FR-AI-001 and FR-AI-003 most need kept. Committed now
            # instead. Safe because no other write is pending on these paths:
            # the decrement only happens on ROUTINE.
            await session.commit()
        else:
            await session.flush()

        logger.info(
            "scan %s outcome=%s concerns=%d signals=%d decremented=%s",
            scan.id,
            outcome.value,
            len(scan.concerns or []),
            len(scan.clinical_signals or []),
            quota_decremented,
        )
        return scan


def _concerns_payload(analysis: RoutedAnalysis) -> list[dict[str, str]]:
    return [
        {"concernId": f.concern.value, "severity": f.severity.value}
        for f in analysis.result.concerns
    ]


__all__ = ["ScanPipeline", "ScanResult", "should_decrement"]
