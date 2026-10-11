"""
The internal inference service: the trained skin-issue model. FR-AI-004,
FR-AI-009, FR-AI-010, CON-004.

Three pieces, each usable on its own:

  `SkinIssueModel`     loads the exported ONNX file and its labels.json, finds
                       the face in a photo, preprocesses exactly as training
                       did, and returns a probability per class.
  `evaluate_gate`      reads the evaluation recorded in labels.json and says
                       whether this artefact may serve users, and if not, why.
  `OnnxSkinProvider`   the `AnalysisProvider` the router calls. It serves
                       ACNE_DETECTION and nothing else, and only past the gate.

The command-line tool (ml/vision/detect_skin_issue.py) and the development
endpoint (POST /v1/dev/skin-model/predict) use `SkinIssueModel` directly, so a
photo gets the same answer from all three.

## Why ACNE_DETECTION only

The model predicts one of acne / dark_spots / redness / normal. COMBINED and
CLINICAL_SIGNAL_SCREENING need clinical signals it never learned -- serving
them from this model would answer every safety screen with "nothing found".
COSMETIC_CONCERNS needs nine concerns, not three. ACNE_DETECTION asks exactly
the question the model answers, and is the task SRS Appendix I names for a
custom model.

It detects whether acne is *present*. It measures nothing about extent, so
every finding is graded MILD: the gentlest grade, which leads the rules engine
to the gentlest routine. Grading by the model's confidence would invent a
severity -- confidence is how sure it is, not how much acne there is.

## The gate (FR-AI-010)

A loaded model is not a deployable one. `supports()` answers no -- and the
router falls back to the hosted provider and records why -- unless labels.json
records an evaluation meeting the configured thresholds: overall macro-F1, the
served class's F1, accuracy for every skin-tone group (NFR-SAFE-008) with
enough photos in each to measure it, the gap between groups, an ONNX export
that matched PyTorch, and -- for the int8 file -- a recorded agreement with the
full model. The evaluated artefact has to be the served one (FR-AI-009).

## Images stay in memory (CON-001, FR-CAM-004)

Bytes in, probabilities out. Nothing here writes, caches or logs an image, and
errors are reported by type only, never with the input attached.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from app.core.enums import AnalysisBackend, AnalysisTask, Concern, Severity
from app.schemas.analysis import AnalysisResult, ConcernFinding, Discards
from app.services.analysis.base import (
    AnalysisProvider,
    PreparedImage,
    ProviderResult,
    ProviderUnavailable,
)

if TYPE_CHECKING:
    from app.core.config import Settings

logger = logging.getLogger(__name__)

# Paths in settings are relative to the backend root, wherever the process
# was started from.
BACKEND_ROOT = Path(__file__).resolve().parents[3]

MODEL_ID = "skin-issues-onnx"
NORMAL_LABEL = "normal"
TONE_GROUPS = ("I-II", "III-IV", "V-VI")


class UnreadableImage(ValueError):
    """The bytes are not an image this service can decode."""


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IssuePrediction:
    label: str
    concern_id: str | None
    probability: float


@dataclass(frozen=True)
class SkinIssueResult:
    """One photo's answer. `predictions` is highest probability first."""

    predictions: tuple[IssuePrediction, ...]
    face_found: bool | None  # None when face cropping was not attempted
    threshold: float

    @property
    def top(self) -> IssuePrediction:
        return self.predictions[0]

    @property
    def detected(self) -> tuple[IssuePrediction, ...]:
        """Issues at or above the threshold. "normal" is an absence, not an issue."""
        return tuple(
            p for p in self.predictions
            if p.probability >= self.threshold and p.label != NORMAL_LABEL
        )

    def probability(self, label: str) -> float:
        return next((p.probability for p in self.predictions if p.label == label), 0.0)


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reasons: tuple[str, ...]


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


def resolve(path: str) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else BACKEND_ROOT / candidate


class SkinIssueModel:
    """
    One exported model and its labels.json.

    `predict` is synchronous and CPU-bound; async callers run it in a thread.
    It is safe to call from several threads at once: ONNX Runtime sessions
    are, and the face detector -- which is not -- is used under a lock.
    """

    def __init__(self, model_path: Path, labels_path: Path) -> None:
        # Imported here so the API still starts without them installed: the
        # model is optional (requirements-model.txt), and a missing optional
        # dependency should disable the model, not the scan endpoint.
        import cv2
        import onnxruntime as ort

        if not model_path.is_file():
            raise FileNotFoundError(f"model file not found: {model_path}")
        if not labels_path.is_file():
            raise FileNotFoundError(f"labels.json not found: {labels_path}")

        self.model_path = model_path
        self.labels: dict[str, Any] = json.loads(labels_path.read_text(encoding="utf-8"))
        self._spec = self.labels["input"]
        self.classes: list[str] = list(self.labels["classes"])
        self.concern_ids: dict[str, str | None] = self.labels.get("concern_ids", {})
        self.threshold = float(self.labels.get("threshold", 0.5))

        self._session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        outputs = self._session.get_outputs()[0].shape
        if outputs and isinstance(outputs[-1], int) and outputs[-1] != len(self.classes):
            raise ValueError(
                f"model has {outputs[-1]} outputs but labels.json lists {len(self.classes)} classes"
            )

        # OpenCV 5 dropped CascadeClassifier (requirements pin <5); without it,
        # or with its data file missing, photos are used whole.
        self._detector = None
        if hasattr(cv2, "CascadeClassifier"):
            detector = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            )
            if not detector.empty():
                self._detector = detector
        self._detector_lock = threading.Lock()

    @property
    def model_id(self) -> str:
        return MODEL_ID

    @property
    def model_version(self) -> str:
        """FR-AI-009: the training run and the exact file that served."""
        return f"{self.labels.get('model', 'unknown')}/{self.model_path.name}"

    # -- prediction ----------------------------------------------------------

    def predict(self, data: bytes, *, face_crop: bool = True) -> SkinIssueResult:
        image = _decode(data)
        face_found: bool | None = None
        if face_crop:
            image, face_found = self._crop_face(image)

        tensor = self._preprocess(image)
        scores = self._session.run(None, {self._spec["name"]: tensor})[0][0]
        if (self.labels.get("output") or {}).get("activation") not in ("softmax", "sigmoid"):
            scores = _softmax(scores)  # an export that returns logits

        predictions = sorted(
            (
                IssuePrediction(label=name, concern_id=self.concern_ids.get(name),
                                probability=float(score))
                for name, score in zip(self.classes, scores)
            ),
            key=lambda p: p.probability,
            reverse=True,
        )
        return SkinIssueResult(tuple(predictions), face_found, self.threshold)

    def _crop_face(self, image):
        """The largest face plus a 15% margin, or the whole photo if none is found."""
        if self._detector is None:
            return image, False
        import numpy as np

        # Detect on a copy at most 640 px across: the same faces, a fraction
        # of the time on a 1024 px selfie.
        scale = min(1.0, 640 / max(image.size))
        small = image.convert("L")
        if scale < 1.0:
            small = small.resize((round(image.width * scale), round(image.height * scale)))
        gray = np.asarray(small)
        side = min(gray.shape[:2])
        with self._detector_lock:
            faces = self._detector.detectMultiScale(
                gray, scaleFactor=1.1, minNeighbors=5, minSize=(max(40, side // 6),) * 2
            )
        if len(faces) == 0:
            return image, False

        x, y, w, h = (int(v / scale) for v in max(faces, key=lambda f: f[2] * f[3]))
        dx, dy = int(w * 0.15), int(h * 0.15)
        box = (max(0, x - dx), max(0, y - dy),
               min(image.width, x + w + dx), min(image.height, y + h + dy))
        return image.crop(box), True

    def _preprocess(self, image):
        """
        The training notebook's evaluation transform, reproduced exactly:
        torchvision Resize(shorter side) with PIL bilinear, CenterCrop, scale
        to 0-1, normalise, NCHW. A resize that differs from training -- even
        OpenCV's bilinear in place of PIL's -- shifts the predictions.
        """
        import numpy as np
        from PIL import Image

        shorter = int(self._spec["resize_shorter_side"])
        width, height = image.size
        if width <= height:
            size = (shorter, int(shorter * height / width))
        else:
            size = (int(shorter * width / height), shorter)
        image = image.resize(size, Image.Resampling.BILINEAR)

        crop = int(self._spec["center_crop"])
        left = int(round((image.width - crop) / 2.0))
        top = int(round((image.height - crop) / 2.0))
        image = image.crop((left, top, left + crop, top + crop))

        array = np.asarray(image, dtype=np.float32) / 255.0
        array = (array - np.array(self._spec["mean"], np.float32)) / np.array(self._spec["std"], np.float32)
        return array.transpose(2, 0, 1)[None]


def _decode(data: bytes):
    """RGB image, upright. Raises UnreadableImage for anything else."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    if not data:
        raise UnreadableImage("empty")
    try:
        image = Image.open(io.BytesIO(data))
        image = ImageOps.exif_transpose(image)
        image = image.convert("RGB")
        image.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise UnreadableImage(type(exc).__name__) from None
    return image


def _softmax(scores):
    import numpy as np

    shifted = np.exp(scores - np.max(scores))
    return shifted / shifted.sum()


_MODELS: dict[tuple[Path, Path], SkinIssueModel] = {}
_MODELS_LOCK = threading.Lock()


def load_skin_model(settings: Settings) -> SkinIssueModel:
    """
    The configured model, loaded once per process.

    Raises FileNotFoundError when the artefact is absent and ImportError when
    the optional dependencies are -- callers decide what each means for them.
    """
    key = (resolve(settings.internal_model_path), resolve(settings.internal_model_labels_path))
    with _MODELS_LOCK:
        if key not in _MODELS:
            _MODELS[key] = SkinIssueModel(*key)
        return _MODELS[key]


# ---------------------------------------------------------------------------
# The deployment gate
# ---------------------------------------------------------------------------


def evaluate_gate(
    labels: dict[str, Any],
    settings: Settings,
    *,
    served_file: str,
    served_classes: Iterable[str] = ("acne",),
) -> GateResult:
    """
    FR-AI-010: may this artefact serve users? Every failure is named.

    Reads only what training recorded in labels.json. A metric that was never
    recorded fails: an unmeasured model is not a model that passed.
    """
    reasons: list[str] = []
    validation = labels.get("validation") or {}

    macro_f1 = validation.get("f1_macro")
    if macro_f1 is None:
        reasons.append("no macro-F1 recorded")
    elif macro_f1 < settings.model_gate_min_macro_f1:
        reasons.append(f"macro-F1 {macro_f1:.2f} is below {settings.model_gate_min_macro_f1:.2f}")

    per_class = labels.get("per_class") or {}
    for name in served_classes:
        f1 = (per_class.get(name) or {}).get("f1-score")
        if f1 is None:
            reasons.append(f"no F1 recorded for {name!r}")
        elif f1 < settings.model_gate_min_macro_f1:
            reasons.append(f"{name} F1 {f1:.2f} is below {settings.model_gate_min_macro_f1:.2f}")

    # NFR-SAFE-008. A group with too few photos cannot show the model works
    # for it, so it fails -- dropping it would pass a model nobody measured
    # on darker skin.
    tones = labels.get("accuracy_by_skin_tone") or {}
    measured: dict[str, float] = {}
    for group in TONE_GROUPS:
        entry = tones.get(group)
        if not entry:
            reasons.append(f"no accuracy recorded for skin tone {group}")
            continue
        n, accuracy = int(entry.get("n", 0)), float(entry.get("accuracy", 0.0))
        if n < settings.model_gate_min_tone_samples:
            reasons.append(
                f"only {n} validation photos for skin tone {group} "
                f"(need {settings.model_gate_min_tone_samples})"
            )
        measured[group] = accuracy
        if accuracy < settings.model_gate_min_tone_accuracy:
            reasons.append(
                f"accuracy for skin tone {group} is {accuracy:.0%}, "
                f"below {settings.model_gate_min_tone_accuracy:.0%}"
            )
    if len(measured) >= 2:
        gap = max(measured.values()) - min(measured.values())
        if gap > settings.model_gate_max_tone_gap:
            reasons.append(
                f"skin-tone accuracy gap is {gap:.0%}, above {settings.model_gate_max_tone_gap:.0%}"
            )

    if labels.get("placeholder_classes"):
        reasons.append(f"trained on placeholder images: {', '.join(labels['placeholder_classes'])}")

    onnx_diff = (labels.get("export_check") or {}).get("onnx_max_abs_diff")
    if onnx_diff is None or onnx_diff > 1e-4:
        reasons.append("no recorded check that the ONNX export matches the trained model")

    # FR-AI-009: the evaluated artefact must be the served one. The metrics
    # above were measured on the full model, so the int8 file needs its own
    # recorded agreement with it.
    if "int8" in served_file:
        check = labels.get("int8_check") or {}
        agreement = check.get("agreement")
        if agreement is None:
            reasons.append("no recorded evaluation of the int8 file (re-export with the current notebook)")
        elif agreement < settings.model_gate_min_int8_agreement:
            reasons.append(
                f"int8 file agrees with the full model on {agreement:.1%} of photos, "
                f"below {settings.model_gate_min_int8_agreement:.0%}"
            )

    return GateResult(passed=not reasons, reasons=tuple(reasons))


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------


class OnnxSkinProvider(AnalysisProvider):
    """The internal model behind the CON-004 interface. See the module docstring."""

    backend = AnalysisBackend.INTERNAL_MODEL
    SERVED_TASKS = frozenset({AnalysisTask.ACNE_DETECTION})

    def __init__(self, model: SkinIssueModel, gate: GateResult) -> None:
        self._model = model
        self.gate = gate

    def supports(self, task: AnalysisTask) -> bool:
        return task in self.SERVED_TASKS and self.gate.passed

    async def health(self) -> bool:
        return self.gate.passed

    async def analyse(self, image: PreparedImage, task: AnalysisTask) -> ProviderResult:
        if not self.supports(task):
            # The router asks first, so this is a caller bug -- but answering
            # with a guess would be worse than refusing.
            raise ProviderUnavailable(f"internal model does not serve {task.value}")
        try:
            result = await asyncio.to_thread(self._model.predict, image.data)
        except UnreadableImage:
            return self._result(AnalysisResult(image_usable=False), task)
        except Exception as exc:  # noqa: BLE001 -- reported by type, never with the image
            logger.warning("internal model failed: %s", type(exc).__name__)
            raise ProviderUnavailable(f"internal model failed: {type(exc).__name__}") from None

        concerns: tuple[ConcernFinding, ...] = ()
        if result.probability("acne") >= result.threshold:
            concerns = (ConcernFinding(concern=Concern.ACNE, severity=Severity.MILD),)
        return self._result(AnalysisResult(image_usable=True, concerns=concerns), task)

    def _result(self, response: AnalysisResult, task: AnalysisTask) -> ProviderResult:
        return ProviderResult(
            response=response,
            discards=Discards(),
            backend=self.backend,
            task=task,
            model_id=self._model.model_id,
            model_version=self._model.model_version,
        )


def build_internal_provider(settings: Settings) -> OnnxSkinProvider | None:
    """
    The provider for the router, or None.

    None when no task is assigned to the internal model (nothing is loaded,
    nothing costs memory), or when the artefact cannot be loaded -- the router
    then falls back to the hosted provider and records the reason. A failing
    gate still returns a provider: it is registered, answers no, and the
    fallback reason says which threshold it missed.
    """
    if not settings.internal_model_tasks.strip():
        return None
    try:
        model = load_skin_model(settings)
    except ImportError:
        logger.warning("internal model not loaded: install requirements-model.txt")
        return None
    except Exception as exc:  # noqa: BLE001 -- a bad artefact must not stop the API
        logger.warning("internal model not loaded: %s: %s", type(exc).__name__, exc)
        return None

    gate = evaluate_gate(model.labels, settings, served_file=model.model_path.name)
    if gate.passed:
        logger.info("internal model %s passed the FR-AI-010 gate", model.model_version)
    else:
        logger.warning(
            "internal model %s loaded but NOT serving (FR-AI-010): %s",
            model.model_version, "; ".join(gate.reasons),
        )
    return OnnxSkinProvider(model, gate)
