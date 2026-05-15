"""Onboarding card builders + strict callback parser.

SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-CARDS-001 + REQ-ONBOARD-CARDS-002.

Mirrors `app/channels/clarify.py` 의 패턴 — 결정론적 빌더, LLM 호출 없음.

Public surface:
    - `Button` / `Keyboard` 타입 alias
    - `build_mood_card(lang, selections)`
    - `build_color_card(lang, selections)`
    - `build_fit_card(lang, selections)`
    - `build_pinterest_card(lang)`
    - `build_restart_confirmation_card(lang)`
    - `OnboardCallback` 데이터클래스
    - `parse_onboard_callback(callback_data) -> OnboardCallback | None`

Callback format (plan §2.2): `onboard:{stage}:{action}:{value?}` (≤ 30 bytes).

@MX:SPEC: SPEC-ONBOARD-CARDS-001
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.channels.onboarding_values import (
    COLOR_VALUES,
    FIT_VALUES,
    MOOD_VALUES,
    STAGE_BOUNDS,
    OnboardingOption,
    get_option,
)

# ────────────────────────────────────────────────────────────────────────────
# Type aliases — Telegram-friendly (label, callback_data) tuple matrix.
# ────────────────────────────────────────────────────────────────────────────
Button = tuple[str, str]
Keyboard = list[list[Button]]


# ────────────────────────────────────────────────────────────────────────────
# Stage + action whitelists (callback parser safety)
# ────────────────────────────────────────────────────────────────────────────
_VALID_STAGES = frozenset({"mood", "color", "fit", "pinterest", "restart"})
_VALID_ACTIONS_BY_STAGE: dict[str, frozenset[str]] = {
    "mood": frozenset({"toggle", "done", "skip"}),
    "color": frozenset({"toggle", "done", "skip"}),
    "fit": frozenset({"toggle", "done", "skip"}),
    "pinterest": frozenset({"skip", "url_mode"}),
    "restart": frozenset({"yes", "no"}),
}


# ────────────────────────────────────────────────────────────────────────────
# OnboardCallback — parser output
# ────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class OnboardCallback:
    """Parsed `onboard:*` callback payload.

    @MX:SPEC: SPEC-ONBOARD-CARDS-001
    """

    stage: Literal["mood", "color", "fit", "pinterest", "restart"]
    action: str
    value: str | None = None
    raw: str = ""


def parse_onboard_callback(callback_data: str) -> OnboardCallback | None:
    """Strict parser for `onboard:{stage}:{action}:{value?}` callbacks.

    Returns `None` for any malformed input (wrong prefix, unknown stage/action,
    `toggle` action without value, value referencing unknown option, extra
    segments). Whitelist enforcement prevents callback-injection from drifting
    into uncovered code paths.

    @MX:SPEC: SPEC-ONBOARD-CARDS-001
    """
    if not callback_data or not isinstance(callback_data, str):
        return None
    if not callback_data.startswith("onboard:"):
        return None

    parts = callback_data.split(":")
    # `onboard:stage:action` (3) or `onboard:stage:action:value` (4).
    if len(parts) not in (3, 4):
        return None
    _, stage_raw, action_raw, *rest = parts
    stage = stage_raw.strip().lower()
    action = action_raw.strip().lower()
    if stage not in _VALID_STAGES:
        return None
    if action not in _VALID_ACTIONS_BY_STAGE[stage]:
        return None

    value: str | None = None
    if rest:
        v = rest[0].strip().lower()
        if not v:
            return None
        value = v

    # toggle requires a value; non-toggle actions must NOT include a value.
    if action == "toggle":
        if value is None:
            return None
        # stage must be a card stage with options; pinterest/restart have no toggle.
        if stage not in ("mood", "color", "fit"):
            return None
        if get_option(stage, value) is None:
            return None
    else:
        if value is not None:
            return None

    return OnboardCallback(
        stage=stage,  # type: ignore[arg-type]
        action=action,
        value=value,
        raw=callback_data,
    )


# ────────────────────────────────────────────────────────────────────────────
# Builders — KO/EN strings + selection-marker prefix
# ────────────────────────────────────────────────────────────────────────────
_PROMPTS: dict[tuple[str, str], str] = {
    ("mood", "ko"): "어떤 무드가 좋아요? (2~3개)",
    ("mood", "en"): "Which moods do you like? (2-3)",
    ("color", "ko"): "어떤 컬러가 끌려요? (2~3개)",
    ("color", "en"): "Which colors call to you? (2-3)",
    ("fit", "ko"): "어떤 핏이 편해요? (1~2개)",
    ("fit", "en"): "Which fit feels right? (1-2)",
    ("pinterest", "ko"): (
        "🔗 핀터레스트 보드/프로필/핀 URL을 보내주세요.\n여러 핀 URL을 한 번에 붙여넣어도 돼요. 건너뛰셔도 좋아요."
    ),
    ("pinterest", "en"): (
        "🔗 Send me a Pinterest board / profile / pin URL.\nMultiple pin URLs in one message work too. Or skip."
    ),
    ("restart", "ko"): "취향을 다시 설정할까요?",
    ("restart", "en"): "Want to set your taste preferences again?",
}

_DONE_LABEL: dict[str, str] = {"ko": "다음 →", "en": "Next →"}
_SKIP_LABEL: dict[str, str] = {"ko": "건너뛰기", "en": "Skip"}
_PINTEREST_URL_LABEL: dict[str, str] = {"ko": "URL 보낼게요", "en": "I'll send URL"}
_PINTEREST_SKIP_LABEL: dict[str, str] = {"ko": "건너뛰기", "en": "Skip"}
_RESTART_YES_LABEL: dict[str, str] = {"ko": "네", "en": "Yes"}
_RESTART_NO_LABEL: dict[str, str] = {"ko": "아니오", "en": "No"}


def _normalize_lang(lang: str | None) -> str:
    return "ko" if (lang or "").lower().startswith("ko") else "en"


def _option_label(opt: OnboardingOption, lang: str, *, selected: bool) -> str:
    """Render a single option button label with optional ✓ checkmark prefix.

    Emoji prefix from the catalog (display-only, NOT part of callback_data).
    """
    body = opt.ko_label if lang == "ko" else opt.en_label
    marker = "✓ " if selected else ""
    return f"{marker}{opt.emoji} {body}".strip()


def _multi_select_card(
    *,
    stage: str,
    options: list[OnboardingOption],
    lang: str,
    selections: list[str],
    cols: int = 2,
) -> tuple[str, Keyboard]:
    """Shared scaffolding for mood/color/fit (multi-select with Next/Skip row).

    Rows of `cols` option buttons, followed by a final row containing the
    Next button (always present — bounds enforced server-side on tap) and
    a Skip escape hatch (kept available to keep new-user UX forgiving).
    """
    lang = _normalize_lang(lang)
    selected_set = {s for s in (selections or []) if isinstance(s, str)}

    rows: Keyboard = []
    current_row: list[Button] = []
    for opt in options:
        label = _option_label(opt, lang, selected=opt.value in selected_set)
        cb = f"onboard:{stage}:toggle:{opt.value}"
        current_row.append((label, cb))
        if len(current_row) >= cols:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)

    # Footer row: [Next, Skip]
    rows.append(
        [
            (_DONE_LABEL[lang], f"onboard:{stage}:done"),
            (_SKIP_LABEL[lang], f"onboard:{stage}:skip"),
        ]
    )

    text = _PROMPTS[(stage, lang)]
    return text, rows


# @MX:ANCHOR: [AUTO] Mood multi-select card. Callback shape is part of the
#   onboarding wire contract — drift breaks resume-mid-flow recovery.
# @MX:SPEC: SPEC-ONBOARD-CARDS-001
# @MX:REASON: REQ-ONBOARD-CARDS-002 — snapshot test pins button shape; multiple
#   downstream nodes (onboard_mood, parse_onboard_callback) depend on it.
def build_mood_card(lang: str, selections: list[str] | None = None) -> tuple[str, Keyboard]:
    """Stage 1 mood card (8 options, multi-select, min 2).

    @MX:SPEC: SPEC-ONBOARD-CARDS-001
    """
    return _multi_select_card(
        stage="mood",
        options=MOOD_VALUES,
        lang=lang,
        selections=selections or [],
        cols=2,
    )


def build_color_card(lang: str, selections: list[str] | None = None) -> tuple[str, Keyboard]:
    """Stage 2 color card (6 options, multi-select, min 2).

    @MX:SPEC: SPEC-ONBOARD-CARDS-001
    """
    return _multi_select_card(
        stage="color",
        options=COLOR_VALUES,
        lang=lang,
        selections=selections or [],
        cols=2,
    )


def build_fit_card(lang: str, selections: list[str] | None = None) -> tuple[str, Keyboard]:
    """Stage 3 fit card (4 options, multi-select, min 1).

    @MX:SPEC: SPEC-ONBOARD-CARDS-001
    """
    return _multi_select_card(
        stage="fit",
        options=FIT_VALUES,
        lang=lang,
        selections=selections or [],
        cols=2,
    )


def build_pinterest_card(lang: str) -> tuple[str, Keyboard]:
    """Stage 4 Pinterest prompt — text + [URL 보낼게요 / 건너뛰기] row.

    @MX:SPEC: SPEC-ONBOARD-CARDS-001
    """
    lang = _normalize_lang(lang)
    text = _PROMPTS[("pinterest", lang)]
    rows: Keyboard = [
        [
            (_PINTEREST_URL_LABEL[lang], "onboard:pinterest:url_mode"),
            (_PINTEREST_SKIP_LABEL[lang], "onboard:pinterest:skip"),
        ]
    ]
    return text, rows


def build_restart_confirmation_card(lang: str) -> tuple[str, Keyboard]:
    """Returning-user `/start` confirmation card.

    @MX:SPEC: SPEC-ONBOARD-CARDS-001
    """
    lang = _normalize_lang(lang)
    text = _PROMPTS[("restart", lang)]
    rows: Keyboard = [
        [
            (_RESTART_YES_LABEL[lang], "onboard:restart:yes"),
            (_RESTART_NO_LABEL[lang], "onboard:restart:no"),
        ]
    ]
    return text, rows


# Stage-bounds re-export — callers (nodes) validate min/max on `done` tap.
__all__ = [
    "Button",
    "Keyboard",
    "OnboardCallback",
    "STAGE_BOUNDS",
    "build_color_card",
    "build_fit_card",
    "build_mood_card",
    "build_pinterest_card",
    "build_restart_confirmation_card",
    "parse_onboard_callback",
]
