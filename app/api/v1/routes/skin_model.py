"""
Development endpoint: the trained skin-issue model's raw output on one photo.

    POST /v1/dev/skin-model/predict      multipart, field "image"
    POST /v1/dev/skin-model/predict?faceCrop=false

For checking the model on real selfies -- what it predicts, how sure it is,
whether it found the face -- without running a scan. Switched off unless
SKIN_MODEL_DEBUG_ENDPOINT=true, and never served in production whatever that
says.

## Not a way around the scan

A scan runs triage, clinical signal screening, the rules engine and the quota,
and its findings reach the user only through server copy (IF-UI-001). This
endpoint does none of that, so it answers only signed-in users, records
nothing, and is not something the app calls. Probabilities and class names are
for a developer reading them, not for a user.

Same Zero-Save rules as a scan (CON-001): the upload is read into memory,
predicted on and dropped. It is never written or logged.
"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, File, Query, UploadFile

from app.api.deps import CurrentUser, SettingsDep
from app.api.v1.routes.scan import ALLOWED_CONTENT_TYPES, MAX_IMAGE_BYTES
from app.core.errors import DevEndpointDisabled, ImageUnusable, SkinModelUnavailable
from app.schemas.skin_model import GateStatus, IssueScore, SkinModelPrediction
from app.services.analysis.onnx_skin import UnreadableImage, evaluate_gate, load_skin_model

router = APIRouter(prefix="/dev/skin-model", tags=["dev"])


@router.post("/predict", response_model=SkinModelPrediction)
async def predict_skin_issues(
    user: CurrentUser,
    settings: SettingsDep,
    image: UploadFile = File(...),
    face_crop: bool = Query(default=True, alias="faceCrop"),
) -> SkinModelPrediction:
    if settings.is_production or not settings.skin_model_debug_endpoint:
        raise DevEndpointDisabled()

    contents = await image.read()
    await image.close()
    if not contents:
        raise ImageUnusable("No image was received.")
    if len(contents) > MAX_IMAGE_BYTES:
        raise ImageUnusable("That photo is too large.")
    if (image.content_type or "image/jpeg") not in ALLOWED_CONTENT_TYPES:
        raise ImageUnusable("That file isn't a photo we can use.")

    try:
        model = load_skin_model(settings)
    except (FileNotFoundError, ImportError) as exc:
        raise SkinModelUnavailable(detail={"reason": type(exc).__name__}) from None

    started = time.perf_counter()
    try:
        result = await asyncio.to_thread(model.predict, contents, face_crop=face_crop)
    except UnreadableImage:
        raise ImageUnusable("That file isn't a photo we can read.") from None
    finally:
        del contents  # CON-001: nothing below can hold on to it
    elapsed_ms = round((time.perf_counter() - started) * 1000)

    detected = {p.label for p in result.detected}
    gate = evaluate_gate(model.labels, settings, served_file=model.model_path.name)
    return SkinModelPrediction(
        model_id=model.model_id,
        model_version=model.model_version,
        face_found=result.face_found,
        top_issue=result.top.label,
        detected=[p.label for p in result.detected],
        issues=[
            IssueScore(label=p.label, concern_id=p.concern_id,
                       probability=round(p.probability, 4), detected=p.label in detected)
            for p in result.predictions
        ],
        threshold=result.threshold,
        inference_ms=elapsed_ms,
        deployment_gate=GateStatus(passed=gate.passed, reasons=list(gate.reasons)),
    )
