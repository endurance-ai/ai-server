# Coverage Plan: kiko ai-server

## Context

`/moai coverage` 실행으로 테스트 커버리지 갭 분석을 수행함.
- **목표**: quality.yaml `test_coverage_target: 85%`
- **현황**: `pytest-cov` 미설치 → 정확한 수치 측정 불가. 정적 분석(소스↔테스트 파일 매핑)으로 갭 식별.
- **테스트 현황**: 1144 passed, 14 skipped (pytest 단독 실행), ruff/format 클린
- **개발 모드**: DDD → characterization tests 패턴 사용

## 선행 작업: pytest-cov 설치

`pytest-cov`가 `pyproject.toml` dev deps에 없음. 모든 Phase 전에 추가 필요.

```toml
# pyproject.toml [project.optional-dependencies] 또는 [tool.uv.dev-dependencies]
"pytest-cov>=5.0"
```

설치 후 전체 커버리지 측정:
```bash
uv add --dev pytest-cov
uv run pytest --cov=app --cov-report=term-missing -q
```

## 갭 분석 (정적 매핑 기준)

### P1 — Critical (순수 함수 or 수익 임계 경로)

| 파일 | 규모 | 이유 |
|------|------|------|
| `scoring/brand_2tower_rescore.py` | 117줄 | 추천 품질 핵심, 순수 함수 → 즉시 테스트 가능 |
| `scoring/personalize_rerank.py` | 192줄 | TasteProfile 기반 재랭킹, 순수 함수 → 즉시 테스트 가능 |
| `agents/intent_classifier.py` | 244줄 | 멀티턴 라우팅 핵심 (color_swap/fit_change/mood_shift 분류), LLM mock 필요 |
| `services/subscription_service.py` | 199줄 | 구독 upsert, 수익 임계 경로 |
| `api/webhooks/apple_notifications.py` | 104줄 | Apple Server Notification 수신, 수익 임계 경로 |

### P2 — High (최근 추가 or 에이전트 핵심 도구)

| 파일 | 규모 | 이유 |
|------|------|------|
| `agents/tools/refine_search.py` | 238줄 | 핵심 에이전트 도구, `last_query` 패턴 사용 (260522 추가) |
| `agents/pending_gender.py` | 57줄 | SPEC-GENDER-PIN-001 (260522 추가), 직접 단위 테스트 없음 |
| `agents/last_query.py` | 62줄 | SPEC-GENDER-PIN-001 follow-up (260522 추가), 직접 단위 테스트 없음 |
| `agents/origin_image.py` | 131줄 | 신규 모듈, 목적 불명, 커버리지 0 |
| `core/apple_iap.py` | — | IAP JWS decode 로직, iap_api 테스트에서 간접 사용만 |
| `core/iap_catalog.py` | — | 카탈로그 매핑, 직접 테스트 없음 |
| `infrastructure/repositories/brand_embedding_cache.py` | — | brand rescore가 의존하는 캐시 |
| `infrastructure/repositories/brand_node_cache.py` | — | brand rescore가 의존하는 캐시 |
| `infrastructure/repositories/style_node.py` | — | style_nodes_api가 의존하는 repo |
| `graphs/nodes/intro.py` | — | first-touch 인트로 노드, 테스트 없음 |

### P3 — Medium (단순 엔드포인트 / 어댑터)

- `api/devices.py`, `api/feedback.py`, `api/legal.py` — 단순 CRUD 엔드포인트
- `channels/recommendation.py` — Protocol/Port 인터페이스
- `api/debug.py` — 어드민 디버그 엔드포인트 (인증 중심)

### P4 — Skip (타입 정의 / pass-through)

- `core/types.py`, `domain/search.py` — 타입만
- `services/database_service.py` — SupabaseProvider pass-through
- `providers/*.py` — 외부 서비스 래퍼 (integration test 대상)
- `pipeline/demo_fixtures.py` — 데모용
- `channels/factory.py`, `channels/schemas.py`, `core/di.py`

## 구현 계획

### Step 1: pytest-cov 설치 (선행)

파일: `pyproject.toml`
변경: dev dependencies에 `pytest-cov>=5.0` 추가

### Step 2: P1 테스트 생성 (expert-testing 위임)

DDD 모드이므로 characterization test 패턴 사용.

**`scoring/brand_2tower_rescore.py`** → `tests/test_scoring/test_brand_2tower_rescore.py`
- 순수 함수: 브랜드 벡터 있는 경우 distance blend 검증
- fail-open: 브랜드 miss 시 원본 distance 유지 검증
- α 가중치 경계값 테스트

**`scoring/personalize_rerank.py`** → `tests/test_scoring/test_personalize_rerank.py`
- TasteProfile 없을 때 passthrough 검증
- liked_brands 가산점 검증
- disliked_brands 감점 검증
- gender mismatch 패널티 검증
- fail-open: brand cache miss 시 base score만 사용 검증

**`agents/intent_classifier.py`** → `tests/test_agents/test_intent_classifier.py`
- LLM mock 사용 (기존 test_llm_client.py 패턴 참조)
- 5가지 intent (color_swap/fit_change/mood_shift/identity_preservation/free_form) 분류 검증
- LLM 실패 시 free_form fallback 검증

**`services/subscription_service.py`** → `tests/test_auth/test_subscription_service.py`
- `upsert_from_transaction` DB mock 패턴 (test_iap_api.py 참조)

**`api/webhooks/apple_notifications.py`** → `tests/test_api/test_apple_webhook.py`
- Apple notification type별 라우팅 검증
- 인증 헤더 검증

### Step 3: P2 테스트 생성 (expert-testing 위임)

**`agents/tools/refine_search.py`** → `tests/test_agents/test_refine_search.py`
- `get_last_query` → base_query 복원 검증 (260522 패턴)
- `set_last_query` 갱신 검증
- 기존 `test_search_products_*.py` 패턴 참조

**`agents/pending_gender.py` + `agents/last_query.py`** → `tests/test_agents/test_pending_state.py`
- set/get/clear 라이프사이클 검증
- 재시작 시 유실 무해 (None 폴백) 검증

### Step 4: 커버리지 재측정 및 리포트

```bash
uv run pytest --cov=app --cov-report=term-missing -q
```
- 85% 목표 달성 여부 확인
- 미달 시 P3 항목 추가 생성 여부 결정

## 기존 패턴 참조

| 새 테스트 | 참조 파일 |
|----------|-----------|
| scoring 테스트 | `tests/test_arch_ai_001/test_diversify_characterization.py` |
| agent tool 테스트 | `tests/test_agent_v2/test_search_products_*.py` |
| LLM mock 패턴 | `tests/test_agent_v2/test_llm_client.py` |
| IAP/구독 DB mock | `tests/test_auth/test_iap_api.py` |
| pending state 패턴 | `tests/test_agents/test_pending_question.py` |

## 검증

1. `uv run pytest tests/test_scoring/ -v` → 새 scoring 테스트 패스
2. `uv run pytest tests/test_agents/test_intent_classifier.py -v`
3. `uv run pytest --cov=app --cov-report=term-missing -q` → 85% 확인
4. `uv run ruff check . && uv run ruff format --check .` → 린트 클린
5. `uv run pytest -q` → 전체 회귀 없음

## 예상 신규 파일

```
tests/
├── test_scoring/
│   ├── __init__.py
│   ├── test_brand_2tower_rescore.py     (P1)
│   └── test_personalize_rerank.py       (P1)
├── test_agents/
│   ├── test_intent_classifier.py        (P1)
│   ├── test_refine_search.py            (P2)
│   └── test_pending_state.py            (P2)
└── test_auth/
    ├── test_subscription_service.py     (P1)
    └── (test_apple_webhook.py → test_api/)
tests/test_api/
    └── test_apple_webhook.py            (P1)
```

총 8개 신규 테스트 파일. expert-testing 에이전트에 위임.
