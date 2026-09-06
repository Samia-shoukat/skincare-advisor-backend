"""
Content endpoints. IF-UI-001.

Everything the client displays as a claim comes from here rather than from a
string baked into a screen. Three consequences follow, and all three are
requirements rather than conveniences:

  * A wording correction reaches users without an app store release.
  * There is exactly one copy of each claim, so it cannot drift between two
    screens that show the same thing.
  * The review claim can be resolved against the database on every fetch, which
    is what makes FR-ONB-008's default state reliable.

These routes are unauthenticated on purpose. The limitations statement has to
render on the consent screen, which by definition precedes a completed account,
and the questionnaire has to render before a skin type exists. None of it is
user-specific.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import SessionDep, SettingsDep
from app.clinical.copy import strings
from app.db.models.review import resolve_review_claim
from app.services.questionnaire import get_questionnaire

router = APIRouter(prefix="/content", tags=["content"])


@router.get("/strings")
async def get_strings(session: SessionDep, settings: SettingsDep) -> dict:
    """
    The full claim bundle, with one version stamp over all of it.

    The client caches on `version` and refetches when it changes. A single
    stamp rather than one per string is deliberate: these strings are reviewed
    together, and a mix of versions on one screen would defeat the review.
    """
    claim = await resolve_review_claim(session, settings.active_matrix_version)
    return strings.bundle(review_claim=str(claim["claim"]))


@router.get("/questionnaire")
async def get_skin_type_questionnaire(settings: SettingsDep) -> dict:
    """
    The FR-ONB-006 questionnaire, without its scoring weights.

    Weights are stripped in `Questionnaire.as_payload`. A client that can see
    them can work backwards from a desired skin type to the answers that
    produce it -- and the whole routine is built on that value.
    """
    questionnaire = get_questionnaire(settings.questionnaire_path)
    return questionnaire.as_payload()


@router.get("/review-claim")
async def get_review_claim(session: SessionDep, settings: SettingsDep) -> dict:
    """
    FR-ONB-008.

    Returns `substantiated: false` and no reviewer block unless a signed record
    exists for the active matrix version. Clients branch on `substantiated`.
    """
    return await resolve_review_claim(session, settings.active_matrix_version)