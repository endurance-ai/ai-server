---
id: SPEC-ONBOARD-CARDS-001
plan_version: 0.1.0
spec_version: 0.3.2
created: 2026-05-15
methodology: DDD (ANALYZE-PRESERVE-IMPROVE)
target_branch: feature/benchmark-noscroll
---

# Implementation Plan — SPEC-ONBOARD-CARDS-001 v0.3.2

> **Scope guard**: WHAT/WHY는 spec.md v0.3.2(PASS)에서 잠긴 상태. plan.md는 **HOW**만 결정한다. 27개 REQ (ENTRY 001-003, CARDS 001-003, PINTEREST 001-007, SEED 001-002, MEMORY-AMEND-001, MIGRATION 001-002, GRAPH 001-002, LANG 001-002, COMPLETION 001-002, OBS-001, PERF-001, SEC-001) 와 13개 manual scenarios (a)–(m) 는 그대로 따른다. plan.md가 추가로 잠그는 결정은 SPEC §Open Questions (1–6) 해소만이다.

> **Methodology**: **DDD (ANALYZE-PRESERVE-IMPROVE)**. 본 SPEC은 **기존 12 노드를 한 글자도 안 건드린다** (REQ-ONBOARD-GRAPH-001). 따라서 PRESERVE 범위는 좁다 — 손대는 *기존* 파일은 `app/graphs/fashion_bot.py`(노드/엣지 추가), `app/graphs/routing.py`(라우팅 함수 추가), `app/graphs/state.py`(필드 추가), `app/channels/session.py`(필드 추가), `app/channels/session_pg.py`(컬럼 매핑), `app/channels/taste_profile.py` + `_pg.py`(Protocol 메서드 추가), `app/channels/link_resolver.py`(batch 헬퍼 추가), `app/api/webhooks/telegram.py`(/start 라우팅), `app/core/config.py`(env vars), `app/main.py`(provider 워밍업). 각 *기존 동작* 표면을 건드리는 PR은 characterization test 선행 — 새 6 노드 / 새 3 모듈(`onboarding_cards.py`, `onboarding_values.py`, `pinterest_url.py`, `apify.py`)는 greenfield.

> **HARD prerequisite (cross-SPEC gate)**: REQ-ONBOARD-MEMORY-AMEND-001. SPEC-MEMORY-001 HISTORY에 v1.1.0 amendment 엔트리가 commit `0c59e8b`로 이미 land된 상태이므로 **이 게이트는 통과**. `seed_from_onboarding` Protocol 확장 코드 머지 가능. plan.md §10에서 PR 분할 순서로 다시 확인.

> **HARD prerequisite (migration ordering)**: SPEC-CONVERSATION-LOG-001의 `0003_create_log_conversation_event.py`가 이미 land. 본 SPEC migration은 **`0004_add_onboarded_at.py`** — branch start 시점에 `ls migrations/versions/` 로 재확인 (ONB-T01 첫 step).

---

## 0. Assumption Audit

| # | Assumption | Confidence | Risk if wrong |
|---|---|---|---|
| A1 | `migrations/versions/`의 현재 최신은 `0003_create_log_conversation_event.py`. SPEC-CONVERSATION-LOG-001 Phase 1-4 모두 land 완료. → 본 SPEC revision = **`0004`** | High (verified via `ls migrations/versions/` 직전 확인: 0001/0002/0003) | 다른 SPEC이 먼저 `0004`를 차지하면 `0005`로 리넘버링 — ONB-T01 첫 step에서 한번 더 확인. |
| A2 | `ai.user_session` 테이블은 PRIMARY KEY = `chat_id`. 컬럼 추가는 `ALTER TABLE ... ADD COLUMN`로 zero-downtime (Postgres 16 fast-path). `IF NOT EXISTS`는 컬럼-add에도 지원 (PG 9.6+). 백필은 `op.execute("UPDATE ... SET onboarded_at = now() WHERE onboarded_at IS NULL")`. | High (PG 16 docs verified at SPEC draft time) | None — DDL은 idempotent. |
| A3 | `app/providers/db_pool.py::get_pool()` 패턴 그대로 재사용 가능 — SPEC-MEMORY-001 v1.1.0 amendment(commit `0c59e8b`)로 풀 + `MEMORY_BACKEND_IS_POSTGRES` flag 모두 사용 가능. 새 풀 X. | High (verified by SPEC-CONVERSATION-LOG-001 plan §0 A2) | None. |
| A4 | `app/channels/link_resolver.py::resolve(url)`는 단일 URL 입력. `_safe_get` + SSRF guard + 1h cache + Pinterest originals 치환 모두 내부 헬퍼. `resolve_batch(urls, concurrency=5)`는 같은 캐시·SSRF·redirect 정책으로 wrap 가능. 기존 `resolve(url)` 호출자(현재: `resolve_image` 노드 + 단발 URL flow) 무영향. | High (link_resolver.py 178라인 인스펙트) | resolve_batch 가 캐시·SSRF·timeout 정책 어느 하나라도 break하면 R9 cascade — characterization test 강제. |
| A5 | `apify-client` Python SDK (`pip install apify-client`) 가 `epctex/pinterest-scraper` actor를 호출하는 표준 방식. actor input schema는 `{startUrls:[{url}], maxItems:N, ...}` — v0.3.2 SPEC draft 시점에 web verified. profile mode와 board mode 모두 같은 actor가 처리. | Medium | Actor 이름/스키마가 다르면 `APIFY_PINTEREST_ACTOR_ID` env override로 hot-swap. `plan §3.3` cascade. |
| A6 | testcontainers-postgres 는 SPEC-MEMORY-001 + SPEC-CONVERSATION-LOG-001로 이미 dev-deps. CI runner에서 docker 사용 가능. | High | CI에서 docker 미지원이면 PG-bound tests skip — local-only로 운용. |
| A7 | `app/observability/event_payloads.py::OnboardSelectPayload`, `PinterestIngestPayload`, `TasteSource = Literal["click", "onboard", "pinterest", ...]` 는 SPEC-CONVERSATION-LOG-001 Phase 1로 이미 정의됨. 본 SPEC은 emit site만 채우면 LOG-T23 `xfail(strict=True)` 마커가 xpass → 마커 제거 task가 자연스럽게 cleanup. | High (verified — `app/observability/event_payloads.py:37-45, 167-177` 인스펙트) | LOG-T23 마커 제거 안 하면 CI red. ONB-T22에서 명시적 cleanup. |
| A8 | Pinterest actor가 `pin.it` 단축 URL을 자동 expansion 한다고 가정 (SPEC OQ-2). 만약 actor가 raw `pin.it` 거부하면 `link_resolver._safe_get` redirect-follow로 우회 expand 후 actor에 보드/프로필 full URL 전달. | Low-Medium (web doc 부재) | OQ-2 explicit 정리 — 구현 시점에 small probe로 확인. 대안 path는 §3.6에서 lock. |
| A9 | Code comment 언어는 surrounding style 따름. 신규 모듈 `onboarding_cards.py`/`onboarding_values.py`는 SPEC-CLARIFY-CARDS-001의 `clarify.py`/`clarify_values.py` 패턴 모방 — Korean docstring + bilingual inline comments. `apify.py`/`pinterest_url.py`는 SPEC-MEMORY-001 패턴 (English-leaning). | Medium | PR review에서 조정. |

**Critical surfacing**: A1 (`ls migrations/versions/` 재확인) + A8 (Pinterest actor의 `pin.it` 처리 probe) 만 코드 시작 전 추가 검증 필요. 나머지는 모두 SPEC 또는 코드 인스펙트로 검증 완료.

---

## 1. Migration — `migrations/versions/0004_add_onboarded_at.py`

### 1.1 컬럼 추가 결정 (resolves OQ-1 + REQ-ONBOARD-PINTEREST-007 cache storage)

REQ-ONBOARD-MIGRATION-001 은 `onboarded_at TIMESTAMPTZ NULL` 한 컬럼만 명시한다. **그러나** REQ-ONBOARD-PINTEREST-007 의 cache storage 선택지(SPEC §L508-512: "(a) `user_session.last_pinterest_scrape_at` + `user_session.last_pinterest_payload JSONB`, (b) Redis-style ephemeral store, (c) in-memory dict per worker") 중 (a)를 plan.md에서 lock한다. 이유:

- **(b) Redis**: 현재 스택에 Redis 없음(`docs/ARCHITECTURE.md` 확인 — Postgres + Modal + LiteLLM 뿐). 신규 infra cost.
- **(c) In-memory dict**: 재시작 손실 + multi-worker 불일치(`uvicorn --workers 1` 가정은 fragile — 운영 확장 시 deadlock).
- **(a) PG 컬럼**: durable, scoped per user_session row, 신규 infra 0건. **선택**.

따라서 migration 0004는 4개 컬럼을 한 번에 추가:

| 컬럼 | 타입 | NULL | 용도 | 백필 정책 |
|---|---|---|---|---|
| `onboarded_at` | `TIMESTAMPTZ` | yes | REQ-ONBOARD-ENTRY-001 — `/start` gate sentinel | existing rows → `now()` (REQ-ONBOARD-MIGRATION-001) |
| `last_pinterest_scrape_url` | `TEXT` | yes | REQ-ONBOARD-PINTEREST-007 cache key (normalized) | NULL (cache empty) |
| `last_pinterest_scrape_at` | `TIMESTAMPTZ` | yes | TTL boundary check (`now() - last_pinterest_scrape_at < CACHE_TTL`) | NULL |
| `last_pinterest_pins` | `JSONB` | yes | cached `{image_urls: [...], aggregated_weights: {...}}` for cache hit replay | NULL |

**Why JSONB cache payload includes `aggregated_weights`** (not just image URLs): Vision call cost dominates. Caching only image URLs forces re-Vision-call on cache hit — defeats the cap purpose. Caching the **final aggregated weights dict** (~500 bytes per user) lets cache-hit path skip BOTH Apify AND Vision, just call `seed_from_onboarding(merged_weights)` directly. SPEC R9 cost ceiling honored.

**Row-size bound**: `aggregated_weights` is the *post-aggregation* dict (≤ 100 keys × ~30 bytes = 3KB). `image_urls` cap at 80 entries × ~200 bytes = 16KB. Total row size growth per onboarded user ≈ **20KB max** — acceptable (current row ~2KB, growth = 10×, no risk for 100K users = 2GB total).

### 1.2 Migration source layout

```python
"""add onboarded_at + pinterest cache columns to user_session

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-15

SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-MIGRATION-001 + REQ-ONBOARD-PINTEREST-007.
Adds 4 nullable columns to ai.user_session. Backfills existing rows with
onboarded_at = now() (treated as already-onboarded — see SPEC REQ §5).
Idempotent under re-run via IF NOT EXISTS.
"""

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"

def upgrade() -> None:
    op.execute("SET search_path TO ai")
    op.execute("""
        ALTER TABLE ai.user_session
            ADD COLUMN IF NOT EXISTS onboarded_at TIMESTAMPTZ NULL,
            ADD COLUMN IF NOT EXISTS last_pinterest_scrape_url TEXT NULL,
            ADD COLUMN IF NOT EXISTS last_pinterest_scrape_at TIMESTAMPTZ NULL,
            ADD COLUMN IF NOT EXISTS last_pinterest_pins JSONB NULL
    """)
    # REQ-ONBOARD-MIGRATION-001 backfill — existing users = already-onboarded.
    op.execute(
        "UPDATE ai.user_session SET onboarded_at = now() WHERE onboarded_at IS NULL"
    )

def downgrade() -> None:
    op.execute("""
        ALTER TABLE ai.user_session
            DROP COLUMN IF EXISTS last_pinterest_pins,
            DROP COLUMN IF EXISTS last_pinterest_scrape_at,
            DROP COLUMN IF EXISTS last_pinterest_scrape_url,
            DROP COLUMN IF EXISTS onboarded_at
    """)
```

**No NOT NULL** on `onboarded_at` — `IS NULL` 은 canonical "new user" sentinel (REQ-ONBOARD-MIGRATION-001).
**No NEW INDEX** — `chat_id` PRIMARY KEY로 모든 lookup 충분. cache check은 single row lookup.
**No FK** — same posture as SPEC-CONVERSATION-LOG-001.

### 1.3 Cutover order (R8 mitigation cascade)

배포 순서:

1. **First**: `alembic upgrade head` on dev-app Postgres (4 컬럼 추가 + backfill).
2. **Then**: 코드 deploy (새 6 노드 + Session dataclass 확장).
3. Gap period: 기존 코드는 새 컬럼을 무시 (`select *`이 추가 컬럼을 받아도 `_from_db_row`에서 `getattr(default=None)` 패턴). 신규 코드는 백필된 `onboarded_at`를 정상 인식.

문서화 위치: `docs/infra/deployment.md` cutover 체크리스트.

---

## 2. Module Structure — Card Catalog & Builders

### 2.1 `app/channels/onboarding_values.py` (NEW)

SPEC-CLARIFY-CARDS-001 `clarify_values.py` 패턴 모방. 평행 모듈로 두는 이유는 (a) clarify 카드와 onboarding 카드의 라이프사이클이 완전히 분리됨 (clarify는 weak-vision 분기, onboarding은 `/start`), (b) clarify 옵션이 vision schema enum과 동기화될 가능성이 있는 반면 onboarding 옵션은 product team이 자유롭게 진화 (Non-Goal #14).

```python
# app/channels/onboarding_values.py
from dataclasses import dataclass, field

@dataclass(frozen=True)
class OnboardingOption:
    value: str                                  # snake_case, callback-stable (R7)
    label_ko: str                               # ≤ 16 chars (R4 / Telegram safety)
    label_en: str
    keywords_to_boost: list[str] = field(default_factory=list)  # 1..5 (REQ-ONBOARD-CARDS-001)

# Stage 1 — mood (8 options, min=2, max=3)
MOOD_OPTIONS: list[OnboardingOption] = [...]
# Stage 2 — color (6 options, min=2, max=3)
COLOR_OPTIONS: list[OnboardingOption] = [...]
# Stage 3 — fit (4 options, min=1, max=2)
FIT_OPTIONS: list[OnboardingOption] = [...]

STAGE_BOUNDS: dict[str, tuple[int, int]] = {
    "mood": (2, 3),
    "color": (2, 3),
    "fit": (1, 2),
}

# REQ-ONBOARD-LANG-002 — intro line tables (KO + EN, exact strings).
INTRO_LINES_KO: list[str] = [
    "안녕하세요, kiko 예요. 🐱",
    "당신만의 패션 큐레이터로 함께할게요.",
    "처음이니까 취향부터 알아볼게요!",
    "📸 사진을 보내면 비슷한 옷을 찾아드려요.",
    "🔗 핀터레스트나 인스타 링크도 OK.",
    "💬 '오버핏 좋아해' 같은 자연어도 받아요.",
    "먼저 무드부터 골라볼까요? ↓",
]
INTRO_LINES_EN: list[str] = [
    "Hi, I'm kiko. 🐱",
    "Your personal fashion curator.",
    "Since you're new, let's figure out your taste first!",
    "📸 Send me a photo, I'll find similar pieces.",
    "🔗 Pinterest / Instagram links also work.",
    "💬 You can also type — e.g. 'I like oversized'.",
    "Let's start with mood ↓",
]

# Confirmation card "다시 시작할까요?" (REQ-ONBOARD-ENTRY-001 returning-user path)
RESTART_PROMPT_KO = "다시 시작할까요?"
RESTART_PROMPT_EN = "Start over?"
ADDITIVE_NOTICE_KO = "지금 선택은 기존 취향에 더해집니다"
ADDITIVE_NOTICE_EN = "These will be added to your existing taste"
```

**Snapshot test (REQ-ONBOARD-CARDS-001 AC)**: `test_onboarding_cards.py::test_option_catalog_shape_and_uniqueness` asserts (8/6/4 lengths, value uniqueness per stage, label length ≤ 16, keywords_to_boost 1..5).

### 2.2 `app/channels/onboarding_cards.py` (NEW)

Builders + multi-select toggle helpers:

```python
# app/channels/onboarding_cards.py
from app.channels.adapter import InlineKeyboardButton  # SPEC-CLARIFY-CARDS-001 reuse
from app.channels.onboarding_values import (
    MOOD_OPTIONS, COLOR_OPTIONS, FIT_OPTIONS, STAGE_BOUNDS,
    OnboardingOption,
)

def build_mood_card(
    *, lang: str, selected: list[str]
) -> tuple[str, list[list[InlineKeyboardButton]]]:
    """Returns (prompt_text, inline_keyboard_matrix).

    REQ-ONBOARD-CARDS-002 — toggle: callback `onboard:mood:toggle:{value}`;
    next button: `onboard:mood:next`; skip button: `onboard:mood:skip`.
    Selected options get a "✓ " prefix (re-render via editMessageReplyMarkup).
    """
    ...

def build_color_card(*, lang, selected) -> ...: ...
def build_fit_card(*, lang, selected) -> ...: ...

def build_pinterest_card(*, lang: str) -> ...:
    """REQ-ONBOARD-PINTEREST-001 — three-mode prompt + [URL 보낼게요] / [건너뛰기]."""
    ...

def build_restart_confirmation_card(*, lang: str) -> ...:
    """REQ-ONBOARD-ENTRY-001 returning-user path — [네 / 아니오] confirmation."""
    ...

def parse_onboard_callback(callback_data: str) -> OnboardCallback | None:
    """Parses `onboard:{stage}:{action}:{value?}` strictly. Returns tagged
    union OnboardCallback or None on malformed input."""
    ...
```

**Callback payload contract (resolves OQ — callback_data byte budget)**:

| Format | Example | Bytes | Status |
|---|---|---|---|
| `onboard:{stage}:toggle:{value}` | `onboard:mood:toggle:cleangirl` | 30 | ✓ (< 64) |
| `onboard:{stage}:next` | `onboard:mood:next` | 18 | ✓ |
| `onboard:{stage}:skip` | `onboard:mood:skip` | 18 | ✓ |
| `onboard:pinterest:url_mode` | `onboard:pinterest:url_mode` | 26 | ✓ |
| `onboard:restart:yes` / `:no` | `onboard:restart:yes` | 19 | ✓ |

가장 긴 mood value(`cleangirl`)에 toggle prefix를 붙여도 30 byte로 Telegram 64-byte 한도의 절반 이하. 안전 margin 충분.

### 2.3 Re-render mechanics (REQ-ONBOARD-CARDS-002 AC)

Card multi-select은 매 toggle마다 `editMessageReplyMarkup` 호출로 동일 메시지 slot에서 ✓ 마커 갱신. 새 `sendMessage` 호출 X — 채팅 spam 방지.

`TelegramAdapter.edit_inline_keyboard(chat_id, message_id, keyboard)` 헬퍼가 이미 SPEC-CLARIFY-CARDS-001로 존재 (verify with `grep editMessageReplyMarkup app/channels/telegram/adapter.py`). 없으면 ONB-T03 에서 추가 — Telegram API `editMessageReplyMarkup` 한 줄 wrapper.

`source_message_id`는 stage entry 시점에 `state.onboard_card_message_id: int | None`에 저장 (WorkingState 신규 필드, §5에서 lock). 매 toggle은 그 message_id로 edit. R4(48h edit limit) 은 SESSION_TTL 30분으로 자연 회피.

---

## 3. Pinterest Pipeline — Classifier · Apify · Link Resolver Batch · Vision

### 3.1 `app/channels/pinterest_url.py` (NEW) — Classifier

REQ-ONBOARD-PINTEREST-002. 4-way tagged union:

```python
# app/channels/pinterest_url.py
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit
import re

@dataclass(frozen=True)
class _Pins:    urls: tuple[str, ...]; truncated: bool = False
@dataclass(frozen=True)
class _Board:   url: str
@dataclass(frozen=True)
class _Profile: url: str
@dataclass(frozen=True)
class _None:    pass

PinInput = _Pins | _Board | _Profile | _None  # tagged union via isinstance

_HOST_RE = re.compile(r"^([a-z]{2}\.)?pinterest\.com$|^pin\.it$|^www\.pinterest\.com$")
_PIN_PATH_RE = re.compile(r"^/pin/\d+/?$")
_BOARD_PATH_RE = re.compile(r"^/[^/]+/[^/]+/?$")
_PROFILE_PATH_RE = re.compile(r"^/[^/]+/?$")

def classify_pinterest_input(text: str, *, max_pins: int = 20) -> PinInput:
    """Extract Pinterest URLs from free text, classify into 4-way taxonomy.

    Host parse: urllib.parse.urlsplit (NO regex-only — SSRF defense per
    REQ-ONBOARD-SEC-001 / REQ-ONBOARD-PINTEREST-004). Scheme normalized to
    https (bare URL auto-promoted). http://, javascript:, IDN homographs,
    `pinterest.com.evil.com` all classify NONE.

    Precedence: PIN > BOARD > PROFILE (REQ-ONBOARD-PINTEREST-002).
    """
    ...
```

**Test matrix (REQ-ONBOARD-PINTEREST-002 AC)**:

| Input | Expected |
|---|---|
| `"pinterest.com/pin/111/"` | `Pins(("https://pinterest.com/pin/111/",))` |
| `"pinterest.com/jane/coats/"` | `Board("https://pinterest.com/jane/coats/")` |
| `"pinterest.com/jane/"` | `Profile("https://pinterest.com/jane/")` |
| `"check board pinterest.com/jane/coats/ and pinterest.com/pin/111/ pinterest.com/pin/222/"` | `Pins((pin1, pin2))` — board dropped (precedence) |
| `"pinterest.com/jane/ pinterest.com/jane/coats/"` | `Board(...)` — board beats profile |
| 25 pin URLs | `Pins(20 urls, truncated=True)` |
| `"javascript:alert(1) http://evil.com https://pinterest.com.evil.com/"` | `_None` |
| `"pin.it/abc123"` | `Pins(("https://pin.it/abc123",))` — pin.it shape → PIN bucket |
| `"https://kr.pinterest.com/jane/coats/"` | `Board(...)` |

20개 attack URL set은 `tests/test_onboarding/test_pinterest_classify.py` 에 parametrize.

### 3.2 `app/providers/apify.py` (NEW) — Async wrapper

```python
# app/providers/apify.py
import asyncio
import logging
from typing import Literal, TypedDict
from apify_client import ApifyClientAsync  # NEW pyproject.toml dep

from app.core.config import settings

logger = logging.getLogger(__name__)

class PinResult(TypedDict):
    image_url: str
    pin_url: str | None
    title: str | None

class ApifyProvider:
    def __init__(self, token: str | None, actor_id: str) -> None:
        self._token = (token or "").strip()
        self._actor_id = actor_id
        self._client: ApifyClientAsync | None = None

    @property
    def available(self) -> bool:
        return bool(self._token)

    async def start(self) -> None:
        if not self.available:
            logger.info("🎨 [APIFY] token absent — modes A/B disabled (mode C still works)")
            return
        self._client = ApifyClientAsync(token=self._token)

    async def run_pinterest_scrape(
        self,
        url: str,
        *,
        mode: Literal["board", "profile"],
        max_items: int,
        timeout_s: float,
    ) -> list[PinResult]:
        """REQ-ONBOARD-PINTEREST-005 — graceful degrade: returns [] on any failure
        (no creds, timeout, actor error, empty result). NEVER raises."""
        if not self.available or self._client is None:
            logger.info("🎨 [APIFY][degraded] reason=no_token mode=%s", mode)
            return []
        actor = self._client.actor(self._actor_id)
        run_input = {"startUrls": [{"url": url}], "maxItems": max_items, "mode": mode}
        try:
            run = await asyncio.wait_for(actor.call(run_input=run_input), timeout=timeout_s)
        except asyncio.TimeoutError:
            logger.info("🎨 [APIFY][degraded] reason=timeout mode=%s", mode)
            return []
        except Exception as exc:  # ApifyApiError or HTTP-level
            logger.info("🎨 [APIFY][degraded] reason=%s mode=%s", type(exc).__name__, mode)
            return []
        if run is None or run.get("status") != "SUCCEEDED":
            return []
        dataset_id = run.get("defaultDatasetId")
        if not dataset_id:
            return []
        try:
            items = await self._client.dataset(dataset_id).list_items(limit=max_items)
        except Exception:
            return []
        return [_normalize_pin(it) for it in (items.items or []) if _has_valid_image(it)]
```

**SSRF guard on image_url** (REQ-ONBOARD-SEC-001): `_has_valid_image` reuses `app/channels/schemas._ssrf_guard_url` — same gate that `link_resolver._safe_get` uses. Defense in depth before Modal `/embed` is hit.

**Logging policy (REQ-ONBOARD-SEC-001)**: NO raw URL, NO token. `host + path[:8]` only via `_safe_log_url(url)`. Tested in `test_apify_provider.py::test_no_url_leaks_in_logs`.

### 3.3 Apify actor handling for `pin.it` short URLs (resolves OQ-2)

SPEC OQ-2 — does `epctex/pinterest-scraper` follow `pin.it`?  Plan decision: **probe at implementation time in ONB-T04**. Fallback path (if actor refuses raw `pin.it`):

1. `link_resolver._safe_get(pin_it_url)` follows redirects with SSRF guard → returns final `pinterest.com/pin/{id}/` URL.
2. Re-classify the expanded URL via `classify_pinterest_input(final_url)`.
3. Pass to actor (board/profile mode) or skip if classifier returns NONE.

This fallback adds ~200ms latency but keeps the user flow alive. Probe outcome documented in `docs/infra/deployment.md` + ONB-T04 verification step.

### 3.4 `link_resolver.resolve_batch(urls, concurrency=5)` (MODIFIED)

```python
# app/channels/link_resolver.py — appended to existing module
import asyncio

async def resolve_batch(urls: list[str], *, concurrency: int = 5) -> list[str]:
    """REQ-ONBOARD-PINTEREST-005 mode C — batch resolution preserving existing
    single-URL semantics: failures → omitted from result (NOT exceptions).

    Concurrency cap via asyncio.Semaphore. Same _safe_get / SSRF guard /
    redirect-follow / Pinterest originals substitution / 1h cache as resolve().
    """
    if not urls:
        return []
    sem = asyncio.Semaphore(min(concurrency, len(urls)))

    async def _one(u: str) -> list[str]:
        async with sem:
            return await resolve(u)  # existing single-URL impl

    results = await asyncio.gather(*(_one(u) for u in urls), return_exceptions=True)
    out: list[str] = []
    for r in results:
        if isinstance(r, list) and r:
            out.extend(r)
        # else: failure → silently omit (consistency with single-URL fallback)
    return out
```

**Characterization test (DDD PRESERVE)**: `tests/test_channels/test_link_resolver_characterization.py::test_resolve_single_url_unchanged_after_batch_addition` — same 5 fixtures (Pinterest pin, IG fail, http→https, og:image extract, cache hit) as pre-SPEC. Then `test_link_resolver_batch.py` covers new behavior.

### 3.5 Vision batch aggregation — shared helper

```python
# app/graphs/nodes/_pinterest_helpers.py (NEW — internal to nodes/)
from typing import TypedDict
from app.channels.vision import analyze_image  # existing

class _AggregatedWeights(TypedDict):
    keyword_weights: dict[str, float]
    brand_weights: dict[str, float]
    successfully_analyzed: int

async def aggregate_pin_weights(
    image_urls: list[str],
    *,
    pin_weight: float,
    concurrency: int = 5,
) -> _AggregatedWeights:
    """Modal embed + Vision v2 batch analysis with concurrency cap.

    REQ-ONBOARD-PINTEREST-006 — per-pin failures filter out (no exception
    propagation). Returns aggregated weight dict + successfully_analyzed count.

    Token deduplication: same brand_lower / keyword across 10 pins → contributes
    once at the source. Decay-based reinforcement is handled downstream by
    `seed_from_onboarding`.
    """
    sem = asyncio.Semaphore(concurrency)
    async def _analyze_one(url: str) -> tuple[set[str], set[str]] | None:
        async with sem:
            try:
                vr = await analyze_image(url)  # existing Vision v2 entry
            except Exception:
                return None
            if vr is None or not vr.items:
                return None
            keywords: set[str] = set()
            brands: set[str] = set()
            for item in vr.items:
                if item.subcategory: keywords.add(item.subcategory.lower())
                if item.colorFamily: keywords.add(item.colorFamily.lower())
                if item.fit: keywords.add(item.fit.lower())
                if item.brand_lower: brands.add(item.brand_lower)
            if vr.mood:
                keywords.update(t.lower() for t in (vr.mood.tags or []))
            return keywords, brands

    results = await asyncio.gather(*(_analyze_one(u) for u in image_urls), return_exceptions=True)
    keyword_weights: dict[str, float] = {}
    brand_weights: dict[str, float] = {}
    success = 0
    for r in results:
        if not isinstance(r, tuple):  # exception or None
            continue
        success += 1
        kws, brs = r
        for k in kws:
            keyword_weights[k] = keyword_weights.get(k, 0.0) + pin_weight
        for b in brs:
            brand_weights[b] = brand_weights.get(b, 0.0) + pin_weight
    return _AggregatedWeights(
        keyword_weights=keyword_weights,
        brand_weights=brand_weights,
        successfully_analyzed=success,
    )
```

**Per-call Vision timeout** is inherited from existing `analyze_image` impl (`VISION_TIMEOUT_MS` env). One hanging pin's tokens are NOT included — `success` decrements, completion message reports N-1.

**Cost ceiling (R9 cascade)**: 80 pins × ~$0.005 (gpt-4o-mini) = **$0.40 per Pinterest stage**. With `APIFY_PINTEREST_MAX_ITEMS=80` cap + concurrency=5, R9 acceptance met. Future SPEC may add `PINTEREST_VISION_SAMPLE_SIZE` for top-K filtering (Pinterest engagement-score sampling) — **out of scope here**.

### 3.6 Shared `_ingest_pinterest_pins()` helper (single seed call discipline cascade)

`onboard_pinterest` 노드와 `pinterest_ingest` 노드의 코어 파이프라인 공통화:

```python
# app/graphs/nodes/_pinterest_helpers.py
async def ingest_pinterest_pins(
    state: WorkingState,
    classifier_result: PinInput,
    *,
    apify_provider: ApifyProvider,
    session_store: SessionStore,
    taste_store: TasteProfileStore,
    continuous_origin: bool,
) -> _IngestOutcome:
    """Shared ingest pipeline for both onboard_pinterest (Stage 4) and
    pinterest_ingest (continuous path).

    Returns _IngestOutcome with:
      - success_count: int — successfully analyzed pin count
      - aggregated_weights: AggregatedWeights | None — None on degraded path
      - mode: Literal["board","profile","pins","none","degraded"]
      - cache_hit: bool — REQ-ONBOARD-PINTEREST-007

    Side effects:
      - Modes A/B: writes user_session cache columns on success.
      - continuous_origin=True: calls taste_store.seed_from_onboarding directly.
      - continuous_origin=False: stashes aggregated_weights into
        state.onboard_pin_weights (consumed by completion node — REQ-ONBOARD-PINTEREST-006).
    """
    ...
```

**Why split seed-call vs stash by `continuous_origin`**: REQ-ONBOARD-PINTEREST-006 acceptance L526 explicitly requires:

- **Onboarding path** (`continuous_origin=False`): `seed_from_onboarding` NOT called in this helper; weights stashed in `state.onboard_pin_weights`; completion node makes **one** combined call merging cards + pins.
- **Continuous path** (`continuous_origin=True`): `seed_from_onboarding` called **directly here** with aggregated weights; no completion node phase exists.

This dichotomy is what AC L669 ("exactly one `seed_from_onboarding` call per onboarding session") allows — "onboarding session" scope = onboarding flow only, NOT post-onboarding continuous bootstrap. Documented inline in helper docstring.

### 3.7 24h cache check & write (REQ-ONBOARD-PINTEREST-007)

```python
async def _check_pinterest_cache(
    sess: Session, normalized_url: str, ttl_s: int,
) -> _AggregatedWeights | None:
    """Returns cached aggregated_weights if (sess.last_pinterest_scrape_url ==
    normalized_url) AND (now() - sess.last_pinterest_scrape_at < ttl_s).
    Else None — caller proceeds with Apify call.

    Mode C (pins) callers SHALL NOT invoke this (cache exempt per SPEC L516).
    """
    if sess.last_pinterest_scrape_url != normalized_url:
        return None
    if sess.last_pinterest_scrape_at is None:
        return None
    if (datetime.now(UTC) - sess.last_pinterest_scrape_at).total_seconds() > ttl_s:
        return None
    payload = sess.last_pinterest_pins
    if not isinstance(payload, dict):
        return None
    return _AggregatedWeights(
        keyword_weights=payload.get("aggregated_weights", {}).get("keyword_weights", {}),
        brand_weights=payload.get("aggregated_weights", {}).get("brand_weights", {}),
        successfully_analyzed=int(payload.get("successfully_analyzed", 0)),
    )

def _normalize_pinterest_url(url: str) -> str:
    """SPEC REQ-ONBOARD-PINTEREST-007 — lowercase host, strip trailing /, strip query."""
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    path = p.path.rstrip("/")
    return f"https://{host}{path}"
```

Cache write happens after successful Apify+Vision aggregation: `sess.last_pinterest_scrape_url`, `sess.last_pinterest_scrape_at = now()`, `sess.last_pinterest_pins = {"image_urls": [...], "aggregated_weights": {...}, "successfully_analyzed": N}`. Then `session_store.update(sess)`.

**Langfuse span side effect**: cache hit path emits `pinterest.continuous_ingest` (or stage span) with `metadata.cache_hit=True` (REQ-ONBOARD-OBS-001).

---

## 4. TasteProfileStore Protocol Extension

### 4.1 New method on Protocol (cross-SPEC additive — REQ-ONBOARD-MEMORY-AMEND-001)

```python
# app/channels/taste_profile.py — append to existing Protocol
class TasteProfileStore(Protocol):
    # ... existing methods (frozen per SPEC-MEMORY-001 v1.1.0 — additive-only) ...
    async def seed_from_onboarding(
        self,
        user_key: str,
        *,
        keyword_weights: dict[str, float],
        brand_weights: dict[str, float] | None = None,
    ) -> None: ...
```

### 4.2 InMemoryTasteProfileStore implementation

```python
# app/channels/taste_profile.py
class InMemoryTasteProfileStore:
    # ... existing ...
    async def seed_from_onboarding(
        self, user_key, *, keyword_weights, brand_weights=None,
    ) -> None:
        """REQ-ONBOARD-SEED-001 — additive merge, NOT overwrite. Reuses
        existing decay-then-add reinforce_* APIs so existing weights decay
        naturally and new weights add."""
        async with self.lock_for(user_key):
            profile = self.get_or_create(user_key)
            if keyword_weights:
                # Single batched call OR per-keyword? per-keyword for weight precision.
                for kw, w in keyword_weights.items():
                    profile.reinforce_liked_keywords([kw], weight=w)
            if brand_weights:
                for br, w in brand_weights.items():
                    profile.reinforce_liked_brand(br, weight=w)
            self.update(profile)
```

### 4.3 PostgresTasteProfileStore implementation

```python
# app/channels/taste_profile_pg.py
class PostgresTasteProfileStore:
    # ... existing ...
    async def seed_from_onboarding(
        self, user_key, *, keyword_weights, brand_weights=None,
    ) -> None:
        """Same semantics as InMemory — lock + load + reinforce_* per-key + persist.
        The lock uses the same `lock_for(user_key)` that other backends honor
        (per SPEC-MEMORY-001 v1.1.0 additive Protocol)."""
        async with self.lock_for(user_key):
            profile = await self._load_or_create(user_key)
            if keyword_weights:
                for kw, w in keyword_weights.items():
                    profile.reinforce_liked_keywords([kw], weight=w)
            if brand_weights:
                for br, w in brand_weights.items():
                    profile.reinforce_liked_brand(br, weight=w)
            await self._persist(profile)
```

**Test (REQ-ONBOARD-SEED-001 AC)**: `tests/test_onboarding/test_taste_seed.py::test_seed_additive_merge_preserves_existing` — pre-populate `liked_brands={"ami":0.6}`, call `seed_from_onboarding(keyword_weights={"oversized":0.7}, brand_weights={"lemaire":0.7})`, assert post-state has `ami` (decayed) + `lemaire` + `oversized` all > 0.

### 4.4 Weight default range validation (REQ-ONBOARD-SEED-002)

```python
# app/core/config.py — pydantic validators
@model_validator(mode="after")
def _validate_onboarding_weights(self) -> "Settings":
    card_w = self.ONBOARDING_CARD_SEED_WEIGHT
    pin_w = self.ONBOARDING_PINTEREST_PIN_WEIGHT
    no_click = self.IMPLICIT_NO_CLICK_WEIGHT
    click = self.IMPLICIT_CLICK_WEIGHT
    if not (no_click < card_w <= click):
        logger.warning(
            "[ONBOARD] ONBOARDING_CARD_SEED_WEIGHT=%.2f outside recommended (%.2f, %.2f]",
            card_w, no_click, click,
        )
    if not (no_click < pin_w <= card_w):
        logger.warning(
            "[ONBOARD] ONBOARDING_PINTEREST_PIN_WEIGHT=%.2f outside recommended (%.2f, %.2f]",
            pin_w, no_click, card_w,
        )
    return self
```

WARN only — operator override respected per SPEC AC.

### 4.5 Cross-SPEC amendment status (REQ-ONBOARD-MEMORY-AMEND-001)

**Already satisfied** as of 2026-05-14: commit `0c59e8b` (SPEC-MEMORY-001 v1.1.0 amendment) is land on the working branch. `seed_from_onboarding` Protocol extension is now sanctioned by upstream SPEC. ONB-T05 starts immediately — no pre-merge gate to wait on.

Verification step (ONB-T05 first action): `git log --oneline -- .moai/specs/SPEC-MEMORY-001/spec.md | head -3` confirms the amendment commit predates this SPEC's code work.

---

## 5. WorkingState + Session Dataclass Extensions

### 5.1 `app/graphs/state.py::WorkingState` — 5 new fields

```python
class WorkingState(InputState):
    # ... existing fields ...

    # SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-GRAPH-002 — onboarding state machine.
    onboard_stage: Literal["intro", "mood", "color", "fit", "pinterest", "done"] | None = None
    onboard_selections: dict[str, list[str]] = Field(
        default_factory=lambda: {"mood": [], "color": [], "fit": []}
    )
    # Inline-keyboard re-render target (REQ-ONBOARD-CARDS-002 — editMessageReplyMarkup).
    onboard_card_message_id: int | None = None
    # Continuous path vs onboarding path discriminator (REQ-ONBOARD-PINTEREST-003).
    continuous_origin: bool = False
    # Pinterest weight staging area — completion node consumes (REQ-ONBOARD-PINTEREST-006).
    onboard_pin_weights: dict[str, Any] | None = None
```

**Pydantic v2 `extra="forbid"` compatibility**: all 5 fields have defaults → external constructors still work without specifying them (REQ-ONBOARD-MIGRATION-002 AC).

### 5.2 `app/channels/session.py::Session` — persisted subset

```python
@dataclass
class Session:
    # ... existing ...

    # SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-MIGRATION-002.
    onboarded_at: datetime | None = None
    # Persisted subset of WorkingState onboarding scratchpad (REQ-ONBOARD-GRAPH-002).
    onboard_stage: str | None = None  # "intro" | "mood" | "color" | "fit" | "pinterest" | "done"
    onboard_selections: dict = field(default_factory=lambda: {"mood": [], "color": [], "fit": []})
    onboard_card_message_id: int | None = None
    # Pinterest cache (REQ-ONBOARD-PINTEREST-007).
    last_pinterest_scrape_url: str | None = None
    last_pinterest_scrape_at: datetime | None = None
    last_pinterest_pins: dict | None = None
```

**Backward compat (REQ-ONBOARD-MIGRATION-002 AC)**: all new fields default to `None` or empty — existing `Session(chat_id=1)` calls still valid.

### 5.3 `app/channels/session_pg.py` — column mapping

`PostgresSessionStore._to_jsonable` cascade은 5-step (SPEC-MEMORY-001) — `datetime` → ISO string, `dict` → JSONB serialization 모두 기존 cascade로 처리. 변경 X. `_from_db_row` 에서 새 컬럼 6개(`onboarded_at`, 3개 Pinterest cache, 2개 onboard_stage/selections — `onboard_card_message_id`는 in-memory only, NOT persisted to keep migration scope small) 읽기 + Session 인스턴스화.

**Wait — onboard_card_message_id persist 여부 결정**: REQ-ONBOARD-GRAPH-002 says "mid-flow drop SHALL persist progress" within SESSION_TTL. If a user drops on Stage 1 mood card and returns 10 minutes later, `editMessageReplyMarkup` needs the original message_id. So `onboard_card_message_id` MUST persist.

→ **Revised migration**: ONB-T01 migration 0004 adds **6 columns total**:

| 컬럼 | 타입 |
|---|---|
| `onboarded_at` | `TIMESTAMPTZ NULL` |
| `onboard_stage` | `TEXT NULL` |
| `onboard_selections` | `JSONB NULL` (default `'{"mood":[],"color":[],"fit":[]}'::jsonb`) |
| `onboard_card_message_id` | `BIGINT NULL` |
| `last_pinterest_scrape_url` | `TEXT NULL` |
| `last_pinterest_scrape_at` | `TIMESTAMPTZ NULL` |
| `last_pinterest_pins` | `JSONB NULL` |

7 컬럼. Backfill: `onboarded_at = now()` for existing rows (REQ-ONBOARD-MIGRATION-001), rest left NULL.

### 5.4 InputState — no new fields needed

InputState already has `thread_id` / `turn_no` (SPEC-CONVERSATION-LOG-001 Phase 1). No additions for this SPEC — onboarding context is per-turn scratchpad living on WorkingState only.

---

## 6. Graph Topology — 6 New Nodes + Routing

### 6.1 Node registration (REQ-ONBOARD-GRAPH-001)

```python
# app/graphs/fashion_bot.py — additions to build_graph()
builder.add_node("onboard_intro", onboard_intro)
builder.add_node("onboard_mood", onboard_mood)
builder.add_node("onboard_color", onboard_color)
builder.add_node("onboard_fit", onboard_fit)
builder.add_node("onboard_pinterest", onboard_pinterest)
builder.add_node("pinterest_ingest", pinterest_ingest)
```

**Existing 12 nodes — zero changes** (REQ-ONBOARD-GRAPH-001 AC, verified by topology diff test in ONB-T22).

### 6.2 Edge wiring (8 new edges)

| # | From | To | Conditional? | Predicate |
|---|---|---|---|---|
| 1 | `ingest` | `onboard_intro` | yes | `onboarding_required(state)` — `Session.onboarded_at IS NULL` OR re-trigger keyword |
| 2 | `ingest` | `pinterest_ingest` | yes | `is_continuous_pinterest(state)` — `onboarded_at IS NOT NULL` AND text contains Pinterest URL (mode B/C only, not mode A here — see §6.3 nuance) |
| 3 | `onboard_intro` | `onboard_mood` | no (always) | — |
| 4 | `onboard_mood` | `onboard_color` | yes | `after_onboard_mood(state)` — `mood:next` AND bounds met |
| 5 | `onboard_color` | `onboard_fit` | yes | `after_onboard_color(state)` |
| 6 | `onboard_fit` | `onboard_pinterest` | yes | `after_onboard_fit(state)` — Pinterest enabled AND not skip |
| 7 | `onboard_fit` | `END` | yes (cascade with 6) | Pinterest disabled OR skip → completion handled inside `onboard_fit` (seed call + onboarded_at set + bye message) |
| 8 | `onboard_pinterest` | `END` | no (always) | — completion handled inside `onboard_pinterest` (seed call + onboarded_at) |
| 9 | `pinterest_ingest` | `END` | no (always) | — additive merge done, `onboarded_at` unchanged |

**Headline "8 new edges" reconciliation** (per SPEC L654-664): "8" counts inter-node edges 1-6 + 2 → END terminators that exit the onboarding subgraph (edges 7, 8). Edge 9 (`pinterest_ingest → END`) is structural and not counted. Topology test asserts the **precise 9-entry routing**, not the headline.

### 6.3 Routing edge case — continuous Pinterest mode A precedence (clarification)

REQ-ONBOARD-PINTEREST-003 L401-405: "WHEN a user who is NOT in an onboarding stage sends a text message, AND `classify_pinterest_input(message_text)` returns non-NONE, THE SYSTEM SHALL route to `pinterest_ingest`."

This includes mode A (board URL) — not just modes B/C as my edge table draft suggested. Correction: edge 2 predicate is `onboarded_at IS NOT NULL AND classify_pinterest_input(text) is not _None`. All three modes route to `pinterest_ingest`. The Stage 4 onboarding card handles `AWAITING_PINTEREST_URL` state separately (state-machine priority per REQ L416).

### 6.4 Routing functions (`app/graphs/routing.py` additions)

```python
def _route_after_ingest(state: WorkingState) -> str:
    # ... existing logic ...
    sess = get_store().get_or_create(state.chat_id)

    # SPEC-ONBOARD-CARDS-001 — onboarding gate.
    if onboarding_required(state, sess):
        return "onboard_intro"
    # Continuous Pinterest bootstrap (REQ-ONBOARD-PINTEREST-003).
    if is_continuous_pinterest(state, sess):
        return "pinterest_ingest"
    # ... rest of existing logic ...

def onboarding_required(state: WorkingState, sess: Session) -> bool:
    """REQ-ONBOARD-ENTRY-001 + 002.

    True if:
      (a) user sent /start AND sess.onboarded_at IS NULL, OR
      (b) user sent re-trigger keyword ("온보딩 다시" | "취향 다시 설정" | /reset), OR
      (c) sess.onboard_stage is in {"intro","mood","color","fit","pinterest"} (resume mid-flow), OR
      (d) callback `onboard:*` (consume by node logic; the node parses the action).

    NOT True if:
      - sess.onboarded_at IS NOT NULL AND no re-trigger keyword AND no onboard:* callback.
        That case: /start → confirmation card sent from a SEPARATE branch (not onboard_intro).
        Handled by short-circuit emit in the webhook intake or as a pre-onboard_intro
        sub-route (see §7.2 for /start parsing).
    """
    ...

def is_continuous_pinterest(state: WorkingState, sess: Session) -> bool:
    """REQ-ONBOARD-PINTEREST-003.

    True iff:
      - sess.onboarded_at IS NOT NULL
      - sess.onboard_stage in {None, "done"}
      - state.message.text is not None
      - classify_pinterest_input(state.message.text) is not _None
      - rate-limit window not active (sess.last_pinterest_scrape_at is None
        OR now() - sess.last_pinterest_scrape_at > PINTEREST_CONTINUOUS_RATELIMIT_S)

    Side-effect: sets state.continuous_origin = True (so the shared
    _ingest_pinterest_pins helper chooses the direct-seed branch).
    """
    ...
```

**Rate-limit gate location**: in `is_continuous_pinterest` predicate **AND** inside `pinterest_ingest` node body. Predicate-only gating is fragile (state can change between predicate evaluation and node execution). Double-check inside node = defense in depth. Rate-limit msg sent from node body when triggered.

### 6.5 6 new node bodies (sketches)

Each node is a thin function `async def node_X(state) -> dict[state_delta]`. Common pattern: read sticky lang via `session_lang(sess)`, build card, send via adapter, update `state.onboard_stage`, persist session.

```python
# app/graphs/nodes/onboard_intro.py
async def onboard_intro(state: WorkingState) -> dict:
    """REQ-ONBOARD-ENTRY-001 + REQ-ONBOARD-LANG-002.

    Sends 3-line greeting + 3-line usage + Stage 1 mood card. Sticky lang.
    Auto-advances to onboard_mood node (no user input wait between intro
    text and first card — SPEC L731 "Followed immediately (same webhook turn)").
    """
    adapter = get_adapter()
    sess = get_store().get_or_create(state.chat_id)
    lang = session_lang(sess)
    intro_lines = INTRO_LINES_KO if lang == "ko" else INTRO_LINES_EN
    await adapter.send_text(state.chat_id, "\n".join(intro_lines))
    # Stage 1 card — emitted from this node, then state.onboard_stage="mood".
    text, kb = build_mood_card(lang=lang, selected=[])
    msg_id = await adapter.send_text_with_buttons(state.chat_id, text, kb)
    sess.onboarded_at = None  # explicit — gate still active until completion
    sess.onboard_stage = "mood"
    sess.onboard_selections = {"mood": [], "color": [], "fit": []}
    sess.onboard_card_message_id = msg_id
    get_store().update(sess)
    return {"onboard_stage": "mood", "onboard_card_message_id": msg_id}
```

```python
# app/graphs/nodes/onboard_mood.py — driven by callback handling
async def onboard_mood(state: WorkingState) -> dict:
    """REQ-ONBOARD-CARDS-002.

    Handles `onboard:mood:{action}:{value?}` callbacks:
      - toggle: update selection + editMessageReplyMarkup re-render
      - next: bounds check → if OK advance to onboard_color; else toast
      - skip: advance to onboard_color with empty selection
    """
    ...
```

Same shape for `onboard_color`, `onboard_fit` (with stage-specific bounds).

```python
# app/graphs/nodes/onboard_pinterest.py — Stage 4 entry + URL handling
async def onboard_pinterest(state: WorkingState) -> dict:
    """REQ-ONBOARD-PINTEREST-001 + 004 + 005 + 006.

    Two sub-stages:
      A) Card display + [URL 보낼게요 / 건너뛰기] (first entry).
      B) URL receipt + classify + ingest + completion (subsequent text turn while
         sess.state == AWAITING_PINTEREST_URL).

    Skip path: complete onboarding with card-only weights (delegates to
    _complete_onboarding helper).
    URL path: _ingest_pinterest_pins(continuous_origin=False) → stashes weights
    in state.onboard_pin_weights → _complete_onboarding makes combined seed call.
    """
    ...
```

```python
# app/graphs/nodes/pinterest_ingest.py — continuous bootstrap path
async def pinterest_ingest(state: WorkingState) -> dict:
    """REQ-ONBOARD-PINTEREST-003.

    Triggered when onboarded user sends Pinterest URL outside onboarding.
    Calls _ingest_pinterest_pins(continuous_origin=True) → direct seed call
    + mode-specific confirmation message. onboarded_at unchanged.
    """
    ...
```

### 6.6 `_complete_onboarding` helper (single seed call discipline — REQ-ONBOARD-COMPLETION-001)

```python
# app/graphs/nodes/_onboard_helpers.py
async def complete_onboarding(state: WorkingState) -> None:
    """Single combined seed call + onboarded_at marking + completion message.

    Order (REQ-ONBOARD-COMPLETION-001 L759 — crash-safety):
      1. Compute card-derived weights from state.onboard_selections via
         onboarding_values.OPTIONS[*].keywords_to_boost (× ONBOARDING_CARD_SEED_WEIGHT).
      2. Merge with state.onboard_pin_weights (union; weights summed on overlap).
         Pin weights are None if Pinterest skipped/degraded.
      3. taste_store.seed_from_onboarding(user_key, keyword_weights=merged,
         brand_weights=merged_brands)  ← EXACTLY ONE CALL.
      4. sess.onboarded_at = datetime.now(UTC); session_store.update(sess).
      5. Send completion message (variant by Pinterest outcome).
      6. Reset state.onboard_stage="done"; state.onboard_pin_weights=None.

    Crash-safety: order 3 BEFORE order 4 — crash mid-way leaves onboarded_at=NULL
    so user re-onboards on next /start (acceptable; one duplicate seed call is
    tolerable, orphaned onboarded_at without seed is not).
    """
    ...
```

**Test (REQ-ONBOARD-COMPLETION-001 AC L758)**: `tests/test_onboarding/test_completion_flow.py::test_pinterest_success_makes_exactly_one_seed_call` — assert `mock_taste_store.seed_from_onboarding.call_count == 1` with union args.

---

## 7. Webhook Entry Routing — `/start` + Re-trigger Keywords

### 7.1 Current state of `app/api/webhooks/telegram.py`

SPEC-CONVERSATION-LOG-001 LOG-T08+09+10 가 이미 intake emit + `_resolve_thread_id` + flow classifier 추가함. 본 SPEC 변경 범위는 **graph invocation 직전 분기 한 곳**:

- `/start` 처음 → fresh `Session.onboarded_at IS NULL` → 그래프가 자연스럽게 onboard_intro로 라우팅 (이미 routing 변경으로 처리).
- `/start` 두 번째 (onboarded) → "다시 시작할까요?" 카드 전송 후 ainvoke 생략. **하지만** 카드 자체가 graph로 가야 callback이 적절히 처리됨. **결정**: `/start` 자체는 항상 일반 text turn으로 처리. routing 함수가 onboarded 상태 + `/start` 키워드 감지 시 별도 노드 `onboard_restart_confirm` 로 분기 → 그러나 이는 7번째 신규 노드가 됨 (SPEC은 6개로 못박음).

**Better decision**: `onboard_intro` 노드가 entry 시점에 `Session.onboarded_at IS NOT NULL` AND state.message.text == "/start" 분기를 갖고 confirmation card만 보내는 sub-branch. 노드 이름은 그대로 `onboard_intro` — 두 가지 사용 케이스:

(a) 신규 사용자 → 인사 + Stage 1 카드.
(b) 기존 사용자 + /start → "다시 시작할까요?" confirmation card.

```python
async def onboard_intro(state: WorkingState) -> dict:
    sess = get_store().get_or_create(state.chat_id)
    lang = session_lang(sess)
    is_restart_attempt = (
        sess.onboarded_at is not None
        and (state.message.text or "").strip().lower() == "/start"
    )
    if is_restart_attempt:
        # REQ-ONBOARD-ENTRY-001 returning-user path — confirmation only.
        text, kb = build_restart_confirmation_card(lang=lang)
        await adapter.send_text_with_buttons(state.chat_id, text, kb)
        return {"onboard_stage": None}  # do NOT enter onboarding flow yet
    # Fresh user OR explicit re-trigger keyword OR confirm "yes" callback.
    # ... intro + Stage 1 card ...
```

Confirmation card `[네 / Yes]` callback (`onboard:restart:yes`) → `ingest` router detects it → re-routes to `onboard_intro` with a synthetic state flag (or simply: `onboarding_required` predicate re-evaluates because callback consumption resets `onboard_stage`). Confirmation `[아니오]` → simple ack reply, no state change.

This collapses to **6 nodes** as REQ-ONBOARD-GRAPH-001 requires. The confirmation card lives inside `onboard_intro` — a logical sub-state, not a separate node.

### 7.2 Re-trigger keyword regex

```python
# in routing.py — onboarding_required helper
RE_RESTART = re.compile(r"^(/reset|온보딩 다시|취향 다시 설정)\b", re.IGNORECASE)

def _is_restart_keyword(text: str | None) -> bool:
    if not text:
        return False
    return bool(RE_RESTART.match(text.strip()))
```

**AC tested** (REQ-ONBOARD-ENTRY-002 L260):
- `/RESET` → match (case-insensitive).
- `"온보딩 다시 하기 싫어"` → match `^온보딩 다시\b` first 8 chars, BUT `\b` after "다시" + space → space is NOT word-boundary in Korean unicode word chars; this Python regex MAY produce false positive. **Test**: confirm `\b` behavior with `pytest.parametrize` on 10 known-good and 5 known-bad strings. If false positive observed → switch to exact match check or stricter anchor like `^(/reset|온보딩 다시|취향 다시 설정)$`.

**Plan decision lock**: use **exact-match (whitespace-trimmed)** semantics rather than `\b` anchor — Python regex `\b` on Korean is unreliable. Acceptance test enumerates 10 inputs.

### 7.3 Intake emit for /start (LOG-T23 cleanup cascade)

SPEC-CONVERSATION-LOG-001 LOG-T08 already emits `user_text` on `/start`. This SPEC does NOT modify intake — the `onboarded_at` gate happens at routing.

**onboard_select emit** for each card stage callback advance (REQ-ONBOARD-OBS-001 cascade with SPEC-CONVERSATION-LOG-001 catalog):

```python
# inside each onboard_{mood,color,fit,pinterest} node when stage advances
emit(
    event_type="onboard_select",
    user_key=user_key_for(state),
    chat_id=state.chat_id,
    thread_id=state.thread_id,
    turn_no=state.turn_no,
    payload={
        "stage": "mood",
        "axis": "mood",
        "selected_values": list(state.onboard_selections["mood"]),
    },
)
```

This populates the `taste_update.source="onboard"` and `"pinterest"` emit sites that LOG-T23 xfail-strict markers are waiting for. The markers flip to xpass and ONB-T22 removes them.

---

## 8. Sticky Language Wiring

`app/channels/lang.py::session_lang(sess)` 헬퍼 그대로 재사용. 변경 X.

각 6 노드의 첫 줄: `lang = session_lang(sess)`. 모든 user-facing 문자열 (`build_*_card`, `_complete_onboarding`의 완료 메시지, restart confirmation) 은 `lang` argument를 받아 KO/EN 분기.

**`Session.lang` default = `BOT_DEFAULT_LANG` (REQ-ONBOARD-LANG-001 L703-704)**: 현재 `Session.lang = "en"` default. plan 변경 — `Session.lang = settings.BOT_DEFAULT_LANG` 으로 동적 default. 그러나 dataclass field cannot reference runtime settings cleanly — 대안: `Session.__post_init__`에서 `if self.lang == "" or self.lang is None: self.lang = settings.BOT_DEFAULT_LANG`. 또는 `factory_or_default`. **결정**: dataclass default를 `BOT_DEFAULT_LANG`로 직접 두지 않고, `_resolve_session_for_new_user(sess)` 헬퍼가 신규 row 생성 시점에 `sess.lang = settings.BOT_DEFAULT_LANG` 명시 — 기존 `lang="en"` default는 backward-compat 유지(테스트 영향 없음).

---

## 9. Observability — 6 Langfuse Spans

REQ-ONBOARD-OBS-001 — 5 stage + 1 continuous span:

| Node | Span name | Metadata |
|---|---|---|
| `onboard_intro` | `onboarding.intro` | `{lang, is_restart_attempt}` |
| `onboard_mood` | `onboarding.stage.mood` | `{stage:"mood", selections_count, lang}` |
| `onboard_color` | `onboarding.stage.color` | `{stage:"color", selections_count, lang}` |
| `onboard_fit` | `onboarding.stage.fit` | `{stage:"fit", selections_count, lang}` |
| `onboard_pinterest` | `onboarding.stage.pinterest` | `{stage:"pinterest", url_mode, pin_count, cache_hit, lang}` |
| `pinterest_ingest` | `pinterest.continuous_ingest` | `{url_mode, pin_count, cache_hit, lang}` |

**PII rule (REQ-ONBOARD-OBS-001 L791)**: NO raw `chat_id` / `from_user_id`. Use `hash_id(chat_id)` helper from `app/observability/pii.py`. Test asserts span payload string-search for the literal chat_id integer returns 0 matches.

Implementation: each node body wrapped with `@observe(name="onboarding.stage.mood", as_type="span")` decorator (existing pattern from SPEC-AGENT-001). `update_current_trace(metadata={...})` called inside the node body to attach per-call dims.

---

## 10. PR Splitting & Approval Points

### 10.1 Recommended PR sequence

| PR | Tasks | Scope | Approx LOC |
|---|---|---|---|
| PR-001 | ONB-T01, T02, T03, T04, T05, T07 | Foundation: migration, value catalog, classifier, Apify provider, link_resolver batch, Protocol extension | ~600 |
| PR-002 | ONB-T06, T08, T09 | Card builders + helpers (`onboarding_cards.py`, `_pinterest_helpers.py`, `_complete_onboarding`) | ~400 |
| PR-003 | ONB-T10, T11, T12, T13, T14 | Stage 1-3 nodes (`onboard_intro`, mood, color, fit) | ~500 |
| PR-004 | ONB-T15, T16 | Stage 4 + continuous (`onboard_pinterest`, `pinterest_ingest`) | ~400 |
| PR-005 | ONB-T17, T18 | Graph topology + routing + webhook entry | ~250 |
| PR-006 | ONB-T19, T20, T21, T22 | Validation: 13 scenarios, LOG-T23 xfail cleanup, coverage gate, ruff | ~300 + tests |

### 10.2 Approval points (before ONB-T01 start)

1. **Migration 0004 verification** — `ls migrations/versions/` 재확인. `0003` 점유 (SPEC-CONVERSATION-LOG-001), `0004` 미점유 확인.
2. **7-column migration scope** — `onboarded_at` + 3 cache columns + 3 onboard_state columns. Plan-side decision: lock all 7 in a single migration to minimize alembic churn.
3. **`apify-client` Python SDK 채택** — `pyproject.toml` main deps 추가. License: Apache-2.0 (verified Apify GitHub). Version pin: `apify-client>=1.7.0,<2` (last stable at SPEC draft).
4. **Pinterest actor `epctex/pinterest-scraper` 검증** — actor 이름 + input schema + profile/board mode 지원 모두 ONB-T04 probe로 재확인. fallback: `APIFY_PINTEREST_ACTOR_ID` env override.
5. **`pin.it` short-URL fallback** — OQ-2. Plan §3.3 — `link_resolver._safe_get` 우회 expand. ONB-T04 probe로 actor의 native handling 우선 확인.
6. **6th node `pinterest_ingest` 명명** — confirmed by SPEC v0.3.0 D1+D2 fix (6 nodes / 8 new edges).
7. **`onboard_card_message_id` 영속화** — §5.3 결정 — Session 컬럼으로 persist (mid-flow drop 복원 위해).

### 10.3 Cross-SPEC handoff

- **SPEC-MEMORY-001 amendment**: already land (`0c59e8b`). No blocker.
- **SPEC-CONVERSATION-LOG-001 catalog cleanup**: ONB-T22 removes `tests/test_conversation_log/test_payload_shapes.py::test_taste_update_unimplemented_source_xfail[onboard|pinterest]` xfail-strict markers. Coordinate with LOG-T23 ownership (same author) so CI doesn't flake on PR-006.

---

## 11. Test Strategy

### 11.1 Test files (10 files, per SPEC DoD L1012 enumerating 8 + 2 characterization)

| 파일 | 책임 | DB |
|---|---|---|
| `tests/test_onboarding/test_onboarding_cards.py` | option catalog snapshot (8/6/4 lengths, label ≤ 16, value uniqueness, kw 1..5) + intro lines snapshot | no |
| `tests/test_onboarding/test_pinterest_classify.py` | 25+ URL fixtures: 4-way taxonomy + PIN>BOARD>PROFILE precedence + 20-pin cap + attack URLs → NONE | no |
| `tests/test_onboarding/test_pinterest_url_validation.py` | host allowlist regex `^([a-z]{2}\.)?pinterest\.com$|^pin\.it$|^www\.pinterest\.com$` + 3-strike auto-skip + scheme normalization | no |
| `tests/test_onboarding/test_apify_provider.py` | mocked actor success / 30s timeout / empty / 401 / missing token → all paths graceful | no |
| `tests/test_onboarding/test_link_resolver_batch.py` | concurrency cap 5 + per-URL fail → omitted + cache reuse + SSRF | no |
| `tests/test_onboarding/test_taste_seed.py` | `seed_from_onboarding` additive merge (InMemory + Postgres) + weight range warn | testcontainers |
| `tests/test_onboarding/test_migration.py` | `alembic upgrade head` → 7 columns + backfill all existing rows; `downgrade -1` clean | testcontainers |
| `tests/test_onboarding/test_onboard_nodes.py` | 5 stage 노드 unit (intro/mood/color/fit/pinterest) — toggle, bounds, skip, edit re-render, sticky lang | mocked adapter + in-memory store |
| `tests/test_onboarding/test_pinterest_ingest.py` | `pinterest_ingest` node — 3 modes, additive merge, no onboarded_at change, rate-limit, 24h cache | testcontainers |
| `tests/test_onboarding/test_completion_flow.py` | end-to-end (intro→mood→color→fit→pinterest→done) — single seed call discipline, both Pinterest-success and Pinterest-skip variants | testcontainers |

Plus 2 characterization tests for modified files (DDD PRESERVE):
- `tests/test_channels/test_link_resolver_characterization.py` — single-URL `resolve()` semantics unchanged post-batch addition.
- `tests/test_graph/test_topology_characterization.py` — pre-SPEC 12-node graph diff = 0 after node additions (REQ-ONBOARD-GRAPH-001 AC L671).

Total ≥ 35 test cases (matches DoD L1012).

### 11.2 Characterization test policy (DDD PRESERVE)

Modified files that need PRESERVE baselines:

| File | Characterization concern | Test approach |
|---|---|---|
| `app/channels/link_resolver.py` | `resolve(url)` single-URL behavior unchanged | 5 fixtures: Pinterest pin, IG returns [], http→https redirect, og:image extract, cache hit. Run pre-SPEC + post-SPEC, assert identical output. |
| `app/channels/session.py` | `Session(chat_id=1)` no-arg constructor still works | Re-run existing `tests/test_channels/test_session.py` + new `test_session_default_construction.py`. |
| `app/channels/session_pg.py` | 기존 row round-trip preserved | Re-run existing `tests/test_memory_pg/test_session_store.py`. |
| `app/channels/taste_profile.py` + `_pg.py` | 기존 Protocol methods (5개) unchanged behavior | Re-run existing `tests/test_memory_pg/test_taste_store.py`. |
| `app/graphs/state.py` | InputState/WorkingState backward-compat | Pydantic `extra="forbid"` + default vals — existing tests pass. |
| `app/graphs/routing.py` | 기존 12-node routing decisions unchanged for non-onboarding flows | Re-run existing `tests/test_graph/test_routing.py` + `test_graph_topology.py`. |
| `app/graphs/fashion_bot.py` | 기존 12-node compiled graph unchanged (REQ-ONBOARD-GRAPH-001 AC) | Topology diff test in ONB-T17 + ONB-T22. |
| `app/api/webhooks/telegram.py` | LOG-T08/T09/T10 intake emit unchanged | Re-run `tests/test_conversation_log/test_thread_*.py`. |
| `app/main.py` | Existing lifespan probes unchanged | Re-run `tests/test_main_lifespan.py` (if exists; else smoke). |

### 11.3 Coverage targets (DoD L994)

`pytest --cov` ≥ 85% per module:
- `app/channels/onboarding_cards.py`
- `app/channels/onboarding_values.py`
- `app/channels/pinterest_url.py`
- `app/providers/apify.py`
- `app/graphs/nodes/onboard_intro.py`
- `app/graphs/nodes/onboard_mood.py`
- `app/graphs/nodes/onboard_color.py`
- `app/graphs/nodes/onboard_fit.py`
- `app/graphs/nodes/onboard_pinterest.py`
- `app/graphs/nodes/pinterest_ingest.py`
- `app/graphs/nodes/_pinterest_helpers.py`
- `app/graphs/nodes/_onboard_helpers.py`

### 11.4 13 manual scenarios (a)–(m) — automation map

| Scenario | Automation | File |
|---|---|---|
| (a) Fresh `/start` → 3 stages → completion → photo with non-zero boost | `test_completion_flow.py::test_full_onboarding_seeds_taste_profile` | automated |
| (b) Returning user `/start` → confirmation [No] → IDLE | `test_onboard_nodes.py::test_intro_returning_user_no_path` | automated |
| (c) "온보딩 다시" re-trigger → additive merge | `test_completion_flow.py::test_re_onboarding_additive_merge` | automated |
| (d) 3 invalid URLs → auto-skip | `test_pinterest_url_validation.py::test_three_strike_auto_skip` | automated |
| (e) Valid board URL → real Apify call | manual (dev bot only) — needs real Apify creds | manual |
| (f) Mid-flow drop → resume | `test_onboard_nodes.py::test_resume_from_persisted_stage` | automated |
| (g) `PINTEREST_BOOTSTRAP_ENABLED=false` → skip Stage 4 | `test_onboard_nodes.py::test_pinterest_flag_disabled_skips_stage_4` | automated |
| (h) Mode B profile URL | `test_pinterest_ingest.py::test_profile_mode_apify_path` | automated |
| (i) Mode C 5 pin URLs | `test_pinterest_ingest.py::test_pins_mode_link_resolver_batch` | automated |
| (j) Mixed URLs precedence | `test_pinterest_classify.py::test_mixed_pin_and_board_pins_win` | automated |
| (k) Continuous bootstrap | `test_pinterest_ingest.py::test_continuous_path_no_onboarded_at_mutation` | automated |
| (l) Rate-limit | `test_pinterest_ingest.py::test_rate_limit_within_5_minutes` | automated |
| (m) `APIFY_TOKEN=""` + mode C | `test_pinterest_ingest.py::test_no_apify_token_mode_c_still_works` | automated |

12/13 automated. Scenario (e) requires real Apify creds — verified during cutover smoke test, documented in `docs/infra/deployment.md`.

---

## 12. Risk Mitigation Strategies (Plan-specific)

| Risk (SPEC §) | Plan response |
|---|---|
| R1 — Apify actor rename | `APIFY_PINTEREST_ACTOR_ID` env hot-swap. Actor version pinned at SPEC draft. Document fallback actor candidates in `docs/infra/env.md`. |
| R2 — Apify cost | `APIFY_PINTEREST_MAX_ITEMS=80` cap (≤ 150). 24h cache (§3.7) drops cost on re-trigger. ~$0.01 per Apify call × ≤ 1 per user per 24h = bounded. |
| R3 — Protocol amendment (resolved) | Pre-merge OK — commit `0c59e8b` already land. ONB-T05 verifies before code commit. |
| R4 — `editMessageReplyMarkup` 48h limit | SESSION_TTL=1800s ≪ 48h. Stale fallback to `sendMessage` — accept noise. |
| R5 — Concurrent webhook → out-of-order toggles | Existing `lock_for(chat_id) asyncio.Lock` (SPEC-MEMORY-001 v1.1.0) serializes turns. UI redraw glitch at worst. |
| R6 — Pinterest URL PII leak | §3.2 `_safe_log_url(url)` host+8 chars only. Langfuse spans store host only (REQ-ONBOARD-OBS-001). |
| R7 — Card option drift | `value` snake_case strings frozen by snapshot test (`test_onboarding_cards.py`). label_ko/en safely editable. New options add only — never remove. |
| R8 — Migration race | Cutover order: alembic FIRST, code deploy AFTER. Documented in `docs/infra/deployment.md` (§1.3). |
| R9 — Vision cost spike | Concurrency=5 cap + 80-pin cap + per-call timeout = ≤ $0.40 per Pinterest stage. 24h cache drops repeat cost to $0. Plan §3.5. |
| R10 — Sticky lang mid-flow flip | One-message stale lang acceptable. `session_lang(sess)` resolved at node entry. |
| R11 — Repeated /start clutter | `editMessageReplyMarkup` on most-recent confirmation card if within 60s; else new card. (`test_onboard_nodes.py::test_repeated_start_within_60s_edits_not_resends`.) |
| R12 — `/reset` collision | Plan §7.2 decision: exact-match semantics, NOT `\b` anchor. 10 test fixtures cover edge cases. |

---

## 13. Out of scope (per SPEC § Non-Goals re-check)

- Instagram saved-posts import — separate SPEC.
- Group chat onboarding — bot is 1:1 DM.
- QR deeplink — separate.
- Product catalog ingestion from pins — pins are taste seed only.
- Per-user card option personalization — static catalog.
- A/B testing on card option order — static.
- Onboarding analytics dashboards — Langfuse spans only.
- Multi-tenant / multi-bot — single bot.
- Account merging — fresh chat_id starts fresh.
- Private/locked Pinterest boards — degraded path.
- "Forget my onboarding" privacy endpoint — separate SPEC.
- Onboarding completion webhook to `kikoai/app` — internal flow.
- Card enum sync with `analyze.ts` — independent vocabularies.
- Overwrite mode for re-onboarding — additive only.
- 5th+ card stage — separate SPEC revision.
- Pinterest OAuth / private Saved pins / Idea Pins — separate SPEC.
- `pin.it` short-URL expander beyond `link_resolver` redirect-follow — already covered.
- Mixed-mode "use both board AND pins" — classifier precedence, documented.
- Top-K Vision sampling by Pinterest engagement score — out of scope (R9 already capped by max_items).

---

End of plan.md.
