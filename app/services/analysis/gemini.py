"""
Gemini vision provider. IF-SW-003, DEP-001, FR-AI-001.

Two things here carry requirement weight beyond ordinary API plumbing.

## The prompt

Appendix G is explicit: prompts ask for observable characteristics, not
interpretations. "Are there areas visibly darker than the surrounding skin, and
where" — never "does this person have a pigmentation disorder".

A prompt that asks for an interpretation will get one, and the schema would
then be the only thing between that interpretation and the user. Two defences
are better than one, but the first one has to actually be trying.

## Retention

FR-CAM-004 states the Zero-Save Policy is not satisfied if the analysis
provider retains inputs. That is a console setting on Google's side, not
something this file can enforce, so `/readyz` asserts the flag at startup
instead. Deleting a file locally while a copy sits on someone else's server is
not zero-save.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

import httpx

from app.core.config import Settings
from app.core.enums import AnalysisBackend, AnalysisTask
from app.schemas.analysis import parse_provider_response
from app.services.analysis.base import (
    AnalysisProvider,
    PreparedImage,
    ProviderResult,
    ProviderUnavailable,
)

logger = logging.getLogger(__name__)

_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Temporary provider errors worth one or two more tries, and the waits between.
_TRANSIENT = frozenset({429, 500, 503})
_RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0)

# The whole call -- main model, fallbacks and retries -- finishes inside this,
# so the client's 30-second upload timeout never fires first: a user should
# see "try again", not a hung spinner.
_BUDGET_SECONDS = 27.0
# The main model's share when a fallback is configured. An overloaded model
# can hang for 40s before answering; capping it leaves the fallback time.
_PRIMARY_TIMEOUT = 15.0


# The response contract, restated to the provider. The Pydantic schema is the
# authority; this is the instruction that tries to make the model match it.
_SCHEMA_INSTRUCTION = """Return JSON only. No prose, no markdown fences.

{
  "imageUsable": boolean,
  "cosmeticConcerns": [{"concernId": <CONCERN>, "severity": "MILD"|"MODERATE"|"PRONOUNCED"}],
  "clinicalSignals": [{"signalId": <SIGNAL>, "observation": string, "confidence": "LOW"|"MODERATE"|"HIGH"}]
}

CONCERN is one of: ACNE, EXCESS_OIL, DRYNESS, DEHYDRATION, POST_ACNE_MARKS,
UNEVEN_TONE, ENLARGED_PORES, MILD_REDNESS, FINE_LINES.

SIGNAL is one of: PIGMENT_PATCHES, INFLAMED_PATCHES, SCALING_PLAQUES,
PERSISTENT_REDNESS, NODULAR_LESIONS, UNIFORM_PAPULES, BLISTERS, OPEN_WOUND,
INFECTION_SIGNS, IRREGULAR_LESION.

Use no other identifier. If nothing fits, return an empty array.

"observation" describes appearance only: what is visible, and where. It must
not name, suggest, or hint at any medical condition, and must not use phrases
such as "looks like", "consistent with", or "suggestive of". Write it as a
neutral description a photographer could verify.

Set "imageUsable" to false if the face is not clearly visible, the image is too
dark or blurred to assess, or the frame is obstructed."""


# What a clinical signal is NOT.
#
# Added after live testing, where three scans in a row were referred for
# "darker skin around the eyes". Dark circles fit "an area darker than the
# surrounding skin" exactly, and nearly everyone has them -- so nearly every
# user was told to see a doctor and never saw a routine. A screening layer that
# fires on almost everyone is not a safety feature: it is noise that trains
# people to ignore the one referral that mattered.
#
# The exclusions below are ordinary cosmetic or normal findings, and the SRS
# already has somewhere to put them (UNEVEN_TONE, POST_ACNE_MARKS). This
# narrows what is screened; it does not change what happens when something IS
# found. NEEDS CLINICAL REVIEW (OI-010): a practitioner should confirm that
# nothing worth referring hides in this list -- periorbital darkening can, more
# rarely, accompany conditions this app does not attempt to detect.
_NOT_CLINICAL_SIGNALS = (
    "\nDo NOT report any of the following as clinicalSignals. They are ordinary "
    "cosmetic characteristics or normal variation, and reporting them sends "
    "someone to a doctor for nothing:\n"
    "- shadowing or darker skin around, under, or on the eyelids (dark circles), "
    "however dark, and including when both eyes are affected\n"
    "- flat marks left where a blemish has healed\n"
    "- freckles, ordinary moles, or generally uneven skin tone\n"
    "- darkening or redness only where a spot or blemish is\n"
    "- shadows cast by lighting, hair, or the angle of the photograph\n"
    "Report these as cosmeticConcerns where they fit (UNEVEN_TONE, "
    "POST_ACNE_MARKS), or not at all.\n\n"
)


_TASK_PROMPTS: dict[AnalysisTask, str] = {
    # OBJ-002 asks for a single analysis call per scan. This prompt carries
    # both halves of Appendix G so that one call answers both questions; the
    # split prompts below exist for the routing shapes Appendix I describes,
    # where a trained model takes one task and the provider keeps the other.
    AnalysisTask.COMBINED: (
        "Describe what is visible on the skin in this photograph.\n\n"
        "First, cosmetic characteristics. Are there visible blemishes or "
        "raised spots, and where? Areas with visible shine or surface oil? "
        "Areas that appear flaky, rough, or tight? Flat marks left where a "
        "blemish has healed? Is skin tone visibly uneven across areas? Are "
        "pore openings visibly enlarged? Is there mild diffuse pinkness? Are "
        "there fine surface lines?\n\n"
        "Second, and separately, report any of the following if visible:\n"
        "- a discrete patch clearly darker than the skin around it, with a "
        "defined edge, in one place rather than mirrored on both sides\n"
        "- raised inflamed areas with a defined edge\n"
        "- areas with visible scale or flaking over a raised patch\n"
        "- redness covering a broad area rather than isolated spots\n"
        "- firm lumps under the skin surface\n"
        "- many small bumps of similar size and even distribution\n"
        "- fluid-filled bumps\n"
        "- broken skin\n"
        "- crusting, oozing, or a visible yellow film\n"
        "- a mark with an uneven edge or more than one colour\n\n"
        + _NOT_CLINICAL_SIGNALS
        + "Report the first group as cosmeticConcerns and the second as "
        "clinicalSignals. Describe appearance and location only. Do not "
        "interpret and do not name a condition.\n\n" + _SCHEMA_INSTRUCTION
    ),
    AnalysisTask.COSMETIC_CONCERNS: (
        "Describe what is visible on the skin in this photograph.\n\n"
        "Consider: are there visible blemishes or raised spots, and where? Are "
        "there areas with visible shine or surface oil? Are there areas that "
        "appear flaky, rough, or tight? Are there flat marks left where a "
        "blemish has healed? Is skin tone visibly uneven across areas? Are pore "
        "openings visibly enlarged? Is there mild diffuse pinkness? Are there "
        "fine surface lines?\n\n"
        "Report only cosmeticConcerns. Return clinicalSignals as an empty "
        "array.\n\n" + _SCHEMA_INSTRUCTION
    ),
    AnalysisTask.ACNE_DETECTION: (
        "Are there visible blemishes or raised spots on the skin in this "
        "photograph, and roughly how extensive are they?\n\n"
        "Report only the ACNE concern if present. Return clinicalSignals as an "
        "empty array.\n\n" + _SCHEMA_INSTRUCTION
    ),
    AnalysisTask.CLINICAL_SIGNAL_SCREENING: (
        "Describe any of the following if visible in this photograph:\n"
        "- a discrete patch clearly darker than the skin around it, with a "
        "defined edge, in one place rather than mirrored on both sides\n"
        "- raised inflamed areas with a defined edge\n"
        "- areas with visible scale or flaking over a raised patch\n"
        "- redness covering a broad area rather than isolated spots\n"
        "- firm lumps under the skin surface\n"
        "- many small bumps of similar size and even distribution\n"
        "- fluid-filled bumps\n"
        "- broken skin\n"
        "- crusting, oozing, or a visible yellow film\n"
        "- a mark with an uneven edge or more than one colour\n\n"
        + _NOT_CLINICAL_SIGNALS
        + "Describe appearance and location only. Do not interpret. Do not name a "
        "condition. Report these as clinicalSignals and return cosmeticConcerns "
        "as an empty array.\n\n" + _SCHEMA_INSTRUCTION
    ),
}


class GeminiProvider(AnalysisProvider):
    backend = AnalysisBackend.HOSTED_PROVIDER

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(timeout=25.0)
        self._model = settings.gemini_model
        self._fallbacks = [
            m.strip() for m in settings.gemini_fallback_models.split(",")
            if m.strip() and m.strip() != settings.gemini_model
        ]

    def supports(self, task: AnalysisTask) -> bool:
        """
        Every task for which a prompt exists, provided a key is configured.

        The key check belongs here rather than in the router: an unconfigured
        provider is an unavailable one, and FR-AI-004 requires an unavailable
        backend to be routed around rather than called and caught.
        """
        return bool(self._settings.gemini_api_key) and task in _TASK_PROMPTS

    async def analyse(self, image: PreparedImage, task: AnalysisTask) -> ProviderResult:
        prompt = _TASK_PROMPTS.get(task)
        if prompt is None:
            raise ProviderUnavailable(f"no prompt defined for task {task.value}")

        body = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {
                            "inline_data": {
                                "mime_type": image.content_type,
                                "data": base64.b64encode(image.data).decode("ascii"),
                            }
                        },
                    ]
                }
            ],
            "generationConfig": {
                # As close to reproducible as the provider allows. The same
                # photograph giving different concerns on two runs would make
                # the routine feel arbitrary.
                "temperature": 0.0,
                "responseMimeType": "application/json",
            },
        }

        response, served_by = await self._post(body)
        if served_by != self._model:
            # The main model was overloaded, retired or silent. Worth seeing in
            # the logs: a fallback serving for days means the main model
            # setting should change.
            logger.warning("vision provider: %s unavailable, served by fallback %s",
                           self._model, served_by)

        raw = self._extract_json(response.json())
        parsed, discards = parse_provider_response(raw)

        if discards.any:
            # FR-AI-002 and FR-AI-005 both require discards to be recorded.
            logger.info("provider returned unusable identifiers: %s", discards)

        return ProviderResult(
            response=parsed, discards=discards, backend=self.backend, task=task
        )

    def _attempts(self) -> list[tuple[str, float]]:
        """
        (model, wait before trying) in order.

        With fallbacks configured, an overloaded main model is not retried: a
        503 that has lasted three days (October 2026) does not clear in the
        second a retry waits, and the user's time is better spent on a model
        that is answering. Only the last model gets the short retries. Without
        fallbacks, the main model is retried as before.
        """
        models = [self._model, *self._fallbacks]
        attempts = [(m, 0.0) for m in models]
        attempts += [(models[-1], delay) for delay in _RETRY_DELAYS]
        return attempts

    async def _post(self, body: dict[str, Any]) -> tuple[httpx.Response, str]:
        """
        Try each model until one answers. Returns the response and the model.

        Google answers 503 (overloaded) or 429 (rate limited) under load, and
        a retired model answers 404. Any of those moves on to the next model; a
        400 -- a malformed request -- fails at once, because every model would
        refuse it. Everything happens inside _BUDGET_SECONDS.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _BUDGET_SECONDS
        failures: list[str] = []
        retired: set[str] = set()       # answered 404: retrying cannot help
        for model, delay in self._attempts():
            if model in retired:
                continue
            if delay:
                await asyncio.sleep(delay)
            remaining = deadline - loop.time()
            if remaining < 2.0:
                break
            timeout = min(remaining, _PRIMARY_TIMEOUT) if model == self._model and self._fallbacks else remaining
            try:
                response = await self._client.post(
                    _ENDPOINT.format(model=model),
                    headers={"x-goog-api-key": self._settings.gemini_api_key},
                    json=body,
                    timeout=timeout,
                )
            except httpx.HTTPError as exc:
                # Logged by type only: the request body holds the base64 image,
                # and a face photograph in a log is what FR-CAM-004 forbids.
                failures.append(f"{model}: {type(exc).__name__}")
                continue
            if response.status_code < 400:
                return response, model
            failures.append(f"{model}: HTTP {response.status_code}")
            if response.status_code == 404:
                retired.add(model)
            elif response.status_code not in _TRANSIENT:
                break

        logger.warning("vision provider call failed: %s", "; ".join(failures))
        raise ProviderUnavailable("; ".join(failures) or "no attempt made")

    @staticmethod
    def _extract_json(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            text = payload["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderUnavailable("unexpected provider envelope") from exc

        text = text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Not ProviderUnavailable: the provider answered, and the answer
            # was unusable. That is ANALYSIS_INVALID territory — returning an
            # empty dict lets the schema layer make that call rather than
            # guessing here.
            logger.warning("provider returned non-JSON content")
            return {}

    async def health(self) -> bool:
        return bool(self._settings.gemini_api_key)

    async def aclose(self) -> None:
        await self._client.aclose()