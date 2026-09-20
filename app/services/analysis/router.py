"""
Analysis task routing. FR-AI-004, FR-AI-009, FR-AI-010.

FR-AI-004 requires that each analysis task be routed to either the internal
inference service or a hosted provider "according to server-side
configuration". This module is that configuration being read, and nothing else:
it makes no judgement about which backend is better for a task, because that
judgement belongs in an evaluation record, not in code.

## The deployment gate is the point of this file

FR-AI-010 is the requirement that actually bites, and its rationale says why:
"A trained model reaching users without a recorded evaluation is the failure
this constraint exists to prevent. Evaluation is a precondition of deployment,
not a follow-up task."

So the gate is structural. A provider is asked whether it supports a task
before it is called, and a provider that cannot be reached, is not configured,
or has no recorded evaluation for that task simply answers no. The router then
falls back to the hosted provider and records why. There is no code path that
calls an internal model on the strength of it merely existing.

## Release 1.0 has no internal provider registered

That is a scope decision recorded in ADR-015, not an oversight. SRS v1.3 brought
custom models in scope, but its own open issues undercut deploying one: OI-014
(datasets not acquired), OI-016 (no evaluation set representative of the target
population -- and without one, the per-skin-tone breakdown NFR-SAFE-008 requires
cannot be produced at all), OI-017 (eight of the nine cosmetic concerns have no
face-format labelled dataset). FR-AI-010 forbids deploying a model under exactly
those conditions, so hosted-only is the compliant configuration, not a shortcut
around the requirement.

What this file provides is the thing that makes the decision reversible. When an
evaluation exists, registering an internal provider and naming its tasks in
configuration is the whole change. FR-AI-004's acceptance criterion -- that all
triage and rules-engine test cases pass unmodified after a task is reassigned --
is a test in `tests/test_analysis_routing.py` rather than an aspiration.

## Splitting and merging

A plan of one task (COMBINED) is the baseline, because OBJ-002 asks for a single
analysis call per scan. A plan of several exists for the Appendix I assignment,
where acne detection and clinical signal screening go to trained models and the
remaining concerns stay with the provider. Results from several tasks are merged
back into one Appendix G result before anything downstream sees them, so the
triage stage cannot tell how many calls produced it -- which is CON-004 restated
as behaviour.
"""

from __future__ import annotations

import logging

from app.core.config import Settings
from app.core.enums import AnalysisBackend, AnalysisTask
from app.schemas.analysis import (
    AnalysisResult,
    ConcernFinding,
    Discards,
    SignalFinding,
)
from app.services.analysis.base import (
    AnalysisProvider,
    PreparedImage,
    ProviderResult,
    ProviderUnavailable,
)

logger = logging.getLogger(__name__)


# Ranks used when merging duplicate findings across tasks. Same rule the schema
# layer applies within one response: the stronger reading of the same finding
# wins, so that splitting a scan across two tasks can never quietly downgrade
# something a single call would have reported.
_SEVERITY_RANK = {"MILD": 0, "MODERATE": 1, "PRONOUNCED": 2}
_CONFIDENCE_RANK = {"LOW": 0, "MODERATE": 1, "HIGH": 2}


class RoutingDecision:
    """
    Which backend served one task, and whether that was the first choice.

    `fallback_reason` is None on the ordinary path. Where it is set, FR-AI-004
    requires the fallback to be recorded, and a reason string is the difference
    between knowing a model was skipped and knowing why.
    """

    __slots__ = ("task", "backend", "fallback_reason", "model_id", "model_version")

    def __init__(
        self,
        task: AnalysisTask,
        backend: AnalysisBackend,
        fallback_reason: str | None = None,
        model_id: str | None = None,
        model_version: str | None = None,
    ) -> None:
        self.task = task
        self.backend = backend
        self.fallback_reason = fallback_reason
        self.model_id = model_id
        self.model_version = model_version

    def as_log_entry(self) -> dict[str, str | None]:
        entry: dict[str, str | None] = {
            "task": self.task.value,
            "backend": self.backend.value,
        }
        if self.fallback_reason:
            entry["fallbackReason"] = self.fallback_reason
        # FR-AI-009. Present only where the internal service served the task;
        # a hosted provider has no artefact version to attribute a result to.
        if self.model_id:
            entry["modelId"] = self.model_id
            entry["modelVersion"] = self.model_version
        return entry


class RoutedAnalysis:
    """One merged Appendix G result, plus the routing record behind it."""

    __slots__ = ("result", "discards", "decisions")

    def __init__(
        self,
        result: AnalysisResult,
        discards: Discards,
        decisions: list[RoutingDecision],
    ) -> None:
        self.result = result
        self.discards = discards
        self.decisions = decisions

    @property
    def backends_used(self) -> set[AnalysisBackend]:
        return {d.backend for d in self.decisions}

    def routing_log(self) -> list[dict[str, str | None]]:
        return [d.as_log_entry() for d in self.decisions]

    def model_versions(self) -> dict[str, str] | None:
        """FR-AI-009. None rather than an empty object when nothing applies."""
        versions = {
            d.model_id: d.model_version
            for d in self.decisions
            if d.model_id and d.model_version
        }
        return versions or None


class AnalysisRouter:
    """
    Reads the plan from configuration, runs it, merges the answers.

    Providers are injected rather than constructed here so that a test can
    supply a stub without reaching for a network patch, and so that the
    lifetime of an HTTP client belongs to the application rather than to a
    module import.
    """

    def __init__(
        self,
        settings: Settings,
        hosted: AnalysisProvider,
        internal: AnalysisProvider | None = None,
    ) -> None:
        self._settings = settings
        self._hosted = hosted
        # None in release 1.0. See the module docstring and ADR-015.
        self._internal = internal

    # -- plan ------------------------------------------------------------

    def plan(self) -> list[AnalysisTask]:
        """
        The tasks this scan will run, in order.

        An unrecognised task name in configuration is dropped with a warning
        rather than raising. A typo in an environment variable should not take
        the scan endpoint down, and dropping back to COMBINED yields a complete
        Appendix G answer -- which is the safe direction, since the alternative
        is a scan that silently skips clinical signal screening.
        """
        names = [n.strip().upper() for n in self._settings.analysis_plan.split(",") if n.strip()]

        tasks: list[AnalysisTask] = []
        for name in names:
            try:
                tasks.append(AnalysisTask(name))
            except ValueError:
                logger.warning("analysis plan names an unknown task %r; ignoring", name)

        if not tasks:
            logger.warning("analysis plan is empty or unusable; falling back to COMBINED")
            return [AnalysisTask.COMBINED]

        return tasks

    def _preferred_backend(self, task: AnalysisTask) -> AnalysisBackend:
        """What configuration asks for, before availability is considered."""
        assigned = {
            n.strip().upper()
            for n in self._settings.internal_model_tasks.split(",")
            if n.strip()
        }
        return (
            AnalysisBackend.INTERNAL_MODEL
            if task.value in assigned
            else AnalysisBackend.HOSTED_PROVIDER
        )

    def _resolve(self, task: AnalysisTask) -> tuple[AnalysisProvider, RoutingDecision]:
        """
        Pick the provider for one task, applying the FR-AI-010 gate.

        Every path that does not end at the internal model records a reason.
        That is the difference between a fallback and a silent downgrade, and
        FR-AI-004 asks for the former.
        """
        preferred = self._preferred_backend(task)

        if preferred is AnalysisBackend.INTERNAL_MODEL:
            if self._internal is None:
                # Release 1.0. No internal inference service is registered, so
                # every task assigned to one falls back here.
                reason = "no internal inference service is registered"
            elif not self._internal.supports(task):
                # FR-AI-010. A provider answers no for an unloaded artefact and
                # for one whose recorded evaluation is missing or below the
                # NFR-SAFE-008 threshold. Both are the same answer here.
                reason = "internal model has no deployable artefact for this task"
            else:
                return self._internal, RoutingDecision(
                    task=task, backend=AnalysisBackend.INTERNAL_MODEL
                )

            logger.info("task %s falling back to hosted provider: %s", task.value, reason)
            return self._hosted, RoutingDecision(
                task=task,
                backend=AnalysisBackend.HOSTED_PROVIDER,
                fallback_reason=reason,
            )

        return self._hosted, RoutingDecision(
            task=task, backend=AnalysisBackend.HOSTED_PROVIDER
        )

    # -- execution -------------------------------------------------------

    async def analyse(self, image: PreparedImage) -> RoutedAnalysis:
        """
        Run the configured plan and return one merged result.

        Raises `ProviderUnavailable` if no task could be served. Lets
        `AnalysisSchemaError` propagate: a reply that fails Appendix G
        validation is ANALYSIS_INVALID, and FR-AI-001 requires no result to be
        displayed and the quota to be left alone.
        """
        tasks = self.plan()

        results: list[ProviderResult] = []
        decisions: list[RoutingDecision] = []
        failures: list[str] = []

        for task in tasks:
            provider, decision = self._resolve(task)

            if not provider.supports(task):
                # The hosted provider is the last resort; if it cannot serve
                # the task either, there is nothing left to try for this one.
                failures.append(f"{task.value}: no provider supports this task")
                continue

            try:
                results.append(await provider.analyse(image, task))
            except ProviderUnavailable as exc:
                failures.append(f"{task.value}: {exc}")
                continue

            decisions.append(decision)

        if not results:
            # Every task failed. Raised rather than returned as an empty result
            # because an empty Appendix G response is indistinguishable from
            # "clear skin, no findings" -- and answering a failed clinical
            # signal screening with "nothing found" is the one mistake this
            # subsystem must not make.
            raise ProviderUnavailable("; ".join(failures) or "no tasks were run")

        if failures:
            # A partial plan still produces an answer, but an incomplete one.
            # Logged at warning because a scan missing its clinical signal task
            # has lost a safety layer, even though it can still generate a
            # routine.
            logger.warning("analysis plan partially failed: %s", "; ".join(failures))

        merged, discards = _merge(results)
        return RoutedAnalysis(result=merged, discards=discards, decisions=decisions)

    async def aclose(self) -> None:
        await self._hosted.aclose()
        if self._internal is not None:
            await self._internal.aclose()


def _merge(results: list[ProviderResult]) -> tuple[AnalysisResult, Discards]:
    """
    Fold several task results into one Appendix G result.

    `image_usable` is an AND across tasks: if any backend could not read the
    photograph, the photograph is not usable. The alternative -- trusting the
    task that managed an answer -- would let a scan proceed on an image that
    the clinical signal screening could not assess, producing a routine backed
    by only half the safety check.
    """
    if len(results) == 1:
        return results[0].response, results[0].discards

    image_usable = all(r.response.image_usable for r in results)

    concerns: dict[str, ConcernFinding] = {}
    for result in results:
        for finding in result.response.concerns:
            existing = concerns.get(finding.concern.value)
            if existing is None or (
                _SEVERITY_RANK[finding.severity.value]
                > _SEVERITY_RANK[existing.severity.value]
            ):
                concerns[finding.concern.value] = finding

    signals: dict[str, SignalFinding] = {}
    for result in results:
        for finding in result.response.signals:
            existing = signals.get(finding.signal.value)
            if existing is None or (
                _CONFIDENCE_RANK[finding.confidence.value]
                > _CONFIDENCE_RANK[existing.confidence.value]
            ):
                signals[finding.signal.value] = finding

    discards = Discards()
    for result in results:
        discards = discards.merged_with(result.discards)

    # Re-sorted into enumeration order after merging, for the same reason the
    # schema layer sorts: FR-REC-001 requires identical routines from identical
    # input, and dictionary insertion order here depends on which task answered
    # first.
    from app.core.enums import ClinicalSignal, Concern

    merged = AnalysisResult(
        image_usable=image_usable,
        concerns=tuple(
            concerns[c.value] for c in Concern if c.value in concerns
        ),
        signals=tuple(
            signals[s.value] for s in ClinicalSignal if s.value in signals
        ),
    )
    return merged, discards
