"""
Token verification. IF-COMM-002.

The client signs in with Google or Apple directly against Supabase. Supabase
issues a signed JWT. This module's entire job is to check that signature and
read the subject claim.

Note what it does not do: it does not issue tokens, does not verify passwords,
does not store credentials, and never sees one. FR-ONB-001 requires exactly
that, and the requirement holds because there is no code here capable of
breaking it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import jwt
from jwt import InvalidTokenError

from app.core.config import Settings
from app.core.errors import Unauthorised

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenClaims:
    """The only parts of the token this application cares about."""

    subject: str  # becomes User.auth_id
    provider: str  # "google" | "apple"
    email: str | None = None


def verify_token(token: str, settings: Settings) -> TokenClaims:
    """
    Decode and verify a Supabase access token.

    Every failure path raises the same generic `Unauthorised`. The specific
    reason goes to the log, where it helps you, rather than to the response,
    where it would help someone probing the endpoint.
    """
    if not settings.jwt_secret:
        # Falling back to an unverified decode would make this whole module
        # decorative, so refuse instead. /readyz reports this before it bites.
        logger.error("JWT_SECRET is not configured; refusing to verify tokens")
        raise Unauthorised()

    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            audience=settings.jwt_audience or None,
            # Without `require`, a token missing `exp` would verify and never
            # expire. Being explicit costs nothing and closes that door.
            options={"require": ["sub", "exp"]},
        )
    except InvalidTokenError as exc:
        logger.info("token rejected: %s", type(exc).__name__)
        raise Unauthorised() from exc

    subject = payload.get("sub")
    if not subject:
        raise Unauthorised()

    # Supabase nests the identity provider under app_metadata. The fallbacks
    # keep this working if that shape changes, rather than crashing.
    provider = (
        (payload.get("app_metadata") or {}).get("provider")
        or payload.get("provider")
        or "unknown"
    )

    return TokenClaims(subject=subject, provider=provider, email=payload.get("email"))