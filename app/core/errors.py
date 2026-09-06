"""
Error taxonomy and handlers.

IF-COMM-003 asks for two things on every failure: a machine-readable code the
client can branch on, and a human-readable message it can show. It also asks
that no internal implementation detail leaks. That second half is why the
catch-all handler at the bottom exists -- an unhandled exception reaching the
client as a stack trace, or a database error string naming a column, is a
requirement violation, not just untidy output.

Messages here are written for the user, not the developer. The developer's
version goes to the log via `detail`, which is never serialised into a response.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request, status
from fastapi.responses import JSONResponse

from app.core.enums import ErrorCode

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Base class for every error the API deliberately returns."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "BAD_REQUEST"
    message: str = "The request could not be completed."

    def __init__(self, message: str | None = None, detail: dict[str, Any] | None = None) -> None:
        self.message = message or self.message
        # For logs only. Never appears in a response body.
        self.detail = detail or {}
        super().__init__(self.message)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class Unauthorised(AppError):
    """
    IF-COMM-002. Deliberately generic.

    Distinguishing "expired" from "malformed" from "wrong audience" tells an
    attacker probing the endpoint more than it tells a legitimate user, whose
    client should simply re-authenticate in all three cases.
    """

    status_code = status.HTTP_401_UNAUTHORIZED
    code = "UNAUTHORISED"
    message = "Sign in to continue."


# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------


class ImmutableField(AppError):
    """
    FR-ONB-002 and FR-ONB-006. Date of birth and skin type are set once.

    The message says the value cannot be changed in the app and stops there --
    it does not explain the rule or hint at a way around it.
    """

    status_code = status.HTTP_409_CONFLICT
    code = "IMMUTABLE_FIELD"
    message = "This detail can't be changed from within the app."


class InvalidDateOfBirth(AppError):
    """
    FR-ONB-002, and carefully scoped.

    This fires for a future date or an age above the upper bound. It must NEVER
    fire for an under-13 date: FR-ONB-003 requires that case to produce an
    account with scan access withdrawn, silently. Rejecting it here would state
    the threshold by implication and invite a second attempt with a different
    date.
    """

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "INVALID_DATE_OF_BIRTH"
    message = "Please check the date you entered."


class InvalidQuestionnaireAnswers(AppError):
    """
    FR-ONB-006. An answer set that cannot be scored.

    Almost always a stale client running against a newer questionnaire version,
    so the message tells the user to reload rather than to try again -- retrying
    the same answers would fail identically.
    """

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "INVALID_QUESTIONNAIRE_ANSWERS"
    message = "Please reload the questionnaire and answer all the questions."


class ConsentVersionMismatch(AppError):
    """
    FR-ONB-007. The statement changed between the screen rendering and the tap.

    Recording this as a consent would mean storing agreement to text the user
    never read, which is precisely what the version field exists to prevent.
    """

    status_code = status.HTTP_409_CONFLICT
    code = "CONSENT_VERSION_MISMATCH"
    message = "The terms have been updated. Please review them again."


class OnboardingIncomplete(AppError):
    """SRS 4.3: every onboarding requirement is satisfied before a scan."""

    status_code = status.HTTP_409_CONFLICT
    code = "ONBOARDING_INCOMPLETE"
    message = "Finish setting up your profile before scanning."


class ScanAccessBlocked(AppError):
    """
    FR-ONB-003.

    The message names no age and no threshold. It gives a support address so a
    genuine mis-entry has a route to a human, and nothing else. A user who
    learns why they were blocked learns what to type next time.
    """

    status_code = status.HTTP_403_FORBIDDEN
    code = "SCAN_ACCESS_BLOCKED"
    message = "This account can't use the scan feature."


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


class QuotaExceeded(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = ErrorCode.QUOTA_EXCEEDED.value
    message = "You've used your scan. Your saved routine is still available."


class AnalysisInvalid(AppError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = ErrorCode.ANALYSIS_INVALID.value
    message = "The analysis couldn't be completed. Please try again."


class ImageUnusable(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = ErrorCode.IMAGE_UNUSABLE.value
    message = "That photo couldn't be used. Please retake it."


class ProviderUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = ErrorCode.PROVIDER_UNAVAILABLE.value
    message = "The service is temporarily unavailable. Please try again shortly."


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _body(code: str, message: str) -> dict[str, str]:
    return {"errorCode": code, "message": message}


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    logger.info("handled error path=%s code=%s detail=%s", request.url.path, exc.code, exc.detail)
    return JSONResponse(status_code=exc.status_code, content=_body(exc.code, exc.message))


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    The last line of defence for IF-COMM-003.

    `logger.exception` puts the traceback in the server log. The response body
    gets a fixed sentence and nothing more.
    """
    logger.exception("unhandled error path=%s", request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_body("INTERNAL_ERROR", "Something went wrong. Please try again."),
    )