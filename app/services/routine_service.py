"""
Routine generation for a scan. SRS 4.2 stages 6 and 7.

Glue, deliberately thin: load the matrix, run the engine, check the invariants,
match products, save. Every decision lives in the engine, the matrix, or the
catalogue filter; this module only puts them in order.

## The invariant check runs on every real routine

The test suite proves all 16,384 input combinations are safe against the
shipped matrix. But FR-REC-006 lets the matrix change at runtime without a
release -- a published matrix could contain a combination the tests never saw.
So every routine is checked again, here, before it is shown. A routine that
fails is never displayed and never charged for; the scan ends in ERROR and the
violation is logged loudly, because it means the published rules are unsafe.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.catalogue import Product
from app.db.models.routine import Routine as RoutineRow
from app.db.models.scan_log import ScanLog
from app.db.models.user import User
from app.engine.invariants import check_routine
from app.engine.rules import RoutineInput, generate_routine
from app.services.analysis.router import RoutedAnalysis
from app.services.catalogue import match_products
from app.services.matrix_loader import MatrixStore

logger = logging.getLogger(__name__)


@dataclass
class BuiltRoutine:
    payload: dict[str, Any]
    matrix_version: str
    matrix_fell_back: bool
    violations: list[str] = field(default_factory=list)
    # [{"concernId": "ACNE", "severity": "MODERATE"}] -- what the analysis
    # found, kept with the routine so the screen can show it after a reload.
    concerns: list[dict[str, str]] = field(default_factory=list)
    missing_tiers: list[dict[str, str]] = field(default_factory=list)
    omitted: list[dict[str, str]] = field(default_factory=list)
    product_ids: list[str] = field(default_factory=list)


def routine_input(user: User, analysis: RoutedAnalysis) -> RoutineInput:
    """
    Everything the engine may see.

    FR-ONB-006: skin type from the questionnaire, never from the image.
    FR-ONB-004: age band on every routine request.
    FR-AI-006: concerns only -- no clinical signal can reach this point, and
    `RoutineInput` has no field that could carry one.
    """
    return RoutineInput(
        skin_type=user.skin_type,
        age_band=user.age_band,
        is_pregnant=user.is_pregnant,
        on_prescription_treatment=user.on_prescription_treatment,
        concerns=frozenset(f.concern for f in analysis.result.concerns),
        referral_flag=user.referral_flag,
    )


class RoutineService:
    def __init__(self, matrix_store: MatrixStore) -> None:
        self._store = matrix_store

    async def matrix_version(self) -> str:
        return (await self._store.get()).matrix.version

    async def build(
        self, session: AsyncSession, user: User, analysis: RoutedAnalysis
    ) -> BuiltRoutine:
        loaded = await self._store.get()
        matrix = loaded.matrix
        if loaded.fell_back:
            # FR-REC-006: "the fallback is recorded".
            logger.warning("routine generated from fallback matrix %s", matrix.version)

        data = routine_input(user, analysis)
        routine = generate_routine(matrix, data)

        violations = check_routine(matrix, data, routine)
        if violations:
            logger.error(
                "ROUTINE INVARIANT VIOLATION under matrix %s: %s", matrix.version, violations
            )
            return BuiltRoutine(
                payload={},
                matrix_version=matrix.version,
                matrix_fell_back=loaded.fell_back,
                violations=violations,
            )

        payload, missing = await match_products(session, matrix, data, routine)

        product_ids = sorted(
            {
                option["id"]
                for step in payload["am"] + payload["pm"]
                for option in (step["products"]["budget"], step["products"]["premium"])
                if option
            }
        )

        return BuiltRoutine(
            payload=payload,
            matrix_version=matrix.version,
            matrix_fell_back=loaded.fell_back,
            concerns=[
                {"concernId": f.concern.value, "severity": f.severity.value}
                for f in analysis.result.concerns
            ],
            missing_tiers=missing,
            omitted=routine.omitted,
            product_ids=product_ids,
        )

    async def save(
        self, session: AsyncSession, user: User, scan: ScanLog, built: BuiltRoutine
    ) -> RoutineRow:
        products = []
        if built.product_ids:
            products = list(
                (
                    await session.execute(select(Product).where(Product.id.in_(built.product_ids)))
                ).scalars()
            )

        row = RoutineRow(
            user_auth_id=user.auth_id,
            scan_log_id=scan.id,
            matrix_version=built.matrix_version,
            catalogue_version=products[0].catalogue_version if products else None,
            # `concerns` rides along inside `steps` rather than in its own
            # column: it is display data for the routine screen, it is written
            # once with the routine, and adding a column would mean a migration
            # for something no query ever filters on.
            steps={
                "am": built.payload["am"],
                "pm": built.payload["pm"],
                "concerns": built.concerns,
            },
            omitted_steps=built.omitted or None,
            missing_tiers=built.missing_tiers or None,
            products=products,
        )
        session.add(row)
        await session.flush()
        return row


async def latest_routine(session: AsyncSession, user: User) -> RoutineRow | None:
    """FR-SUB-005. The most recent routine, whatever the allowance says."""
    result = await session.execute(
        select(RoutineRow)
        .where(RoutineRow.user_auth_id == user.auth_id)
        .order_by(RoutineRow.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def routine_response(row: RoutineRow) -> dict[str, Any]:
    """The shape the client renders, from a stored row."""
    return {
        "routineId": row.id,
        "matrixVersion": row.matrix_version,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "am": row.steps.get("am", []),
        "pm": row.steps.get("pm", []),
        # Severity per concern, for the skin-vitals display. Absent on routines
        # generated before this was stored, so the client must tolerate [].
        "concerns": row.steps.get("concerns", []),
        "omitted": row.omitted_steps or [],
    }
