"""
Scan endpoint, end to end. FR-AI-*, FR-TRI-*, FR-SUB-003.

Every request goes through the real router, dependencies, pipeline, schema
validation, association table, and error handlers. Only the vision provider is
replaced -- by a stub that returns a fixed Appendix G payload -- so a failure
here means the application is wrong, not the harness.

The stub's payloads are raw dictionaries, run through the same
`parse_provider_response` the real provider uses. Handing the pipeline
pre-validated objects would skip the layer most of these tests exist to check.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from app.api.deps import get_scan_pipeline
from app.clinical.copy import strings
from app.clinical.copy.observations import OBSERVATIONS
from app.clinical.copy.prohibited_terms import find_condition_names
from app.core.enums import AnalysisBackend, AnalysisTask, ClinicalSignal
from app.db.models import ScanLog, User
from app.main import app as fastapi_app
from app.schemas.analysis import parse_provider_response
from app.services.analysis.base import (
    AnalysisProvider,
    PreparedImage,
    ProviderResult,
    ProviderUnavailable,
)
from app.services.analysis.router import AnalysisRouter
from app.services.matrix_loader import MatrixStore
from app.services.routine_service import RoutineService
from app.services.scan_pipeline import ScanPipeline
from tests.conftest import ANSWERS_OILY, TEST_CONSENT_VERSION, auth

JPEG = b"\xff\xd8\xff\xe0" + b"not-really-a-photo" * 8


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class StubProvider(AnalysisProvider):
    """Returns one fixed payload, or raises. Counts its calls."""

    def __init__(
        self,
        raw: Any = None,
        *,
        fail: bool = False,
        backend: AnalysisBackend = AnalysisBackend.HOSTED_PROVIDER,
        model: tuple[str, str] | None = None,
    ) -> None:
        self.raw = raw
        self.fail = fail
        self.backend = backend
        self.model = model
        self.calls: list[AnalysisTask] = []
        self.images: list[PreparedImage] = []

    def supports(self, task: AnalysisTask) -> bool:
        return True

    async def analyse(self, image: PreparedImage, task: AnalysisTask) -> ProviderResult:
        self.calls.append(task)
        self.images.append(image)
        if self.fail:
            raise ProviderUnavailable("stubbed outage")
        parsed, discards = parse_provider_response(self.raw)
        return ProviderResult(
            response=parsed,
            discards=discards,
            backend=self.backend,
            task=task,
            model_id=self.model[0] if self.model else None,
            model_version=self.model[1] if self.model else None,
        )


class RoutineSpy(RoutineService):
    """The real routine service, counting how often it is asked for a routine."""

    def __init__(self, settings) -> None:
        super().__init__(MatrixStore(settings))
        self.calls = 0

    async def build(self, session, user, analysis):
        self.calls += 1
        return await super().build(session, user, analysis)


def use_pipeline(settings, provider, *, routine=None, internal=None) -> ScanPipeline:
    router = AnalysisRouter(settings, hosted=provider, internal=internal)
    pipeline = ScanPipeline(settings, router, routines=routine)
    fastapi_app.dependency_overrides[get_scan_pipeline] = lambda: pipeline
    return pipeline


def clear(concerns=None) -> dict:
    return {
        "imageUsable": True,
        "cosmeticConcerns": concerns
        if concerns is not None
        else [{"concernId": "ACNE", "severity": "MODERATE"}],
        "clinicalSignals": [],
    }


def with_signal(signal="INFLAMED_PATCHES", observation=None, confidence="HIGH") -> dict:
    return {
        "imageUsable": True,
        "cosmeticConcerns": [{"concernId": "DRYNESS", "severity": "MILD"}],
        "clinicalSignals": [
            {
                "signalId": signal,
                "observation": observation or "A raised red area on the left cheek.",
                "confidence": confidence,
            }
        ],
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def onboard(client, subject="scanner", *, referral=False, pregnant=False) -> dict:
    headers = auth(subject)
    r = await client.post("/v1/auth/session", headers=headers)
    assert r.status_code == 200, r.text
    r = await client.post(
        "/v1/me/date-of-birth",
        headers=headers,
        json={"dateOfBirth": "1995-06-15", "confirmed": True},
    )
    assert r.status_code == 200, r.text
    r = await client.post(
        "/v1/me/safety-answers",
        headers=headers,
        json={
            "pregnantOrBreastfeeding": pregnant,
            "prescriptionAcneTreatment": False,
            "openWoundsOrChangingMole": referral,
            "diagnosedEczemaPsoriasisRosacea": False,
        },
    )
    assert r.status_code == 200, r.text
    r = await client.post("/v1/me/skin-type", headers=headers, json={"answers": ANSWERS_OILY})
    assert r.status_code == 200, r.text
    r = await client.post(
        "/v1/me/consent", headers=headers, json={"acknowledgedVersion": TEST_CONSENT_VERSION}
    )
    assert r.status_code == 200, r.text
    return headers


async def scan(client, headers, key: str | None = None):
    extra = {"Idempotency-Key": key} if key else {}
    return await client.post(
        "/v1/scans/",
        headers={**headers, **extra},
        files={"image": ("scan.jpg", JPEG, "image/jpeg")},
    )


async def logs(session_factory, subject="scanner") -> list[ScanLog]:
    async with session_factory() as db:
        result = await db.execute(
            select(ScanLog).where(ScanLog.user_auth_id == subject).order_by(ScanLog.created_at)
        )
        return list(result.scalars())


async def remaining(session_factory, subject="scanner") -> int:
    async with session_factory() as db:
        return (await db.get(User, subject)).scans_remaining


# ---------------------------------------------------------------------------
# Referral on clinical signal -- FR-TRI-002, FR-TRI-003, FR-TRI-004
# ---------------------------------------------------------------------------


async def test_clinical_signal_produces_a_referral(client, settings, session_factory):
    use_pipeline(settings, StubProvider(with_signal()))
    headers = await onboard(client)

    r = await scan(client, headers)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "REFERRAL"
    assert body["referral"]["kind"] == "OBSERVED"
    assert body["referral"]["signals"][0]["observation"] == "A raised red area on the left cheek."
    assert "routine" not in body
    assert "concerns" not in body


async def test_referral_does_not_consume_quota(client, settings, session_factory):
    """FR-TRI-004. Never charged for being told to see a doctor."""
    use_pipeline(settings, StubProvider(with_signal()))
    headers = await onboard(client)

    await scan(client, headers)

    assert await remaining(session_factory) == 1


async def test_rules_engine_is_never_invoked_on_referral(client, settings):
    """FR-AI-006. A signal can only cause a referral, never a routine."""
    spy = RoutineSpy(settings)
    use_pipeline(settings, StubProvider(with_signal()), routine=spy)
    headers = await onboard(client)

    r = await scan(client, headers)

    assert r.json()["outcome"] == "REFERRAL"
    assert spy.calls == 0


async def test_referral_response_never_carries_a_signal_identifier(client, settings):
    """SRS 4.2: identifiers are internal and never rendered to the user."""
    use_pipeline(settings, StubProvider(with_signal("PIGMENT_PATCHES")))
    headers = await onboard(client)

    r = await scan(client, headers)

    assert "PIGMENT_PATCHES" not in r.text
    assert "signalId" not in r.text


async def test_referral_is_logged_with_triggering_signals(client, settings, session_factory):
    """FR-TRI-002: the scan log records REFERRAL and the signal identifiers."""
    use_pipeline(settings, StubProvider(with_signal("BLISTERS", confidence="MODERATE")))
    headers = await onboard(client)

    await scan(client, headers)

    [log] = await logs(session_factory)
    assert log.outcome.value == "REFERRAL"
    assert [s["signalId"] for s in log.clinical_signals] == ["BLISTERS"]
    assert log.quota_decremented is False
    assert log.analysis_backend is AnalysisBackend.HOSTED_PROVIDER


# ---------------------------------------------------------------------------
# Associations -- FR-AI-007, FR-AI-008
# ---------------------------------------------------------------------------


async def test_no_condition_name_reaches_the_user_in_release_one(client, settings, session_factory):
    """
    Every Appendix H row ships disabled. Even a HIGH-confidence signal with a
    table row must display the observation alone.
    """
    use_pipeline(settings, StubProvider(with_signal("SCALING_PLAQUES", confidence="HIGH")))
    headers = await onboard(client)

    r = await scan(client, headers)

    signal = r.json()["referral"]["signals"][0]
    assert "associations" not in signal
    assert find_condition_names(r.text) == []

    [log] = await logs(session_factory)
    assert log.displayed_associations is None
    assert log.suppression_reasons == [{"signalId": "SCALING_PLAQUES", "reason": "ENTRY_DISABLED"}]


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (with_signal("INFLAMED_PATCHES", confidence="MODERATE"), "CONFIDENCE_BELOW_HIGH"),
        (with_signal("IRREGULAR_LESION", confidence="HIGH"), "NO_TABLE_ENTRY"),
        (
            {
                "imageUsable": True,
                "cosmeticConcerns": [],
                "clinicalSignals": [
                    {"signalId": s, "observation": "x y z", "confidence": "HIGH"}
                    for s in ("BLISTERS", "OPEN_WOUND", "INFLAMED_PATCHES")
                ],
            },
            "MULTIPLE_SIGNALS",
        ),
    ],
)
async def test_suppression_reason_is_recorded(client, settings, session_factory, payload, reason):
    """FR-AI-008: given suppression, the scan log records why."""
    use_pipeline(settings, StubProvider(payload))
    headers = await onboard(client)

    await scan(client, headers)

    [log] = await logs(session_factory)
    assert {s["reason"] for s in log.suppression_reasons} == {reason}


# ---------------------------------------------------------------------------
# Observation validation -- DR-008
# ---------------------------------------------------------------------------


async def test_observation_naming_a_condition_is_replaced(client, settings, session_factory):
    use_pipeline(
        settings,
        StubProvider(with_signal("PIGMENT_PATCHES", observation="Patches that look like melasma.")),
    )
    headers = await onboard(client)

    r = await scan(client, headers)

    shown = r.json()["referral"]["signals"][0]["observation"]
    assert shown == OBSERVATIONS[ClinicalSignal.PIGMENT_PATCHES]
    assert "melasma" not in r.text.lower()

    [log] = await logs(session_factory)
    # DR-008: validated before storage, not only before display.
    assert "melasma" not in str(log.clinical_signals).lower()
    assert log.clinical_signals[0]["observationSource"] == "CURATED"
    assert log.discards["substitutedObservations"][0]["terms"] == ["look like", "melasma"]


async def test_a_bad_observation_never_costs_the_referral(client, settings):
    """
    The reason for substituting rather than discarding: dropping the signal
    would drop the referral, which FR-AI-006 exists to prevent.
    """
    use_pipeline(settings, StubProvider(with_signal("OPEN_WOUND", observation="")))
    headers = await onboard(client)

    r = await scan(client, headers)

    assert r.json()["outcome"] == "REFERRAL"


# ---------------------------------------------------------------------------
# Consultation summary -- FR-TRI-005
# ---------------------------------------------------------------------------


async def test_summary_has_observation_date_answers_and_disclaimer(client, settings):
    use_pipeline(settings, StubProvider(with_signal()))
    headers = await onboard(client, pregnant=True)

    r = await scan(client, headers)

    summary = r.json()["referral"]["summary"]
    assert "A raised red area on the left cheek." in summary
    assert f"{strings.SUMMARY_DATE_LABEL}: 20" in summary
    assert f"{strings.SUMMARY_ANSWER_LABELS['SQ1']}: Yes" in summary
    assert f"{strings.SUMMARY_ANSWER_LABELS['SQ4']}: No" in summary
    assert strings.SUMMARY_NOT_A_DIAGNOSIS in summary


async def test_summary_names_no_condition_despite_the_safety_questions_doing_so(client, settings):
    """
    Safety question 4 names eczema, psoriasis and rosacea. Echoing it verbatim
    would put condition names into a summary FR-TRI-005 says must have none.
    """
    use_pipeline(settings, StubProvider(with_signal()))
    headers = await onboard(client)

    r = await scan(client, headers)

    assert find_condition_names(r.json()["referral"]["summary"]) == []


# ---------------------------------------------------------------------------
# Declared referral -- FR-TRI-001, UC-003
# ---------------------------------------------------------------------------


async def test_flagged_user_is_refused_before_the_image_is_sent(client, settings):
    provider = StubProvider(clear())
    use_pipeline(settings, provider)
    headers = await onboard(client, referral=True)

    r = await scan(client, headers)

    assert r.status_code == 409
    assert r.json()["errorCode"] == "REFERRAL_REQUIRED"
    assert provider.calls == []


async def test_flagged_user_gets_a_declared_referral_without_a_photo(client, settings):
    use_pipeline(settings, StubProvider(clear()))
    headers = await onboard(client, referral=True)

    r = await client.get("/v1/scans/referral", headers=headers)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "DECLARED"
    assert body["signals"] == []
    assert strings.SUMMARY_NO_PHOTO in body["summary"]
    assert f"{strings.SUMMARY_ANSWER_LABELS['SQ3']}: Yes" in body["summary"]


async def test_unflagged_user_has_no_declared_referral(client, settings):
    use_pipeline(settings, StubProvider(clear()))
    headers = await onboard(client)

    r = await client.get("/v1/scans/referral", headers=headers)

    assert r.status_code == 404
    assert r.json()["errorCode"] == "NOT_REFERRED"


# ---------------------------------------------------------------------------
# Unusable and invalid -- FR-AI-001, FR-AI-002, FR-AI-003
# ---------------------------------------------------------------------------


async def test_unusable_image_asks_for_a_retake_and_keeps_quota(client, settings, session_factory):
    use_pipeline(settings, StubProvider({"imageUsable": False}))
    headers = await onboard(client)

    r = await scan(client, headers)

    assert r.status_code == 422
    assert r.json()["errorCode"] == "IMAGE_UNUSABLE"
    assert await remaining(session_factory) == 1
    # Committed despite the error response -- the audit row must survive the
    # request's rollback.
    [log] = await logs(session_factory)
    assert log.outcome.value == "UNUSABLE"


async def test_all_concerns_discarded_is_unusable(client, settings, session_factory):
    """FR-AI-002: every concern discarded means unusable, not an empty routine."""
    use_pipeline(
        settings,
        StubProvider(clear([{"concernId": "ACNE_SCARS", "severity": "MILD"}])),
        routine=RoutineSpy(settings),
    )
    headers = await onboard(client)

    r = await scan(client, headers)

    assert r.json()["errorCode"] == "IMAGE_UNUSABLE"
    [log] = await logs(session_factory)
    assert log.discards["unknownConcerns"] == ["ACNE_SCARS"]


async def test_malformed_response_is_analysis_invalid(client, settings, session_factory):
    use_pipeline(settings, StubProvider({"cosmeticConcerns": "nope"}))
    headers = await onboard(client)

    r = await scan(client, headers)

    assert r.status_code == 502
    assert r.json()["errorCode"] == "ANALYSIS_INVALID"
    assert await remaining(session_factory) == 1
    [log] = await logs(session_factory)
    assert log.outcome.value == "ERROR"


async def test_provider_outage_is_provider_unavailable(client, settings, session_factory):
    use_pipeline(settings, StubProvider(fail=True))
    headers = await onboard(client)

    r = await scan(client, headers)

    assert r.status_code == 503
    assert r.json()["errorCode"] == "PROVIDER_UNAVAILABLE"
    assert await remaining(session_factory) == 1


async def test_error_body_leaks_no_internal_detail(client, settings):
    """IF-COMM-003."""
    use_pipeline(settings, StubProvider({"cosmeticConcerns": "nope"}))
    headers = await onboard(client)

    r = await scan(client, headers)

    assert set(r.json()) == {"errorCode", "message"}
    assert "imageUsable" not in r.text


# ---------------------------------------------------------------------------
# Cleared triage -- the Feature 4 seam, and quota -- FR-SUB-003
# ---------------------------------------------------------------------------


async def test_cleared_scan_without_rules_engine_is_an_honest_error(
    client, settings, session_factory
):
    use_pipeline(settings, StubProvider(clear()))
    headers = await onboard(client)

    r = await scan(client, headers)

    assert r.status_code == 503
    assert await remaining(session_factory) == 1
    [log] = await logs(session_factory)
    assert log.outcome.value == "ERROR"
    assert log.discards["pendingStage"] == "RULES_ENGINE"
    assert log.concerns == [{"concernId": "ACNE", "severity": "MODERATE"}]


async def test_routine_decrements_exactly_once(client, settings, session_factory):
    use_pipeline(settings, StubProvider(clear()), routine=RoutineSpy(settings))
    headers = await onboard(client)

    r = await scan(client, headers)

    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "ROUTINE"
    assert r.json()["scansRemaining"] == 0
    assert await remaining(session_factory) == 0


async def test_retry_with_same_key_decrements_at_most_once(client, settings, session_factory):
    """
    FR-SUB-003. Also the regression for replay-before-eligibility: the retry
    must return the result, not QUOTA_EXCEEDED, even though the first request
    used the last scan.
    """
    provider = StubProvider(clear())
    use_pipeline(settings, provider, routine=RoutineSpy(settings))
    headers = await onboard(client)

    first = await scan(client, headers, key="retry-1")
    second = await scan(client, headers, key="retry-1")

    assert first.status_code == 200 and second.status_code == 200, second.text
    assert first.json()["scanId"] == second.json()["scanId"]
    assert len(provider.calls) == 1
    assert await remaining(session_factory) == 0


async def test_second_scan_without_key_is_refused(client, settings):
    """FR-SUB-001 and FR-SUB-002."""
    provider = StubProvider(clear())
    use_pipeline(settings, provider, routine=RoutineSpy(settings))
    headers = await onboard(client)

    await scan(client, headers)
    r = await scan(client, headers)

    assert r.status_code == 403
    assert r.json()["errorCode"] == "QUOTA_EXCEEDED"
    assert len(provider.calls) == 1


async def test_referral_leaves_a_later_routine_scan_possible(client, settings, session_factory):
    """FR-TRI-004 end to end: the allowance is still usable after a referral."""
    provider = StubProvider(with_signal())
    use_pipeline(settings, provider, routine=RoutineSpy(settings))
    headers = await onboard(client)

    await scan(client, headers)
    provider.raw = clear()
    r = await scan(client, headers)

    assert r.json()["outcome"] == "ROUTINE"


# ---------------------------------------------------------------------------
# Routing -- FR-AI-004, FR-AI-009, FR-AI-010, OBJ-002
# ---------------------------------------------------------------------------


async def test_baseline_is_a_single_provider_call(client, settings):
    """OBJ-002: a single analysis call per scan."""
    provider = StubProvider(with_signal())
    use_pipeline(settings, provider)
    headers = await onboard(client)

    await scan(client, headers)

    assert provider.calls == [AnalysisTask.COMBINED]


async def test_task_assigned_to_missing_internal_model_falls_back(
    client, settings, session_factory
):
    """
    FR-AI-004 and FR-AI-010. Reassigning a task in configuration changes the
    log, not the outcome -- this is the same referral test as above, unchanged.
    """
    settings.internal_model_tasks = "COMBINED"
    use_pipeline(settings, StubProvider(with_signal()))
    headers = await onboard(client)

    r = await scan(client, headers)

    assert r.json()["outcome"] == "REFERRAL"
    [log] = await logs(session_factory)
    assert log.routing[0]["backend"] == "HOSTED_PROVIDER"
    assert "no internal inference service" in log.routing[0]["fallbackReason"]


async def test_internal_model_records_its_version(client, settings, session_factory):
    """FR-AI-009."""
    settings.analysis_plan = "COSMETIC_CONCERNS,CLINICAL_SIGNAL_SCREENING"
    settings.internal_model_tasks = "CLINICAL_SIGNAL_SCREENING"
    hosted = StubProvider(clear([{"concernId": "DRYNESS", "severity": "MILD"}]))
    internal = StubProvider(
        {
            "imageUsable": True,
            "clinicalSignals": [
                {"signalId": "BLISTERS", "observation": "Small raised bumps.", "confidence": "LOW"}
            ],
        },
        backend=AnalysisBackend.INTERNAL_MODEL,
        model=("signal-screen", "0.3.1"),
    )
    use_pipeline(settings, hosted, internal=internal)
    headers = await onboard(client)

    r = await scan(client, headers)

    assert r.json()["outcome"] == "REFERRAL"
    assert hosted.calls == [AnalysisTask.COSMETIC_CONCERNS]
    assert internal.calls == [AnalysisTask.CLINICAL_SIGNAL_SCREENING]
    [log] = await logs(session_factory)
    assert log.model_versions == {"signal-screen": "0.3.1"}
    assert {r["backend"] for r in log.routing} == {"HOSTED_PROVIDER", "INTERNAL_MODEL"}


async def test_split_plan_is_unusable_if_any_task_is(client, settings):
    """
    A clinical screening that could not read the photo must not let a routine
    through on the strength of the concerns task alone.
    """
    settings.analysis_plan = "COSMETIC_CONCERNS,CLINICAL_SIGNAL_SCREENING"
    settings.internal_model_tasks = "CLINICAL_SIGNAL_SCREENING"
    use_pipeline(
        settings,
        StubProvider(clear()),
        internal=StubProvider({"imageUsable": False}, backend=AnalysisBackend.INTERNAL_MODEL),
        routine=RoutineSpy(settings),
    )
    headers = await onboard(client)

    r = await scan(client, headers)

    assert r.json()["errorCode"] == "IMAGE_UNUSABLE"


# ---------------------------------------------------------------------------
# Transport -- IF-COMM-002
# ---------------------------------------------------------------------------


async def test_scan_requires_authentication(client, settings):
    provider = StubProvider(clear())
    use_pipeline(settings, provider)

    r = await client.post("/v1/scans/", files={"image": ("scan.jpg", JPEG, "image/jpeg")})

    assert r.status_code == 401
    assert provider.calls == []


async def test_non_image_upload_is_refused_before_analysis(client, settings):
    provider = StubProvider(clear())
    use_pipeline(settings, provider)
    headers = await onboard(client)

    r = await client.post(
        "/v1/scans/", headers=headers, files={"image": ("x.txt", b"hello", "text/plain")}
    )

    assert r.json()["errorCode"] == "IMAGE_UNUSABLE"
    assert provider.calls == []


# ---------------------------------------------------------------------------
# Base64 upload -- the fallback when multipart fails on the device
# ---------------------------------------------------------------------------


async def scan_base64(client, headers, key: str | None = None, image: bytes = JPEG):
    import base64 as _b64

    extra = {"Idempotency-Key": key} if key else {}
    return await client.post(
        "/v1/scans/base64",
        headers={**headers, **extra},
        json={"imageBase64": _b64.b64encode(image).decode(), "contentType": "image/jpeg"},
    )


async def test_base64_upload_takes_the_same_path(client, settings, session_factory):
    provider = StubProvider(with_signal())
    use_pipeline(settings, provider)
    headers = await onboard(client)

    r = await scan_base64(client, headers)

    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "REFERRAL"
    assert provider.images[0].data == JPEG  # the bytes survived the round trip


async def test_base64_upload_respects_eligibility(client, settings):
    use_pipeline(settings, StubProvider(clear()))
    headers = await onboard(client, referral=True)

    r = await scan_base64(client, headers)

    assert r.status_code == 409
    assert r.json()["errorCode"] == "REFERRAL_REQUIRED"


async def test_base64_upload_requires_authentication(client, settings):
    use_pipeline(settings, StubProvider(clear()))
    import base64 as _b64

    r = await client.post(
        "/v1/scans/base64",
        json={"imageBase64": _b64.b64encode(JPEG).decode()},
    )
    assert r.status_code == 401


async def test_corrupt_base64_is_refused(client, settings):
    use_pipeline(settings, StubProvider(clear()))
    headers = await onboard(client)

    r = await client.post(
        "/v1/scans/base64", headers=headers, json={"imageBase64": "not base64 !!"}
    )

    assert r.status_code == 422
    assert r.json()["errorCode"] == "IMAGE_UNUSABLE"


async def test_both_upload_shapes_share_the_idempotency_key(client, settings, session_factory):
    """A retry that switches shape must not scan twice."""
    from tests.test_routine import seed

    await seed(session_factory)
    provider = StubProvider(clear())
    use_pipeline(settings, provider, routine=RoutineSpy(settings))
    headers = await onboard(client)

    first = await scan(client, headers, key="shared-key")
    second = await scan_base64(client, headers, key="shared-key")

    assert first.status_code == 200 and second.status_code == 200, second.text
    assert first.json()["scanId"] == second.json()["scanId"]
    assert len(provider.calls) == 1
