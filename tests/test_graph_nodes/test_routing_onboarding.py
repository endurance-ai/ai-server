"""SPEC-ONBOARD-CARDS-001 / ONB-T18 — onboarding routing predicates.

Covers `onboarding_required`, `is_continuous_pinterest`, `_is_restart_keyword`,
`_resolve_onboard_stage_target`, and the gated `_route_after_ingest` behavior.

@MX:SPEC: SPEC-ONBOARD-CARDS-001
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import HttpUrl

from app.channels.schemas import ChannelMessage
from app.graphs.routing import (
    _is_restart_keyword,
    _resolve_onboard_stage_target,
    _route_after_ingest,
    is_continuous_pinterest,
    onboarding_required,
)
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import InMemorySessionStore, set_store


@pytest.fixture(autouse=True)
def _store():
    s = InMemorySessionStore()
    set_store(s)
    yield s


def _msg(**kw) -> ChannelMessage:
    base: dict = {"chat_id": 7, "received_at": datetime.now(tz=UTC)}
    if "urls" in kw:
        kw["urls"] = [HttpUrl(u) if not isinstance(u, HttpUrl) else u for u in kw["urls"]]
    base.update(kw)
    return ChannelMessage(**base)


def _state(**kw) -> WorkingState:
    message = kw.pop("message", _msg(text="hi"))
    return WorkingState(message=message, chat_id=7, **kw)


# ── _is_restart_keyword ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "/reset",
        "/RESET",
        "  /reset  ",
        "온보딩 다시",
        "취향 다시 설정",
        "reset taste",
        "Reset Taste",
    ],
)
def test_restart_keyword_matches(text: str):
    assert _is_restart_keyword(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        None,
        "/reset now",
        "온보딩 다시 하기 싫어",  # extra suffix → no match (exact-trim semantics)
        "hello world",
        "/start",
    ],
)
def test_restart_keyword_no_match(text):
    assert _is_restart_keyword(text) is False


# ── onboarding_required ──────────────────────────────────────────────────────


def test_onboarding_required_fresh_user_with_text(_store):
    sess = _store.get_or_create(7)
    state = _state(message=_msg(text="hi"))
    assert onboarding_required(state, sess) is True


def test_onboarding_required_returns_false_when_flag_disabled(_store, monkeypatch):
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "ONBOARDING_CARDS_ENABLED", False)
    sess = _store.get_or_create(7)
    state = _state(message=_msg(text="hi"))
    assert onboarding_required(state, sess) is False


def test_onboarding_required_restart_keyword_even_when_onboarded(_store):
    sess = _store.get_or_create(7)
    sess.onboarded_at = datetime.now(tz=UTC)
    state = _state(message=_msg(text="/reset"))
    assert onboarding_required(state, sess) is True


def test_onboarding_required_resume_mid_flow(_store):
    sess = _store.get_or_create(7)
    sess.onboarded_at = datetime.now(tz=UTC)
    sess.onboard_stage = "mood"
    state = _state(message=_msg(text="anything"))
    assert onboarding_required(state, sess) is True


def test_onboarding_required_onboard_callback_routes_through_gate(_store):
    sess = _store.get_or_create(7)
    sess.onboarded_at = datetime.now(tz=UTC)
    state = _state(message=_msg(callback_data="onboard:mood:toggle:cozy", callback_query_id="q"))
    assert onboarding_required(state, sess) is True


def test_onboarding_required_onboarded_user_normal_text_skips_gate(_store):
    sess = _store.get_or_create(7)
    sess.onboarded_at = datetime.now(tz=UTC)
    state = _state(message=_msg(text="show me oversized tees"))
    assert onboarding_required(state, sess) is False


# ── is_continuous_pinterest ──────────────────────────────────────────────────


def test_continuous_pinterest_true_for_onboarded_user_with_pin_url(_store, monkeypatch):
    # 사용자 피드백 — continuous bootstrap 기본 OFF. 명시 활성화 시에만 True.
    monkeypatch.setattr("app.graphs.routing.settings.PINTEREST_CONTINUOUS_ENABLED", True, raising=False)
    sess = _store.get_or_create(7)
    sess.onboarded_at = datetime.now(tz=UTC)
    sess.onboard_stage = "done"
    state = _state(message=_msg(text="https://www.pinterest.com/pin/123456789/"))
    assert is_continuous_pinterest(state, sess) is True


def test_continuous_pinterest_false_by_default_flag(_store):
    # 기본값 False — 온보딩 끝난 유저의 핀 URL 은 일반 검색 흐름으로 가야 함.
    sess = _store.get_or_create(7)
    sess.onboarded_at = datetime.now(tz=UTC)
    sess.onboard_stage = "done"
    state = _state(message=_msg(text="https://www.pinterest.com/pin/123456789/"))
    assert is_continuous_pinterest(state, sess) is False


def test_continuous_pinterest_false_when_not_onboarded(_store):
    sess = _store.get_or_create(7)
    state = _state(message=_msg(text="https://www.pinterest.com/pin/1/"))
    assert is_continuous_pinterest(state, sess) is False


def test_continuous_pinterest_false_when_in_onboarding(_store):
    sess = _store.get_or_create(7)
    sess.onboarded_at = datetime.now(tz=UTC)
    sess.onboard_stage = "mood"
    state = _state(message=_msg(text="https://www.pinterest.com/pin/1/"))
    assert is_continuous_pinterest(state, sess) is False


def test_continuous_pinterest_false_when_text_not_pinterest(_store):
    sess = _store.get_or_create(7)
    sess.onboarded_at = datetime.now(tz=UTC)
    state = _state(message=_msg(text="just a normal sentence"))
    assert is_continuous_pinterest(state, sess) is False


def test_continuous_pinterest_false_within_rate_limit(_store, monkeypatch):
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "PINTEREST_CONTINUOUS_RATELIMIT_S", 300)
    sess = _store.get_or_create(7)
    sess.onboarded_at = datetime.now(tz=UTC) - timedelta(days=1)
    sess.last_pinterest_scrape_at = datetime.now(tz=UTC) - timedelta(minutes=2)
    sess.onboard_stage = None
    state = _state(message=_msg(text="https://www.pinterest.com/pin/9999/"))
    assert is_continuous_pinterest(state, sess) is False


def test_continuous_pinterest_true_when_rate_limit_window_expired(_store, monkeypatch):
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "PINTEREST_CONTINUOUS_RATELIMIT_S", 300)
    monkeypatch.setattr(cfg.settings, "PINTEREST_CONTINUOUS_ENABLED", True)
    sess = _store.get_or_create(7)
    sess.onboarded_at = datetime.now(tz=UTC) - timedelta(days=1)
    sess.last_pinterest_scrape_at = datetime.now(tz=UTC) - timedelta(minutes=10)
    sess.onboard_stage = None
    state = _state(message=_msg(text="https://www.pinterest.com/pin/9999/"))
    assert is_continuous_pinterest(state, sess) is True


# ── _resolve_onboard_stage_target ────────────────────────────────────────────


@pytest.mark.parametrize(
    "cb_data,expected",
    [
        ("onboard:mood:toggle:cozy", "onboard_mood"),
        ("onboard:color:done", "onboard_color"),
        ("onboard:fit:skip", "onboard_fit"),
        ("onboard:pinterest:skip", "onboard_pinterest"),
        ("onboard:restart:yes", "onboard_intro"),
    ],
)
def test_resolve_onboard_stage_target_by_callback(_store, cb_data: str, expected: str):
    sess = _store.get_or_create(7)
    state = _state(message=_msg(callback_data=cb_data, callback_query_id="q"))
    assert _resolve_onboard_stage_target(sess, state) == expected


def test_resolve_onboard_stage_target_by_session_resume(_store):
    sess = _store.get_or_create(7)
    sess.onboard_stage = "fit"
    state = _state(message=_msg(text="hello"))
    assert _resolve_onboard_stage_target(sess, state) == "onboard_fit"


def test_resolve_onboard_stage_target_defaults_to_intro(_store):
    sess = _store.get_or_create(7)
    state = _state(message=_msg(text="hi"))
    assert _resolve_onboard_stage_target(sess, state) == "onboard_intro"


# ── _route_after_ingest gate priorities ──────────────────────────────────────


def test_route_after_ingest_onboarding_wins_over_text_routing(_store):
    _store.get_or_create(7)  # ensure session exists; gate predicate looks it up
    # Fresh user — should land in onboard_intro, NOT respond / router_text.
    state = _state(message=_msg(text="hello"))
    assert _route_after_ingest(state) == "onboard_intro"


def test_route_after_ingest_continuous_pinterest_for_onboarded(_store, monkeypatch):
    # PINTEREST_CONTINUOUS_ENABLED=True 일 때만 pinterest_ingest 로 라우팅.
    monkeypatch.setattr("app.graphs.routing.settings.PINTEREST_CONTINUOUS_ENABLED", True, raising=False)
    sess = _store.get_or_create(7)
    sess.onboarded_at = datetime.now(tz=UTC) - timedelta(days=1)
    sess.onboard_stage = None
    state = _state(message=_msg(text="https://www.pinterest.com/pin/777/"))
    assert _route_after_ingest(state) == "pinterest_ingest"


def test_route_after_ingest_normal_flow_still_works_when_onboarded(_store):
    """DDD PRESERVE — onboarded user with photo → resolve_image (no onboarding)."""
    sess = _store.get_or_create(7)
    sess.onboarded_at = datetime.now(tz=UTC)
    state = _state(message=_msg(photo_file_id="fid"))
    assert _route_after_ingest(state) == "resolve_image"


def test_route_after_ingest_onboard_callback_resumes_mid_flow(_store):
    _store.get_or_create(7)
    state = _state(message=_msg(callback_data="onboard:color:toggle:beige", callback_query_id="q"))
    assert _route_after_ingest(state) == "onboard_color"
