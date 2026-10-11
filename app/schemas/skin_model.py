"""
Response of the development endpoint POST /v1/dev/skin-model/predict.

The model's raw output, for testing. The app never calls this endpoint: what a
user sees comes from a scan, through the triage stage and the rules engine.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class IssueScore(_Base):
    label: str
    # The backend concern this class maps to; null for "normal".
    concern_id: str | None = Field(alias="concernId")
    # 0-1. Softmax over the classes, so the scores sum to 1.
    probability: float
    detected: bool


class GateStatus(_Base):
    passed: bool
    reasons: list[str]


class SkinModelPrediction(_Base):
    model_id: str = Field(alias="modelId")
    model_version: str = Field(alias="modelVersion")
    # null when face cropping was switched off for the request.
    face_found: bool | None = Field(alias="faceFound")
    top_issue: str = Field(alias="topIssue")
    # Issues at or above the threshold, highest first. Empty means the model
    # found none, or was not confident about any.
    detected: list[str]
    issues: list[IssueScore]
    threshold: float
    inference_ms: int = Field(alias="inferenceMs")
    # FR-AI-010. Whether this artefact may serve real scans, and if not, why.
    deployment_gate: GateStatus = Field(alias="deploymentGate")
