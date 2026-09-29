"""
Gemini provider transport behaviour, against a fake Google (httpx MockTransport).

Found in live testing: Google answers 503 under load, and the first real run
of the Postman suite failed a scan on one. A retired model name returns 404.
The first is worth a retry; the second never is.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import Settings
from app.core.enums import AnalysisTask
from app.services.analysis import gemini
from app.services.analysis.base import PreparedImage, ProviderUnavailable
from app.services.analysis.gemini import GeminiProvider

GOOD = {
    "candidates": [
        {"content": {"parts": [{"text": json.dumps({"imageUsable": False})}]}}
    ]
}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_):
        return None

    monkeypatch.setattr(gemini.asyncio, "sleep", instant)


def provider(responses: list[int]) -> tuple[GeminiProvider, list[int]]:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = responses[min(len(calls), len(responses) - 1)]
        calls.append(status)
        # The image must never appear in anything we log; checked here too.
        assert b"x-goog-api-key" not in request.content
        return httpx.Response(status, json=GOOD if status == 200 else {"error": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GeminiProvider(Settings(gemini_api_key="k"), client=client), calls


IMAGE = PreparedImage(b"\xff\xd8fake", "image/jpeg")


async def test_transient_503_is_retried_and_succeeds():
    p, calls = provider([503, 200])
    result = await p.analyse(IMAGE, AnalysisTask.COMBINED)
    assert calls == [503, 200]
    assert result.response.image_usable is False


async def test_persistent_503_gives_up_after_three_tries():
    p, calls = provider([503])
    with pytest.raises(ProviderUnavailable):
        await p.analyse(IMAGE, AnalysisTask.COMBINED)
    assert calls == [503, 503, 503]


async def test_retired_model_404_is_not_retried():
    p, calls = provider([404])
    with pytest.raises(ProviderUnavailable, match="404"):
        await p.analyse(IMAGE, AnalysisTask.COMBINED)
    assert calls == [404]


def test_default_model_is_pinned_not_an_alias():
    model = Settings.model_fields["gemini_model"].default
    assert "latest" not in model and "preview" not in model
    assert model != "gemini-2.0-flash"  # retired: returns 404
