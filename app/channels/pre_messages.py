"""SPEC-AGENT-UX-P0-001 v0.2.0 / REQ-UX-004 — Pre-action messages.

1~2초 이상 걸리는 작업 직전에 사용자에게 보낼 의도-안내 멘트 단일 소스.
typing indicator(REQ-UX-003) 만으로는 "뭐가 진행 중인지" 인지가 약하다는
베타 피드백을 반영해, 다음 5개 entry point 직전에 고정 한국어/영어 안내
멘트를 먼저 발송한다 (firing order: pre-message → typing → 본 작업).

Firing sites (현 구현 기준):
- `app/graphs/nodes/vision.py`        → key="vision"
- `app/agents/tools/analyze_image.py`   → key="analyze_image"
- `search`: 2026-08-26 발사 중단 — 스피너/typing 이 이미 검색 중임을 보여줘
  "잠깐만, …찾아볼게" 멘트가 잉여였다(사용자 피드백). `pinterest` 와 동일하게
  키/값은 dict 에 보존(재도입 대비)하되 search_products / refine_search 는 더 이상
  발사하지 않는다.
- `pinterest`: PRE_MESSAGES 에 키는 보존 (SPEC contract). 단 현재 코드베이스에는
  pinterest_ingest 그래프 노드가 존재하지 않아 (SPEC-ONBOARD-LITE-001 §4 에서
  제거됨) 런타임 firing site 가 없다. 추후 Pinterest 스크랩 경로 재도입 시
  이 dict 의 "pinterest" 키를 그대로 사용한다.

Drift 정책: dict 키/값은 SPEC-level 변경. snapshot 테스트가 키 셋트와 값
비-빈 여부를 잠근다.

@MX:ANCHOR: [AUTO] Single source for fixed user-facing pre-action wording.
@MX:REASON: SPEC-AGENT-UX-P0-001 REQ-UX-004 잠금 — wording drift 는 SPEC version bump.
@MX:SPEC: SPEC-AGENT-UX-P0-001
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from app.channels.adapter import MessengerAdapter

logger = logging.getLogger(__name__)


# Single source of truth — KO/EN fixed wording.
# 키 셋트: vision / search / pinterest / analyze_image. 변경은 SPEC version bump.
PRE_MESSAGES: dict[str, dict[str, str]] = {
    "vision": {
        "ko": "사진 잘 봤어, 잠깐 들여다볼게.",
        "en": "Got it. Let me take a closer look.",
    },
    "search": {
        "ko": "잠깐만, 마음에 들 만한 거 찾아볼게.",
        "en": "One sec, let me find something you'll love.",
    },
    "pinterest": {
        "ko": "보드 살펴볼게, 잠깐만.",
        "en": "Checking out your board, hang tight.",
    },
    "analyze_image": {
        "ko": "이미지 들여다볼게.",
        "en": "Looking at this image.",
    },
}


def _marker_key(key: str) -> str:
    """Idempotency marker key for ctx / state dict."""
    return f"_pre_msg_sent:{key}"


# Module-level 노드용 per-turn marker 저장소. `WorkingState` 가 extra="forbid"
# 이라 직접 필드를 추가할 수 없어, thread_id(UUID, 턴마다 유일) 를 키로 사용.
# LangGraph 가 같은 turn 안에서 노드를 re-execute 해도 같은 thread_id 면 중복 차단.
# 메모리 누수 방지: 64 thread 초과 시 가장 오래된 절반 폐기 (간단 LRU 근사).
_NODE_MARKERS: dict[UUID, set[str]] = {}
_NODE_MARKERS_MAX = 64


def _node_marker_get(thread_id: UUID, key: str) -> bool:
    sent = _NODE_MARKERS.get(thread_id)
    return sent is not None and key in sent


def _node_marker_set(thread_id: UUID, key: str) -> None:
    if thread_id not in _NODE_MARKERS and len(_NODE_MARKERS) >= _NODE_MARKERS_MAX:
        # 가장 오래된 절반 폐기 (insertion order — Python dict 보장).
        drop_n = _NODE_MARKERS_MAX // 2
        for old_tid in list(_NODE_MARKERS.keys())[:drop_n]:
            _NODE_MARKERS.pop(old_tid, None)
    _NODE_MARKERS.setdefault(thread_id, set()).add(key)


async def fire_pre_message(
    adapter: MessengerAdapter | None,
    marker_store: dict[str, Any] | None,
    *,
    key: str,
    lang: str,
    chat_id: int | None,
    thread_id: UUID | None = None,
) -> bool:
    """고정 사전 안내 멘트 1회 발사 — idempotent, fail-open.

    - `key`: `PRE_MESSAGES` 의 키 중 하나 (vision / search / pinterest / analyze_image).
    - `lang`: "ko" / "en". 다른 값이면 "en" fallback.
    - `marker_store`: tools 는 `ctx` dict 전달. graph 노드는 None 전달 +
      `thread_id` 로 module-level 마커 사용.
    - `chat_id`: 호출자가 session/state 에서 추출.
    - `thread_id`: 그래프 노드용 — module-level 마커 키. None 이면 노드 마커 skip.

    절대 raise 하지 않는다. send 실패 시 DEBUG 1줄.
    반환: 실제로 send_text 가 성공한 경우 True, 그 외 False.
    """
    if adapter is None or not chat_id:
        return False

    text_map = PRE_MESSAGES.get(key)
    if not text_map:
        logger.debug("pre_message: unknown key=%r — skipped", key)
        return False
    text = text_map.get(lang) or text_map["en"]

    marker = _marker_key(key)
    # Idempotency check — tools 와 nodes 양쪽 모두.
    if marker_store is not None and marker_store.get(marker):
        return False
    if thread_id is not None and _node_marker_get(thread_id, key):
        return False

    try:
        await adapter.send_text(int(chat_id), text)
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("pre_message send failed key=%s lang=%s: %r", key, lang, exc)
        return False

    # 성공 시에만 marker set — 실패는 다음 dispatch 에서 재시도 가능.
    if marker_store is not None:
        marker_store[marker] = True
    if thread_id is not None:
        _node_marker_set(thread_id, key)
    return True


def _reset_node_markers_for_tests() -> None:
    """Test 격리용 — module-level 마커 비움. 운영 코드에서 호출 금지."""
    _NODE_MARKERS.clear()
