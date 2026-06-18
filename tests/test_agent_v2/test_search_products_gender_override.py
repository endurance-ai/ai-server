"""B23 — LLM gender hallucination override.

Beta trace 499840bb (chat 73e5c867, 09:22): female user pinned to
taste_profile.gender='women', LLM injected 'men' into a follow-up
text_query that had no gender word in the raw user message. Pre-fix the
system trusted the LLM's per-request gender and ran the search against
'men' results — frustrating the user.

The fix: when LLM-supplied gender contradicts the pinned profile AND the
current user message carries no gender signal, the pinned profile wins.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import app.agents.tools.search_products as sp


class TestUserTextGenderSignal:
    def test_korean_explicit_women(self):
        assert sp._user_text_gender_signal("여자 크롭탑 추천해줘") == "women"

    def test_korean_explicit_men(self):
        assert sp._user_text_gender_signal("남자 후드 추천") == "men"

    def test_korean_silent_inference_men(self):
        assert sp._user_text_gender_signal("남편 옷 사고 싶은데") == "men"
        assert sp._user_text_gender_signal("아빠 선물용") == "men"

    def test_korean_silent_inference_women(self):
        assert sp._user_text_gender_signal("엄마한테 선물할 거") == "women"
        assert sp._user_text_gender_signal("여친 옷") == "women"

    def test_english_for_him(self):
        assert sp._user_text_gender_signal("Something for him") == "men"

    def test_english_for_her(self):
        assert sp._user_text_gender_signal("Looking for her birthday gift") == "women"

    def test_no_signal(self):
        assert sp._user_text_gender_signal("볼드한 부츠 찾아줘") is None
        assert sp._user_text_gender_signal("black hoodie please") is None

    def test_empty(self):
        assert sp._user_text_gender_signal("") is None
        assert sp._user_text_gender_signal(None) is None


class TestSwapGenderToken:
    def test_swap_men_to_women(self):
        assert sp._swap_gender_token("bold boots men", "men", "women") == "bold boots women"

    def test_swap_case_insensitive(self):
        assert sp._swap_gender_token("Bold Boots MEN", "men", "women") == "Bold Boots women"

    def test_no_op_when_same(self):
        assert sp._swap_gender_token("bold boots men", "men", "men") == "bold boots men"

    def test_substring_not_swapped(self):
        # 'menswear' must NOT become 'womenswear' — only whole-word `men` swaps.
        assert sp._swap_gender_token("menswear shop men", "men", "women") == "menswear shop women"


@pytest.mark.asyncio
async def test_llm_gender_overridden_by_pinned_profile_when_user_silent(monkeypatch):
    """The smoking-gun case: LLM put 'men' but user's raw message had no
    gender signal AND profile is pinned to 'women' → embedded text must be
    rewritten to 'women'."""
    captured: dict[str, str] = {}

    async def spy_embed_text(q: str):
        captured["embed_text"] = q
        return [0.1] * 768

    async def stub_rpc(fn_name, params):
        return [{"id": "p1", "name": "Boot", "brand": "X", "distance": 0.1, "degraded": False}]

    async def stub_diversify(state):
        state.final_candidates = list(state.raw_candidates or [])
        return state

    monkeypatch.setattr("app.pipeline.embed.EmbedProvider.embed_text", staticmethod(spy_embed_text))
    monkeypatch.setattr("app.pipeline.search.DatabaseProvider.rpc", staticmethod(stub_rpc))
    monkeypatch.setattr("app.pipeline.diversify.diversify_step", stub_diversify)
    monkeypatch.setattr("app.channels.pre_messages.fire_pre_message", AsyncMock())
    # Profile says women (pinned earlier via gender card).
    monkeypatch.setattr(sp, "_lookup_profile_gender", lambda ctx: "women")

    args = {"text_query": "bold chunky boots men"}  # LLM hallucinated 'men'
    ctx = {
        "chat_id": 7,
        "user_key": "u:7",
        # Raw user message has NO gender signal — the override condition.
        "text_query": "볼드한 부츠 찾아줘! 발렌시아가 스톰퍼랑 비슷한 느낌으로",
    }
    await sp.dispatch(args, ctx)

    embedded = captured["embed_text"]
    assert "women" in embedded
    assert "men" not in embedded.split()  # whole-word check


@pytest.mark.asyncio
async def test_llm_gender_kept_when_user_explicitly_signalled(monkeypatch):
    """Per-request override semantics preserved — when the user's raw message
    explicitly mentions a gender (gift mode etc.), trust the LLM verbatim
    even when it differs from the pinned profile."""
    captured: dict[str, str] = {}

    async def spy_embed_text(q: str):
        captured["embed_text"] = q
        return [0.1] * 768

    monkeypatch.setattr("app.pipeline.embed.EmbedProvider.embed_text", staticmethod(spy_embed_text))
    monkeypatch.setattr("app.pipeline.search.DatabaseProvider.rpc", staticmethod(AsyncMock(return_value=[])))

    async def stub_div(state):
        state.final_candidates = []
        return state

    monkeypatch.setattr("app.pipeline.diversify.diversify_step", stub_div)
    monkeypatch.setattr("app.channels.pre_messages.fire_pre_message", AsyncMock())
    # Profile says women, but user is shopping for their husband.
    monkeypatch.setattr(sp, "_lookup_profile_gender", lambda ctx: "women")

    args = {"text_query": "leather wallet men"}
    ctx = {
        "chat_id": 7,
        "user_key": "u:7",
        "text_query": "남편 줄 지갑 추천해줘",  # explicit gift signal for him
    }
    await sp.dispatch(args, ctx)

    embedded = captured["embed_text"]
    assert "men" in embedded.split()  # LLM gender kept, NOT overridden


@pytest.mark.asyncio
async def test_llm_gender_kept_when_no_profile_pin(monkeypatch):
    """When profile has NO pinned gender, LLM-supplied gender is trusted
    (best-effort fallback) — no override happens."""
    captured: dict[str, str] = {}

    async def spy_embed_text(q: str):
        captured["embed_text"] = q
        return [0.1] * 768

    monkeypatch.setattr("app.pipeline.embed.EmbedProvider.embed_text", staticmethod(spy_embed_text))
    monkeypatch.setattr("app.pipeline.search.DatabaseProvider.rpc", staticmethod(AsyncMock(return_value=[])))

    async def stub_div(state):
        state.final_candidates = []
        return state

    monkeypatch.setattr("app.pipeline.diversify.diversify_step", stub_div)
    monkeypatch.setattr("app.channels.pre_messages.fire_pre_message", AsyncMock())
    monkeypatch.setattr(sp, "_lookup_profile_gender", lambda ctx: None)

    args = {"text_query": "bold boots men"}
    ctx = {"chat_id": 7, "user_key": "u:7", "text_query": "볼드한 부츠"}
    await sp.dispatch(args, ctx)

    embedded = captured["embed_text"]
    # No pinned profile to override with — LLM gender stays.
    assert "men" in embedded.split()
