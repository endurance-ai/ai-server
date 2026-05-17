"""SPEC-AGENT-V3-REACT / T2 — V2 baseline snapshot helpers.

Captures the SPEC-AGENT-V2-REACT observable contract *as it is now* so the
4-sub-flag-all-off byte-identical guard (REQ-AGENT-V3-COMPAT-ALLOFF-001) can
assert byte-exactness BEFORE any gap code exists and after every gap is added.

These are characterization snapshots — they encode current behaviour, not
desired behaviour. They must NOT be edited to accommodate a gap; a gap that
changes them with its flag OFF is a defect.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from typing import Any

from app.channels.schemas import ChannelMessage
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import Session, SessionState
from app.infrastructure.memory.taste_profile import TasteProfile

# ── 6 representative scenario inputs (AC-X.1) ──────────────────────────────
# (1) photo + "비슷한 거"  (2) "운동복" text search  (3) "더 저렴한 거"
# (4) "ami 좋아해" taste   (5) "안녕" off-topic       (6) ambiguous "옷" clarify


def _state(
    *,
    text: str | None = None,
    callback: str | None = None,
    image_url: str | None = None,
    detected_items: list[dict[str, Any]] | None = None,
    selected_item_index: int | None = None,
    style_node: str | None = None,
) -> WorkingState:
    msg = ChannelMessage(
        chat_id=42,
        text=text,
        callback_data=callback,
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    return WorkingState(
        message=msg,
        chat_id=42,
        from_user_id=99,
        image_url=image_url,
        detected_items=detected_items or [],
        selected_item_index=selected_item_index,
        vision_outfit_style_node_primary=style_node,
    )


def scenario_states() -> dict[str, tuple[WorkingState, Session]]:
    """The 6 AC-X.1 scenarios as (state, session) pairs."""

    def _sess(lang: str = "en", state: SessionState = SessionState.IDLE) -> Session:
        s = Session(chat_id=42, state=state)
        s.lang = lang
        return s

    photo_items = [
        {
            "label": "navy oversized blazer",
            "subcategory": "blazer",
            "fit": "oversized",
            "colorFamily": "NAVY",
            "searchQuery": "navy oversized blazer",
            "searchQueryKo": "네이비 오버사이즈 블레이저",
        }
    ]
    return {
        "photo_similar": (
            _state(
                callback="item:0",
                image_url="https://example.com/p.jpg",
                detected_items=photo_items,
                selected_item_index=0,
            ),
            _sess(lang="ko", state=SessionState.AWAITING_INTENT),
        ),
        "text_search": (_state(text="운동복 보여줘"), _sess(lang="ko")),
        "cheaper": (_state(text="더 저렴한 거"), _sess(lang="ko")),
        "taste_update": (_state(text="나 ami 좋아해"), _sess(lang="ko")),
        "off_topic": (_state(text="안녕"), _sess(lang="ko")),
        "clarify": (_state(text="옷"), _sess(lang="ko")),
    }


def system_prompt_snapshot() -> str:
    from app.agents.react_loop import _SYSTEM_PROMPT

    return _SYSTEM_PROMPT


def user_message_snapshots() -> dict[str, str]:
    from app.agents.react_loop import _build_user_message

    return {k: _build_user_message(st, se) for k, (st, se) in scenario_states().items()}


def tool_names_snapshot() -> tuple[str, ...]:
    from app.agents.tool_registry import TOOL_NAMES

    return TOOL_NAMES


def registry_shape_snapshot() -> dict[str, dict[str, Any]]:
    """Per-tool metadata projection that is byte-stable (excludes class objs)."""
    from app.agents.tool_registry import REGISTRY

    out: dict[str, dict[str, Any]] = {}
    for name, meta in REGISTRY.items():
        out[name] = {
            "name": meta["name"],
            "description": meta["description"],
            "dispatch_fn_path": meta["dispatch_fn_path"],
            "langfuse_span_tag": meta["langfuse_span_tag"],
            "side_effect_doc": meta["side_effect_doc"],
            "terminates_loop": meta["terminates_loop"],
            "args_keys": sorted(getattr(meta["args_typeddict"], "__annotations__", {}).keys()),
            "result_keys": sorted(getattr(meta["result_typeddict"], "__annotations__", {}).keys()),
        }
    return out


def toolmessage_content_v2(result: dict[str, Any]) -> str:
    """The exact ToolMessage content serialization used by run_react_loop."""
    return json.dumps(result, default=str)[:2000]


def taste_profile_field_snapshot() -> dict[str, str]:
    """Field name → type-repr + default-kind for the TasteProfile dataclass."""
    out: dict[str, str] = {}
    for f in dataclasses.fields(TasteProfile):
        if f.default is not dataclasses.MISSING:
            default_kind = repr(f.default)
        elif f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
            default_kind = (
                f"factory:{f.default_factory.__name__ if hasattr(f.default_factory, '__name__') else 'lambda'}"
            )
        else:
            default_kind = "required"
        out[f.name] = f"{f.type}|{default_kind}"
    return out


# ── Frozen baseline values (captured 2026-05-17, pre-gap, V2 ground truth) ──

V2_TOOL_NAMES = (
    "analyze_image",
    "search_products",
    "refine_search",
    "update_taste",
    "ask_user_clarification",
    "get_recent_history",
    "respond",
)

V2_TASTE_PROFILE_FIELDS = {
    "user_key",
    "liked_brands",
    "disliked_brands",
    "liked_keywords",
    "disliked_keywords",
    "price_min_observed",
    "price_max_observed",
    "last_active",
}

# Frozen V2 type|default reprs (captured 2026-05-17, pre-Gap4). Gap4 must NOT
# change any of these — it only ADDS disliked_*_ts (additive schema).
V2_TASTE_PROFILE_FIELD_REPRS = {
    "user_key": "str|required",
    "liked_brands": "dict[str, float]|factory:dict",
    "disliked_brands": "dict[str, float]|factory:dict",
    "liked_keywords": "dict[str, float]|factory:dict",
    "disliked_keywords": "dict[str, float]|factory:dict",
    "price_min_observed": "int | None|None",
    "price_max_observed": "int | None|None",
    "last_active": "float|factory:<lambda>",
}
