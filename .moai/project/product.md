# product.md — kiko.ai AI 서버 제품 개요

패션 추천 AI 서버. `kikoai/app`(Next.js) 웹 경로의 검색 오케스트레이션과 kiko 앱/웹 클라이언트 기반 대화형 패션 추천 두 가지 경로를 제공한다.

---

## 프로젝트 개요

kiko.ai AI 서버는 kiko.ai 패션 플랫폼의 AI 검색/추천 전담 서버다. FastAPI + LangGraph 기반으로 두 가지 진입 경로를 제공한다.

1. **웹 경로** — `kikoai/app`(Next.js)이 Vision 분석까지 마친 단일 아이템을 `POST /recommend`로 전달하면, 이미지 임베딩 → 하이브리드 검색 → 다양성 필터 → product_id[] 반환.
2. **앱/웹 채팅 경로** — 사용자가 앱·웹에서 패션 이미지·Pinterest 링크를 보내면 `/v1/chat` SSE → LangGraph StateGraph → 동일 파이프라인 → 카드 스트리밍 응답.

현재 상태: 활발 개발 중인 초기 운영 단계 (v0.1.0). kikoai/app 과 연동되어 추천 파이프라인을 지속적으로 개선하고 있다.

---

## 타깃 호출자

kiko.ai AI 서버는 두 가지 외부 호출자를 가진다.

**웹 경로 (kikoai/app)**:
1. kikoai/app 이 Apify 스크래핑 → Cloudflare R2 업로드 → GPT-4o-mini Vision 분석을 완료한다.
2. 분석이 끝난 단일 아이템(이미지 URL + 분석 결과)을 `POST /recommend` 로 전송한다.
3. kiko.ai AI 서버가 추천 product_id[] 를 반환한다.
4. kikoai/app 이 AI 서버로부터 5xx 또는 타임아웃을 받으면 v4 검색(`/api/search-products`)으로 폴백한다.

**앱/웹 채팅 경로 (kiko 앱 · 웹)**:
1. 사용자가 앱 또는 웹 채팅에서 패션 이미지 또는 Pinterest 링크를 전송한다.
2. 클라이언트가 `POST /v1/chat/sessions/{id}/messages` (앱 JWT) 로 메시지를 전달하고 SSE 로 응답을 구독한다.
3. LangGraph StateGraph가 ingest → vision → pick_item → (clarify?) → (evaluator) → search → send_results → respond 흐름으로 처리한다.
4. SSE 이벤트(카드 + 텍스트 + 인라인 버튼)로 추천 결과를 전달한다.

---

## 핵심 기능

### 1. 통합 Vision 스키마 (SPEC-VISION-UNIFY-001)

채팅 채널의 GPT-4o-mini Vision 호출이 kikoai/app 웹 경로와 동일한 rich JSON 스키마를 출력한다. outfit-level 필드(styleNode, mood, palette, style)와 per-item 필드(subcategory, fit, colorFamily, searchQuery, searchQueryKo 등)를 포함하며, 두 채널의 검색 품질이 동일한 기준을 갖는다. `VISION_SCHEMA_V2` 환경변수로 즉시 롤백 가능하다.

### 2. 자기-비평 루프 (SPEC-AGENTIC-CRITIQUE-001)

Reflexion 패턴 기반 내부 검색 품질 평가기(`evaluator` 노드)가 `search_node` 결과를 채점하고 미달 시 자동 재검색한다. 0건 결과 fast-path(LLM 비용 없이 broaden delta 적용), LLM 기반 정밀 평가, 반복 한도(기본 2회), stagnation/score-regression/타임아웃 3중 안전 가드를 갖춘다. `SELF_CRITIQUE_ENABLED` 환경변수로 즉시 비활성화 가능하다.

### 3. 인라인 키보드 Clarify 카드 (SPEC-CLARIFY-CARDS-001)

Vision weak 판정(subcategory 모호, fit/colorFamily 누락 등) 시 자유 텍스트 질문 대신 결정론적 인라인 키보드 카드 1개를 전송한다. `pick_clarify_axis(vision_result)` 순수 함수가 우선순위 6단계(category_pick → formality → fit → occasion → subcategory_disambiguation → generic_fallback)로 축을 선택하고, 사용자 탭 시 `clarify:<axis>:<value>` 콜백이 `apply_clarify` 노드로 직행해 검색 입력을 보강한다. LLM 비용 0, 타이핑 마찰 0.

### 4. 이미지 임베딩 (`pipeline/embed.py`)

Modal HTTP 엔드포인트(`/embed`)를 호출해 FashionSigLIP 모델로 이미지 벡터를 생성한다. GPU T4 scale-to-zero 환경이므로 콜드스타트(최대 90초)를 고려한 타임아웃 설정이 필요하다.

### 2. 하이브리드 벡터 검색 (`pipeline/search.py`)

Supabase `search_products_v5` RPC 를 호출한다. RPC 내부에서 다음 세 가지를 결합한다:

- Dense 검색: HNSW 인덱스 기반 벡터 유사도 (FashionSigLIP 임베딩)
- Sparse 검색: pgroonga BM25 텍스트 검색 (상품명·브랜드·카테고리)
- RRF (Reciprocal Rank Fusion): 두 결과를 통합해 top-50 후보 반환

### 3. 다양성 필터 (`pipeline/diversify.py`)

Python 레이어에서 비즈니스 로직을 적용한다:

- 브랜드 캡: 동일 브랜드 최대 2개
- 플랫폼 캡: 동일 플랫폼 최대 3개
- tolerance 파라미터: 점수 허용 범위 조정 → target_count 결정
- 최종 정렬 후 top-N 반환 (기본 15개)

### 4. 관측성 (`observability/langfuse.py`)

각 파이프라인 step 에 `@observe` 데코레이터를 적용해 Langfuse self-host 서버로 trace 를 전송한다. LiteLLM proxy 는 `success_callback: ["langfuse"]` 로 LLM 호출을 자동 추적한다. Langfuse 키 미설정 시 no-op 폴백으로 동작한다.

### 5. 헬스체크 (`api/health.py`)

| 엔드포인트 | 인증 | 용도 |
|-----------|------|------|
| `GET /health` | 없음 | 컨테이너 liveness — 불리언 응답 |
| `GET /health/ready` | X-Internal-Token | Supabase 연결 점검 — readiness probe |

---

## 책임 분리

kiko.ai 는 검색 오케스트레이션에만 집중한다. 다른 책임은 아래와 같이 분리된다.

| 레이어 | 위치 | 책임 |
|--------|------|------|
| UI / 세션 / Auth | Vercel / kikoai/app | 사용자 세션, Supabase Auth, 검색 결과 렌더링 |
| 스크래핑 / 저장 | kikoai/app | Apify 스크래핑, R2 업로드, DB 저장 |
| Vision 분석 | kikoai/app (LiteLLM 경유) | GPT-4o-mini 이미지 분석 |
| v4 폴백 | kikoai/app | AI 서버 장애 시 구버전 검색 |
| **검색 오케스트레이션** | **kikoai/ai (이 프로젝트)** | **임베딩 → 검색 → 다양성 → 반환** |
| FashionSigLIP 임베딩 | Modal (scale-to-zero GPU) | 단건 + 배치 임베딩 |
| 벡터 DB + 텍스트 검색 | Supabase pgvector + pgroonga | search_products_v5 RPC |
| Observability | Langfuse self-host (EC2) | Trace SSOT |

---

## Non-goals

다음 기능은 kiko.ai AI 서버의 책임 범위 밖이다.

- **세션 관리 / 인증**: kikoai/app 이 담당한다.
- **웹 경로 Vision 분석**: 웹 `/recommend` 호출 시 Vision 분석은 kikoai/app 에서 LiteLLM proxy 를 통해 수행한다. 채팅 경로는 자체 Vision 모듈(`app/channels/vision.py`)을 보유한다.
- **배치 추천**: `scripts/embed_batch_local.py` 는 운영 이미지에 포함되지 않는 로컬 전용 스크립트다.
- **상품 데이터 수집/저장**: Apify 스크래핑, R2/DB 저장은 kikoai/app 책임이다.
- **`enhance_query` LLM 리파인**: feature flag 기본 off. 활성화 시 LiteLLM 경유 sparse 쿼리 정제가 파이프라인에 추가된다.
- **그룹 채팅 / 멀티유저 세션**: 채팅 채널은 1:1 대화 범위만 지원한다 (SPEC-MSG-001).

---

## 단기 로드맵

아래 항목은 활발 개발 단계에서 우선순위에 따라 진행 예정이다. 시간 추정은 생략한다.

| 우선순위 | 항목 | 비고 |
|---------|------|------|
| Priority High | `enhance_query` LLM 리파인 활성화 | feature flag 기본 off → 운영 데이터 후 on |
| Priority High | sparse-only 폴백 모드 | Modal /embed 실패 시 502 대신 텍스트 검색만으로 응답 |
| Priority High | Langfuse observability 강화 | REQ-VISION-OBSV-001 / CLARIFY 스팬 attribute 이연 항목 처리 |
| Priority Medium | `/health/ready` Supabase 점검 세분화 | 연결 지연 임계값 포함 |
| Priority Medium | A/B 리랭크 로직 (`diversify.py` 확장) | 다양성 캡 파라미터 실험 |
| Priority Medium | 점수 회귀 시 이전 이터레이션 결과 복원 | SPEC-AGENTIC-CRITIQUE-001 REQ-LOOP-SAFETY-002a 이연 항목 |
| Priority Low | B4 episodic memory | 자기-비평 결과 학습, clarify 버튼 개인화 |
