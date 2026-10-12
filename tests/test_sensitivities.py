"""
Sensitive skin and fragrance allergy: asked, stored, changeable -- and never a
gate. The second half matters more: an optional question that quietly became
an onboarding step would lock every existing account out of scanning.
"""

from __future__ import annotations

from tests.conftest import auth
from tests.test_scan import onboard

ANSWERS = {"sensitiveSkin": True, "fragranceAllergy": False}


async def test_a_new_account_is_asked_but_not_blocked(client):
    headers = auth("sensitive-1")
    session = (await client.post("/v1/auth/session", headers=headers)).json()
    assert session["needsSensitivities"] is True

    headers = await onboard(client, "sensitive-1")
    profile = (await client.get("/v1/me", headers=headers)).json()
    assert profile["onboardingComplete"] is True
    assert profile["sensitiveSkin"] is None and profile["fragranceAllergy"] is None

    eligibility = (await client.get("/v1/scans/eligibility", headers=headers)).json()
    assert eligibility["canScan"] is True


async def test_answers_are_stored_and_shown(client):
    headers = await onboard(client, "sensitive-2")
    response = await client.post("/v1/me/sensitivities", headers=headers, json=ANSWERS)
    assert response.status_code == 200
    assert response.json() == {**ANSWERS, "onboardingComplete": True}

    profile = (await client.get("/v1/me", headers=headers)).json()
    assert (profile["sensitiveSkin"], profile["fragranceAllergy"]) == (True, False)
    session = (await client.post("/v1/auth/session", headers=headers)).json()
    assert session["needsSensitivities"] is False


async def test_answers_can_change(client):
    headers = await onboard(client, "sensitive-3")
    await client.post("/v1/me/sensitivities", headers=headers, json=ANSWERS)
    await client.post("/v1/me/sensitivities", headers=headers,
                      json={"sensitiveSkin": False, "fragranceAllergy": True})
    profile = (await client.get("/v1/me", headers=headers)).json()
    assert (profile["sensitiveSkin"], profile["fragranceAllergy"]) == (False, True)


async def test_answering_before_onboarding_finishes_is_fine(client):
    headers = auth("sensitive-4")
    await client.post("/v1/auth/session", headers=headers)
    response = await client.post("/v1/me/sensitivities", headers=headers, json=ANSWERS)
    assert response.status_code == 200
    assert response.json()["onboardingComplete"] is False


async def test_both_answers_are_required(client):
    headers = await onboard(client, "sensitive-5")
    response = await client.post("/v1/me/sensitivities", headers=headers, json={"sensitiveSkin": True})
    assert response.status_code == 422


async def test_requires_sign_in(client):
    assert (await client.post("/v1/me/sensitivities", json=ANSWERS)).status_code == 401
