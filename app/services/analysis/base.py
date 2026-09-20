"""
The single analysis interface required by CON-004.

CON-004 is the most consequential constraint in Section 2.5, and it is easy to
underestimate because it asks for nothing visible. It requires that every
analysis task be reachable through one interface, such that a task can move
between the internal inference service and a hosted provider without changes to
the triage stage, the rules engine, or the client.

That matters for a reason FR-AI-004's rationale states directly: routing is a
configuration decision, not an architectural one. A task moves to a trained
model when its evaluation supports the move, and back if it does not. An
interface that only one side can satisfy turns "back" into a rewrite, and a
rewrite is not something anyone does in response to a bad evaluation -- which
means the model stays deployed.

So the interface is narrow on purpose. A provider is handed a prepared image
and a task, and returns a validated Appendix G result. It is told nothing about
the user, the skin type, the safety flags, or what will be done with the answer.
That is not information hiding for tidiness: a provider that could see the
safety flags could be influenced by them, and FR-AI-006 requires clinical signal
identification to be capable only of causing a referral, never of suppressing
one.

## What a provider must not do

It must not write the image anywhere (CON-001, FR-CAM-004). `PreparedImage`
holds bytes in memory and has no path, no file handle, and no `save` method --
there is nothing to write with. Logging is the likelier accident, so the rule is
stated here as well as in each implementation: no provider logs a request body.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.core.enums import AnalysisBackend, AnalysisTask
from app.schemas.analysis import AnalysisResult, Discards


class ProviderUnavailable(RuntimeError):
    """
    The backend could not be reached, or answered in a way that is not an
    answer at all.

    Distinct from a schema failure. A schema failure means the provider replied
    and the reply was unusable (ANALYSIS_INVALID); this means there was no
    reply (PROVIDER_UNAVAILABLE). The user needs different advice for each --
    "try again" versus "try again shortly" -- and the router needs the
    difference to decide whether a fallback is worth attempting.
    """


@dataclass(frozen=True)
class PreparedImage:
    """
    One image, in memory, for the duration of one request.

    FR-CAM-003 has already resized this to a longest edge of 1024, encoded it
    as JPEG at quality 80, and stripped the EXIF block on the client. Nothing
    here re-encodes it: a second pass would cost latency against the
    NFR-PERF-001 budget and would add nothing the client did not already do.

    There is no filename and no path, and that absence is the point. DR-001
    says the payload must not exist as a persisted column anywhere; a type with
    no route to disk is a stronger guarantee than a rule about not using one.
    """

    data: bytes
    content_type: str = "image/jpeg"

    def __repr__(self) -> str:
        # Overridden so that an exception rendered into a log line cannot carry
        # a face photograph with it. FR-CAM-004 requires that a log entry
        # contain no image data, and a dataclass repr would print every byte.
        return f"PreparedImage(bytes={len(self.data)}, content_type={self.content_type!r})"


@dataclass(frozen=True)
class ProviderResult:
    """
    What one task returned, and who returned it.

    `backend` is recorded on the scan log under FR-AI-004, so any past result
    can be attributed to whatever served it. `model_id` and `model_version` are
    required by FR-AI-009 whenever the internal inference service served the
    task, and are None for a hosted provider, which has no artefact version to
    record.
    """

    response: AnalysisResult
    discards: Discards
    backend: AnalysisBackend
    task: AnalysisTask | None = None
    model_id: str | None = None
    model_version: str | None = None


class AnalysisProvider(ABC):
    """
    One analysis backend. Implementations live alongside this file.

    A provider declares which tasks it can serve rather than being asked to
    guess: the router needs to know before it calls, because FR-AI-004 requires
    an unavailable backend to fall back rather than fail, and a fallback
    discovered by catching an exception mid-scan is a fallback that has already
    cost the user the latency.
    """

    backend: AnalysisBackend

    @abstractmethod
    async def analyse(self, image: PreparedImage, task: AnalysisTask) -> ProviderResult:
        """
        Answer one task about one image.

        Raises `ProviderUnavailable` where no answer could be obtained. A reply
        that fails Appendix G validation is not raised here -- it is returned
        as a result the caller can reject, because the distinction between "no
        answer" and "a bad answer" is one the router acts on.
        """

    @abstractmethod
    def supports(self, task: AnalysisTask) -> bool:
        """Whether this provider can serve `task` right now."""

    async def health(self) -> bool:
        """Whether this provider is configured well enough to be called."""
        return True

    async def aclose(self) -> None:
        """Release any held connections. Called at application shutdown."""
        return None
