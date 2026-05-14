---
id: SPEC-ONBOARD-CARDS-001
version: 0.2.0
status: draft
created: 2026-05-14
updated: 2026-05-14
author: hchsa77@gmail.com
priority: P0
issue_number: null
labels: [onboarding, telegram, taste-profile, pinterest, apify, cards, agentic, continuous-bootstrap]
---

# SPEC-ONBOARD-CARDS-001: 3-Stage Card Onboarding + Multi-Mode Pinterest Bootstrap for Telegram Fashion Bot

## HISTORY

- 2026-05-14 (v0.2.0): Pinterest 입력 모드 3가지 (보드/프로필/개별 핀) 통합 지원, 온보딩 외 시점에도 점진적 부트스트랩 가능. SPEC 작성자: 사용자 요청 (kiko.ai). 출발점: noscroll 벤치마킹 대화 + `app/channels/link_resolver.py` 가 이미 단일 pin og:image 를 처리한다는 사실 인지 → 개별 pin URL 다발은 기존 인프라 재사용으로 거의 무료. 본 버전에서 신설/확장:
  - **모드 A (Board URL)**: 기존 `pinterest.com/<user>/<board>/` (v0.1.0 의 단일 모드).
  - **모드 B (Profile URL)**: `pinterest.com/<user>/` — Apify 의 profile mode 로 최근 공개 핀 (cap 100) 크롤. 보드 mode 와 같은 graceful-degrade 경로.
  - **모드 C (Individual Pin URLs)**: 한 메시지에 다수의 `pinterest.com/pin/<id>/` URL (공백/줄바꿈 구분, **최대 20개/턴**). 기존 `link_resolver.py` 의 og:image 파서를 batch 화하여 재사용 → 신규 외부 의존성 없음, 구현 비용 최소.
  - 새 helper `app/channels/pinterest_url.py` 가 자유 텍스트에서 Pinterest URL 을 추출 + 4-way 분류 (`PIN[]` / `BOARD` / `PROFILE` / `NONE`).
  - **연속 부트스트랩 (REQ-ONBOARD-PINTEREST-CONTINUOUS)**: 모드 B/C 는 온보딩 종료 후에도 임의의 시점에 동작 — 사용자가 "이 핀들 봐줘" 식으로 URL 을 보내면 `pinterest_ingest` 노드가 `ingest` 라우터의 새 분기로 발동, `TasteProfile` 에 incremental merge. 온보딩 재진입 트리거되지 않음.
  - URL 분류 우선순위: **PIN > BOARD > PROFILE** (혼합 메시지에 board + pins 가 같이 있으면 pins 가 이긴다 — 더 좁고 명시적인 신호).
  - 모드 C 의 per-pin weight 는 모드 A/B 와 동일하게 `ONBOARDING_PINTEREST_PIN_WEIGHT` (0.5) 사용. 20-pin 캡으로 단일 메시지의 weight 폭발 방지.
  - **Non-Goal D 추가**: 비공개 "Saved" / Idea Pins (Pinterest OAuth 필요) — 본 SPEC 범위 외.
  - 영향 모듈 추가: `app/channels/pinterest_url.py` (NEW), `app/graphs/nodes/pinterest_ingest.py` (NEW — 온보딩 외 호출 경로), `app/providers/apify.py` 의 board/profile 두 mode 지원, `app/channels/link_resolver.py` batch 확장.
- 2026-05-14 (v0.1.0): 최초 초안. **noscroll** (https://noscroll.com) 의 "bootstrap-don't-cold-start" 패턴 — SMS/Telegram 기반 AI 뉴스 에이전트가 X 계정 import 로 사용자 취향을 부트스트랩하는 — 을 패션 도메인으로 이식. 벤치마크 자료: `docs/_tmp/noscroll-benchmark.html`. SPEC-MEMORY-001 (Postgres `user_taste_profile` / `user_session`) + SPEC-CLARIFY-CARDS-001 (인라인 키보드 카드 인프라) + SPEC-IMPLICIT-FB-001 (`TasteProfile.reinforce_*` 가중치 API) 위에 올라간다. 신규 유저의 `/start` 진입 시 cold-start 갭을 (1) 3-stage 결정형 카드 (mood / color / fit) + (2) 선택형 Pinterest 보드 URL import 로 메꿔, 첫 검색 결과부터 의미 있는 선호 신호를 갖춘 `TasteProfile` 을 갖게 한다. 본 SPEC 은 **WHAT** 과 **WHY** 만 정의한다 — 카드 라벨 카피의 KO/EN 최종안, Apify actor 호출 페이로드, Vision batch 큐 동시성 튜닝 등 **HOW** 는 `plan.md` / Run phase 에서 결정한다.

---

## Goal

현재 신규 사용자가 `/start` 를 눌렀을 때 봇이 하는 일은 짧은 인사말 한 줄과 "사진을 보내달라" 는 안내가 전부다. `TasteProfile` 은 비어 있는 상태에서 첫 사진을 받고, 첫 검색 결과는 사실상 **취향 없는 임베딩 유사도 추천** 이 되어 사용자가 "이거 별로" 같은 critique 를 여러 라운드 돌려야 비로소 의미 있는 결과가 나온다. 이것은 정확히 noscroll 이 X 계정 import 로 해결한 "cold-start gap" 패턴이다 (참고: `docs/_tmp/noscroll-benchmark.html`).

본 SPEC 은 이 갭을 두 갈래로 메운다:

1. **3-stage 결정형 카드 온보딩** — mood (8개, 2~3개 다중선택) → color palette (6개, 2~3개 다중선택) → fit (4개, 1~2개 다중선택). 모든 카드는 LLM 호출 없이 정적 옵션. 한 stage 가 끝나면 [다음 →] 버튼으로 진행, 각 카드 우측 상단에 [건너뛰기] 옵션 제공. SPEC-CLARIFY-CARDS-001 의 인라인 키보드 + `send_card` 어댑터 인프라를 그대로 재사용한다.

2. **(선택) Pinterest 보드 URL bootstrap** — 3 stage 완료 후 봇이 "🎨 Pinterest 보드 URL이 있으면 보내주세요" 를 제안. 사용자가 URL 을 보내면 Apify (`epctex/pinterest-scraper`) 로 50–100 핀을 크롤 → 기존 Modal FashionSigLIP + Vision LLM (`channels/vision.py` v2 schema) batch 로 brand / style / color 메타데이터 추출 → `TasteProfile.reinforce_liked_*` 로 가중치 머지. Apify creds 없음 또는 timeout (> 30s) 인 경우 graceful skip — 카드 기반 시드만으로 온보딩 완료. Feature flag `PINTEREST_BOOTSTRAP_ENABLED=true` (default) 로 전체 stage 끄기 가능.

핵심 설계 원칙:

1. **재진입 안전 (idempotent on the second `/start`)**. `user_session.onboarded_at` 컬럼 (신규) 이 NULL 이 아닌 경우 — 즉 한 번이라도 온보딩을 완료한 사용자 — 는 `/start` 를 다시 눌러도 자동으로 카드 흐름에 재진입하지 않는다. 대신 "다시 시작할까요?" 확정 카드 [네 / 아니오] 를 보낸다. 명시적 키워드 ("온보딩 다시", "취향 다시 설정", `/reset`) 만 흐름을 재시작시킨다. 이는 noscroll 이 X 재import 를 항상 명시적으로 묻는 패턴과 일치.

2. **재온보딩은 additive merge, NOT overwrite**. 재진입한 사용자의 카드 선택은 기존 `TasteProfile` 의 `liked_keywords` / `liked_brands` 에 가중치 합산으로 추가된다 (decay multiplier 그대로). overwrite 모드는 의도적으로 제공하지 않는다 — 이유는 (a) `TasteProfile` 은 30일 LRU staleness 이지 hard reset 이 아니다 (SPEC-MEMORY-001 REQ-MEMORY-PERSIST-003), (b) 사용자가 "다시 시작" 의 의미를 "내 모든 학습 데이터 지우기" 로 오해할 가능성 — 이는 SPEC 12 의 미래형 privacy SPEC 영역. 재온보딩 시 봇은 **현재 메시지** 에서 "지금 선택은 기존 취향에 더해집니다" 를 한 줄 명시한다 (REQ-ONBOARD-REENTRY-001 cascade).

3. **카드 시드 가중치는 0.7, Pinterest 시드는 0.5 (per-pin) 또는 1.0 cap (per-board)**. SPEC-IMPLICIT-FB-001 의 명시적 click 신호 (`weight=1.0`) 보다 약하고, no-click 부정 신호 (`weight=0.2`) 보다 강하게 위치시킨다. 카드 선택은 노력 비용이 명시적 클릭보다 작지만 무관심 클릭보다는 큰 신호로 해석. **모든 weight 는 `plan.md` 에서 튜닝 가능 영역으로 두며 본 SPEC 의 acceptance 는 "≤ 1.0 (click 보다 작게) AND ≥ no-click weight (0.2 보다 크게)" 범위만 강제한다.**

4. **Pinterest stage 실패는 절대 온보딩을 막지 않는다**. Apify 호출 실패, 잘못된 URL, 빈 보드, Vision batch timeout 등 모든 케이스에서 "Pinterest 가져오기 실패. 카드 선택만으로 시작할게요." 메시지 후 정상적으로 `onboarded_at` 을 마킹하고 흐름을 종료. Pinterest 는 **bonus 신호** 이지 필수 path 가 아니다.

5. **외부 행위 byte-identical for 기존 사용자**. 이미 `TasteProfile` row 가 존재하는 (= POC 단계에서 이미 봇을 써본) 사용자는 본 SPEC 배포 후에도 온보딩 흐름을 강제로 보지 않는다. 이는 `onboarded_at` 컬럼의 backfill 정책 — 신규 컬럼은 모든 기존 row 에 대해 **현재 시점 (`now()`) 으로 backfill** 하여 "이미 온보딩 완료" 로 간주 — 으로 보장된다 (REQ-ONBOARD-MIGRATION-001).

6. **Sticky 언어 보존**. 본 SPEC 은 SPEC-AGENT-001 의 KO/EN sticky language (`Session.lang`) 패턴을 그대로 따른다. `/start` 의 초기 인사말은 봇 기본 언어 (config `BOT_DEFAULT_LANG`, default `ko`) 로 시작하되, 사용자가 카드 진행 중 한 번이라도 텍스트 (예: "다시 해줘") 를 보내면 `detect_lang` 이 호출되어 `Session.lang` 이 갱신되고 이후 모든 stage 가 그 언어로 응답된다. 카드 라벨은 KO 와 EN 두 벌을 사전 정의해 두며 (`onboarding_values.py`), 콜백 데이터의 `value` 는 언어와 무관한 영문 snake_case 로 표준화 (`mood:minimal`, `color:monotone`, `fit:oversized`).

이 SPEC 은 LangGraph 그래프 토폴로지를 **확장** 하지 **재구성** 하지 않는다. 신규 5개 노드 (`onboard_intro`, `onboard_mood`, `onboard_color`, `onboard_fit`, `onboard_pinterest`) 가 `ingest` 라우터의 새 분기 (`onboarding_required` 조건) 로 추가될 뿐, 기존 12 노드와 그 간선은 한 글자도 변경하지 않는다.

---

## Background

### 현재 `/start` 흐름

`app/api/webhooks/telegram.py::handle_message` → `app/channels/telegram/webhook.py::parse_update` → `app/graphs/fashion_bot.py::graph.ainvoke(InputState(...))` → `ingest` 노드 → (사용자가 텍스트만 보냈으므로) `router_text` → `respond` 노드 → "안녕하세요! 사진을 보내주세요" 류 안내.

문제:

- `TasteProfile` 은 비어 있다 (`get_or_create` 가 새 row 를 `liked_brands={}, liked_keywords={}` 로 만든다).
- 첫 사진 검색 시 `search_products_v5` RPC 의 `boost_brands` / `boost_keywords` / `exclude_brands` 파라미터는 모두 빈 dict 로 들어간다 → 사실상 임베딩 유사도만으로 추천.
- 사용자가 "이거 별로" / "ami 좋아해" 류 명시적 비평을 보내야 비로소 `TasteProfile` 이 누적되기 시작.
- 즉 **첫 3-5 라운드는 사용자에게 가치를 주지 못한 채 비용만 발생** 한다 (Modal embed + Postgres RPC + LLM critique).

### noscroll 벤치마크 (`docs/_tmp/noscroll-benchmark.html`)

noscroll 은 SMS 기반 AI 뉴스 에이전트로, 가입 직후 사용자에게:

1. X (구 Twitter) 핸들 또는 followee 리스트를 요청.
2. 받은 계정의 최근 활동 (팔로우, 좋아요, 리트윗) 을 import.
3. 이를 시드로 사용자가 관심 가질 토픽 클러스터를 추출.
4. 첫 뉴스 다이제스트부터 시드 토픽 기반으로 큐레이팅.

핵심 인사이트: **"가입 직후 시드 데이터를 가진 사용자는 첫 인터랙션부터 만족도가 비-시드 사용자 대비 유의미하게 높다"** — 즉 cold-start gap 을 줄이는 것이 retention 의 1차 결정 변수. 본 SPEC 은 이를 패션 도메인으로 이식한다:

- noscroll 의 "X 계정 import" → 본 SPEC 의 "Pinterest 보드 URL import" (선택형, optional).
- noscroll 의 "토픽 클러스터" → 본 SPEC 의 "mood / color / fit 카드 선택" (필수형, 결정형).

### 카드 옵션 카탈로그 (informative — formalized in REQ-ONBOARD-CARDS-001)

#### Stage 1 — Mood (8개, 2~3개 다중선택)

| value (callback) | label_ko | label_en | keywords_to_boost |
|---|---|---|---|
| `minimal` | 미니멀 | Minimal | `["minimal", "clean", "essential"]` |
| `street` | 스트릿 | Street | `["street", "urban", "casual"]` |
| `cleangirl` | 클린걸 | Clean Girl | `["clean girl", "preppy", "modest"]` |
| `y2k` | Y2K | Y2K | `["y2k", "retro", "early 2000s"]` |
| `vintage` | 빈티지 | Vintage | `["vintage", "retro", "thrifted"]` |
| `corewave` | 코어웨이브 | Corewave | `["core", "aesthetic", "niche"]` |
| `workwear` | 워크웨어 | Workwear | `["workwear", "utility", "carhartt"]` |
| `feminine` | 페미닌 | Feminine | `["feminine", "soft", "delicate"]` |

#### Stage 2 — Color palette (6개, 2~3개 다중선택)

| value | label_ko | label_en | keywords_to_boost |
|---|---|---|---|
| `monotone` | 모노톤 | Monotone | `["black", "white", "monochrome"]` |
| `earthtone` | 어스톤 | Earthtone | `["beige", "brown", "tan", "khaki"]` |
| `pastel` | 파스텔 | Pastel | `["pastel", "soft pink", "baby blue"]` |
| `vivid` | 비비드 | Vivid | `["vivid", "bold color", "saturated"]` |
| `neutral` | 뉴트럴 | Neutral | `["neutral", "off white", "stone"]` |
| `dark` | 다크 | Dark | `["dark", "black", "charcoal"]` |

#### Stage 3 — Fit (4개, 1~2개 다중선택)

| value | label_ko | label_en | keywords_to_boost |
|---|---|---|---|
| `oversized` | 오버핏 | Oversized | `["oversized", "loose", "relaxed"]` |
| `slim` | 슬림핏 | Slim | `["slim fit", "fitted"]` |
| `regular` | 레귤러 | Regular | `["regular fit", "standard"]` |
| `crop` | 크롭 | Crop | `["crop", "cropped"]` |

각 카드 마지막 행에는 다음이 추가된다:

- `[다음 →]` / `[Next →]` — 최소 선택 수를 만족했을 때만 활성 표시 (Telegram 은 비활성 버튼이 없으므로 라벨 prefix 로 `· 다음 →` 표시 + 누르면 "최소 N개 선택해 주세요" 토스트로 거절).
- `[건너뛰기]` / `[Skip]` — 어떤 stage 든 0개 선택 후 진행 가능. 해당 stage 의 seed 만 비어 남는다.

콜백 페이로드는 `onboard:{stage}:{action}:{value}` 형태:

- `onboard:mood:toggle:minimal` — 선택 토글 (체크박스).
- `onboard:mood:next` — stage 진행.
- `onboard:mood:skip` — stage 스킵.
- `onboard:pinterest:skip` — Pinterest stage 거부 (URL 안 보냄).

### Pinterest bootstrap 흐름 (informative)

```
사용자 → "🎨 Pinterest 보드 URL이 있으면 보내주세요" 카드 ([URL 보낼게요 / 건너뛰기])
       ↓ ([URL 보낼게요] 탭)
봇   → "보드 URL 한 줄로 보내주세요" 안내
       ↓ (사용자가 https://www.pinterest.com/{user}/{board}/ 전송)
ingest → onboard_pinterest 노드 (state == AWAITING_PINTEREST_URL)
       ↓
URL validation (host ∈ {pinterest.com, www.pinterest.com, pin.it}) → invalid → "URL 형식이 안 맞아요" + skip
       ↓ valid
Apify actor epctex/pinterest-scraper 호출 (input: { startUrls: [{ url }], maxItems: 100 })
       ↓ 30s timeout
빈 결과 / timeout / actor error → "Pinterest 가져오기 실패. 카드 선택만으로 시작할게요." + 정상 종료
       ↓ 결과 N pins
각 pin 의 image_url 을 Modal /embed 배치 (기존 EmbedProvider 재사용) + Vision LLM v2 schema 호출
       ↓
brand_lower / keyword tokens 추출 → 가중치 dict 머지 → taste_store.update()
       ↓
"🎉 N개 핀 분석 완료, 취향 시드 만들었어요" 완료 메시지 + onboarded_at = now()
```

Pinterest stage 의 모든 외부 호출 (Apify, Modal, Vision) 은 기존 provider 모듈을 그대로 재사용 — 본 SPEC 은 **새 외부 의존성** 으로 `apify-client` (Python SDK) 만 추가한다.

### Affected modules in kikoai/ai (informative)

**NEW**:

- `app/graphs/nodes/onboard_intro.py` — `/start` 진입 후 인사 3줄 + 사용법 3줄 + Stage 1 카드 전송. 사용자 텍스트 입력 없음, 곧장 다음 노드로 자동 진행.
- `app/graphs/nodes/onboard_mood.py` — Stage 1. 다중선택 토글 누적 + [다음] / [건너뛰기] 대기.
- `app/graphs/nodes/onboard_color.py` — Stage 2. 동일 패턴.
- `app/graphs/nodes/onboard_fit.py` — Stage 3. 동일 패턴.
- `app/graphs/nodes/onboard_pinterest.py` — Stage 4 (선택). Pinterest URL 카드 → URL 수신 → classify → Apify (board/profile) OR link_resolver batch (pins) → Vision batch → merge.
- `app/graphs/nodes/pinterest_ingest.py` — NEW (v0.2.0). 온보딩 외 시점에 사용자가 Pinterest URL 을 보냈을 때 발동하는 노드. REQ-ONBOARD-PINTEREST-CONTINUOUS 의 핵심 진입점. `onboard_pinterest` 와 코어 파이프라인은 공유 (둘 다 `apify_provider.run_pinterest_scrape` + `link_resolver.resolve_batch` + Vision batch + `seed_from_onboarding` 을 호출하는 헬퍼 `_ingest_pinterest_pins(...)` 를 공유). 차이는 (a) 진입 조건, (b) 완료 메시지 wording, (c) `onboarded_at` 미터치.
- `app/channels/onboarding_cards.py` — 3 stage 의 카드 빌더 (`build_mood_card(lang, selected)`, `build_color_card(...)`, `build_fit_card(...)`, `build_pinterest_card(lang)`). SPEC-CLARIFY-CARDS-001 의 `clarify.py` 와 평행 구조. Pinterest 카드의 프롬프트 텍스트는 3가지 URL 모드 모두 안내.
- `app/channels/onboarding_values.py` — 카드 옵션 카탈로그 (위 3개 표) + KO/EN 라벨 + `keywords_to_boost` 매핑. SPEC-CLARIFY-CARDS-001 의 `clarify_values.py` 와 평행 구조.
- `app/channels/pinterest_url.py` — NEW (v0.2.0). `classify_pinterest_input(text: str) -> PinInput` 헬퍼. `PinInput` 은 `PINS(urls: list[str])` / `BOARD(url: str)` / `PROFILE(url: str)` / `NONE` 의 4-way tagged union (Pydantic v2 discriminated union 또는 `dataclass + Literal` tag). 우선순위 PIN > BOARD > PROFILE. 캡 20 pin URLs / 1 board / 1 profile.
- `app/providers/apify.py` — `apify-client` 기반 비동기 wrapper. 메서드 `run_pinterest_scrape(url: str, mode: Literal["board","profile"], max_items: int, timeout_s: float) -> list[PinResult]` — board mode 와 profile mode 모두 지원 (v0.2.0 변경: `run_pinterest_board_scrape` → 통합 `run_pinterest_scrape`). graceful degrade (creds 없음 / timeout) 는 `None` 또는 빈 리스트 반환으로 표현 — exception 으로 호출자 흐름을 끊지 않는다.
- `migrations/versions/XXXX_add_onboarded_at_to_user_session.py` — Alembic revision. `user_session.onboarded_at: timestamptz NULL`. 기존 row 는 `now()` 로 backfill (REQ-ONBOARD-MIGRATION-001).
- `tests/test_onboarding/test_onboard_nodes.py` — 5 노드 단위 테스트 (state transition, 카드 발신, callback 파싱).
- `tests/test_onboarding/test_onboarding_cards.py` — 카드 빌더 + 옵션 카탈로그 스냅샷.
- `tests/test_onboarding/test_apify_provider.py` — Apify wrapper (mocked actor 응답, timeout, missing creds).
- `tests/test_onboarding/test_taste_seed.py` — `TasteProfile.seed_from_onboarding()` 가중치 round-trip + additive merge 시나리오.
- `tests/test_onboarding/test_pinterest_url_validation.py` — host allowlist + pin.it 단축 URL 처리.
- `tests/test_onboarding/test_pinterest_classify.py` — NEW (v0.2.0). `classify_pinterest_input` 4-way taxonomy: PIN/BOARD/PROFILE/NONE 분류 정확성, PIN > BOARD > PROFILE 우선순위, 20-pin cap, 25개 입력시 truncate, 혼합 메시지, 공격 URL.
- `tests/test_onboarding/test_pinterest_ingest.py` — NEW (v0.2.0). `pinterest_ingest` 노드 단위 테스트: 온보딩 완료 사용자가 pin URLs/profile/board 보낼 때 노드 발동 + additive merge + `onboarded_at` 미터치 + rate-limit 동작.
- `tests/test_onboarding/test_link_resolver_batch.py` — NEW (v0.2.0). `resolve_batch` 동시성, 일부 실패 시 빈 결과 누락, 캡 동작.

**MODIFIED**:

- `app/api/webhooks/telegram.py` — `/start` 명령 파싱 (현재는 일반 텍스트로 흘려보내고 있음). 추가로 "온보딩 다시" / "취향 다시 설정" / "/reset" 트리거 라우팅.
- `app/graphs/fashion_bot.py` — 신규 6 노드 (5 onboarding + 1 `pinterest_ingest`) + 신규 edge 등록. `ingest` 의 라우팅 조건에 (a) `onboarding_required(state)` 추가 — `onboarded_at` NULL 이면 `onboard_intro` 로 분기, (b) `is_continuous_pinterest(state)` 추가 — 온보딩 완료 사용자가 Pinterest URL 을 보낸 경우 `pinterest_ingest` 로 분기 (REQ-ONBOARD-PINTEREST-CONTINUOUS).
- `app/channels/link_resolver.py` — **확장** (v0.2.0). 현재 단일 URL 만 처리하는 `async def resolve(url) -> list[str]` 옆에 `async def resolve_batch(urls: list[str], concurrency: int = 5) -> list[str]` 추가. `asyncio.gather` + `Semaphore` 로 동시성 캡, 실패한 URL 은 결과에서 누락 (현행 single-URL fallback `[]` 와 일관).
- `app/graphs/state.py` — `WorkingState` 에 신규 필드 추가:
  - `onboard_stage: Literal["intro","mood","color","fit","pinterest","done"] | None`
  - `onboard_selections: dict[str, list[str]]` — 예: `{"mood": ["minimal","street"], "color": ["monotone"]}`
- `app/graphs/routing.py` — `after_ingest` / `after_onboard_*` 등 신규 라우팅 함수.
- `app/channels/session.py` — `Session.onboarded_at: datetime | None = None` 필드 추가. **dataclass 시그니처 변경이지만 default 값 제공** 으로 SPEC-MEMORY-001 REQ-MEMORY-PROTOCOL-001 의 "Protocol 무변경" 약속과 양립 (Protocol 메서드 시그니처는 그대로, dataclass 필드 추가는 SPEC-MEMORY-001 Non-Goal #10 의 명시적 후속 SPEC 영역).
- `app/channels/session_pg.py` — `Session` 의 신규 `onboarded_at` 필드를 `user_session.onboarded_at` 컬럼에 read/write. `_to_jsonable` 캐스케이드는 변경 없음.
- `app/channels/taste_profile.py` — Protocol 에 **신규 메서드** `seed_from_onboarding(user_key: str, weights: dict[str, float]) -> None` 추가. **이는 Protocol surface 확장** — additive 변경이라 기존 호출자에 영향 없으나 SPEC-MEMORY-001 REQ-MEMORY-PROTOCOL-001 의 "한 글자도 안 바뀐다" 와 충돌. R3 + Open Question 2 에서 다룬다.
- `app/channels/taste_profile_pg.py` / `taste_profile.py` (InMemory 구현) — 위 신규 메서드 구현.
- `app/core/config.py` — 신규 env vars (`PINTEREST_BOOTSTRAP_ENABLED`, `APIFY_TOKEN`, `APIFY_PINTEREST_ACTOR_ID`, `APIFY_PINTEREST_MAX_ITEMS`, `APIFY_PINTEREST_TIMEOUT_S`, `ONBOARDING_CARD_SEED_WEIGHT`, `ONBOARDING_PINTEREST_PIN_WEIGHT`, `BOT_DEFAULT_LANG`).
- `app/main.py` — lifespan 워밍업 단계에 Apify provider 초기화 (env 가 있을 때만).
- `pyproject.toml` — main deps: `apify-client` 추가.

**UNCHANGED (asserted)**:

- `app/pipeline/**` — 검색 파이프라인 무관.
- `app/graphs/nodes/{vision,resolve_image,pick_item,ask_clarify,apply_clarify,search,evaluator,critique_apply,send_results,taste_update,respond}.py` — 12 노드 어떤 것도 수정하지 않는다.
- `app/channels/clarify.py` / `clarify_values.py` — SPEC-CLARIFY-CARDS-001 의 카드 인프라는 별개 파일로 두고 본 SPEC 은 그 옆에 평행 모듈을 만든다 (재사용은 카드 builder 의 inline keyboard 조립 헬퍼 레벨에서만).
- `app/providers/database.py` / `embedding.py` / `llm.py` — 그대로 재사용.
- `app/channels/lang.py` — sticky 언어 헬퍼 재사용.

---

## Requirements (EARS)

### 진입 & 분기 (REQ-ONBOARD-ENTRY-*)

#### REQ-ONBOARD-ENTRY-001 — `/start` SHALL gate on `onboarded_at` [P0] (Ubiquitous + State-driven)

**WHEN** a user sends `/start` to the bot
**AND** the user's `user_session.onboarded_at` IS NULL (또는 `Session.onboarded_at is None`),
**THE SYSTEM SHALL** enter the onboarding flow by routing to `onboard_intro` node within the same webhook turn.

**WHEN** a user sends `/start`
**AND** `onboarded_at` IS NOT NULL,
**THE SYSTEM SHALL** send a confirmation card "다시 시작할까요? / Start over?" with `[네 / Yes]` and `[아니오 / No]` buttons and SHALL NOT auto-restart the flow.

**Acceptance**:

- An integration test creates a fresh `Session` (`onboarded_at=None`), simulates `/start`, asserts the first message sent is the intro + Stage 1 mood card.
- An integration test creates a session with `onboarded_at=datetime.now(UTC)`, simulates `/start`, asserts only the confirmation card is sent (no mood card, no taste profile mutation).
- The intro message + Stage 1 card SHALL be sent within **2 seconds** wall-clock from webhook receipt (REQ-ONBOARD-PERF-001 cascade). Measured by a test fixture that wraps `graph.ainvoke` in `time.monotonic()` boundaries.

#### REQ-ONBOARD-ENTRY-002 — Explicit re-trigger keywords SHALL restart the flow [P0] (Event-driven)

**WHEN** a user sends a text message matching any of: `"온보딩 다시"`, `"취향 다시 설정"`, `/reset` (exact-or-prefix match, case-insensitive, whitespace-trimmed),
**THE SYSTEM SHALL** enter the onboarding flow REGARDLESS of `onboarded_at` value, AND SHALL display a one-line notice "지금 선택은 기존 취향에 더해집니다 / These will be added to your existing taste" before Stage 1 card.

**Acceptance**:

- An integration test with `onboarded_at NOT NULL` sends "온보딩 다시" and asserts (a) the additive-merge notice is sent, (b) Stage 1 mood card follows.
- The keyword match SHALL be case-insensitive: "/RESET" works.
- The keyword match SHALL NOT trigger inside other compound phrases (e.g., "온보딩 다시 하기 싫어" — implementation MAY use anchored regex `^(/reset|온보딩 다시|취향 다시 설정)\b`). The exact regex is finalized in `plan.md`.

#### REQ-ONBOARD-ENTRY-003 — Confirmation card "다시 시작할까요?" SHALL gate on user consent [P0] (Event-driven)

**WHEN** the user taps `[네 / Yes]` on the "다시 시작할까요?" confirmation card,
**THE SYSTEM SHALL** enter the onboarding flow with the additive-merge notice displayed.

**WHEN** the user taps `[아니오 / No]`,
**THE SYSTEM SHALL** send "알겠어요, 사진 보내주시면 추천해 드릴게요 / OK, send a photo and I'll recommend." and return to the IDLE state. No `TasteProfile` mutation. No `onboarded_at` change.

**Acceptance**: Integration tests for both branches assert state transitions and the absence/presence of `taste_store.update()` calls (mock assertion).

---

### 카드 빌더 & 다중선택 (REQ-ONBOARD-CARDS-*)

#### REQ-ONBOARD-CARDS-001 — Card catalog SHALL match the documented option tables [P0] (Ubiquitous)

**THE SYSTEM SHALL** define exactly the options enumerated in the "카드 옵션 카탈로그" section above (Stage 1 — 8 moods, Stage 2 — 6 colors, Stage 3 — 4 fits) in `app/channels/onboarding_values.py`. Each option SHALL have:

- A language-agnostic `value` (snake_case, used in callback payload).
- A `label_ko` and `label_en` string.
- A `keywords_to_boost: list[str]` of at least 1 keyword and at most 5.

**Acceptance**:

- A snapshot test asserts `mood_options()`, `color_options()`, `fit_options()` return lists of the exact lengths (8, 6, 4) with the exact `value` strings documented.
- A test asserts every option's `label_ko` and `label_en` are non-empty and ≤ 16 characters (Telegram inline button label hard limit consideration).
- A test asserts no two options within the same stage share a `value`.
- A test asserts every option's `keywords_to_boost` is `1 ≤ len ≤ 5` and all-lowercase.

#### REQ-ONBOARD-CARDS-002 — Multi-select toggle SHALL enforce min/max bounds [P0] (Event-driven + Unwanted-behaviour)

**WHEN** a user taps an option button (callback `onboard:{stage}:toggle:{value}`),
**THE SYSTEM SHALL** toggle the value in `WorkingState.onboard_selections[stage]`:

- If not currently selected: add it AND re-render the card with that button now showing a checkmark prefix ("✓ 미니멀").
- If currently selected: remove it AND re-render without the checkmark.

**WHEN** a user taps `[다음 →]` callback (`onboard:{stage}:next`),
**THE SYSTEM SHALL** validate that `len(selections[stage])` falls within the stage's min/max bounds:

| stage | min | max |
|---|---|---|
| `mood` | 2 | 3 |
| `color` | 2 | 3 |
| `fit` | 1 | 2 |

**IF** validation fails (selections too few),
**THEN THE SYSTEM SHALL** answer the callback with a Telegram toast message "{N}개에서 {M}개 사이로 선택해 주세요 / Pick {N}-{M} please" AND SHALL NOT advance to the next stage.

**IF** validation fails (selections too many at toggle time — i.e., user tried to add an (max+1)-th item),
**THEN THE SYSTEM SHALL** reject the toggle add with toast "최대 {M}개까지 / Max {M}" AND SHALL NOT mutate `selections`.

**Acceptance**:

- Integration test: user taps 4 mood options sequentially; the 4th tap produces a toast and `selections["mood"]` has exactly 3 items.
- Integration test: user taps 1 mood option then `[Next]`; toast "2~3개 선택" appears, no stage advance.
- Integration test: user taps 2 mood options then `[Next]`; stage advances to color card.
- Re-render messages SHALL use Telegram `editMessageReplyMarkup` (not new `sendMessage`) to avoid spam — the card stays in the same chat slot.

#### REQ-ONBOARD-CARDS-003 — `[건너뛰기]` SHALL allow zero-selection progress [P0] (Optional)

**WHERE** a stage's `[건너뛰기 / Skip]` button is tapped (callback `onboard:{stage}:skip`),
**THE SYSTEM SHALL** advance to the next stage with `selections[stage] = []` (empty list, NOT None).

**Acceptance**:

- Integration test: user taps Skip on mood; color card is sent; `selections["mood"] == []`.
- The empty list SHALL flow through to `seed_from_onboarding` and contribute zero weight (no exception, no warning log).

---

### Pinterest bootstrap (REQ-ONBOARD-PINTEREST-*)

#### REQ-ONBOARD-PINTEREST-001 — Pinterest stage SHALL be feature-flag-gated AND skippable, accepting three input modes [P0] (Optional + Event-driven)

**WHERE** `PINTEREST_BOOTSTRAP_ENABLED=true` AND (`APIFY_TOKEN` is set in environment OR mode C is available without Apify),
**THE SYSTEM SHALL** display the Pinterest card after Stage 3 (fit) completion, with buttons `[URL 보낼게요 / Send URL]` and `[건너뛰기 / Skip]`. The card prompt text SHALL inform the user that **three URL shapes** are accepted:

- **모드 A (Board URL)**: `https://pinterest.com/<user>/<board>/` — Apify board mode 로 최대 100 pin 크롤.
- **모드 B (Profile URL)**: `https://pinterest.com/<user>/` — Apify profile mode 로 최근 공개 핀 (cap 100).
- **모드 C (Pin URLs)**: 개별 핀 URL 다수 (`https://pinterest.com/pin/<id>/`) 를 한 메시지에 공백/줄바꿈으로 구분, **최대 20개/턴**. 기존 `link_resolver.py` 의 og:image 파이프 재사용 — Apify 불필요.

[HARD] 모드 C 는 `APIFY_TOKEN` 이 없어도 동작한다. 즉 `PINTEREST_BOOTSTRAP_ENABLED=true` AND `APIFY_TOKEN` 미설정 케이스에서 카드는 표시하되, 모드 A/B 시도시 "보드/프로필 가져오기는 비활성 — 개별 핀 URL 만 받아요" 로 degraded 응답.

**WHERE** `PINTEREST_BOOTSTRAP_ENABLED=false`,
**THE SYSTEM SHALL** skip the Pinterest stage entirely AND complete onboarding immediately after Stage 3, setting `onboarded_at=now()` and sending the completion message.

**WHEN** the user taps `[건너뛰기]` on the Pinterest card,
**THE SYSTEM SHALL** complete onboarding identically to the flag-off path.

**Acceptance**:

- Unit test with `PINTEREST_BOOTSTRAP_ENABLED=false`: after Stage 3 [Next], the next message is the completion message (no Pinterest card).
- Unit test with `PINTEREST_BOOTSTRAP_ENABLED=true` AND `APIFY_TOKEN=""`: Pinterest card IS shown; sending a board URL triggers degraded "pin URLs only" response; sending pin URLs proceeds via link_resolver path.
- Unit test with both set: after Stage 3 [Next], Pinterest card is sent supporting all three modes.
- Integration test: user taps Skip on Pinterest card; assert completion message + `onboarded_at` row update.

#### REQ-ONBOARD-PINTEREST-CLASSIFY — URL classification helper SHALL implement a 4-way taxonomy [P0] (Ubiquitous)

**THE SYSTEM SHALL** introduce `app/channels/pinterest_url.py` (or extend `link_resolver.py`) exposing a pure synchronous function:

```python
def classify_pinterest_input(text: str) -> PinInput: ...

# PinInput is a tagged union:
#   PinInput.PINS(urls: list[str])  — one or more pin/<id>/ URLs
#   PinInput.BOARD(url: str)        — exactly one board URL
#   PinInput.PROFILE(url: str)      — exactly one profile URL
#   PinInput.NONE                   — no Pinterest URL detected
```

Canonical regex patterns (the helper SHALL accept hosts `pinterest.com`, `pin.it`, `www.pinterest.*`, country subdomains like `kr.pinterest.com`):

| Shape | Regex (host-stripped) | Example |
|---|---|---|
| Pin | `^/pin/\d+/?$` | `pinterest.com/pin/123456789/` |
| Board | `^/[^/]+/[^/]+/?$` (and NOT a pin) | `pinterest.com/jane/fall-coats/` |
| Profile | `^/[^/]+/?$` | `pinterest.com/jane/` |

**Precedence on mixed-URL input** (HARD): when a single message contains multiple Pinterest URLs of different shapes, the classifier SHALL select **PIN > BOARD > PROFILE** — the most specific signal wins. Example: message contains 1 board URL + 2 pin URLs → result is `PinInput.PINS([pin1, pin2])`, the board is ignored. Example: 1 profile + 1 board → `PinInput.BOARD(...)`. This deterministic precedence prevents ambiguity at the cost of dropping the weaker signal.

**Caps & limits**:

- Maximum **20** pin URLs per single message in `PINS(...)`. Excess URLs are truncated (first 20 kept), and the user receives a notice "최대 20개까지만 처리해요 / Processing first 20 only" appended to the response.
- Maximum **1** board URL per message — if 2+ boards detected, the classifier MAY fall back to the first (deterministic by first occurrence in the text).
- Maximum **1** profile URL per message — same rule.

**THE SYSTEM SHALL** use `urllib.parse.urlsplit` for host extraction (NO regex-only URL parsing — SSRF guard alignment with REQ-ONBOARD-SEC-001 and `models/request.py`).

**Acceptance**:

- Unit test: 20 attack URLs (`javascript:...`, IDN homograph attacks, `pinterest.com.evil.com`) all classify as `NONE`.
- Unit test: pin precedence — `"check this board pinterest.com/jane/coats/ and pinterest.com/pin/111/ pinterest.com/pin/222/"` → `PINS(["pinterest.com/pin/111/", "pinterest.com/pin/222/"])`.
- Unit test: 25 pin URLs in one message → returns `PINS([...20 items...])`, sets a `truncated=True` flag on the result.
- Unit test: pin URL alone, board URL alone, profile URL alone — each classified correctly.
- Unit test: bare profile vs board ambiguity — `pinterest.com/jane/coats` (no trailing slash) classified as BOARD; `pinterest.com/jane` as PROFILE.

#### REQ-ONBOARD-PINTEREST-CONTINUOUS — Modes B and C SHALL work outside onboarding [P0] (Event-driven)

**WHEN** a user who is **NOT** in an onboarding stage (`Session.onboarded_at IS NOT NULL` AND `WorkingState.onboard_stage IN {None, "done"}`) sends a text message,
**AND** `classify_pinterest_input(message_text)` returns non-`NONE`,
**THE SYSTEM SHALL** route the turn to a new graph node `pinterest_ingest` via a new conditional edge from `ingest`. This node SHALL:

1. Execute the same Apify (modes A, B) or `link_resolver` batch (mode C) pipeline as REQ-ONBOARD-PINTEREST-004.
2. Call `taste_store.seed_from_onboarding(user_key, ...)` with the aggregated weights — **additive merge into the existing TasteProfile**, identical semantics to REQ-ONBOARD-SEED-001.
3. Send a context-appropriate confirmation message:
   - Mode A: "📌 보드에서 N개 핀 분석해서 취향에 더했어요 / Added N pins from your board to your taste."
   - Mode B: "👤 프로필에서 N개 핀 분석해서 취향에 더했어요 / Added N pins from your profile."
   - Mode C: "📌 {N}개 핀 분석해서 취향에 더했어요 / Added {N} pins to your taste."
4. SHALL NOT mutate `Session.onboarded_at` (it stays NOT NULL from the original onboarding).
5. SHALL NOT route into the onboarding card flow (no Stage 1/2/3 cards re-shown).

**WHERE** the user IS in an onboarding stage AND sends Pinterest URLs mid-flow,
**THE SYSTEM SHALL** NOT re-route to `pinterest_ingest` — the existing onboarding state machine has priority. Pin URLs sent during `AWAITING_PINTEREST_URL` state flow through the existing onboarding Pinterest handler. Pin URLs sent during Stage 1/2/3 cards are ignored (user is expected to tap buttons, not type URLs).

**Rate-limit (R2 cascade)**: continuous ingest from the SAME user_key SHALL be rate-limited to **1 ingest per 5 minutes** to prevent abuse. Excess invocations within the window receive "🕐 잠시 후 다시 시도해 주세요 (5분에 한 번) / Try again in a few minutes (1× per 5 min)" and DO NOT call Apify/link_resolver. Exact mechanism (in-memory dict vs `user_session` column) → `plan.md`.

**Acceptance**:

- Integration test: user with `onboarded_at NOT NULL` sends `"pinterest.com/pin/111/ pinterest.com/pin/222/"` → `pinterest_ingest` invoked → 2 pins through link_resolver → `seed_from_onboarding` called → confirmation message sent → `onboarded_at` unchanged.
- Integration test: same user sends `"pinterest.com/jane/"` (profile) → Apify profile mode → seed merged → mode-B confirmation.
- Integration test: rate-limit — 2 consecutive Pinterest URL messages within 1 minute → second one receives rate-limit notice, no taste mutation.
- Integration test: user mid-onboarding (Stage 1 mood card) sends pin URLs as text → `pinterest_ingest` NOT triggered (no taste mutation outside the onboarding flow).
- The new `pinterest_ingest` node SHALL be registered in `fashion_bot.py` with the conditional edge from `ingest`, AND SHALL be reachable from both the onboarding Stage 4 path AND this text-handling path. The implementation MAY share the node by treating the onboarding-Pinterest flow as "set state.continuous_origin=False; invoke pinterest_ingest", but this is `plan.md` territory.

#### REQ-ONBOARD-PINTEREST-002 — URL validation SHALL allowlist Pinterest hosts AND classify shape [P0] (Unwanted-behaviour)

**WHEN** the bot is in `AWAITING_PINTEREST_URL` state AND receives a text message,
**THE SYSTEM SHALL** call `classify_pinterest_input(text)` (REQ-ONBOARD-PINTEREST-CLASSIFY) and act on the result:

- `PinInput.PINS(urls)` — proceed to mode C ingestion (link_resolver batch, no Apify).
- `PinInput.BOARD(url)` — proceed to mode A ingestion (Apify board mode).
- `PinInput.PROFILE(url)` — proceed to mode B ingestion (Apify profile mode).
- `PinInput.NONE` — see invalid path below.

The classifier internally enforces host allowlist:

- `pinterest.com`
- `www.pinterest.com`
- `pin.it`
- Country subdomains: `*.pinterest.com` for any 2-letter ccTLD prefix (e.g., `kr.pinterest.com`, `jp.pinterest.com`).

Scheme MUST be `https` (or empty — auto-promoted to `https://` by the classifier before host check). `http://` is rejected.

**IF** classification returns `PinInput.NONE` (non-URL text, scheme not `https`, host not in allowlist, or pattern matches none of pin/board/profile),
**THEN THE SYSTEM SHALL** reply with "Pinterest URL을 못 알아봤어요. 보드/프로필/핀 URL 중 하나를 보내주세요. / Couldn't read that. Send a Pinterest board, profile, or pin URL." AND SHALL stay in `AWAITING_PINTEREST_URL` state (user can retry).

**IF** the user sends 3 consecutive NONE-classifying messages in the same state,
**THEN THE SYSTEM SHALL** auto-skip Pinterest stage and proceed to completion with "URL 인식이 안 되네요. 카드 선택만으로 시작할게요 / I can't read those URLs. Card seeds only."

**Acceptance**:

- Property test feeds 20 attack URLs (`javascript:...`, `http://evil.com`, `https://pinterest.com.attacker.com/`, IDN homographs) → all classify as `NONE`.
- Test that `https://www.pinterest.com/user/board/`, `https://pin.it/abc123`, `https://kr.pinterest.com/user/board/`, `https://pinterest.com/pin/123/`, `https://pinterest.com/jane/` all classify correctly per shape.
- Test that 3 NONE-classifying URLs trigger auto-skip.
- Test that 5 pin URLs in one message → `PINS` with 5 items → mode C path.
- The validation SHALL use Python `urllib.parse.urlsplit` + explicit host check inside `classify_pinterest_input`. NO regex-only URL parsing (SSRF defense — `webhook URL parsing rule` in CLAUDE.md project rules).

#### REQ-ONBOARD-PINTEREST-003 — Apify scrape (modes A, B) SHALL respect timeout and graceful degrade [P0] (Event-driven + Unwanted-behaviour)

**WHEN** the classifier returns `PinInput.BOARD(url)` or `PinInput.PROFILE(url)`,
**THE SYSTEM SHALL** invoke `apify_provider.run_pinterest_scrape(url, mode=Literal["board","profile"], max_items=APIFY_PINTEREST_MAX_ITEMS, timeout_s=APIFY_PINTEREST_TIMEOUT_S)` and await its result, with a hard deadline of `APIFY_PINTEREST_TIMEOUT_S` (default `30.0` seconds) enforced via `asyncio.wait_for`. The provider SHALL support both `board` and `profile` modes via the same Apify actor (`epctex/pinterest-scraper` exposes both — confirmed at SPEC draft time).

**Mode C (`PinInput.PINS(urls)`) bypasses Apify entirely.** It SHALL invoke `link_resolver.resolve_batch(urls)` (NEW — see Affected Modules below) which is `app/channels/link_resolver.py` extended to accept a list and run resolvers in parallel (concurrency cap `min(len(urls), 5)`, same per-URL timeout `_TIMEOUT=8.0` already in the module). No Apify token needed. Returns `list[str]` of og:image URLs, with failures filtered out (consistent with existing single-URL `resolve()` returning `[]`).

**IF** the call returns successfully with N ≥ 1 pins,
**THEN THE SYSTEM SHALL** proceed to Vision batch analysis.

**IF** the call raises `asyncio.TimeoutError`, OR `apify_client.ApifyApiError`, OR returns an empty list, OR `APIFY_TOKEN` is missing/invalid,
**THEN THE SYSTEM SHALL**:

1. Log a single line at INFO level (`[ONBOARD][pinterest] degraded reason={reason}` — no raw URL or token in the log).
2. Send the user message "Pinterest 가져오기 실패. 카드 선택만으로 시작할게요. / Pinterest fetch failed. Card seeds only.".
3. Proceed to completion (set `onboarded_at`, seed taste profile with card selections only).

**THE SYSTEM SHALL NOT** retry the Apify call within the same onboarding session. The user MAY re-trigger via the `/reset` flow later.

**Acceptance**:

- Mock test: `apify_provider.run_pinterest_board_scrape` raises `asyncio.TimeoutError`; assert (a) degraded message sent, (b) onboarding completes, (c) INFO log line with `reason=timeout` present.
- Mock test: actor returns `[]`; assert same degraded path.
- Mock test: `APIFY_TOKEN=None`; assert the call short-circuits (no actual HTTP attempt), still degraded path triggered.
- The 30s deadline SHALL be enforced from the inside — `asyncio.wait_for(actor.call(), timeout=30.0)`.

#### REQ-ONBOARD-PINTEREST-004 — Pinterest pins (any mode) SHALL go through Vision batch + taste merge [P0] (Event-driven)

**WHEN** mode A/B Apify returns N pins with image URLs, OR mode C link_resolver returns N og:image URLs,
**THE SYSTEM SHALL**:

1. Filter pins to those with valid HTTPS image URLs (host validation reusing the SSRF guard from `models/request.py`). Mode C URLs are already SSRF-checked by `link_resolver._safe_get` — but the post-resolve image_url is re-checked here for defense in depth.
2. For each remaining pin, call the existing `EmbedProvider` (Modal `/embed`) AND the existing `app/channels/vision.py` Vision v2 schema extractor in parallel (max concurrency: `min(N, 5)`).
3. Aggregate the resulting `brand` / `searchQuery` / `style` / `colorFamily` / `mood` tokens across all pins into a weighted dict.
4. Call `taste_store.seed_from_onboarding(user_key, weights=aggregated_dict)` with `weight = ONBOARDING_PINTEREST_PIN_WEIGHT` (default `0.5`) per pin contribution. Mode is irrelevant for weighting — a pin is a pin regardless of whether it came from a board, profile, or direct URL.
5. Send a completion message "🎉 {N}개 핀 분석 완료, 취향 시드 만들었어요 / 🎉 Analyzed {N} pins, taste seed ready" with N = number of successfully analyzed pins.

**THE SYSTEM SHALL** complete this entire pipeline within `APIFY_PINTEREST_TIMEOUT_S + 60` seconds (default `90s` total wall-clock from URL receipt to completion message). If exceeded → degraded path with "분석에 시간이 너무 오래 걸려요. 카드 선택만으로 시작할게요."

**Acceptance**:

- Integration test (mocked Modal + Vision): 5 pin inputs → assert 5 embed calls + 5 vision calls fired in ≤ 2 concurrent batches → assert `seed_from_onboarding` called once with aggregated dict containing tokens from all 5.
- Integration test: 50 pin inputs with one Vision call hanging → assert per-call timeout (`VISION_TIMEOUT_MS` from existing env) kicks in, hanging pin's tokens are NOT included in the seed, completion message reports `N = 49`.
- The aggregation SHALL deduplicate identical tokens (e.g., 10 pins all having brand "ami" → contributes a single weighted entry, not 10 separate weights — the existing decay logic in `reinforce_*` handles repeated reinforcement; here we collapse per-onboarding-session at the source).

---

### Taste profile seeding (REQ-ONBOARD-SEED-*)

#### REQ-ONBOARD-SEED-001 — `seed_from_onboarding` SHALL be additive, NOT overwrite [P0] (Ubiquitous)

**THE SYSTEM SHALL** add a new method to the `TasteProfileStore` Protocol:

```python
async def seed_from_onboarding(
    self,
    user_key: str,
    keyword_weights: dict[str, float],
    brand_weights: dict[str, float] | None = None,
) -> None: ...
```

This method SHALL:

1. Call `get_or_create(user_key)` to retrieve the current profile (creates fresh empty if no row).
2. For each `(keyword, weight)` pair, call the existing `reinforce_liked_keywords([keyword], weight=weight)` cascade — which applies the decay-then-add semantics already in production.
3. For each `(brand, weight)` pair, call `reinforce_liked_brand(brand, weight=weight)`.
4. Persist via `update(profile)`.

**THE SYSTEM SHALL NOT** clear, overwrite, or replace any existing `liked_brands` / `liked_keywords` / `disliked_*` weights.

**Acceptance**:

- Integration test: profile has `liked_brands={"ami": 0.6}` pre-existing. Call `seed_from_onboarding(user_key, keyword_weights={"oversized": 0.7}, brand_weights={"lemaire": 0.7})`. Read back: assert `liked_brands == {"ami": (0.6 * decay) , "lemaire": (some_value > 0)}` AND `liked_keywords == {"oversized": (some_value > 0)}`. The exact post-decay values are validated only by ordering (lemaire > 0, ami still present), NOT exact equality — decay multiplier is implementation detail.
- Integration test: re-trigger onboarding on the same user; assert weights *increase* (additive), never reset to onboarding-only values.

#### REQ-ONBOARD-SEED-002 — Card seed weight SHALL be in `(no-click weight, click weight)` interval [P0] (Ubiquitous)

**THE SYSTEM SHALL** apply weight `ONBOARDING_CARD_SEED_WEIGHT` (default `0.7`) when seeding from card selections, AND weight `ONBOARDING_PINTEREST_PIN_WEIGHT` (default `0.5`) per-pin when seeding from Pinterest.

The defaults SHALL satisfy:

- `0.2 < ONBOARDING_CARD_SEED_WEIGHT ≤ 1.0` — strictly above SPEC-IMPLICIT-FB-001's `IMPLICIT_NO_CLICK_WEIGHT` (0.2 default), at-or-below `IMPLICIT_CLICK_WEIGHT` (1.0 default).
- `0.2 < ONBOARDING_PINTEREST_PIN_WEIGHT ≤ ONBOARDING_CARD_SEED_WEIGHT` — Pinterest is a softer signal than an explicit card tap.

**Acceptance**:

- A config test asserts the default values satisfy the inequalities.
- A test asserts that if an operator overrides with values outside the interval (e.g., `ONBOARDING_CARD_SEED_WEIGHT=0.1`), startup logs a WARN (`[ONBOARD] weight outside recommended range`) but does not crash — the operator's override is respected.

---

### 마이그레이션 & 호환성 (REQ-ONBOARD-MIGRATION-*)

#### REQ-ONBOARD-MIGRATION-001 — Existing users SHALL be backfilled as already-onboarded [P0] (Ubiquitous)

**THE SYSTEM SHALL** introduce an Alembic revision (filename pattern `migrations/versions/XXXX_add_onboarded_at_to_user_session.py`) that:

1. Adds `user_session.onboarded_at: timestamptz NULL` column.
2. Backfills `UPDATE user_session SET onboarded_at = now() WHERE onboarded_at IS NULL` as part of the SAME revision (one `op.execute` after `op.add_column`).
3. Downgrade SHALL `op.drop_column("user_session", "onboarded_at")`.

**THE SYSTEM SHALL NOT** add a NOT NULL constraint — `onboarded_at IS NULL` is the canonical "new user" sentinel.

**Acceptance**:

- Alembic test (`testcontainers[postgres]`): pre-populate `user_session` with 3 rows, run `alembic upgrade head`, assert all 3 rows have `onboarded_at IS NOT NULL` AND new INSERTs without explicit `onboarded_at` default to `NULL`.
- Alembic test: `alembic downgrade -1` removes the column cleanly.

#### REQ-ONBOARD-MIGRATION-002 — `Session` dataclass extension SHALL be backward-compatible [P0] (Ubiquitous)

**THE SYSTEM SHALL** add `onboarded_at: datetime | None = None` to the `Session` dataclass in `app/channels/session.py`. The default value `None` SHALL ensure:

- Existing test fixtures that construct `Session(chat_id=...)` without the new field continue to work.
- The `PostgresSessionStore._to_jsonable` cascade handles `datetime` → JSONB serialization via the existing `default=str` fallback in tier 5 (no new tier needed).
- `_from_db_row` round-trips the `timestamptz` column as a `datetime` (UTC) or `None`.

**Acceptance**:

- A test constructs `Session(chat_id=1)` with no other args, writes via `PostgresSessionStore.update`, reads back, asserts `onboarded_at is None`.
- A test sets `onboarded_at = datetime.now(UTC)`, round-trips, asserts equality within microsecond tolerance.

---

### 상태 머신 & 그래프 토폴로지 (REQ-ONBOARD-GRAPH-*)

#### REQ-ONBOARD-GRAPH-001 — Five new nodes SHALL be added without modifying existing nodes [P0] (Ubiquitous)

**THE SYSTEM SHALL** register five new nodes in `app/graphs/fashion_bot.py`:

- `onboard_intro`
- `onboard_mood`
- `onboard_color`
- `onboard_fit`
- `onboard_pinterest`

AND SHALL add exactly the following edges:

- `ingest` → `onboard_intro` (conditional: `onboarding_required(state)` true)
- `onboard_intro` → `onboard_mood` (unconditional after intro message sent)
- `onboard_mood` → `onboard_color` (conditional: `next` callback + min bounds met)
- `onboard_color` → `onboard_fit` (conditional: `next` callback + min bounds met)
- `onboard_fit` → `onboard_pinterest` (conditional: `PINTEREST_BOOTSTRAP_ENABLED` AND `APIFY_TOKEN` set)
- `onboard_fit` → END (conditional: Pinterest disabled)
- `onboard_pinterest` → END (always, after URL handling OR skip OR degraded)

**THE SYSTEM SHALL NOT** modify any of the existing 12 nodes' implementation. A diff of `nodes/{vision,resolve_image,pick_item,ask_clarify,apply_clarify,search,evaluator,critique_apply,send_results,taste_update,respond}.py` between this SPEC's start and end state SHALL show zero changes.

**Acceptance**:

- A topology snapshot test (`tests/test_graph_topology.py` extended) compares the registered nodes list pre/post SPEC and asserts exactly the 5 additions, no removals.
- A diff-based test (`git diff` analyzed) asserts none of the 12 existing node files were touched.

#### REQ-ONBOARD-GRAPH-002 — Mid-flow drop SHALL persist progress [P0] (State-driven)

**WHILE** a user is mid-onboarding (`WorkingState.onboard_stage in {"mood","color","fit","pinterest"}`),
**THE SYSTEM SHALL** persist `onboard_stage` and `onboard_selections` to `user_session` on every node exit (via the existing `session_store.update(sess)` call pattern). This SHALL ensure that if the user drops mid-flow and returns hours later (within `SESSION_TTL_SECONDS`, default 1800s = 30 min), they resume at the same stage with prior selections preserved.

**WHEN** a user with a non-expired session sends `/start` mid-flow (stage != "done" AND stage != None),
**THE SYSTEM SHALL** resume from the persisted stage, NOT restart.

**WHEN** the session has expired (lazy TTL — SPEC-MEMORY-001 REQ-MEMORY-SESSION-002),
**THE SYSTEM SHALL** restart from `onboard_intro` since the persisted session row was replaced with defaults.

**Acceptance**:

- Integration test: complete Stage 1 (mood) → simulate webhook delivery delay 10 minutes → send `/start` → assert Stage 2 (color) card is sent, NOT mood card; assert `selections["mood"]` is preserved.
- Integration test: complete Stage 1 → wait until `ttl_expires_at < now()` → send `/start` → assert mood card is sent fresh, `selections == {}`.
- The 2 new `WorkingState` fields (`onboard_stage`, `onboard_selections`) SHALL be added to `app/graphs/state.py` AND to `app/channels/session.py` `Session` dataclass with `default_factory=lambda: ({"mood": [], "color": [], "fit": []})` for the dict and `None` for the stage marker.

---

### 언어 & UX (REQ-ONBOARD-LANG-*)

#### REQ-ONBOARD-LANG-001 — Sticky language SHALL apply from intro message onward [P0] (State-driven)

**WHILE** a user is in any onboarding stage,
**THE SYSTEM SHALL** render all bot messages (intro, card prompts, button labels, error toasts, completion message) in `session_lang(sess)` (per SPEC-AGENT-001 + `app/channels/lang.py`).

**WHEN** the user has no prior session,
**THE SYSTEM SHALL** initialize `Session.lang = settings.BOT_DEFAULT_LANG` (default `"ko"`).

**WHEN** the user sends a text message during onboarding that contains Hangul,
**THE SYSTEM SHALL** update `Session.lang = "ko"` (existing `remember_lang` behavior).

**WHEN** the user sends a text message containing no Hangul and at least one Latin letter,
**THE SYSTEM SHALL** update `Session.lang = "en"`.

**Acceptance**:

- Integration test: fresh session with `BOT_DEFAULT_LANG=ko`, user taps buttons only — assert all messages are KO.
- Integration test: fresh session with `BOT_DEFAULT_LANG=ko`, user types "let me restart" mid-flow — assert subsequent messages flip to EN.
- Integration test: language-agnostic callback `value` strings (e.g., `mood:minimal`) are unchanged regardless of `Session.lang`.

#### REQ-ONBOARD-LANG-002 — Intro message SHALL follow the documented format [P0] (Ubiquitous)

**THE SYSTEM SHALL** send, as the first message in the flow, an intro consisting of:

1. **3-line greeting** (kiko persona, KO/EN sticky). 예시 KO:
   - "안녕하세요, kiko 예요. 🐱"
   - "당신만의 패션 큐레이터로 함께할게요."
   - "처음이니까 취향부터 알아볼게요!"
2. **3-line usage guide**:
   - "📸 사진을 보내면 비슷한 옷을 찾아드려요."
   - "🔗 핀터레스트나 인스타 링크도 OK."
   - "💬 '오버핏 좋아해' 같은 자연어도 받아요."
3. A trailing line "먼저 무드부터 골라볼까요? ↓ / Let's start with mood ↓"
4. Followed immediately (same webhook turn) by the Stage 1 mood card.

**Acceptance**:

- Snapshot test asserts the exact KO text and EN text strings emitted (the strings live in `onboarding_values.py::INTRO_LINES_KO` / `INTRO_LINES_EN`).
- The intro + mood card MUST arrive within 2 seconds (REQ-ONBOARD-PERF-001).

---

### 완료 & 다음 단계 (REQ-ONBOARD-COMPLETION-*)

#### REQ-ONBOARD-COMPLETION-001 — Completion SHALL set `onboarded_at` atomically with seed [P0] (Ubiquitous)

**WHEN** onboarding reaches its terminal step (Stage 3 [Next] → Pinterest skipped, OR Pinterest success, OR Pinterest degraded),
**THE SYSTEM SHALL** within a single logical turn:

1. Call `taste_store.seed_from_onboarding(user_key, ...)` with the aggregated card + Pinterest weights.
2. Set `Session.onboarded_at = datetime.now(UTC)` AND call `session_store.update(sess)` so the column is persisted.
3. Send a completion message:
   - Pinterest success: "🎉 N개 핀 분석 완료. 사진 보내주시면 추천해 드릴게요!"
   - Pinterest skipped/degraded: "✨ 취향 잘 기억해 둘게요. 사진 보내주세요!"
4. Reset `WorkingState.onboard_stage = "done"`.

**Acceptance**:

- Integration test: complete the full flow. Assert (a) exactly one `seed_from_onboarding` call, (b) exactly one `session_store.update` call with `onboarded_at IS NOT NULL`, (c) the user-visible completion message is one of the two documented strings.
- The order MUST be: seed first, then mark `onboarded_at` — so that a crash between (1) and (2) leaves `onboarded_at NULL` and the user re-onboards on next `/start` (a minor duplicate seed is tolerable; an orphaned `onboarded_at` with no seed is NOT).

#### REQ-ONBOARD-COMPLETION-002 — Onboarding SHALL NOT pre-empt subsequent first photo [P0] (Ubiquitous)

**WHEN** a user has just completed onboarding (`onboarded_at` just set, `WorkingState.onboard_stage == "done"`),
**AND** the user immediately sends a photo in the SAME webhook session,
**THE SYSTEM SHALL** route the photo through the normal `ingest` → `resolve_image` → `vision` → `search` → `send_results` pipeline (the existing 12-node graph), NOT loop back into onboarding.

**Acceptance**:

- Integration test: complete onboarding → send photo URL in next webhook → assert the photo flows to `vision_node` and `send_results` returns recommendations.

---

### 관측 (REQ-ONBOARD-OBS-*)

#### REQ-ONBOARD-OBS-001 — Onboarding events SHALL emit Langfuse spans [P1] (Ubiquitous)

**THE SYSTEM SHALL** decorate each of the 5 new node entry functions with the existing `@observe` decorator from `app/observability/langfuse.py`, using span names:

- `onboarding.intro`
- `onboarding.stage.mood`
- `onboarding.stage.color`
- `onboarding.stage.fit`
- `onboarding.stage.pinterest`

Each span SHALL include metadata:

- `stage`: the stage name
- `selections_count`: `len(selections[stage])` if applicable
- `lang`: the resolved `session_lang`

**THE SYSTEM SHALL NOT** include raw `chat_id` or `from_user_id` in span metadata (PII rule per SPEC-AGENT-001 REQ-OBSV-005). Use the existing `user_key_hash` helper.

**Acceptance**:

- Unit test against a Langfuse mock asserts the 5 spans appear with the correct names + metadata keys.
- A test asserts no span contains raw integer chat_id values (string-match search for the test fixture's chat_id value in span payload returns nothing).
- When `@observe` is the no-op fallback (current state per SPEC-MEMORY-001 R-OBS), the decoration is a pass-through.

---

### 성능 & 보안 (REQ-ONBOARD-PERF-*, REQ-ONBOARD-SEC-*)

#### REQ-ONBOARD-PERF-001 — Total onboarding wall-clock SHALL be ≤ 5 minutes for the median user [P1] (Ubiquitous)

**THE SYSTEM SHALL** target a median end-to-end onboarding completion time of ≤ 300 seconds (5 min) from `/start` to the completion message, excluding sustained user inactivity. Per-step latency budgets:

- `/start` → Intro + Stage 1 card: **≤ 2s** (server-side; hard requirement per REQ-ONBOARD-ENTRY-001).
- Stage N tap → Stage N+1 card: **≤ 1s** (server-side).
- Pinterest URL → completion: **≤ 90s** total (REQ-ONBOARD-PINTEREST-004).

**Acceptance**:

- A benchmark test simulates the full flow with mocked Apify (returning 20 pins in 5s) and Vision (50ms each) and asserts total wall-clock ≤ 30s with all stages auto-tapped.
- A latency test asserts the `/start` → first message round-trip is < 2s under `pytest -p no:randomly`.

#### REQ-ONBOARD-SEC-001 — Apify token and Pinterest URLs SHALL NOT leak [P0] (Unwanted-behaviour)

**THE SYSTEM SHALL NOT** log:

- `APIFY_TOKEN` (raw or partial).
- Full Pinterest URLs (truncate to host + 8 chars of path, e.g., `pinterest.com/user/abc...`).
- Full image URLs of scraped pins (host + 6 chars of path).

**THE SYSTEM SHALL** validate every scraped pin's `image_url` host against an HTTPS-only allowlist before passing to Modal `/embed` — reusing the SSRF guard from `models/request.py::RecommendRequest.image_url`.

**Acceptance**:

- A log-capture test reproduces a Pinterest scrape and asserts the captured log lines contain NO occurrence of the test Apify token string, NO occurrence of the full URL.
- A test injects a pin with `image_url=javascript:alert(1)` and asserts it is filtered out before Modal call (no embed RPC issued for it).

---

## Environment Variables (introduced or modified by this SPEC)

| Var | Required | Default | Description |
|---|---|---|---|
| `PINTEREST_BOOTSTRAP_ENABLED` | no | `true` | Master switch for Stage 4 AND continuous Pinterest ingest (REQ-ONBOARD-PINTEREST-CONTINUOUS). When `false`, Pinterest card never shown and continuous routing disabled. REQ-ONBOARD-PINTEREST-001. |
| `APIFY_TOKEN` | no | — | Apify API token. **When unset/empty: modes A/B (board/profile) are disabled but mode C (individual pin URLs) still works via link_resolver.** Card is still shown when `PINTEREST_BOOTSTRAP_ENABLED=true` regardless of token. REQ-ONBOARD-PINTEREST-001. |
| `APIFY_PINTEREST_ACTOR_ID` | no | `epctex/pinterest-scraper` | Apify actor slug. Supports both board and profile modes. Confirmed available as of 2026-05-14 (web verified). REQ-ONBOARD-PINTEREST-003. |
| `APIFY_PINTEREST_MAX_ITEMS` | no | `80` | Cap on pins to scrape per board OR profile. Range `[20, 150]`. REQ-ONBOARD-PINTEREST-003. |
| `APIFY_PINTEREST_TIMEOUT_S` | no | `30.0` | Hard deadline for the Apify call (board or profile mode). REQ-ONBOARD-PINTEREST-003. |
| `PINTEREST_MAX_PINS_PER_TURN` | no | `20` | Cap on individual pin URLs accepted in a single message (mode C). REQ-ONBOARD-PINTEREST-CLASSIFY. |
| `PINTEREST_CONTINUOUS_RATELIMIT_S` | no | `300` | Min seconds between continuous Pinterest ingests per user_key (modes B/C outside onboarding). REQ-ONBOARD-PINTEREST-CONTINUOUS. |
| `ONBOARDING_CARD_SEED_WEIGHT` | no | `0.7` | `seed_from_onboarding` weight per card-derived keyword. Must satisfy `0.2 < w ≤ 1.0`. REQ-ONBOARD-SEED-002. |
| `ONBOARDING_PINTEREST_PIN_WEIGHT` | no | `0.5` | Per-pin weight contribution. Must satisfy `0.2 < w ≤ ONBOARDING_CARD_SEED_WEIGHT`. REQ-ONBOARD-SEED-002. |
| `BOT_DEFAULT_LANG` | no | `ko` | Initial `Session.lang` for first-time users with no language signal yet. REQ-ONBOARD-LANG-001. |

All new vars are read once at startup via `app/core/config.py::Settings` and exposed as typed properties.

---

## Non-Goals (out of scope for this SPEC)

1. **Instagram saved-posts import.** A separate future SPEC (`SPEC-ONBOARD-IG-001` or similar) will handle IG bootstrap once IG OAuth flow exists in `kikoai/app`. Pinterest is the only external import in scope here.
2. **Group chat onboarding.** Telegram group chats are out of scope — the bot's primary surface is 1:1 DM. Group chat `/start` SHALL be treated as a no-op (existing behavior preserved).
3. **QR deeplink token bind.** "Scan this QR to start onboarding" landing flow is out of scope.
4. **Product catalog crawler automation.** Pinterest pin → product DB ingestion is out of scope — Pinterest pins are used only for taste profile seeding, NOT to expand the product catalog.
5. **`pin.it` deep resolution.** `pin.it` shortened URLs are accepted (host allowlisted) but actual short-URL → board URL resolution depends on the Apify actor's handling. If the actor cannot follow `pin.it`, the user is asked to send a `pinterest.com/...` URL instead (graceful degrade path).
6. **Card option personalization.** All users see the same 8 moods / 6 colors / 4 fits. A/B testing or per-user reordering is deferred.
7. **Real-time card mood A/B testing.** The 8 mood values are static; no Bayesian bandit selection.
8. **Onboarding survey analytics dashboards.** Beyond Langfuse spans, no separate dashboards / metrics pipelines.
9. **Multi-tenant / multi-bot scaling.** Single bot, single Telegram channel.
10. **Account merging.** If a user changes Telegram accounts mid-onboarding, the new `chat_id` starts fresh (no merge logic).
11. **Pinterest privacy-locked boards.** Private/locked boards return empty from Apify → degraded path. No special "this board is private" message.
12. **GDPR-style "forget my onboarding" endpoint.** No user-facing delete endpoint for `TasteProfile` or `onboarded_at`. Deferred (matches SPEC-MEMORY-001 Non-Goal #15).
13. **Onboarding completion notification webhook to `kikoai/app`.** kikoai/app is unaware of onboarding state; this SPEC is kikoai/ai-internal.
14. **Card option enum sync with kikoai/app `analyze.ts`.** The onboarding card options do NOT need to match Vision schema enums (`subcategory`, `fit` etc.) verbatim — they are independent taste-seed keywords. Existing SPEC-VISION-UNIFY-001 enums stay untouched.
15. **Overwrite mode for re-onboarding.** Always additive merge per REQ-ONBOARD-SEED-001. Hard reset / privacy-delete is a separate SPEC.
16. **More than 4 stages.** This SPEC defines exactly 3 mandatory card stages + 1 optional Pinterest stage. Adding a 5th stage requires a new SPEC revision.
17. **Private Pinterest "Saved" pins / Idea Pins / secret boards** (v0.2.0 Non-Goal D). These require Pinterest OAuth authorization — the user would have to grant kiko.ai permission to read their private content. Out of scope for this SPEC. Only PUBLIC pins/boards/profiles accessible via Apify's anonymous scrape are supported. A future SPEC (`SPEC-PINTEREST-OAUTH-001` or similar) MAY introduce OAuth-gated private content if the product direction demands it.
18. **Resolution of `pin.it` shortened URLs in mode C** beyond what `link_resolver.py` already does. The existing module follows redirects manually with SSRF guards; that path is reused as-is. No new short-URL expander module.
19. **Mixed-mode messages where the user intends "use both"** — e.g., "use this board AND these pins". The classifier applies PIN > BOARD > PROFILE precedence and drops the lower-precedence URLs. Users wanting both must send two separate messages (5 min apart due to rate-limit). Documented as expected behavior, not a bug.

---

## Exclusions (What NOT to Build)

(Mirrors Non-Goals — explicit list for SPEC-checker compliance.)

1. No Instagram import.
2. No group chat support.
3. No QR deeplink token bind.
4. No product catalog ingestion from Pinterest pins.
5. No deep `pin.it` short-URL resolution beyond Apify's native handling.
6. No per-user card option personalization.
7. No A/B testing on card option order/wording.
8. No dedicated onboarding analytics dashboards beyond Langfuse spans.
9. No multi-tenant scaling.
10. No account merging.
11. No special handling of private/locked Pinterest boards beyond the generic degraded path.
12. No user-facing privacy/delete endpoint for onboarding data.
13. No webhook notification to `kikoai/app` on onboarding completion.
14. No enum sync between onboarding values and Vision v2 schema enums.
15. No "overwrite" mode for re-onboarding — additive only.
16. No 5th+ card stage.
17. No Pinterest OAuth flow / private Saved pins / Idea Pins / secret boards (v0.2.0 Non-Goal D).
18. No `pin.it` short-URL expander beyond existing `link_resolver.py` redirect-following.
19. No multi-mode "use both board AND these pins" — classifier precedence applies; users send separate messages.

---

## Stakeholders

| Role | Responsibility |
|---|---|
| Product / Founder (hchsa77@gmail.com) | Approves the noscroll-inspired UX direction, the 3-stage card list (mood/color/fit + option labels), the additive-merge re-onboarding policy (Non-Goal #15), the Pinterest weight defaults (REQ-ONBOARD-SEED-002), the existing-user backfill policy (REQ-ONBOARD-MIGRATION-001). |
| AI Server Owner (this SPEC) | All work in `app/graphs/nodes/onboard_*.py` (NEW), `app/channels/onboarding_cards.py` (NEW), `app/channels/onboarding_values.py` (NEW), `app/providers/apify.py` (NEW), `app/graphs/fashion_bot.py` (MODIFIED), `app/api/webhooks/telegram.py` (MODIFIED `/start` parsing + re-trigger keywords), `app/core/config.py`, `app/channels/session.py` + `_pg.py`, `app/channels/taste_profile.py` + `_pg.py` (additive method), `migrations/`, `pyproject.toml`. |
| dev-app Postgres operator | Reviews and applies the new Alembic revision adding `onboarded_at` column. Verifies backfill ran for all existing rows. |
| Apify (third-party) | No coordination needed; we treat it as a managed service. Quota responsibility falls on the AI Server Owner. |
| Langfuse operator | No action — `@observe` no-op behavior preserved until SPEC-OBSERVABILITY-002 activates. |
| Modal team | Out of scope. Existing `/embed` endpoint is reused. |
| kikoai/app team | Out of scope. This is kikoai/ai-internal flow. |

---

## Risks & Mitigations

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | **Apify actor `epctex/pinterest-scraper` is renamed or removed**, breaking Pinterest stage silently. | Low | Medium | `APIFY_PINTEREST_ACTOR_ID` env var allows hot-swap. Apify call wrapped in catch-all that degrades to "Pinterest fetch failed" — bot keeps working. Lock the actor version (`@latest` → pinned version tag in `plan.md`). |
| R2 | **Apify cost overrun**. At 80 pins × $0.01–0.02 / 100 listings = ~$0.01 per onboarding. 1000 onboardings = $10. Acceptable for POC but unbounded for growth. | Medium | Low | Hard cap `APIFY_PINTEREST_MAX_ITEMS=80` (≤ 150). Rate-limit per `user_key` (1 Pinterest scrape per 24h, even on re-onboarding) — implemented as a "skip with already-bootstrapped" message if `onboarded_at < 24h ago` AND user is in `/reset` re-flow with Pinterest. `plan.md` decides exact rate-limit mechanism. |
| R3 | **`TasteProfileStore.seed_from_onboarding` extends the Protocol** — directly contradicts SPEC-MEMORY-001 REQ-MEMORY-PROTOCOL-001 ("Protocol surface SHALL be unchanged"). | High | Medium | This SPEC explicitly amends SPEC-MEMORY-001 with a single additive method on `TasteProfileStore`. The contradiction is acknowledged and resolved here: REQ-ONBOARD-SEED-001 supersedes REQ-MEMORY-PROTOCOL-001 for this specific method. Both InMemory and Postgres backends MUST implement it. The Protocol's other methods remain frozen. A note SHALL be added to SPEC-MEMORY-001's HISTORY in a follow-up commit (Open Question 2). |
| R4 | **Card UI re-render via `editMessageReplyMarkup` may fail** for very old card messages (Telegram 48h edit limit). | Low | Low | Onboarding flow timeout is the SESSION_TTL (default 30 min) — far below 48h. Even if exceeded, falling back to a new `sendMessage` is acceptable noise. |
| R5 | **Concurrent webhook deliveries during onboarding** could produce out-of-order toggles (user double-taps quickly). | Medium | Low | The existing in-process `asyncio.Lock` per chat_id (SPEC-MEMORY-001 REQ-MEMORY-PROTOCOL-001) serializes turns within a single worker. Out-of-order toggle delivery is at worst a UI redraw glitch — `selections` state is read fresh on every turn. |
| R6 | **Pinterest URLs contain personally-identifying info** (board names, user handles) that may end up in logs or Langfuse traces. | Medium | Medium | REQ-ONBOARD-SEC-001 truncates URLs in logs. Langfuse spans include only `host` + `selections_count`, not full URLs. PII rule from SPEC-AGENT-001 REQ-OBSV-005 applies. |
| R7 | **Card option drift**: future product team wants to add/remove a mood/color/fit option. Touching `onboarding_values.py` retroactively re-shifts callback semantics for in-flight sessions. | Low | Low | The `value` strings (snake_case keys like `mood:minimal`) are stable identifiers — changing labels (`label_ko`/`label_en`) is safe; changing `value` is breaking. A snapshot test (REQ-ONBOARD-CARDS-001) enforces `value` stability per stage. New options MAY be added (`mood:dopamine` etc.) without breaking — old sessions just won't have new values in their selections. |
| R8 | **`onboarded_at` backfill races with new signups** between revision apply and code deploy. | Low | Low | Standard deploy order: apply revision FIRST (backfill all existing rows to `onboarded_at=now()`), THEN deploy code. During the gap, new signups have `onboarded_at NULL` from the (pre-deploy) code's default, but the new code will pick them up as "needs onboarding" — which is the correct semantic. Documented in `plan.md` cutover checklist. |
| R9 | **Vision LLM batch cost** spikes during Pinterest stage (50–100 LLM calls per onboarding). | Medium | Medium | Concurrency cap of 5 (REQ-ONBOARD-PINTEREST-004). At ~1¢ per Vision call, 100 pins ≈ $1 per onboarding's Pinterest stage. Cap `APIFY_PINTEREST_MAX_ITEMS=80` keeps it under $1. `plan.md` decides whether to use a cheaper Vision model (`gpt-4o-mini` already) and/or filter pins to top-K by Pinterest's own popularity score before Vision. |
| R10 | **Sticky language flip mid-flow** could produce mixed-language messages if a stage card is rendered before the language flip processes. | Low | Low | Language is resolved at node entry from the persisted `Session.lang`. A language flip in turn N takes effect at turn N+1 (the very next bot message). One message of stale language is acceptable. |
| R11 | **Confirmation card "다시 시작할까요?" gets stale** if the user re-sends `/start` repeatedly. Each `/start` re-sends the card → chat clutter. | Low | Low | The confirmation card response uses `editMessageReplyMarkup` on the most recent confirmation card if one exists in the last 60s. `plan.md` decides exact mechanic. |
| R12 | **`/reset` collision with future commands**. If we later add `/reset_password` or similar, the prefix match `^/reset\b` correctly isolates `/reset` only. | Low | Low | The regex anchor `\b` boundary prevents false matches. Documented in REQ-ONBOARD-ENTRY-002. |

---

## Open Questions (deferred to plan.md / implementation)

1. **Exact Alembic revision filename** for the `onboarded_at` column. Lean toward `migrations/versions/0003_add_onboarded_at_to_user_session.py` (assumes SPEC-IMPLICIT-FB-001's `card_impression` is `0002`). `plan.md` confirms ordering.
2. **Amendment of SPEC-MEMORY-001 REQ-MEMORY-PROTOCOL-001** to acknowledge the new `seed_from_onboarding` method (R3). Either (a) update SPEC-MEMORY-001 HISTORY with a follow-up entry, or (b) leave it as a known cross-SPEC contradiction documented here. `plan.md` decides on disposition (preferred: do (a) for cleanliness).
3. **`pin.it` short-URL handling**: does the Apify actor follow them automatically, or do we need a separate HEAD request to expand? `plan.md` validates against the actor's docs at implementation time.
4. **Vision batch concurrency value**. Default `min(N, 5)` is conservative. May increase to 10 after observing real Modal QPS. `plan.md` decides post-prototype.
5. **Per-user Pinterest rate limit mechanism** (R2). Options: (a) check `onboarded_at` recency, (b) dedicated `pinterest_scrape_attempted_at` column, (c) Redis-like ephemeral counter. Lean (a) — least new state.
6. **Re-onboarding's notice line wording** — "지금 선택은 기존 취향에 더해집니다" is the lean copy; final wording per Product (REQ-ONBOARD-ENTRY-002).
7. **Stage 1 minimum 2 selections** — should it be 1 minimum like fit? Lean keeps 2 (forces deliberation; matches noscroll's "pick at least 3 followees" pattern). Product may reduce to 1.
8. **Card label max length validation** — REQ-ONBOARD-CARDS-001 caps at 16 chars; some labels (e.g., "Clean Girl") are 10 chars EN. Verify Telegram's actual inline button width on iOS/Android in implementation testing.

---

## Cross-References

- **Builds on**:
  - SPEC-MEMORY-001 (Postgres-backed Session / TasteProfile — `onboarded_at` column added on `user_session`, `seed_from_onboarding` method added to `TasteProfileStore`).
  - SPEC-CLARIFY-CARDS-001 (inline-keyboard card infrastructure — `send_card` adapter method, callback_data routing pattern, KO label conventions).
  - SPEC-IMPLICIT-FB-001 (`TasteProfile.reinforce_*` weight API — onboarding seeds use the same weight semantics).
  - SPEC-AGENT-001 (LangGraph 12-node topology — 5 new nodes added without modification of existing nodes; sticky language pattern reused).
  - SPEC-MSG-001 (channel transport, Telegram adapter — `send_card` / `send_text` / `editMessageReplyMarkup` calls).
  - SPEC-VISION-UNIFY-001 (Vision v2 schema reused for Pinterest pin analysis).
- **Amends (cross-SPEC contradiction documented)**:
  - SPEC-MEMORY-001 REQ-MEMORY-PROTOCOL-001 (additive Protocol method `seed_from_onboarding` — see R3 + Open Question 2).
- **Triggers / unblocks**:
  - Future SPEC-ONBOARD-IG-001 (Instagram saved-posts import — same shape, different scraper provider).
  - Future SPEC-RETENTION-001 (analytics on onboarding → first photo → first purchase funnel, depends on `onboarded_at` column).
- **Project context**: `/Users/hansangho/Desktop/kikoai/ai/CLAUDE.md`.
- **Research basis / benchmark**: `docs/_tmp/noscroll-benchmark.html` (noscroll.com — SMS/Telegram AI news agent bootstrap pattern), `docs/research/conversational-shopping-agents.md` takeaway #4 (cold-start gap is the 1st retention driver).

---

## Definition of Done (P0)

- [ ] REQ-ONBOARD-ENTRY-001 / 002 / 003 implemented. `/start` correctly gates on `onboarded_at`; explicit re-trigger keywords work; confirmation card branches both observed.
- [ ] REQ-ONBOARD-CARDS-001 / 002 / 003 implemented. Card catalog matches the documented tables; multi-select min/max enforced; skip flows.
- [ ] REQ-ONBOARD-PINTEREST-001 / 002 / 003 / 004 implemented. Feature flag + token gate; URL host allowlist + SSRF guard; 30s Apify timeout + graceful degrade; Vision batch with concurrency cap; total ≤ 90s budget. **All three modes (board/profile/pins) covered.**
- [ ] **REQ-ONBOARD-PINTEREST-CLASSIFY implemented.** `classify_pinterest_input` 4-way taxonomy with PIN > BOARD > PROFILE precedence; 20-pin cap; `urlsplit`-based parsing (no regex-only); snapshot tests for 20+ URL shapes.
- [ ] **REQ-ONBOARD-PINTEREST-CONTINUOUS implemented.** `pinterest_ingest` node reachable from text-handling path via `is_continuous_pinterest(state)` conditional edge; additive merge into existing TasteProfile; no `onboarded_at` mutation; 5-min rate-limit per user_key; mode-specific confirmation messages.
- [ ] **`link_resolver.resolve_batch(urls, concurrency=5)` extension** implemented with `asyncio.gather` + `Semaphore`. Existing single-URL `resolve()` semantics preserved (failure → `[]`).
- [ ] **`app/providers/apify.py::run_pinterest_scrape(url, mode)`** supports `Literal["board","profile"]`. Mode propagates to actor input payload.
- [ ] REQ-ONBOARD-SEED-001 / 002 implemented. `seed_from_onboarding` additive in both backends; weight defaults satisfy the documented inequalities; out-of-range overrides log WARN but don't crash.
- [ ] REQ-ONBOARD-MIGRATION-001 implemented. Alembic revision adds `onboarded_at` column + backfills existing rows to `now()`; downgrade clean.
- [ ] REQ-ONBOARD-MIGRATION-002 implemented. `Session` dataclass extension is backward-compatible; round-trip via `PostgresSessionStore` preserves `onboarded_at` (or `None`).
- [ ] REQ-ONBOARD-GRAPH-001 / 002 implemented. Exactly 5 new nodes + 7 new edges; zero changes to the 12 existing node files (verified by `git diff` snapshot test); mid-flow drop persists progress within SESSION_TTL.
- [ ] REQ-ONBOARD-LANG-001 / 002 implemented. Sticky language honored across all stages; KO/EN intro snapshots match documented strings; callback `value` strings language-agnostic.
- [ ] REQ-ONBOARD-COMPLETION-001 / 002 implemented. Seed-then-mark ordering enforced; completion message displays the right variant; subsequent photo flows through 12-node pipeline normally.
- [ ] REQ-ONBOARD-OBS-001 implemented. 5 Langfuse spans emitted with correct names; PII rule honored (no raw chat_id).
- [ ] REQ-ONBOARD-PERF-001 implemented. `/start` → first message ≤ 2s asserted; total mocked-flow wall-clock ≤ 30s.
- [ ] REQ-ONBOARD-SEC-001 implemented. APIFY_TOKEN never logged; Pinterest URL host allowlist applied; image_url SSRF guard reused.
- [ ] **Coverage target (TRUST 5 Tested):** New modules `app/graphs/nodes/onboard_intro.py`, `onboard_mood.py`, `onboard_color.py`, `onboard_fit.py`, `onboard_pinterest.py`, `app/channels/onboarding_cards.py`, `app/channels/onboarding_values.py`, `app/providers/apify.py` each report ≥ 85% line coverage in `pytest --cov`.
- [ ] **Existing test suite remains green.** `pytest -q` count is the same or higher vs the pre-SPEC baseline. The 12 existing node files have no test deltas (asserted by diff).
- [ ] `app/core/config.py` and `.env.example` declare all 8 new env vars with documented defaults and inline policy comments (especially the weight-range policy from REQ-ONBOARD-SEED-002).
- [ ] An end-to-end manual test against the dev Telegram bot exercises:
  - (a) Fresh user `/start` → 3-stage cards completable in ≤ 3 minutes → completion message → next photo returns recommendations with non-zero `boost_keywords` from the seed.
  - (b) Returning user (existing `TasteProfile`, backfilled `onboarded_at`) sends `/start` → confirmation card; tap [아니오] → normal IDLE state.
  - (c) Returning user sends "온보딩 다시" → additive-merge notice + Stage 1 card; complete with different selections → `TasteProfile` shows weights from BOTH onboarding runs (decay-applied to old, additive for new).
  - (d) User reaches Stage 4 → sends invalid URL three times → auto-skip + completion.
  - (e) User reaches Stage 4 → sends valid Pinterest board URL → 30s wait → success or degraded path observed end-to-end on dev environment with a small test board.
  - (f) User reaches Stage 1 → drops mid-flow → returns 5 minutes later with `/start` → resumes at Stage 2 (the persisted stage).
  - (g) `PINTEREST_BOOTSTRAP_ENABLED=false` deploy → fresh user completes Stage 3 → completion message immediately, no Pinterest card.
  - **(h) v0.2.0 — Mode B (Profile URL)**: User reaches Stage 4 → sends `https://pinterest.com/jane/` → classifier returns `PROFILE` → Apify profile mode triggered → N pins analyzed → completion message references "프로필에서 N개 핀".
  - **(i) v0.2.0 — Mode C (5 individual pin URLs in one message)**: User reaches Stage 4 → sends "pinterest.com/pin/111/ pinterest.com/pin/222/ pinterest.com/pin/333/ pinterest.com/pin/444/ pinterest.com/pin/555/" → classifier returns `PINS([5 urls])` → `link_resolver.resolve_batch` returns 5 og:image URLs → batch Vision analysis → seed_from_onboarding called → completion message references "5개 핀".
  - **(j) v0.2.0 — Mixed URLs (1 board + 2 pins)**: User reaches Stage 4 → sends "pinterest.com/jane/fall-coats/ pinterest.com/pin/111/ pinterest.com/pin/222/" → classifier returns `PINS([pin1, pin2])` (precedence: PIN beats BOARD) → only 2 pins analyzed, board URL silently dropped. Documented in user-facing acknowledgment: "📌 2개 핀 분석했어요 (보드 URL은 다음에)".
  - **(k) v0.2.0 — Continuous bootstrap (REQ-ONBOARD-PINTEREST-CONTINUOUS)**: A previously-onboarded user (`onboarded_at` set 1 week ago) sends a single pin URL in a normal text message → `pinterest_ingest` node triggered (NOT onboarding re-entry) → `seed_from_onboarding` merges new pin's tokens into existing TasteProfile → confirmation "📌 1개 핀 분석해서 취향에 더했어요" → `onboarded_at` unchanged → subsequent search uses the merged taste profile.
  - **(l) v0.2.0 — Rate-limit on continuous**: User sends pin URLs at T=0 (success) → again at T=2min → "잠시 후 다시 시도해 주세요 (5분에 한 번)" message → at T=6min same URL → success.
  - **(m) v0.2.0 — APIFY_TOKEN unset, mode C only**: With `APIFY_TOKEN=""`, user sends board URL → degraded "보드/프로필 가져오기는 비활성 — 개별 핀 URL 만 받아요"; user sends 3 pin URLs → mode C succeeds via link_resolver path.
- [ ] `ruff check . && ruff format --check .` passes.
- [ ] `pytest -q` passes; new test files: `tests/test_onboarding/{test_onboard_nodes.py, test_onboarding_cards.py, test_apify_provider.py, test_taste_seed.py, test_pinterest_url_validation.py}` (5 files covering all REQs above, ≥ 25 test cases total).

---

## Implementation Plan Outline (informative — formalized in plan.md)

1. **Schema**: Alembic revision adding `user_session.onboarded_at: timestamptz NULL` + backfill `now()` for existing rows.
2. **Config + provider**: `app/core/config.py` env vars; `app/providers/apify.py` async wrapper around `apify-client`.
3. **Card catalog**: `app/channels/onboarding_values.py` (option tables + KO/EN labels + intro strings) + `app/channels/onboarding_cards.py` (card builders + multi-select toggle helpers).
4. **Session extension**: `Session.onboarded_at` + `onboard_stage` + `onboard_selections` dataclass fields. Postgres mapping in `session_pg.py`.
5. **Taste store extension**: `seed_from_onboarding` method on Protocol + both InMemory and Postgres implementations.
6. **Graph nodes**: 5 new `onboard_*.py` files. Each is a thin function (state → state) that calls the appropriate card builder + emits a `send_card` adapter call.
7. **Graph wiring**: `fashion_bot.py` adds the 5 nodes + 7 conditional edges. `routing.py` adds `onboarding_required()`, `after_onboard_*()` functions.
8. **Webhook router**: `webhooks/telegram.py` parses `/start`, "온보딩 다시", "취향 다시 설정", `/reset` → constructs the appropriate InputState marker for `ingest`.
9. **Tests**: `tests/test_onboarding/` directory with 5 files. testcontainers reused from SPEC-MEMORY-001 setup.
10. **Cutover**: alembic upgrade head on dev-app Postgres → deploy code → smoke-test the 7 manual scenarios → monitor `/health/ready` + Langfuse traces for 24h.

---

## Test Plan Outline (informative — formalized in acceptance.md)

- **Unit (`tests/test_onboarding/test_onboarding_cards.py`)**: card option catalog snapshot, label length, value uniqueness, KO/EN label coverage.
- **Unit (`tests/test_onboarding/test_pinterest_url_validation.py`)**: 20+ URL attack vectors, allowed hosts, `pin.it` shorts, 3-strike auto-skip.
- **Unit (`tests/test_onboarding/test_apify_provider.py`)**: mocked actor success / timeout / empty / 401 / missing token paths.
- **Unit (`tests/test_onboarding/test_taste_seed.py`)**: `seed_from_onboarding` additive merge against pre-existing weights; weight-range validation.
- **Integration (`tests/test_onboarding/test_onboard_nodes.py`)**: full state-machine paths (a)–(g) from the manual test scenarios, with mocked Apify and Vision. Uses testcontainers Postgres for the `onboarded_at` round-trip.
- **Migration test**: testcontainers Postgres, pre-populated `user_session`, run `alembic upgrade head`, assert backfill semantics.
- **Topology test**: extend `tests/test_graph_topology.py` to assert exactly 5 new nodes registered; no removals; existing 12 nodes untouched (by hash or file modtime check).
- **Coverage**: `pytest --cov=app.graphs.nodes.onboard_intro --cov=app.graphs.nodes.onboard_mood --cov=app.graphs.nodes.onboard_color --cov=app.graphs.nodes.onboard_fit --cov=app.graphs.nodes.onboard_pinterest --cov=app.channels.onboarding_cards --cov=app.channels.onboarding_values --cov=app.providers.apify` reports ≥ 85% per module.
- **End-to-end manual**: the seven scenarios (a)–(g) in the Definition of Done section.
