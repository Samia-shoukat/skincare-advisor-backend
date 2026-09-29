"""
Account lifecycle and legal content. DR-002, DR-003, DR-004, FR-ONB-003,
FR-ONB-007, Google Play account-deletion policy.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.api.deps import get_auth_admin
from app.core.config import Settings
from app.db.models import Product, Routine, SafetyAnswerChange, ScanLog, User, routine_products
from app.main import app as fastapi_app
from app.services.auth_admin import AuthDeletionFailed
from app.services.retention import run_retention
from tests.test_routine import seed
from tests.test_scan import RoutineSpy, StubProvider, clear, onboard, scan, use_pipeline


class FakeAuthAdmin:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.deleted: list[str] = []

    async def delete_user(self, auth_id: str) -> bool:
        if self.fail:
            raise AuthDeletionFailed("stubbed")
        self.deleted.append(auth_id)
        return True


def use_admin(admin: FakeAuthAdmin) -> FakeAuthAdmin:
    fastapi_app.dependency_overrides[get_auth_admin] = lambda: admin
    return admin


async def count(session_factory, model_or_table, subject: str | None = None) -> int:
    async with session_factory() as db:
        stmt = select(func.count()).select_from(model_or_table)
        if subject is not None:
            stmt = stmt.where(model_or_table.user_auth_id == subject)
        return (await db.execute(stmt)).scalar_one()


async def user_with_routine(client, settings, session_factory) -> dict:
    await seed(session_factory)
    use_pipeline(settings, StubProvider(clear()), routine=RoutineSpy(settings))
    headers = await onboard(client)
    assert (await scan(client, headers)).json()["outcome"] == "ROUTINE"
    return headers


# ---------------------------------------------------------------------------
# DR-002 -- account deletion
# ---------------------------------------------------------------------------


async def test_deletion_removes_every_row_and_the_sign_in(client, settings, session_factory):
    admin = use_admin(FakeAuthAdmin())
    headers = await user_with_routine(client, settings, session_factory)
    products_before = await count(session_factory, Product)

    r = await client.delete("/v1/me", headers=headers)

    assert r.status_code == 204
    assert admin.deleted == ["scanner"]
    assert await count(session_factory, User) == 0
    assert await count(session_factory, ScanLog, "scanner") == 0
    assert await count(session_factory, Routine, "scanner") == 0
    assert await count(session_factory, SafetyAnswerChange, "scanner") == 0
    assert await count(session_factory, routine_products) == 0
    # The catalogue is not the user's data.
    assert await count(session_factory, Product) == products_before


async def test_failed_sign_in_removal_deletes_nothing(client, settings, session_factory):
    """All or nothing: no half-deleted account."""
    use_admin(FakeAuthAdmin(fail=True))
    headers = await user_with_routine(client, settings, session_factory)

    r = await client.delete("/v1/me", headers=headers)

    assert r.status_code == 502
    assert r.json()["errorCode"] == "ACCOUNT_DELETION_FAILED"
    assert await count(session_factory, User) == 1
    assert await count(session_factory, Routine, "scanner") == 1


async def test_deleted_account_cannot_be_used(client, settings, session_factory):
    use_admin(FakeAuthAdmin())
    headers = await user_with_routine(client, settings, session_factory)
    await client.delete("/v1/me", headers=headers)

    r = await client.get("/v1/routines/latest", headers=headers)
    assert r.status_code == 401


async def test_unconfigured_admin_still_deletes_data():
    """Development without a service-role key: data goes, and it says so."""
    from app.services.auth_admin import AuthAdmin

    assert await AuthAdmin(Settings(supabase_url="", supabase_service_role_key="")).delete_user("x") is False


# ---------------------------------------------------------------------------
# DR-003 / DR-004 -- retention
# ---------------------------------------------------------------------------


async def test_retention_deletes_old_scan_logs_but_keeps_routines(client, settings, session_factory):
    headers = await user_with_routine(client, settings, session_factory)
    old = datetime.now(timezone.utc) - timedelta(days=800)
    async with session_factory() as db:
        db.add(ScanLog(user_auth_id="scanner", outcome="REFERRAL", matrix_version="m", created_at=old))
        for log in (await db.execute(select(ScanLog))).scalars():
            log.created_at = old
        await db.commit()

    async with session_factory() as db:
        report = await run_retention(db, datetime.now(timezone.utc), delete_accounts=False)
        await db.commit()

    assert report.scan_logs_deleted == 1  # the referral log; the routine's log is kept
    assert await count(session_factory, Routine, "scanner") == 1
    assert (await client.get("/v1/routines/latest", headers=headers)).status_code == 200


async def test_retention_finds_and_deletes_inactive_accounts(client, settings, session_factory):
    await user_with_routine(client, settings, session_factory)
    async with session_factory() as db:
        user = await db.get(User, "scanner")
        user.last_sign_in_at = datetime.now(timezone.utc) - timedelta(days=731)
        await db.commit()

    admin = FakeAuthAdmin()
    async with session_factory() as db:
        report = await run_retention(db, datetime.now(timezone.utc), delete_accounts=True, auth_admin=admin)
        await db.commit()

    assert report.inactive_accounts == ["scanner"]
    assert admin.deleted == ["scanner"]
    assert await count(session_factory, User) == 0


async def test_active_account_is_not_touched(client, settings, session_factory):
    await user_with_routine(client, settings, session_factory)
    async with session_factory() as db:
        report = await run_retention(db, datetime.now(timezone.utc), delete_accounts=True)
        await db.commit()
    assert report.inactive_accounts == []
    assert await count(session_factory, User) == 1


# ---------------------------------------------------------------------------
# Legal pages and support email
# ---------------------------------------------------------------------------


async def test_legal_pages_are_public_and_name_the_support_email(client, settings):
    settings.support_email = "help@skin.test"
    for path in ("/legal/privacy", "/legal/terms", "/legal/account-deletion"):
        r = await client.get(path)
        assert r.status_code == 200, path
        assert "text/html" in r.headers["content-type"]
        assert "help@skin.test" in r.text, path


async def test_privacy_policy_is_marked_draft_until_retention_is_confirmed(client, settings):
    settings.provider_retention_disabled = False
    assert "do not publish" in (await client.get("/legal/privacy")).text
    settings.provider_retention_disabled = True
    assert "do not publish" not in (await client.get("/legal/privacy")).text


async def test_privacy_policy_states_the_zero_save_policy(client, settings):
    text = (await client.get("/legal/privacy")).text
    assert "never store it" in text
    assert "24 months" in text


async def test_strings_carry_support_email_and_separate_consent_version(client, settings):
    settings.support_email = "help@skin.test"
    body = (await client.get("/v1/content/strings")).json()
    assert "help@skin.test" in body["scanBlockedSupport"]
    assert "help@skin.test" in body["account"]["contact"]
    assert body["consentVersion"] == settings.consent_statement_version
    assert body["consentVersion"] != body["version"]
    assert body["legal"]["privacyPath"] == "/legal/privacy"
