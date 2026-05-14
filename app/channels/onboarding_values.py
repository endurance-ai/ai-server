"""Onboarding card value catalog — mood/color/fit options + KO/EN labels.

SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-CARDS-001, REQ-ONBOARD-LANG-002.

기존 `app/channels/clarify_values.py` 패턴 모방 — 카드 옵션을 immutable
catalog 으로 보관하고 KO/EN 라벨을 함께 묶어둔다. 카드 라이프사이클이
clarify 와 완전히 분리되므로 (clarify = weak-vision 분기, onboarding =
`/start`) 별도 모듈로 둔다.

@MX:SPEC: SPEC-ONBOARD-CARDS-001
@MX:NOTE: value 문자열(snake_case)은 Telegram callback_data 에 그대로 실리므로
변경 시 모든 사용자의 진행 중 카드가 깨진다. R7 — value 추가만 허용, 삭제 금지.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OnboardingOption:
    """A single card option with KO/EN labels + an emoji icon.

    @MX:SPEC: SPEC-ONBOARD-CARDS-001
    """

    value: str  # snake_case, callback-stable (R7)
    ko_label: str  # ≤ 16 chars (Telegram inline button safety)
    en_label: str  # ≤ 16 chars
    emoji: str  # display-only prefix (not included in callback_data)


# ──────────────────────────────────────────────────────────────────────────
# Stage 1 — mood (8 options, min=2, max=3 per STAGE_BOUNDS)
# ──────────────────────────────────────────────────────────────────────────
MOOD_VALUES: list[OnboardingOption] = [
    OnboardingOption(value="minimal", ko_label="미니멀", en_label="Minimal", emoji="🤍"),
    OnboardingOption(value="street", ko_label="스트릿", en_label="Street", emoji="🛹"),
    OnboardingOption(value="cleangirl", ko_label="클린걸", en_label="Clean Girl", emoji="🧼"),
    OnboardingOption(value="y2k", ko_label="Y2K", en_label="Y2K", emoji="💿"),
    OnboardingOption(value="vintage", ko_label="빈티지", en_label="Vintage", emoji="📻"),
    OnboardingOption(value="corewave", ko_label="코어웨이브", en_label="Corewave", emoji="🌊"),
    OnboardingOption(value="workwear", ko_label="워크웨어", en_label="Workwear", emoji="🔧"),
    OnboardingOption(value="feminine", ko_label="페미닌", en_label="Feminine", emoji="🎀"),
]

# ──────────────────────────────────────────────────────────────────────────
# Stage 2 — color (6 options, min=2, max=3)
# ──────────────────────────────────────────────────────────────────────────
COLOR_VALUES: list[OnboardingOption] = [
    OnboardingOption(value="mono", ko_label="모노톤", en_label="Mono", emoji="⚫"),
    OnboardingOption(value="earth", ko_label="어스톤", en_label="Earth", emoji="🍂"),
    OnboardingOption(value="pastel", ko_label="파스텔", en_label="Pastel", emoji="🌸"),
    OnboardingOption(value="vivid", ko_label="비비드", en_label="Vivid", emoji="🌈"),
    OnboardingOption(value="neutral", ko_label="뉴트럴", en_label="Neutral", emoji="🤎"),
    OnboardingOption(value="dark", ko_label="다크", en_label="Dark", emoji="🖤"),
]

# ──────────────────────────────────────────────────────────────────────────
# Stage 3 — fit (4 options, min=1, max=2)
# ──────────────────────────────────────────────────────────────────────────
FIT_VALUES: list[OnboardingOption] = [
    OnboardingOption(value="oversize", ko_label="오버사이즈", en_label="Oversize", emoji="🫧"),
    OnboardingOption(value="slim", ko_label="슬림", en_label="Slim", emoji="✏️"),
    OnboardingOption(value="regular", ko_label="레귤러", en_label="Regular", emoji="📐"),
    OnboardingOption(value="crop", ko_label="크롭", en_label="Crop", emoji="✂️"),
]


# Stage bounds — (min_selections, max_selections) per stage.
STAGE_BOUNDS: dict[str, tuple[int, int]] = {
    "mood": (2, 3),
    "color": (2, 3),
    "fit": (1, 2),
}


def get_option(stage: str, value: str) -> OnboardingOption | None:
    """Lookup helper — returns `None` for unknown stage/value (callback parser
    can use this to validate user-controlled callback_data).

    @MX:SPEC: SPEC-ONBOARD-CARDS-001
    """
    catalog = {"mood": MOOD_VALUES, "color": COLOR_VALUES, "fit": FIT_VALUES}.get(stage)
    if not catalog:
        return None
    for opt in catalog:
        if opt.value == value:
            return opt
    return None
