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


MAIN, FALLBACK = "main-model", "fallback-model"


def provider(responses, *, fallbacks: str = FALLBACK) -> tuple[GeminiProvider, list[tuple[str, int]]]:
    """
    A fake Google. `responses` is either a list of statuses served in turn, or
    a dict of model -> list of statuses (or an exception) for that model.
    Calls are recorded as (model, status).
    """
    calls: list[tuple[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = request.url.path.split("/models/")[1].split(":")[0]
        if isinstance(responses, dict):
            queue = responses[model]
            seen = sum(1 for m, _ in calls if m == model)
            outcome = queue[min(seen, len(queue) - 1)]
        else:
            outcome = responses[min(len(calls), len(responses) - 1)]
        calls.append((model, outcome if isinstance(outcome, int) else type(outcome).__name__))
        # The image must never appear in anything we log; checked here too.
        assert b"x-goog-api-key" not in request.content
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, json=GOOD if outcome == 200 else {"error": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(gemini_api_key="k", gemini_model=MAIN, gemini_fallback_models=fallbacks)
    return GeminiProvider(settings, client=client), calls


IMAGE = PreparedImage(b"\xff\xd8fake", "image/jpeg")


async def test_an_overloaded_main_model_falls_back_at_once():
    p, calls = provider({MAIN: [503], FALLBACK: [200]})
    result = await p.analyse(IMAGE, AnalysisTask.COMBINED)
    assert calls == [(MAIN, 503), (FALLBACK, 200)]   # no wasted retry of MAIN
    assert result.response.image_usable is False


async def test_a_silent_main_model_falls_back():
    p, calls = provider({MAIN: [httpx.ReadTimeout("slow")], FALLBACK: [200]})
    await p.analyse(IMAGE, AnalysisTask.COMBINED)
    assert calls == [(MAIN, "ReadTimeout"), (FALLBACK, 200)]


async def test_the_fallback_is_retried_when_both_are_overloaded():
    p, calls = provider({MAIN: [503], FALLBACK: [503, 200]})
    await p.analyse(IMAGE, AnalysisTask.COMBINED)
    assert calls == [(MAIN, 503), (FALLBACK, 503), (FALLBACK, 200)]


async def test_persistent_503_everywhere_gives_up():
    p, calls = provider([503])
    with pytest.raises(ProviderUnavailable, match="503"):
        await p.analyse(IMAGE, AnalysisTask.COMBINED)
    assert calls == [(MAIN, 503), (FALLBACK, 503), (FALLBACK, 503), (FALLBACK, 503)]


async def test_a_retired_model_falls_through_but_is_never_retried():
    p, calls = provider({MAIN: [404], FALLBACK: [404]})
    with pytest.raises(ProviderUnavailable, match="404"):
        await p.analyse(IMAGE, AnalysisTask.COMBINED)
    assert calls == [(MAIN, 404), (FALLBACK, 404)]


async def test_a_bad_request_fails_at_once():
    p, calls = provider({MAIN: [400], FALLBACK: [200]})
    with pytest.raises(ProviderUnavailable, match="400"):
        await p.analyse(IMAGE, AnalysisTask.COMBINED)
    assert calls == [(MAIN, 400)]   # every model would refuse a malformed request


async def test_without_a_fallback_the_main_model_is_retried():
    p, calls = provider([503, 503, 200], fallbacks="")
    await p.analyse(IMAGE, AnalysisTask.COMBINED)
    assert calls == [(MAIN, 503), (MAIN, 503), (MAIN, 200)]


async def test_fallback_use_is_logged(caplog):
    p, _ = provider({MAIN: [503], FALLBACK: [200]})
    with caplog.at_level("WARNING"):
        await p.analyse(IMAGE, AnalysisTask.COMBINED)
    assert f"served by fallback {FALLBACK}" in caplog.text


def test_default_models_are_pinned_not_aliases():
    for field in ("gemini_model", "gemini_fallback_models"):
        for model in Settings.model_fields[field].default.split(","):
            assert "latest" not in model and "preview" not in model, model
            assert model != "gemini-2.0-flash"  # retired: returns 404
