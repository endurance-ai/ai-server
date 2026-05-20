"""SPEC-AGENT-UX-P0-001 v0.2.0 / REQ-UX-004 — pre_messages 단일 소스 + helper.

검증:
- PRE_MESSAGES 4 키 × KO/EN 비-빈 문자열 (snapshot/shape 잠금).
- fire_pre_message — key/lang 별 send_text 호출 + 마커 set + idempotency.
- fail-open: send_text raise → return False, DEBUG 로그만, 마커 미설정 (재시도 가능).
- AST/source scan: app/ 안에서 PRE_MESSAGES / fire_pre_message import 가
  허용된 4개 firing site + 자기 자신만 갖는다.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Any
from uuid import uuid4

import httpx
import pytest

from app.channels.adapter import MessengerAdapter
from app.channels.pre_messages import (
    PRE_MESSAGES,
    _reset_node_markers_for_tests,
    fire_pre_message,
)
from app.channels.schemas import BotCard, ChannelMessage


class _SpyAdapter(MessengerAdapter):
    def __init__(self) -> None:
        self.texts: list[tuple[int, str]] = []

    async def parse_inbound(self, payload: dict) -> ChannelMessage:  # pragma: no cover
        raise NotImplementedError

    async def send_text(self, chat_id: int, text: str) -> None:
        self.texts.append((chat_id, text))

    async def send_card(self, chat_id: int, card: BotCard) -> int | None:  # pragma: no cover
        return None


class _RaisingAdapter(MessengerAdapter):
    async def parse_inbound(self, payload: dict) -> ChannelMessage:  # pragma: no cover
        raise NotImplementedError

    async def send_text(self, chat_id: int, text: str) -> None:
        raise httpx.TimeoutException("boom")

    async def send_card(self, chat_id: int, card: BotCard) -> int | None:  # pragma: no cover
        return None


@pytest.fixture(autouse=True)
def _reset_markers() -> None:
    _reset_node_markers_for_tests()


# ── PRE_MESSAGES dict shape snapshot ──────────────────────────────────────


def test_pre_messages_shape_snapshot() -> None:
    """4 키 정확히, 각 키마다 ko/en 둘 다 비-빈 문자열."""
    assert set(PRE_MESSAGES.keys()) == {"vision", "search", "pinterest", "analyze_image"}
    for key, langs in PRE_MESSAGES.items():
        assert set(langs.keys()) == {"ko", "en"}, f"key={key} missing ko/en"
        assert langs["ko"].strip(), f"key={key} ko empty"
        assert langs["en"].strip(), f"key={key} en empty"


# ── fire_pre_message — KO / EN matrix ─────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["vision", "search", "pinterest", "analyze_image"])
@pytest.mark.parametrize("lang", ["ko", "en"])
async def test_fire_pre_message_sends_correct_text(key: str, lang: str) -> None:
    spy = _SpyAdapter()
    ctx: dict[str, Any] = {}
    ok = await fire_pre_message(spy, ctx, key=key, lang=lang, chat_id=42)
    assert ok is True
    assert spy.texts == [(42, PRE_MESSAGES[key][lang])]
    assert ctx[f"_pre_msg_sent:{key}"] is True


@pytest.mark.asyncio
async def test_fire_pre_message_unknown_lang_falls_back_to_en() -> None:
    spy = _SpyAdapter()
    ctx: dict[str, Any] = {}
    ok = await fire_pre_message(spy, ctx, key="search", lang="ja", chat_id=42)
    assert ok is True
    assert spy.texts == [(42, PRE_MESSAGES["search"]["en"])]


# ── Idempotency ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fire_pre_message_idempotent_via_ctx() -> None:
    spy = _SpyAdapter()
    ctx: dict[str, Any] = {}
    await fire_pre_message(spy, ctx, key="search", lang="ko", chat_id=42)
    await fire_pre_message(spy, ctx, key="search", lang="ko", chat_id=42)
    assert len(spy.texts) == 1  # 두 번째는 marker 로 차단


@pytest.mark.asyncio
async def test_fire_pre_message_idempotent_via_thread_id() -> None:
    spy = _SpyAdapter()
    tid = uuid4()
    await fire_pre_message(spy, None, key="vision", lang="ko", chat_id=42, thread_id=tid)
    await fire_pre_message(spy, None, key="vision", lang="ko", chat_id=42, thread_id=tid)
    assert len(spy.texts) == 1
    # 다른 thread_id 면 다시 발사.
    await fire_pre_message(spy, None, key="vision", lang="ko", chat_id=42, thread_id=uuid4())
    assert len(spy.texts) == 2


# ── Fail-open ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fire_pre_message_fail_open_swallow(caplog: pytest.LogCaptureFixture) -> None:
    raising = _RaisingAdapter()
    ctx: dict[str, Any] = {}
    with caplog.at_level(logging.DEBUG, logger="app.channels.pre_messages"):
        ok = await fire_pre_message(raising, ctx, key="vision", lang="ko", chat_id=42)
    assert ok is False
    # marker 미설정 → 재시도 가능.
    assert ctx.get("_pre_msg_sent:vision") is None
    # WARN/ERROR 없음, DEBUG 1줄 이상.
    assert all(r.levelname not in ("WARNING", "ERROR") for r in caplog.records)
    assert any(r.levelname == "DEBUG" and "pre_message send failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_fire_pre_message_no_adapter_returns_false() -> None:
    ctx: dict[str, Any] = {}
    ok = await fire_pre_message(None, ctx, key="search", lang="ko", chat_id=42)
    assert ok is False
    assert ctx == {}


@pytest.mark.asyncio
async def test_fire_pre_message_no_chat_id_returns_false() -> None:
    spy = _SpyAdapter()
    ctx: dict[str, Any] = {}
    ok = await fire_pre_message(spy, ctx, key="search", lang="ko", chat_id=None)
    assert ok is False
    assert spy.texts == []


@pytest.mark.asyncio
async def test_fire_pre_message_unknown_key_returns_false(caplog: pytest.LogCaptureFixture) -> None:
    spy = _SpyAdapter()
    ctx: dict[str, Any] = {}
    with caplog.at_level(logging.DEBUG, logger="app.channels.pre_messages"):
        ok = await fire_pre_message(spy, ctx, key="bogus", lang="ko", chat_id=42)
    assert ok is False
    assert spy.texts == []
    assert any("unknown key" in r.message for r in caplog.records)


# ── AST / source scan: import site allow-list ─────────────────────────────


def test_pre_messages_import_sites_allow_list() -> None:
    """REQ-UX-004: PRE_MESSAGES / fire_pre_message import 는 허용된 site 만 가능.

    pinterest_ingest 노드는 현재 부재 (SPEC-ONBOARD-LITE-001 §4). 재도입 시
    이 allow-list 에 `app/graphs/nodes/pinterest_ingest.py` 추가하고 firing
    hook 한 줄을 노드 진입부에 박을 것.
    """
    allowed_runtime = {
        "app/channels/pre_messages.py",  # self
        "app/graphs/nodes/vision.py",
        "app/agents/tools/search_products.py",
        "app/agents/tools/refine_search.py",
        "app/agents/tools/analyze_image.py",
    }
    root = pathlib.Path(__file__).resolve().parents[2]
    offenders: list[str] = []
    for path in (root / "app").rglob("*.py"):
        rel = str(path.relative_to(root))
        src = path.read_text(encoding="utf-8")
        if "pre_messages" not in src:
            continue
        if rel not in allowed_runtime:
            offenders.append(rel)
    assert offenders == [], f"Unauthorized pre_messages import: {offenders}"
