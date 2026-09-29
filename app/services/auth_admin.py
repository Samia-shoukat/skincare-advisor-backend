"""
Deleting the Supabase sign-in. DR-002, and Google Play's account-deletion rule.

Removing our rows is not the whole of "delete my account". The user's identity
-- email, password hash, Google link -- lives in Supabase Auth, and while it
exists they can still sign in. Google Play requires apps with accounts to let
users delete the account itself, so both have to go.

Uses the Supabase Admin API with the service-role key. That key is server-only:
it bypasses every Supabase access rule, which is exactly why this call lives on
the backend and never in the app.
"""

from __future__ import annotations

import logging

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)


class AuthDeletionFailed(RuntimeError):
    """Supabase refused or could not be reached. Nothing should be committed."""


class AuthAdmin:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client

    @property
    def configured(self) -> bool:
        return bool(self._settings.supabase_url and self._settings.supabase_service_role_key)

    async def delete_user(self, auth_id: str) -> bool:
        """
        Delete the sign-in. Returns False if not configured (development).

        A 404 counts as success: the identity is already gone, which is the
        state we wanted, and treating it as a failure would make a retried
        deletion impossible to finish.
        """
        if not self.configured:
            logger.warning(
                "SUPABASE_SERVICE_ROLE_KEY not set: account data deleted but the "
                "Supabase sign-in was NOT removed"
            )
            return False

        url = f"{self._settings.supabase_url.rstrip('/')}/auth/v1/admin/users/{auth_id}"
        key = self._settings.supabase_service_role_key
        headers = {"apikey": key, "Authorization": f"Bearer {key}"}

        try:
            if self._client is not None:
                response = await self._client.delete(url, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.delete(url, headers=headers)
        except httpx.HTTPError as exc:
            raise AuthDeletionFailed(type(exc).__name__) from None

        if response.status_code == 404:
            return True
        if response.status_code >= 400:
            # Status only: the body may echo the request, and the key is in it.
            raise AuthDeletionFailed(f"supabase returned {response.status_code}")
        return True
