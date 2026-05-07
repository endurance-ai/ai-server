"""SPEC-AGENTIC-CRITIQUE-001 — evaluator node unit tests.

Covers REQ-CRITIQUE-EVAL-001..003, REQ-CRITIQUE-EMPTY-001 (fast-path), and
REQ-CRITIQUE-LOOP-SAFETY-002 (deltas_equivalent helper).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from app.channels.schemas import ChannelMessage
from app.channels.session import InMemorySessionStore, set_store
from app.graphs.nodes import evaluator as evaluator_module
from app.graphs.nodes._evaluator_models import (
    CritiqueDelta,
    CritiqueScore,
    deltas_equivalent,
)
from app.graphs.nodes.evaluator import evaluator
from app.graphs.state import WorkingState


@pytest.fixture(autouse=True)
def _reset_store():
    set_store(InMemorySessionStore())
    yield


def _msg() -> ChannelMessage:
    return ChannelMessage(chat_id=42, received_at=datetime.now(tz=UTC))


def _state(**kw) -> WorkingState:
    return WorkingState(message=_msg(), chat_id=42, **kw)


# ── Pydantic schema (REQ-CRITIQUE-EVAL-002) ────────────────────────────────


class TestCritiqueScoreSchema:
    def test_construct_with_full_delta(self):
        score = CritiqueScore(
            score=0.4,
            reasoning="too narrow",
            suggested_delta=CritiqueDelta(intent="broaden", drop_filters=["min_price"]),
            retry=True,
        )
        assert score.retry is True
        assert score.suggested_delta.intent == "broaden"

    def test_score_range_validated(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            CritiqueScore(score=1.5, retry=False)
        with pytest.raises(ValidationError):
            CritiqueScore(score=-0.1, retry=False)

    def test_no_retry_allows_none_delta(self):
        score = CritiqueScore(score=0.85, reasoning="good", retry=False)
        assert score.suggested_delta is None

    def test_extra_field_forbidden(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            CritiqueScore.model_validate({"score": 0.5, "retry": False, "unknown_field": "x"})


# ── deltas_equivalent (REQ-CRITIQUE-LOOP-SAFETY-002) ──────────────────────


class TestDeltasEquivalent:
    def test_identical_deltas_are_equivalent(self):
        a = CritiqueDelta(intent="broaden", exclude_brands=["zara", "ami"])
        b = CritiqueDelta(intent="broaden", exclude_brands=["zara", "ami"])
        assert deltas_equivalent(a, b)

    def test_order_does_not_matter(self):
        a = CritiqueDelta(intent="broaden", exclude_brands=["zara", "ami"])
        b = CritiqueDelta(intent="broaden", exclude_brands=["ami", "zara"])
        assert deltas_equivalent(a, b)

    def test_case_insensitive_normalization(self):
        a = CritiqueDelta(intent="broaden", exclude_brands=["Zara"])
        b = CritiqueDelta(intent="broaden", exclude_brands=["zara"])
        assert deltas_equivalent(a, b)

    def test_different_intent_not_equivalent(self):
        a = CritiqueDelta(intent="broaden")
        b = CritiqueDelta(intent="narrow")
        assert not deltas_equivalent(a, b)

    def test_different_drop_filters_not_equivalent(self):
        a = CritiqueDelta(intent="broaden", drop_filters=["min_price"])
        b = CritiqueDelta(intent="broaden", drop_filters=["max_price"])
        assert not deltas_equivalent(a, b)

    def test_none_handling(self):
        assert deltas_equivalent(None, None)
        assert not deltas_equivalent(None, CritiqueDelta(intent="broaden"))


# ── Empty-result fast-path (REQ-CRITIQUE-EMPTY-001) ────────────────────────


class TestEmptyFastPath:
    @pytest.mark.asyncio
    async def test_empty_first_iteration_skips_llm(self, monkeypatch):
        """When candidates=[] AND retry_count=0, evaluator MUST NOT call LLM."""
        called = []

        async def _spy(prompt):
            called.append(prompt)
            return CritiqueScore(score=1.0, retry=False)

        monkeypatch.setattr(evaluator_module, "_call_llm", _spy)

        delta = await evaluator(_state(candidates=[], critique_retry_count=0))

        assert called == []  # LLM NOT called
        assert delta["critique_pending_delta"] is not None
        assert delta["critique_pending_delta"].intent == "broaden"
        # Trail entry source = fast_path
        trail = delta["critique_trail"]
        assert trail[0]["source"] == "fast_path"

    @pytest.mark.asyncio
    async def test_empty_second_iteration_does_call_llm(self, monkeypatch):
        """REQ-CRITIQUE-EMPTY-001 — the fast-path fires only once per turn."""
        calls = []

        async def _spy(prompt):
            calls.append(prompt)
            return CritiqueScore(score=0.2, retry=False, reasoning="still empty")

        monkeypatch.setattr(evaluator_module, "_call_llm", _spy)

        await evaluator(_state(candidates=[], critique_retry_count=1))
        assert len(calls) == 1  # LLM called on iteration 1


# ── Fail-open paths (REQ-CRITIQUE-EVAL-003) ───────────────────────────────


@pytest.mark.no_evaluator_fail_open
class TestFailOpen:
    @pytest.mark.asyncio
    async def test_timeout_returns_fail_open(self, monkeypatch):
        async def _timeout(*a, **k):
            raise TimeoutError("simulated")

        monkeypatch.setattr(
            "app.graphs.nodes.evaluator.LLMProvider.chat",
            _timeout,
        )
        # Need at least 1 candidate so we don't trigger fast-path
        delta = await evaluator(_state(candidates=[object()], critique_retry_count=0))
        # Score=1.0, no retry, no pending delta
        trail = delta["critique_trail"]
        assert trail[0]["score"] == 1.0
        assert delta["critique_pending_delta"] is None

    @pytest.mark.asyncio
    async def test_http_error_returns_fail_open(self, monkeypatch):
        async def _http_err(*a, **k):
            raise httpx.HTTPError("boom")

        monkeypatch.setattr(
            "app.graphs.nodes.evaluator.LLMProvider.chat",
            _http_err,
        )
        delta = await evaluator(_state(candidates=[object()], critique_retry_count=0))
        assert delta["critique_trail"][0]["score"] == 1.0
        assert delta["critique_pending_delta"] is None

    @pytest.mark.asyncio
    async def test_malformed_json_returns_fail_open(self, monkeypatch):
        async def _bad_json(*a, **k):
            return {"choices": [{"message": {"content": "not valid json"}}]}

        monkeypatch.setattr(
            "app.graphs.nodes.evaluator.LLMProvider.chat",
            _bad_json,
        )
        delta = await evaluator(_state(candidates=[object()], critique_retry_count=0))
        assert delta["critique_trail"][0]["score"] == 1.0

    @pytest.mark.asyncio
    async def test_out_of_range_score_returns_fail_open(self, monkeypatch):
        async def _bad_range(*a, **k):
            return {"choices": [{"message": {"content": json.dumps({"score": 1.5, "retry": False})}}]}

        monkeypatch.setattr(
            "app.graphs.nodes.evaluator.LLMProvider.chat",
            _bad_range,
        )
        delta = await evaluator(_state(candidates=[object()], critique_retry_count=0))
        assert delta["critique_trail"][0]["score"] == 1.0


# ── LLM happy path ─────────────────────────────────────────────────────────


@pytest.mark.no_evaluator_fail_open
class TestLLMHappyPath:
    @pytest.mark.asyncio
    async def test_high_score_no_retry(self, monkeypatch):
        async def _ok(*a, **k):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "score": 0.85,
                                    "reasoning": "great match",
                                    "retry": False,
                                    "suggested_delta": None,
                                }
                            )
                        }
                    }
                ]
            }

        monkeypatch.setattr(
            "app.graphs.nodes.evaluator.LLMProvider.chat",
            _ok,
        )
        delta = await evaluator(_state(candidates=[object()], critique_retry_count=0))
        assert delta["critique_trail"][0]["score"] == 0.85
        assert delta["critique_pending_delta"] is None

    @pytest.mark.asyncio
    async def test_low_score_retry_with_delta(self, monkeypatch):
        async def _retry(*a, **k):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "score": 0.3,
                                    "reasoning": "off-target",
                                    "retry": True,
                                    "suggested_delta": {
                                        "intent": "broaden",
                                        "drop_filters": ["min_price"],
                                    },
                                }
                            )
                        }
                    }
                ]
            }

        monkeypatch.setattr(
            "app.graphs.nodes.evaluator.LLMProvider.chat",
            _retry,
        )
        delta = await evaluator(_state(candidates=[object()], critique_retry_count=0))
        assert delta["critique_pending_delta"] is not None
        assert delta["critique_pending_delta"].intent == "broaden"
        assert delta["critique_trail"][0]["score"] == 0.3
