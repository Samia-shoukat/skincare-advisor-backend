"""
Shared FastAPI dependencies.

`get_current_user` is the one that matters. Every protected route depends on
it, so it is the single place where IF-COMM-002 is enforced -- there is no
route that can accidentally skip authentication, because skipping it means not
declaring the dependency at all, which is visible in the route signature.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import ScanAccessBlocked, Unauthorised
from app.core.security import TokenClaims, verify_token
from app.db.models.user import User
from app.db.session import get_session

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

# Declaring the scheme this way does two things: it parses the Authorization
# header, and it makes Swagger UI render an "Authorize" button so the token can
# be entered once instead of on every request.
#
# auto_error=False keeps the failure ours: FastAPI's built-in 403 would bypass
# the IF-COMM-003 error shape that every other failure in this API uses.
bearer_scheme = HTTPBearer(auto_error=False)


async def get_token_claims(
    settings: SettingsDep,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ] = None,
) -> TokenClaims:
    """
    Pull the bearer token out of the Authorization header and verify it.

    Split from `get_current_user` because `POST /v1/auth/session` needs the
    claims *before* a User row exists -- that endpoint is what creates it.
    """
    if credentials is None or not credentials.credentials:
        raise Unauthorised()
    return verify_token(credentials.credentials, settings)


TokenDep = Annotated[TokenClaims, Depends(get_token_claims)]


async def get_current_user(claims: TokenDep, session: SessionDep) -> User:
    """
    Resolve the authenticated user.

    A valid token with no matching row is treated as unauthenticated rather
    than as a missing resource. It means the account was deleted while a token
    was still live (DR-002 hard-deletes immediately), and the correct client
    response is to sign in again, not to show a 404.
    """
    user = await session.get(User, claims.subject)
    if user is None:
        raise Unauthorised()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_scannable_user(user: CurrentUser) -> User:
    """
    A user permitted to reach the scan feature at all.

    FR-ONB-003 only. Quota, referral flag, and onboarding completeness are
    checked inside the scan pipeline in the fixed order SRS 4.2 specifies, not
    here -- pulling one of those checks forward into a dependency would put it
    out of that order and make the ordering harder to verify.
    """
    if user.scan_access_blocked:
        raise ScanAccessBlocked()
    return user


ScannableUser = Annotated[User, Depends(get_scannable_user)]


async def find_user(session: AsyncSession, auth_id: str) -> User | None:
    """Small helper so route code doesn't repeat the select."""
    result = await session.execute(select(User).where(User.auth_id == auth_id))
    return result.scalar_one_or_none()