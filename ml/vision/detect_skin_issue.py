"""
Run the trained skin-issue model on photos from the command line.

    python ml/vision/detect_skin_issue.py selfie.jpg
    python ml/vision/detect_skin_issue.py a.jpg b.jpg --json
    python ml/vision/detect_skin_issue.py face.jpg --no-face-crop
    python ml/vision/detect_skin_issue.py face.jpg --model ml/vision/artifacts/skin_disease_model.onnx

Run from the backend folder with its venv (needs requirements-model.txt).
Uses the int8 model in ml/vision/artifacts/ by default -- the same file, face
crop and preprocessing as the backend (app/services/analysis/onnx_skin.py), so
a photo gets the same answer here, from the API and inside a scan.

Probabilities are softmax over the classes, so they add up to 100%. The
deployment-gate line says whether this model may serve real scans (FR-AI-010)
and, if not, why. This is a development tool: nothing here is shown to users.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))

from app.core.config import Settings  # noqa: E402
from app.services.analysis.onnx_skin import (  # noqa: E402
    SkinIssueModel,
    UnreadableImage,
    evaluate_gate,
    resolve,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("images", nargs="+", type=Path)
    parser.add_argument("--model", help="ONNX file (default: the int8 model from settings)")
    parser.add_argument("--labels", help="labels.json (default: from settings)")
    parser.add_argument("--no-face-crop", action="store_true", help="use the whole photo")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    settings = Settings(_env_file=BACKEND / ".env")
    model_path = resolve(args.model) if args.model else resolve(settings.internal_model_path)
    labels_path = resolve(args.labels) if args.labels else resolve(settings.internal_model_labels_path)
    try:
        model = SkinIssueModel(model_path, labels_path)
    except FileNotFoundError as exc:
        raise SystemExit(str(exc))
    except ImportError:
        raise SystemExit("Missing libraries: pip install -r requirements-model.txt")

    gate = evaluate_gate(model.labels, settings, served_file=model_path.name)
    results = []
    for path in args.images:
        if not path.is_file():
            results.append({"image": str(path), "error": "file not found"})
            continue
        try:
            result = model.predict(path.read_bytes(), face_crop=not args.no_face_crop)
        except UnreadableImage:
            results.append({"image": str(path), "error": "not a readable image"})
            continue
        results.append({
            "image": str(path),
            "faceFound": result.face_found,
            "topIssue": result.top.label,
            "detected": [p.label for p in result.detected],
            "issues": [
                {"label": p.label, "concernId": p.concern_id, "probability": round(p.probability, 4)}
                for p in result.predictions
            ],
        })

    if args.json:
        print(json.dumps({
            "modelVersion": model.model_version,
            "threshold": model.threshold,
            "deploymentGate": {"passed": gate.passed, "reasons": list(gate.reasons)},
            "results": results,
        }, indent=2))
        return

    print(f"model: {model.model_version}")
    for entry in results:
        print(f"\n{entry['image']}")
        if "error" in entry:
            print(f"  error: {entry['error']}")
            continue
        if entry["faceFound"] is False:
            print("  (no face found -- used the whole photo)")
        for issue in entry["issues"]:
            percent = issue["probability"] * 100
            mark = "DETECTED" if issue["label"] in entry["detected"] else ""
            print(f"  {issue['label']:<11} {percent:5.1f}%  {'#' * round(percent / 5):<20} {mark}")
        if not entry["detected"]:
            print(f"  -> no issue above {model.threshold:.0%}")

    print("\ndeployment gate:", "PASSED" if gate.passed else "NOT PASSED -- must not serve real scans")
    for reason in gate.reasons:
        print(f"  - {reason}")


if __name__ == "__main__":
    main()
