from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# Never send a real Discord signup notification from the test suite.
#
# `settings` loads `.env`, which in a dev checkout may hold a live
# DISCORD_SIGNUP_WEBHOOK_URL. Any test that upserts a new user via
# `/v1/auth/social` (testcontainers → fresh DB → is_new=True) would otherwise
# fire a real webhook POST. This autouse fixture blanks the URL so the feature
# is a no-op in tests regardless of local .env. Tests that assert on the
# notification mock `discord_notify.notify_signup` directly.
@pytest.fixture(autouse=True)
def _disable_discord_webhook(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "DISCORD_SIGNUP_WEBHOOK_URL", "", raising=False)


# SPEC-AGENTIC-CRITIQUE-001 — evaluator default fail-open in tests.
#
# The evaluator node calls LiteLLM via `LLMProvider.chat`. In the test env we
# do NOT want every search-flow test to make a real network call. This autouse
# fixture monkeypatches the evaluator's LLM path to return the synthetic
# fail-open score (score=1.0, retry=False) so existing flow assertions stay
# byte-identical to the pre-SPEC behavior. Tests that exercise the evaluator
# explicitly (`tests/test_critique_evaluator.py`, `tests/test_critique_loop.py`)
# override this fixture or call the LLM path directly via their own mocks.
@pytest.fixture(autouse=True)
def _evaluator_fail_open(monkeypatch, request):
    if "no_evaluator_fail_open" in request.keywords:
        return
    try:
        from app.graphs.nodes import evaluator as _evaluator_module
    except ImportError:
        return

    async def _stub_call(_prompt: str):
        return _evaluator_module._fail_open_score("test-stub")

    monkeypatch.setattr(_evaluator_module, "_call_llm", _stub_call)
