"""
Token verification. IF-COMM-002.

The client signs in against Supabase using Google, Apple, or email. Supabase
issues a signed JWT. This module's whole job is to check that signature and read
the subject claim.

Note what it does not do: it does not issue tokens, does not verify passwords,
does not store credentials, and never sees one. FR-ONB-001 holds because there
is no code here capable of breaking it.

## Two signing algorithms, on purpose

Supabase signs project tokens **asymmetrically** (ES256): it holds a private key
and publishes the matching public keys at a JWKS endpoint. Older projects used
HS256 with a shared secret.

This module accepts both, and not merely for compatibility:

  * **ES256** is what real users' tokens carry. The public key is fetched from
    Supabase and cached in the process.
  * **HS256** is what `scripts/make_test_token.py` and the Postman collection
    mint locally from the project secret. Dropping it would mean the whole
    verification suite could only run against live sign-ins — slow, rate
    limited, and dependent on an email provider.

The algorithm is read from the token header to choose a path, and that read is
unverified, so the permitted set is closed. A token declaring `none`, or any
algorithm not named here, is rejected before a key is looked up. Without that,
an attacker could choose the algorithm and defeat the signature entirely.

The key set is fetched with a plain HTTP call rather than PyJWT's JWKS client,
whose API has moved between releases. One fetch per process, one more if a key
rotates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
import jwt
from jwt import InvalidTokenError, PyJWK
from jwt.exceptions import PyJWKError

from app.core.config import Settings
from app.core.errors import Unauthorised

logger = logging.getLogger(__name__)

# Closed set. `none` is absent deliberately.
ASYMMETRIC_ALGORITHMS = {"ES256", "RS256"}
SYMMETRIC_ALGORITHMS = {"HS256"}

# kid -> PyJWK. Populated on first asymmetric token, refreshed if a kid is
# unknown, which is what a key rotation looks like from here.
_key_cache: dict[str, PyJWK] = {}


@dataclass(frozen=True)
class TokenClaims:
    """The only parts of the token this application cares about."""

    subject: str  # becomes User.auth_id
    provider: str  # "google" | "apple" | "email"
    email: str | None = None


def _fetch_jwks(settings: Settings) -> dict[str, PyJWK]:
    """
    Fetch and parse Supabase's public key set.

    Synchronous inside an async request. That is acceptable here because it
    happens once per process (and once more per key rotation), not per request.
    Making it async would mean an async lock to stop a cold start firing a
    dozen concurrent fetches, which is more machinery than the problem needs.
    """
    if not settings.supabase_url:
        logger.error("SUPABASE_URL is not set; cannot verify asymmetric tokens")
        raise Unauthorised()

    url = settings.supabase_url.rstrip("/") + "/auth/v1/.well-known/jwks.json"
    response = httpx.get(url, timeout=10.0)
    response.raise_for_status()

    keys: dict[str, PyJWK] = {}
    for entry in response.json().get("keys", []):
        kid = entry.get("kid")
        if not kid:
            continue
        try:
            keys[kid] = PyJWK.from_dict(entry)
        except PyJWKError:
            # An unusable entry is skipped rather than failing the whole set —
            # Supabase publishes keys for algorithms we may not accept.
            logger.debug("skipping unusable JWKS entry kid=%s", kid)

    logger.info("loaded %d signing key(s) from Supabase", len(keys))
    return keys


def _signing_key(kid: str | None, settings: Settings) -> object:
    """Resolve a key id to a public key, refetching once if it is unknown."""
    if not kid:
        logger.info("token rejected: no kid in header")
        raise Unauthorised()

    global _key_cache

    if kid not in _key_cache:
        _key_cache = _fetch_jwks(settings)

    key = _key_cache.get(kid)
    if key is None:
        logger.warning("no signing key matches kid=%s", kid)
        raise Unauthorised()

    return key.key


def verify_token(token: str, settings: Settings) -> TokenClaims:
    """
    Decode and verify a Supabase access token.

    Every failure path raises the same generic `Unauthorised`. The specific
    reason goes to the log, where it helps you, rather than to the response,
    where it would help someone probing the endpoint.
    """
    try:
        header = jwt.get_unverified_header(token)
    except InvalidTokenError as exc:
        logger.info("token header unreadable: %s", type(exc).__name__)
        raise Unauthorised() from exc

    algorithm = header.get("alg")

    if algorithm in SYMMETRIC_ALGORITHMS:
        if not settings.jwt_secret:
            # Falling back to an unverified decode would make this module
            # decorative, so refuse instead. /readyz reports this beforehand.
            logger.error("JWT_SECRET is not configured; refusing HS256 tokens")
            raise Unauthorised()
        key: object = settings.jwt_secret

    elif algorithm in ASYMMETRIC_ALGORITHMS:
        try:
            key = _signing_key(header.get("kid"), settings)
        except Unauthorised:
            raise
        except Exception as exc:
            # Logged with the message, not just the type. A JWKS failure is
            # almost always a wrong URL or no network, and the type alone says
            # neither.
            logger.warning(
                "could not resolve signing key: %s: %s", type(exc).__name__, exc
            )
            raise Unauthorised() from exc

    else:
        logger.info("token rejected: unsupported algorithm %r", algorithm)
        raise Unauthorised()

    try:
        payload = jwt.decode(
            token,
            key,
            algorithms=[algorithm],
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