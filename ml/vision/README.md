# Skin-issue model — artefacts and inference

`artifacts/` holds what `ml/notebooks/Skin_Issue_Trainer_Colab.ipynb` exports:

| File | In git? |
|---|---|
| `skin_disease_model.int8.onnx` — served by default (~87 MB) | no, too large |
| `skin_disease_model.onnx` — full precision (~343 MB) | no, too large |
| `labels.json` — classes, preprocessing, recorded evaluation | yes |
| `NOTICE.txt` — SCIN attribution its licence requires | yes |

The backend code lives in `app/services/analysis/onnx_skin.py`. Install its
libraries once: `pip install -r requirements-model.txt`.

## Try it on a photo

From the backend folder:

```
python ml/vision/detect_skin_issue.py selfie.jpg
python ml/vision/detect_skin_issue.py selfie.jpg --json
```

Or through the API, after setting `SKIN_MODEL_DEBUG_ENDPOINT=true` in `.env`
(never served in production):

```
curl -X POST http://localhost:8000/v1/dev/skin-model/predict \
     -H "Authorization: Bearer <token>" -F "image=@selfie.jpg"
```

## Using it in real scans

Two settings, and the gate:

```
ANALYSIS_PLAN=COSMETIC_CONCERNS,ACNE_DETECTION,CLINICAL_SIGNAL_SCREENING
INTERNAL_MODEL_TASKS=ACNE_DETECTION
```

The model serves only if the evaluation in `labels.json` passes the FR-AI-010
gate (thresholds in `app/core/config.py`). Otherwise the scan falls back to
Gemini and the scan log records why. Both tools above print the gate result.
See ADR-028.
