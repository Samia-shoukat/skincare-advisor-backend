"""
The internal skin-issue model. FR-AI-004, FR-AI-009, FR-AI-010, NFR-SAFE-008.

What these tests pin down:

  * the gate fails a model for every reason it should, names each one, and
    fails -- rather than skips -- a skin-tone group too small to measure;
  * the provider serves ACNE_DETECTION only, only past the gate, grades every
    finding MILD, and reports an unreadable photo as unusable rather than as
    an outage;
  * the router falls back to the hosted provider when the gate fails, and
    records the artefact version when it does not (FR-AI-009);
  * the development endpoint is invisible unless switched on, and never
    answers in production.

The model itself is faked except in one test, which loads the real artefact
when it is present and is skipped when it is not.
"""

from __future__ import annotations

import io

import pytest

from app.core.enums import AnalysisBackend, AnalysisTask, Concern, Severity
from app.schemas.analysis import AnalysisResult, Discards
from app.services.analysis import onnx_skin
from app.services.analysis.base import (
    AnalysisProvider,
    PreparedImage,
    ProviderResult,
    ProviderUnavailable,
)
from app.services.analysis.onnx_skin import (
    GateResult,
    IssuePrediction,
    OnnxSkinProvider,
    SkinIssueResult,
    UnreadableImage,
    build_internal_provider,
    evaluate_gate,
    resolve,
)
from app.services.analysis.router import AnalysisRouter
from tests.conftest import auth

PHOTO = PreparedImage(data=b"\xff\xd8\xff\xe0" + b"pretend-jpeg" * 8)


def passing_labels(**overrides) -> dict:
    labels = {
        "validation": {"f1_macro": 0.81},
        "per_class": {"acne": {"f1-score": 0.84}},
        "accuracy_by_skin_tone": {g: {"accuracy": 0.83, "n": 40} for g in ("I-II", "III-IV", "V-VI")},
        "export_check": {"onnx_max_abs_diff": 1.2e-6},
        "int8_check": {"agreement": 0.99},
        "placeholder_classes": [],
    }
    labels.update(overrides)
    return labels


# The first Colab run (labels.json, 2026-10-11), as recorded.
FIRST_COLAB_RUN = {
    "validation": {"f1_macro": 0.5235, "accuracy": 0.6627},
    "per_class": {"acne": {"f1-score": 0.6182}, "dark_spots": {"f1-score": 0.0}},
    "accuracy_by_skin_tone": {
        "I-II": {"accuracy": 0.7931, "n": 29},
        "III-IV": {"accuracy": 0.6061, "n": 33},
        "V-VI": {"accuracy": 0.4, "n": 15},
        "unknown": {"accuracy": 1.0, "n": 6},
    },
    "export_check": {"onnx_max_abs_diff": 1.19e-6},
    "placeholder_classes": [],
}


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_gate_passes_a_model_meeting_every_threshold(settings):
    gate = evaluate_gate(passing_labels(), settings, served_file="skin_disease_model.int8.onnx")
    assert gate == GateResult(passed=True, reasons=())


def test_gate_fails_the_first_colab_run_and_says_why(settings):
    gate = evaluate_gate(FIRST_COLAB_RUN, settings, served_file="skin_disease_model.int8.onnx")
    assert not gate.passed
    text = " | ".join(gate.reasons)
    assert "macro-F1 0.52" in text
    assert "skin tone V-VI" in text
    assert "gap is 39%" in text
    assert "int8" in text


def test_too_few_photos_for_a_skin_tone_fails_rather_than_being_skipped(settings):
    # Accurate on all three groups, but V-VI measured on 12 photos. Dropping
    # that group from the check would pass a model nobody measured on darker
    # skin (NFR-SAFE-008).
    tones = {g: {"accuracy": 0.9, "n": 40} for g in ("I-II", "III-IV")}
    tones["V-VI"] = {"accuracy": 0.9, "n": 12}
    gate = evaluate_gate(passing_labels(accuracy_by_skin_tone=tones), settings, served_file="m.onnx")
    assert not gate.passed
    assert any("only 12 validation photos for skin tone V-VI" in r for r in gate.reasons)


def test_a_model_with_no_recorded_evaluation_fails(settings):
    gate = evaluate_gate({}, settings, served_file="m.onnx")
    assert not gate.passed
    assert "no macro-F1 recorded" in gate.reasons


def test_the_int8_file_needs_its_own_recorded_agreement(settings):
    labels = passing_labels()
    del labels["int8_check"]
    assert evaluate_gate(labels, settings, served_file="skin_disease_model.onnx").passed
    assert not evaluate_gate(labels, settings, served_file="skin_disease_model.int8.onnx").passed

    low = passing_labels(int8_check={"agreement": 0.93})
    gate = evaluate_gate(low, settings, served_file="skin_disease_model.int8.onnx")
    assert any("agrees with the full model on 93.0%" in r for r in gate.reasons)


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------


class FakeModel:
    """Stands in for SkinIssueModel: a fixed acne probability, or a failure."""

    model_id = "skin-issues-onnx"
    model_version = "test-run/skin_disease_model.int8.onnx"

    def __init__(self, acne: float = 0.0, *, unreadable: bool = False, crash: bool = False):
        self.acne, self.unreadable, self.crash = acne, unreadable, crash

    def predict(self, data: bytes, *, face_crop: bool = True) -> SkinIssueResult:
        if self.unreadable:
            raise UnreadableImage("not an image")
        if self.crash:
            raise RuntimeError("session failed")
        rest = (1 - self.acne) / 3
        scores = {"acne": self.acne, "dark_spots": rest, "redness": rest, "normal": rest}
        predictions = sorted(
            (IssuePrediction(name, None, p) for name, p in scores.items()),
            key=lambda p: p.probability, reverse=True,
        )
        return SkinIssueResult(tuple(predictions), True, 0.5)


PASSED, FAILED = GateResult(True, ()), GateResult(False, ("macro-F1 0.52 is below 0.55",))


def test_serves_acne_detection_only_and_only_past_the_gate():
    passed = OnnxSkinProvider(FakeModel(), PASSED)
    assert passed.supports(AnalysisTask.ACNE_DETECTION)
    for task in (AnalysisTask.COMBINED, AnalysisTask.COSMETIC_CONCERNS,
                 AnalysisTask.CLINICAL_SIGNAL_SCREENING):
        assert not passed.supports(task), task
    assert not OnnxSkinProvider(FakeModel(), FAILED).supports(AnalysisTask.ACNE_DETECTION)


async def test_acne_above_threshold_is_a_mild_acne_finding():
    result = await OnnxSkinProvider(FakeModel(acne=0.91), PASSED).analyse(PHOTO, AnalysisTask.ACNE_DETECTION)
    assert result.response.image_usable
    assert [(f.concern, f.severity) for f in result.response.concerns] == [(Concern.ACNE, Severity.MILD)]
    assert not result.response.signals
    # FR-AI-009: which artefact served this.
    assert result.backend is AnalysisBackend.INTERNAL_MODEL
    assert (result.model_id, result.model_version) == (FakeModel.model_id, FakeModel.model_version)


async def test_acne_below_threshold_reports_no_concern():
    result = await OnnxSkinProvider(FakeModel(acne=0.31), PASSED).analyse(PHOTO, AnalysisTask.ACNE_DETECTION)
    assert result.response.image_usable
    assert result.response.concerns == ()


async def test_an_unreadable_photo_is_unusable_not_an_outage():
    provider = OnnxSkinProvider(FakeModel(unreadable=True), PASSED)
    result = await provider.analyse(PHOTO, AnalysisTask.ACNE_DETECTION)
    assert result.response.image_usable is False


async def test_a_model_failure_is_provider_unavailable_without_the_image():
    provider = OnnxSkinProvider(FakeModel(crash=True), PASSED)
    with pytest.raises(ProviderUnavailable) as raised:
        await provider.analyse(PHOTO, AnalysisTask.ACNE_DETECTION)
    assert "pretend-jpeg" not in str(raised.value)  # FR-CAM-004


async def test_refuses_to_answer_a_task_it_does_not_serve():
    with pytest.raises(ProviderUnavailable):
        await OnnxSkinProvider(FakeModel(), FAILED).analyse(PHOTO, AnalysisTask.ACNE_DETECTION)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


class Hosted(AnalysisProvider):
    backend = AnalysisBackend.HOSTED_PROVIDER

    def __init__(self):
        self.calls: list[AnalysisTask] = []

    def supports(self, task: AnalysisTask) -> bool:
        return True

    async def analyse(self, image: PreparedImage, task: AnalysisTask) -> ProviderResult:
        self.calls.append(task)
        return ProviderResult(AnalysisResult(image_usable=True), Discards(), self.backend, task)


def acne_routing(settings):
    return settings.model_copy(update={"analysis_plan": "ACNE_DETECTION",
                                       "internal_model_tasks": "ACNE_DETECTION"})


async def test_a_model_that_fails_the_gate_falls_back_and_says_why(settings):
    hosted = Hosted()
    router = AnalysisRouter(acne_routing(settings), hosted=hosted,
                            internal=OnnxSkinProvider(FakeModel(acne=0.9), FAILED))
    routed = await router.analyse(PHOTO)
    assert hosted.calls == [AnalysisTask.ACNE_DETECTION]
    [decision] = routed.decisions
    assert decision.backend is AnalysisBackend.HOSTED_PROVIDER
    assert decision.fallback_reason == "internal model has no deployable artefact for this task"


async def test_a_model_that_passes_serves_and_its_version_is_recorded(settings):
    hosted = Hosted()
    router = AnalysisRouter(acne_routing(settings), hosted=hosted,
                            internal=OnnxSkinProvider(FakeModel(acne=0.9), PASSED))
    routed = await router.analyse(PHOTO)
    assert hosted.calls == []
    assert routed.backends_used == {AnalysisBackend.INTERNAL_MODEL}
    assert routed.model_versions() == {FakeModel.model_id: FakeModel.model_version}
    assert [f.concern for f in routed.result.concerns] == [Concern.ACNE]


def test_nothing_is_loaded_when_no_task_is_assigned(settings, monkeypatch):
    def must_not_load(_settings):
        raise AssertionError("the model was loaded with no task assigned to it")

    monkeypatch.setattr(onnx_skin, "load_skin_model", must_not_load)
    assert build_internal_provider(settings) is None


def test_a_missing_artefact_falls_back_instead_of_failing(settings, tmp_path):
    missing = acne_routing(settings).model_copy(update={
        "internal_model_path": str(tmp_path / "absent.onnx"),
        "internal_model_labels_path": str(tmp_path / "absent.json"),
    })
    assert build_internal_provider(missing) is None


# ---------------------------------------------------------------------------
# The real artefact, when present
# ---------------------------------------------------------------------------


def test_the_real_artefact_predicts_a_distribution(settings):
    pytest.importorskip("onnxruntime")
    pytest.importorskip("cv2")
    Image = pytest.importorskip("PIL.Image")
    if not resolve(settings.internal_model_path).is_file():
        pytest.skip("no trained model in ml/vision/artifacts")

    model = onnx_skin.load_skin_model(settings)
    buffer = io.BytesIO()
    Image.new("RGB", (640, 480), (196, 150, 120)).save(buffer, format="JPEG")
    result = model.predict(buffer.getvalue())

    assert [p.label for p in result.predictions] == sorted(
        model.classes, key=lambda c: -result.probability(c))
    assert abs(sum(p.probability for p in result.predictions) - 1.0) < 1e-3
    with pytest.raises(UnreadableImage):
        model.predict(b"not an image at all")


# ---------------------------------------------------------------------------
# The development endpoint
# ---------------------------------------------------------------------------

ENDPOINT = "/v1/dev/skin-model/predict"


def upload(data: bytes = PHOTO.data) -> dict:
    return {"image": ("selfie.jpg", data, "image/jpeg")}


async def signed_in(client) -> dict:
    headers = auth("model-tester")
    assert (await client.post("/v1/auth/session", headers=headers)).status_code == 200
    return headers


async def test_endpoint_is_invisible_unless_switched_on(client, settings):
    headers = await signed_in(client)
    response = await client.post(ENDPOINT, headers=headers, files=upload())
    assert response.status_code == 404
    assert response.json()["errorCode"] == "NOT_FOUND"


async def test_endpoint_never_answers_in_production(client, settings):
    headers = await signed_in(client)
    settings.skin_model_debug_endpoint = True
    settings.environment = "production"
    assert (await client.post(ENDPOINT, headers=headers, files=upload())).status_code == 404


async def test_endpoint_requires_sign_in(client, settings):
    settings.skin_model_debug_endpoint = True
    assert (await client.post(ENDPOINT, files=upload())).status_code == 401


async def test_endpoint_returns_the_prediction_as_json(client, settings, monkeypatch):
    from app.api.v1.routes import skin_model as route

    class Loaded(FakeModel):
        labels = FIRST_COLAB_RUN
        model_path = resolve("ml/vision/artifacts/skin_disease_model.int8.onnx")

    monkeypatch.setattr(route, "load_skin_model", lambda _settings: Loaded(acne=0.8))
    headers = await signed_in(client)
    settings.skin_model_debug_endpoint = True

    response = await client.post(ENDPOINT, headers=headers, files=upload())
    assert response.status_code == 200
    body = response.json()
    assert body["topIssue"] == "acne"
    assert body["detected"] == ["acne"]
    assert body["issues"][0] == {"label": "acne", "concernId": None, "probability": 0.8, "detected": True}
    assert body["faceFound"] is True
    assert body["deploymentGate"]["passed"] is False
    assert body["modelVersion"] == FakeModel.model_version


async def test_endpoint_rejects_an_unreadable_upload(client, settings, monkeypatch):
    from app.api.v1.routes import skin_model as route

    class Loaded(FakeModel):
        labels = FIRST_COLAB_RUN
        model_path = resolve("ml/vision/artifacts/skin_disease_model.int8.onnx")

    monkeypatch.setattr(route, "load_skin_model", lambda _settings: Loaded(unreadable=True))
    headers = await signed_in(client)
    settings.skin_model_debug_endpoint = True
    response = await client.post(ENDPOINT, headers=headers, files=upload())
    assert response.status_code == 422
    assert response.json()["errorCode"] == "IMAGE_UNUSABLE"
