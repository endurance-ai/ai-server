# product.md — portal-ai 제품 개요

portal/app(Next.js)이 Vision 분석까지 끝낸 단일 패션 아이템을 받아, 유사 상품 product_id[] 를 반환하는 stateless 추천 검색 마이크로서비스.

---

## 프로젝트 개요

portal-ai 는 Portal.ai 패션 플랫폼의 AI 검색 전담 서버다. FastAPI 기반의 stateless 마이크로서비스로, 단 하나의 핵심 파이프라인을 수행한다: 이미지 임베딩 → 벡터+키워드 하이브리드 검색 → 다양성 필터 → 상품 ID 목록 반환.

현재 상태: 활발 개발 중인 초기 운영 단계 (v0.1.0). portal/app 과 연동되어 추천 파이프라인을 지속적으로 개선하고 있다.

---

## 타깃 호출자

portal-ai 는 UI가 없는 내부 서비스다. 유일한 호출자는 `portal/app`(Next.js, Vercel) 이다.

호출 흐름:

1. portal/app 이 Apify 스크래핑 → Cloudflare R2 업로드 → GPT-4o-mini Vision 분석을 완료한다.
2. 분석이 끝난 단일 아이템(이미지 URL + 분석 결과)을 `POST /recommend` 로 전송한다.
3. portal-ai 가 추천 product_id[] 를 반환한다.
4. portal/app 이 AI 서버로부터 5xx 또는 타임아웃을 받으면 v4 검색(`/api/search-products`)으로 폴백한다.

최종 사용자(패션 앱 이용자)는 portal-ai 를 직접 호출하지 않는다.

---

## 핵심 기능

### 1. 이미지 임베딩 (`pipeline/embed.py`)

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

portal-ai 는 검색 오케스트레이션에만 집중한다. 다른 책임은 아래와 같이 분리된다.

| 레이어 | 위치 | 책임 |
|--------|------|------|
| UI / 세션 / Auth | Vercel / portal/app | 사용자 세션, Supabase Auth, 검색 결과 렌더링 |
| 스크래핑 / 저장 | portal/app | Apify 스크래핑, R2 업로드, DB 저장 |
| Vision 분석 | portal/app (LiteLLM 경유) | GPT-4o-mini 이미지 분석 |
| v4 폴백 | portal/app | AI 서버 장애 시 구버전 검색 |
| **검색 오케스트레이션** | **portal/ai (이 프로젝트)** | **임베딩 → 검색 → 다양성 → 반환** |
| FashionSigLIP 임베딩 | Modal (scale-to-zero GPU) | 단건 + 배치 임베딩 |
| 벡터 DB + 텍스트 검색 | Supabase pgvector + pgroonga | search_products_v5 RPC |
| Observability | Langfuse self-host (EC2) | Trace SSOT |

---

## Non-goals

다음 기능은 portal-ai 의 책임 범위 밖이다.

- **세션 관리 / 인증**: portal/app 이 담당한다.
- **Vision 분석**: GPT-4o-mini 호출은 portal/app 에서 LiteLLM proxy 를 통해 수행한다.
- **배치 추천**: `scripts/embed_batch_local.py` 는 운영 이미지에 포함되지 않는 로컬 전용 스크립트다.
- **상품 데이터 수집/저장**: Apify 스크래핑, R2/DB 저장은 portal/app 책임이다.
- **`enhance_query` LLM 리파인**: 백로그 상태. 현재 파이프라인은 직선 (embed → search → diversify) 이다.
- **LangGraph**: 분기/병렬/체크포인트 필요 시점까지 보류. 현재 plain async state machine.

---

## 단기 로드맵

아래 항목은 활발 개발 단계에서 우선순위에 따라 진행 예정이다. 시간 추정은 생략한다.

| 우선순위 | 항목 | 비고 |
|---------|------|------|
| Priority High | `enhance_query` LLM 리파인 step 추가 | 현재 직선 파이프라인에 백로그로 존재 |
| Priority High | sparse-only 폴백 모드 | Modal /embed 실패 시 502 대신 텍스트 검색만으로 응답 |
| Priority Medium | `/health/ready` Supabase 점검 세분화 | 연결 지연 임계값 포함 |
| Priority Medium | A/B 리랭크 로직 (`diversify.py` 확장) | 다양성 캡 파라미터 실험 |
| Priority Low | LangGraph 마이그레이션 | 분기/병렬/체크포인트 필요 시점에 결정 |
