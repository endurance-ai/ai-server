"""SPEC-AGENT-001 / REQ-AGENT-004 (node 4/10) — pick_item.

Two paths:
1. Multi-item vision result, no current selection → render the picker carousel
   via the channel adapter, leave `selected_item_index=None` so routing emits
   `__end__` (REQ-AGENT-010 — picker-sent-only path bypasses respond).
2. Callback `item:N` → resolve N against `detected_items`, write
   `selected_item_index`, persist the selection in SessionStore (vision_item /
   vision_keywords / state=AWAITING_INTENT) so subsequent webhooks can refer
   to it. Routing then sends us to `critique_apply`.

Side effects (adapter calls, session writes) are deliberate — they preserve
the existing scenario.py behavior (REQ-COMPAT-*).
"""

from __future__ import annotations

import logging
import re

from app.channels.lang import session_lang
from app.channels.vision import derive_legacy_keywords, derive_legacy_label
from app.graphs.nodes._adapter_ctx import get_adapter
from app.graphs.nodes._trace import node_done, node_enter, node_skip
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import SessionState, get_store
from app.infrastructure.memory.taste_profile import user_key_for
from app.infrastructure.repositories.category_family import to_canonical_family
from app.observability.conversation_log import emit
from app.observability.langfuse import observe

logger = logging.getLogger(__name__)

# EN scaffold (default).
PICKER_HEADER_EN = "I see {n} item{s} in this photo 👀\n\n{lines}\n\nWhich one are you after? Tap below 👇"
PICKER_LINE_EN = "{num}  {label} — {desc}"
PICK_INVALID_EN = "Tap one of the buttons above to choose an item 👆"

# KO scaffold — kiko persona, friendly 해요체.
# (KO label format is inlined in `_send_picker` since description is dropped
# in KO mode to avoid mixing languages on one line.)
PICKER_HEADER_KO = "사진에서 {n}개 아이템 발견했어 👀\n\n{lines}\n\n어떤 게 마음에 들어? 아래에서 골라봐 👇"
PICK_INVALID_KO = "위 버튼 중 하나 골라줘 👆"

NUMBER_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]

# Picker button label cap — the wire limit is 256 chars but the
# practical mobile-readable cap is ~40. Truncated labels get a trailing "…".
_LABEL_CAP = 40

# B6 — KO ordinal words mapped to 1-based positions ("첫번째" / "둘째" / etc).
_KO_ORDINALS = {
    "첫": 1,
    "첫번째": 1,
    "첫째": 1,
    "일": 1,
    "두번째": 2,
    "둘째": 2,
    "이": 2,
    "세번째": 3,
    "셋째": 3,
    "삼": 3,
    "네번째": 4,
    "넷째": 4,
    "사": 4,
    "다섯번째": 5,
    "다섯째": 5,
    "오": 5,
}
_EN_ORDINALS = {
    "first": 1,
    "1st": 1,
    "second": 2,
    "2nd": 2,
    "third": 3,
    "3rd": 3,
    "fourth": 4,
    "4th": 4,
    "fifth": 5,
    "5th": 5,
}


def _parse_indices_from_text(text: str, max_index: int) -> list[int]:
    """B6 — extract 0-based item indices from a natural-language reply.

    Accepts:
      "2"             → [1]
      "2번"           → [1]
      "2, 4번"        → [1, 3]
      "1과 3"         → [0, 2]
      "1번이랑 3번"    → [0, 2]
      "두번째"        → [1]
      "first one"     → [0]

    Returns an EMPTY list when nothing parseable is found. Callers decide
    whether to re-send the picker or auto-pick the first parsed index.
    """
    if not text:
        return []
    t = text.strip().lower()
    # 1-based positions to dedupe while preserving order.
    found: list[int] = []
    # (1) explicit digits — also catches "2,4번", "1번이랑 3번", "1 and 3".
    for m in re.finditer(r"\d+", t):
        try:
            n = int(m.group(0))
        except ValueError:
            continue
        if 1 <= n <= max_index and n not in found:
            found.append(n)
    # (2) ordinal words (only when no digit was found — otherwise digits win).
    if not found:
        for word, n in _KO_ORDINALS.items():
            if word in t and 1 <= n <= max_index and n not in found:
                found.append(n)
                break
        for word, n in _EN_ORDINALS.items():
            if word in t and 1 <= n <= max_index and n not in found:
                found.append(n)
                break
    # 1-based → 0-based.
    return [i - 1 for i in found]


# ── Picker 버튼 표시용 짧은 카테고리 라벨 (표시 전용) ──────────────────────
# 이전에는 버튼에 긴 Vision `searchQueryKo`("피티드 크롭 롱슬리브 … 니트 탑
# 여성")를 그대로 노출해 사용자가 매번 읽고 해석해야 했다. 버튼에는 짧은
# 대분류(상의/하의/선글라스/신발 …)만 보여주고, 실제 검색은 콜백 `item:{i}`
# → `detected_items[i]` 의 full searchQuery/keywords 를 그대로 쓰므로(아래
# `pick_item` 콜백 경로) 검색 품질에는 아무 영향이 없다 — 표시만 짧게.

# canonical family(20종) → 짧은 한글 라벨. `to_canonical_family` 가 Vision
# category/subcategory/label 을 이 20종 중 하나로 정규화해 준다.
_FAMILY_LABEL_KO: dict[str, str] = {
    "tops": "상의",
    "bottoms": "하의",
    "dresses": "원피스",
    "outerwear": "아우터",
    "knitwear": "니트",
    "shoes": "신발",
    "sneakers": "스니커즈",
    "bags": "가방",
    "accessories": "액세서리",
    "eyewear": "선글라스",
    "jewelry": "주얼리",
    "headwear": "모자",
    "underwear": "언더웨어",
    "swimwear": "수영복",
    "activewear": "액티브웨어",
    "homeware": "홈웨어",
    "fragrance": "향수",
    "beauty": "뷰티",
    "lifestyle": "라이프스타일",
}

# subcategory → 짧은 한글 라벨. 키는 Vision subcategory enum 과 1:1
# (app/channels/vision_prompt.py `_ENUM_REFERENCE` — Outer/Top/Bottom/Shoes/
# Bag/Dress/Accessories 전체). enum 이 바뀌면 여기도 함께 갱신한다. 여기서
# 못 잡으면 아래 `_short_label` 이 canonical family 라벨(상의/하의/…)로 폴백.
_SUBCATEGORY_LABEL_KO: dict[str, str] = {
    # Outer
    "overcoat": "코트",
    "trench-coat": "트렌치코트",
    "parka": "파카",
    "bomber": "봄버자켓",
    "blazer": "블레이저",
    "cardigan": "가디건",
    "vest": "베스트",
    "anorak": "아노락",
    "leather-jacket": "가죽자켓",
    "denim-jacket": "데님자켓",
    "fleece": "플리스",
    "windbreaker": "바람막이",
    "cape": "케이프",
    "poncho": "판초",
    "shearling": "무스탕",
    "down-jacket": "패딩",
    "field-jacket": "야상",
    "chore-jacket": "워크자켓",
    "overshirt": "오버셔츠",
    "hoodie": "후디",
    # Top
    "t-shirt": "티셔츠",
    "shirt": "셔츠",
    "blouse": "블라우스",
    "polo": "폴로셔츠",
    "sweater": "니트",
    "knit-top": "니트",
    "tank-top": "탱크탑",
    "crop-top": "크롭탑",
    "henley": "헨리넥",
    "turtleneck": "터틀넥",
    "sweatshirt": "맨투맨",
    "rugby-shirt": "럭비셔츠",
    "camisole": "캐미솔",
    # Bottom
    "jeans": "청바지",
    "trousers": "슬랙스",
    "chinos": "치노팬츠",
    "shorts": "반바지",
    "skirt": "스커트",
    "joggers": "조거팬츠",
    "cargo-pants": "카고팬츠",
    "wide-pants": "와이드팬츠",
    "leggings": "레깅스",
    "culottes": "큘롯",
    "sweatpants": "스웨트팬츠",
    # Shoes
    "sneakers": "스니커즈",
    "boots": "부츠",
    "loafers": "로퍼",
    "derby": "더비슈즈",
    "oxford": "옥스퍼드",
    "sandals": "샌들",
    "mules": "뮬",
    "heels": "힐",
    "flats": "플랫슈즈",
    "slides": "슬라이드",
    "chelsea-boots": "첼시부츠",
    "combat-boots": "워커",
    "running-shoes": "러닝화",
    # Bag
    "tote": "토트백",
    "crossbody": "크로스백",
    "backpack": "백팩",
    "clutch": "클러치",
    "shoulder-bag": "숄더백",
    "belt-bag": "벨트백",
    "messenger": "메신저백",
    "bucket-bag": "버킷백",
    "briefcase": "브리프케이스",
    # Dress
    "mini-dress": "미니원피스",
    "midi-dress": "미디원피스",
    "maxi-dress": "맥시원피스",
    "shirt-dress": "셔츠원피스",
    "wrap-dress": "랩원피스",
    "slip-dress": "슬립원피스",
    "knit-dress": "니트원피스",
    # Accessories
    "hat": "모자",
    "cap": "캡모자",
    "scarf": "스카프",
    "belt": "벨트",
    "sunglasses": "선글라스",
    "watch": "시계",
    "necklace": "목걸이",
    "bracelet": "팔찌",
    "ring": "반지",
    "earrings": "귀걸이",
    "tie": "넥타이",
    "gloves": "장갑",
    "socks": "양말",
}

# 같은 라벨이 중복될 때(예: 상의 2개) 색상으로 구분하기 위한 colorFamily→한글.
_COLOR_FAMILY_KO: dict[str, str] = {
    "black": "블랙",
    "white": "화이트",
    "grey": "그레이",
    "gray": "그레이",
    "beige": "베이지",
    "brown": "브라운",
    "navy": "네이비",
    "blue": "블루",
    "green": "그린",
    "red": "레드",
    "pink": "핑크",
    "purple": "퍼플",
    "yellow": "옐로우",
    "orange": "오렌지",
    "cream": "크림",
    "khaki": "카키",
    "ivory": "아이보리",
    "burgundy": "버건디",
}


def _short_label(it: dict, lang: str) -> str:
    """Picker 버튼용 짧은 카테고리 라벨. subcategory 정밀맵 → canonical family 순."""
    sub = (it.get("subcategory") or "").strip().lower()
    if lang == "ko" and sub in _SUBCATEGORY_LABEL_KO:
        return _SUBCATEGORY_LABEL_KO[sub]
    # category 우선, 비어 있으면 subcategory/label 로 family 추론.
    src = (it.get("category") or "").strip() or sub or (it.get("label") or "").strip()
    fam = to_canonical_family(src)
    if lang == "ko":
        if fam != "other" and fam in _FAMILY_LABEL_KO:
            return _FAMILY_LABEL_KO[fam]
        return "아이템"
    # EN — subcategory(짧은 영문) 우선, 없으면 family, 그래도 없으면 "Item".
    if sub:
        return sub.replace("-", " ").title()
    if fam != "other":
        return fam.title()
    return "Item"


def _color_hint(it: dict, lang: str) -> str:
    """중복 라벨 구분용 색상 힌트 (KO 는 한글, EN 은 영문 title-case)."""
    fam = (it.get("colorFamily") or "").strip().lower()
    if not fam:
        return ""
    return _COLOR_FAMILY_KO.get(fam, "") if lang == "ko" else fam.title()


def _picker_labels(items: list[dict], lang: str) -> list[str]:
    """각 아이템의 짧은 라벨을 만들고, 중복되면 색상 힌트로 구분한다."""
    labels = [_short_label(it, lang) for it in items]
    counts: dict[str, int] = {}
    for lbl in labels:
        counts[lbl] = counts.get(lbl, 0) + 1
    out: list[str] = []
    for lbl, it in zip(labels, items):
        if counts[lbl] > 1:
            color = _color_hint(it, lang)
            out.append(f"{lbl} ({color})" if color else lbl)
        else:
            out.append(lbl)
    return out


async def _send_picker(adapter, chat_id: int, items: list[dict], lang: str = "en") -> None:
    """Send the multi-item picker — UX simplification 260610.

    Previous design: long text body listing each item + a row of numeric
    buttons [1️⃣][2️⃣][3️⃣][4️⃣]. Users had to mentally map number → item.

    New design: minimal header + each item rendered as its OWN button (label
    text in the button). One tap, no mapping. Label is truncated to 40 chars
    to stay readable on mobile (the wire limit is 256 but practical UX
    cap is ~40).
    """
    n = len(items)
    # Minimal header — the item names live in the buttons themselves now.
    if lang == "ko":
        body = f"사진에서 {n}개 아이템 발견했어 👀  어떤 게 마음에 들어?"
    else:
        plural = "" if n == 1 else "s"
        body = f"Found {n} item{plural} in the photo 👀  Which one are you after?"

    # 버튼에는 긴 Vision 문구 대신 짧은 카테고리 라벨만 노출한다(표시 전용 —
    # 검색은 콜백 `item:{i}` 인덱스로 full keywords 를 그대로 사용). 중복 라벨은
    # `_picker_labels` 가 색상 힌트로 구분한다.
    shortlist = items[:4]
    labels = _picker_labels(shortlist, lang)
    buttons: list[list[tuple[str, str]]] = []
    for i, _it in enumerate(shortlist):
        num_em = NUMBER_EMOJI[i] if i < len(NUMBER_EMOJI) else f"{i + 1}."
        text = f"{num_em} {labels[i]}"
        if len(text) > _LABEL_CAP:
            text = text[: _LABEL_CAP - 1].rstrip() + "…"
        buttons.append([(text, f"item:{i}")])

    if hasattr(adapter, "send_text_with_keyboard"):
        await adapter.send_text_with_keyboard(chat_id, body, buttons)
    elif hasattr(adapter, "send_text_with_buttons"):
        # Flatten to single-row format if the adapter only supports buttons (legacy).
        flat = [(row[0][0], row[0][1]) for row in buttons]
        await adapter.send_text_with_buttons(chat_id, body, flat)
    else:
        await adapter.send_text(chat_id, body)


# @MX:SPEC: SPEC-CONVERSATION-LOG-001
def _emit_pick_item_done(
    state: WorkingState,
    *,
    items: list[dict],
    picked_index: int,
    auto_picked: bool,
) -> None:
    """LOG-T14 — emit `pick_item_done` (single carousel / callback / auto-pick)."""
    try:
        emit(
            event_type="pick_item_done",
            user_key=user_key_for(state.from_user_id, state.chat_id),
            chat_id=state.chat_id,
            thread_id=state.thread_id,
            turn_no=4,
            payload={
                "candidate_items": [
                    {"label": it.get("label", ""), "subcategory": it.get("subcategory", "")} for it in items[:10]
                ],
                "picked_index": picked_index,
                "auto_picked": auto_picked,
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug("[pick_item] pick_item_done emit best-effort")


@observe(name="node.pick_item", as_type="span")
async def pick_item(state: WorkingState) -> dict:
    node_enter("pick_item")
    msg = state.message
    sess = get_store().get_or_create(state.chat_id)
    breadcrumbs: list[str] = []

    # ── Callback path: item:N ──────────────────────────────────────────────
    if msg.callback_data and msg.callback_data.startswith("item:"):
        try:
            idx = int(msg.callback_data.split(":", 1)[1])
        except (ValueError, IndexError):
            idx = -1

        items = sess.detected_items or state.detected_items
        if not (0 <= idx < len(items)):
            try:
                adapter = get_adapter()
                if msg.callback_query_id and hasattr(adapter, "answer_callback_query"):
                    invalid_msg = "잘못된 선택이야" if session_lang(sess) == "ko" else "Invalid choice"
                    await adapter.answer_callback_query(msg.callback_query_id, invalid_msg)
            except Exception as exc:  # REQ-AGENT-007
                logger.exception("[pick_item] answer_callback_query failed")
                breadcrumbs.append(f"pick_item_error: {type(exc).__name__}"[:120])
            breadcrumbs.append(f"pick_item: invalid idx={idx}")
            node_skip("pick_item", f"invalid idx={idx}")
            return {"log_events": breadcrumbs}

        try:
            adapter = get_adapter()
            if msg.callback_query_id and hasattr(adapter, "answer_callback_query"):
                await adapter.answer_callback_query(msg.callback_query_id, None)
        except Exception:
            logger.exception("[pick_item] answer_callback_query (success) failed")

        item = items[idx]
        sess.selected_item_index = idx
        # SPEC-VISION-UNIFY-001 / REQ-VISION-STATE-003 — preserve full rich
        # VisionItem when available. `vision_result` is the source of truth;
        # `detected_items[idx]` is the legacy projection.
        rich_item = None
        if sess.vision_result is not None:
            try:
                rich_items = list(sess.vision_result.items)
                if 0 <= idx < len(rich_items):
                    rich_item = rich_items[idx]
            except Exception:  # noqa: BLE001
                rich_item = None
        if rich_item is not None:
            sess.vision_selected_item_index = idx
            sess.vision_item = derive_legacy_label(rich_item) or "item"
            sess.vision_keywords = derive_legacy_keywords(rich_item)
        else:
            sess.vision_item = item.get("label") or "item"
            sess.vision_keywords = list(item.get("keywords") or [])
        # Note: original scenario sent the OPENER asking for intent. The graph
        # now flows directly to critique_apply → search_node → respond, so we
        # don't re-prompt the user here. Keep AWAITING_INTENT for symmetry with
        # the legacy state model.
        sess.state = SessionState.AWAITING_INTENT
        get_store().update(sess)
        breadcrumbs.append(f"pick_item: selected idx={idx} label={sess.vision_item}")
        _emit_pick_item_done(state, items=items, picked_index=idx, auto_picked=False)
        node_done("pick_item", picked_idx=idx, label=sess.vision_item)
        return {
            "selected_item_index": idx,
            "detected_items": items,
            "vision_selected_item": rich_item,
            "log_events": breadcrumbs,
            "turn_no": 4,
        }

    # ── B6: Text-digit pick path (e.g. "2", "2번", "2,4번", "두번째") ──────
    # Triggered when state is AWAITING_ITEM_PICK and the user typed a textual
    # answer instead of tapping the inline keyboard. Pre-fix this fell through
    # to the carousel-send path and re-displayed the picker, ignoring the
    # user's reply entirely.
    items_for_text = sess.detected_items or state.detected_items
    if msg.text and sess.state == SessionState.AWAITING_ITEM_PICK and items_for_text:
        parsed = _parse_indices_from_text(msg.text, max_index=len(items_for_text))
        if parsed:
            idx = parsed[0]
            multi_select = len(parsed) > 1
            # Apply the same selection bookkeeping the callback path does.
            item = items_for_text[idx]
            sess.selected_item_index = idx
            rich_item = None
            if sess.vision_result is not None:
                try:
                    rich_items = list(sess.vision_result.items)
                    if 0 <= idx < len(rich_items):
                        rich_item = rich_items[idx]
                except Exception:  # noqa: BLE001
                    rich_item = None
            if rich_item is not None:
                sess.vision_selected_item_index = idx
                sess.vision_item = derive_legacy_label(rich_item) or "item"
                sess.vision_keywords = derive_legacy_keywords(rich_item)
            else:
                sess.vision_item = item.get("label") or "item"
                sess.vision_keywords = list(item.get("keywords") or [])
            sess.state = SessionState.AWAITING_INTENT
            # B6 — stash the remaining indices so `respond` can offer a
            # follow-up button AFTER the active item's cards are delivered.
            # New flow: ack "찾는 중" → cards → "4번도 이어서 보여줄까?".
            sess.pending_pick_indices = list(parsed[1:]) if multi_select else []
            get_store().update(sess)

            # Pre-search ack — short status, no question. The question moves
            # to AFTER card delivery (respond tool) so the user isn't asked
            # something while cards are still loading.
            if multi_select:
                try:
                    adapter = get_adapter()
                    lang = session_lang(sess)
                    note = f"{idx + 1}번 먼저 찾는 중 🐱" if lang == "ko" else f"Looking for #{idx + 1} first 🐱"
                    await adapter.send_text(state.chat_id, note)
                except Exception:  # noqa: BLE001
                    logger.debug("[pick_item] multi-select ack send best-effort skip")

            breadcrumbs.append(f"pick_item: text-picked idx={idx} parsed={parsed} multi={multi_select}")
            _emit_pick_item_done(state, items=items_for_text, picked_index=idx, auto_picked=False)
            node_done("pick_item", picked_idx=idx, label=sess.vision_item, source="text")
            return {
                "selected_item_index": idx,
                "detected_items": items_for_text,
                "vision_selected_item": rich_item,
                "log_events": breadcrumbs,
                "turn_no": 4,
            }
        # No parseable digit/ordinal → fall through to re-send the picker
        # with a gentle nudge.
        breadcrumbs.append("pick_item: text reply didn't match any index → re-prompt")

    # ── Carousel-send path ─────────────────────────────────────────────────
    items = state.detected_items or sess.detected_items
    if not items:
        breadcrumbs.append("pick_item: no items to display")
        node_skip("pick_item", "no items to display")
        return {"log_events": breadcrumbs}

    try:
        adapter = get_adapter()
        await _send_picker(adapter, state.chat_id, items, lang=session_lang(sess))
    except Exception as exc:  # REQ-AGENT-007
        logger.exception("[pick_item] picker send failed")
        breadcrumbs.append(f"pick_item_error: {type(exc).__name__}: {exc}"[:200])
        node_skip("pick_item", f"picker send error {type(exc).__name__}")
        return {"log_events": breadcrumbs}

    sess.detected_items = items
    sess.state = SessionState.AWAITING_ITEM_PICK
    get_store().update(sess)
    breadcrumbs.append(f"pick_item: picker sent n={len(items)} → END")
    # Carousel sent — no specific pick yet, picked_index=-1.
    _emit_pick_item_done(state, items=items, picked_index=-1, auto_picked=False)
    node_done("pick_item", picker_sent=len(items))
    return {"detected_items": items, "log_events": breadcrumbs, "turn_no": 4}
