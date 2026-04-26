# Portal.ai AI Server 설계 스펙

> Next.js에서 검색 + 리파인 로직을 분리하여
> FastAPI 기반 AI 서버로 이관한다. Vision 분석은 Next.js에 유지.
> 핵심 목표: 대화형 리파인이 진짜 작동하는 추천 파이프라인.

---

## 목차

1. [아키텍처 개요](#1-아키텍처-개요)
2. [역할 분리](#2-역할-분리)
3. [인프라 구성](#3-인프라-구성)
4. [파이프라인 설계](#4-파이프라인-설계)
5. [API 인터페이스](#5-api-인터페이스)
6. [벡터 검색 설계](#6-벡터-검색-설계)
7. [LLM 호출 경로](#7-llm-호출-경로)
8. [데이터 흐름](#8-데이터-흐름)
9. [임베딩 파이프라인](#9-임베딩-파이프라인)
10. [에러 처리 및 폴백](#10-에러-처리-및-폴백)
11. [NOT in scope](#11-not-in-scope)

---

## 1. 아키텍처 개요

```
┌─ EC2 (portal-ai-server, t4g.medium, 같은 인스턴스) ───────────┐
│                                                                 │
│  ┌─ nginx ──────────────────────────────────────────────────┐  │
│  │  port 443 (HTTPS, 외부 진입점)                            │  │
│  └──────────────────────────┬───────────────────────────────┘  │
│                              │                                  │
│  ┌─ FastAPI (AI Server) ────┴──────────────────────────────┐   │
│  │  port 8000                                               │   │
│  │  ┌─ Pipeline (plain async) ───────────────────────────┐ │   │
│  │  │  enhance_query → search_products                   │ │   │
│  │  │  → generate_response → collect_metadata            │ │   │
│  │  └────────────────────────────────────────────────────┘ │   │
│  └──────────┬──────────────────────┬───────────────────────┘   │
│             │                      │                            │
│  ┌──────────▼──────────┐  ┌───────▼────────────────────────┐  │
│  │  LiteLLM Proxy      │  │  Qdrant                        │  │
│  │  port 4000           │  │  port 6333 (REST) / 6334 (gRPC)│  │
│  │  (웹 UI 모니터링)    │  │  26K 상품 임베딩 (dense only)   │  │
│  └──────────┬──────────┘  └────────────────────────────────┘  │
│             │                                                   │
└─────────────┼───────────────────────────────────────────────────┘
              │
              ▼
     OpenAI / Bedrock (외부 LLM API)


┌─ Vercel (Next.js) ──────────────────────────────────────────────┐
│  프론트엔드 + 백엔드 API                                         │
│  ├─ Vision 분석 (GPT-4o-mini Vision 직접 호출)                   │
│  ├─ DB 조회/저장 (Supabase)                                     │
│  ├─ 세션 관리 + request 구성                                     │
│  ├─ AI 서버 호출 (VPC 프라이빗 IP)                               │
│  └─ AI 응답의 product ID로 Supabase 조회 → 프론트에 전달         │
└──────────────────────────┬──────────────────────────────────────┘
                           │
              ┌────────────▼────────────────┐
              │  Supabase (PostgreSQL)      │
              │  ├─ products                │
              │  ├─ product_ai_analysis     │
              │  ├─ analyses / sessions     │
              │  ├─ brand_nodes             │
              │  └─ user_feedbacks          │
              └─────────────────────────────┘
```

**참고**: Next.js는 현재 Vercel에 배포되어 있으나, 추후 EC2로 이관 예정.
이관 후에는 모든 서비스가 같은 VPC 내 프라이빗 IP로 통신한다.

---

## 2. 역할 분리

### Next.js (프론트 + 백)

| 역할 | 상세 |
|------|------|
| 프론트엔드 | React UI 렌더링, 결과 표시 |
| Vision 분석 | GPT-4o-mini Vision 직접 호출 (룩 분해 + 아이템 추출 + 스타일/무드) |
| 텍스트 분석 | 프롬프트에서 아이템 구조화 (카테고리/핏/소재/색상) |
| DB 조회/저장 | Supabase CRUD (상품, 세션, 분석 결과, 피드백) |
| 세션 관리 | analysis_sessions + analyses 체인 관리 |
| request 구성 | 분석 결과 + 세션 히스토리 → AI 서버용 request body 조립 |
| 결과 조합 | AI 서버가 리턴한 product ID로 Supabase 조회 → 프론트에 전달 |
| 어드민 | 어드민 대시보드, 검색 디버거 등 |
| 인증 | Supabase Auth (어드민) |

**Vision을 Next.js에 두는 이유:**
- Vision LLM은 단순 API 콜 (GPT-4o-mini → JSON)
- 결과가 UI에 직접 사용됨 (핫스팟 좌표, 팔레트, 무드)
- AI 서버로 보내면 네트워크 홉만 추가
- 나중에 FashionSigLIP 이미지 임베딩을 추가해도 Vision은 여전히 필요 (룩 분해 = 대체 불가)

### AI Server (FastAPI)

| 역할 | 상세 |
|------|------|
| 쿼리 개선 | 대화 맥락 기반 query enhancement (리파인 핵심) |
| 벡터 검색 | Qdrant에서 유사 상품 ID 검색 (dense vector) |
| 응답 생성 | 추천 이유, 매칭 스코어 정리 |
| 메타데이터 | 토큰/비용/latency 기록 |

### AI 서버가 하지 않는 것

- Vision 분석 (Next.js에서 처리)
- Supabase 직접 조회 (상품 상세, 세션 히스토리 등)
- 유저 인증
- 분석 결과 DB 저장
- 프론트엔드 렌더링

---

## 3. 인프라 구성

### EC2 인스턴스

| 항목 | 값 |
|------|------|
| 인스턴스 | t4g.medium (ARM, 2 vCPU, 4GB RAM) |
| AWS 계정 | portal-ai |
| 리전 | ap-northeast-2 (Seoul) |
| OS | Amazon Linux 2023 / Ubuntu 22.04 ARM |

### Docker Compose 구성

```yaml
services:
  ai-server:
    build: ./ai-server
    ports:
      - "8000:8000"
    environment:
      - LITELLM_BASE_URL=http://litellm:4000
      - QDRANT_HOST=qdrant
      - QDRANT_PORT=6333
    depends_on:
      - litellm
      - qdrant
    restart: unless-stopped
    deploy:
      resources:
        limits:
          memory: 1536M

  litellm:
    image: ghcr.io/berriai/litellm:main
    ports:
      - "4000:4000"
    volumes:
      - ./litellm-config.yaml:/app/config.yaml
    command: ["--config", "/app/config.yaml"]
    restart: unless-stopped
    deploy:
      resources:
        limits:
          memory: 512M

  qdrant:
    image: qdrant/qdrant:latest
    ports:
      - "6333:6333"
      - "6334:6334"
    volumes:
      - ./qdrant-data:/qdrant/storage
    restart: unless-stopped
    deploy:
      resources:
        limits:
          memory: 512M

  nginx:
    image: nginx:alpine
    ports:
      - "443:443"
      - "80:80"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf
      - ./certs:/etc/nginx/certs
    depends_on:
      - ai-server
    restart: unless-stopped
```

### 메모리 배분 (4GB)

| 서비스 | 할당 |
|--------|------|
| AI Server (FastAPI) | 1.5GB |
| LiteLLM Proxy | 512MB |
| Qdrant | 512MB |
| nginx + OS | 나머지 (~1GB) |

---

## 4. 파이프라인 설계

### 설계 원칙: plain async → LangGraph 점진 전환

MVP는 **plain async 함수 체인**으로 구현한다. LangGraph는 사용하지 않는다.
리파인 대화 상태가 복잡해지면 **LangGraph Functional API** (`@entrypoint` + `@task`)로 전환.
풀 Graph API는 순환/분기/체크포인트가 필요할 때만.

```
Phase    구현 방식                       전환 시점
──────   ────────────────────────────    ──────────────────────
MVP      plain async 함수 체인           지금
v1.1     LangGraph Functional API       리파인 상태 관리 복잡해질 때
v2.0     LangGraph Graph API            에이전트 루프/분기 필요할 때
```

### 파이프라인 구조

```python
@app.post("/recommend")
async def recommend(req: RecommendRequest) -> RecommendResponse:
    enhanced  = await enhance_query(req.query, req.session_history, req.previous_context)
    results   = await search_products(req.analyzed_items, enhanced, req.gender)
    response  = await generate_response(req.analyzed_items, results)
    metadata  = collect_metadata(enhanced, results)
    return RecommendResponse(search_results=response, metadata=metadata)
```

Vision 분석은 Next.js에서 완료 후 `analyzed_items`로 전달된다.
AI 서버는 이미 구조화된 아이템을 받아서 검색 + 리파인에만 집중.

### Request 모델

```python
from pydantic import BaseModel

class AnalyzedItem(BaseModel):
    id: str                    # "outer", "top", "bottom" 등
    category: str              # Outer, Top, Bottom, ...
    subcategory: str | None
    name: str
    color: str | None
    colorFamily: str | None
    fit: str | None
    fabric: str | None
    searchQuery: str
    season: str | None
    pattern: str | None

class RecommendRequest(BaseModel):
    query: str                          # 유저 입력 텍스트
    gender: str                         # "male" | "female"
    analyzed_items: list[AnalyzedItem]  # Next.js Vision 분석 결과
    session_history: list[dict]         # Next.js가 DB에서 조회한 세션 히스토리
    previous_context: dict | None       # 이전 턴 분석 결과 요약
```

### 스텝별 상세

#### Step 1: enhance_query

```
입력: query + session_history + previous_context
출력: enhanced_query (str)

역할:
- 첫 턴이면 query 그대로 사용 (또는 가볍게 정리)
- 리파인 턴이면 대화 맥락을 반영한 새 쿼리 생성
  예: "좀 더 캐주얼하게" + 이전에 "미니멀 울 코트" 검색
  → "미니멀 스타일, 면/저지 소재, 릴랙스드 핏, 캐주얼 무드"
- LLM 호출 (LiteLLM → GPT-4o-mini)

조건부 로직:
- session_history가 비어있으면 (첫 턴) → 경량 처리
- session_history가 있으면 (리파인) → 풀 맥락 처리
```

#### Step 2: search_products

```
입력: analyzed_items + enhanced_query + gender
출력: search_results

역할:
- 각 analyzed_item에 대해 Qdrant dense 벡터 검색 실행
- 검색 쿼리: 아이템의 searchQuery를 임베딩 API로 벡터화
- Qdrant 메타데이터 필터: category, gender, in_stock, price range
- 아이템당 상위 20개 product_id + 유사도 스코어 리턴

Qdrant 쿼리:
- dense vector: 임베딩 API가 생성한 쿼리 벡터
- filter: { category: "Outer", gender: ["male", "unisex"], in_stock: true }
- limit: 20 (후처리에서 7개로 줄임)
```

#### Step 3: generate_response

```
입력: analyzed_items + search_results
출력: response

역할:
- 검색 결과 정리
- 아이템별 상위 7개 선정 (브랜드/플랫폼 다양성 적용)
- 매칭 이유 텍스트 생성
- 최종 response 구조 조립

다양성 규칙 (기존 로직 유지):
- 브랜드당 max 2개
- 플랫폼당 max 3개
- 아이템당 7개 타겟
```

#### Step 4: collect_metadata

```
입력: 각 스텝의 실행 정보
출력: metadata

역할:
- LLM 호출별 토큰 사용량 집계
- 비용 추정 (모델별 가격 적용)
- 스텝별 latency 기록
- 향후 LangSmith 연동 시 trace ID 포함

기록 항목:
- enhance_query: { model, tokens_in, tokens_out, latency_ms }
- search_products: { qdrant_latency_ms, results_count }
- total_latency_ms
- estimated_cost_usd
```

---

## 5. API 인터페이스

### AI Server → Next.js

#### POST /recommend

메인 엔드포인트. Next.js가 Vision 분석을 완료한 후, 구조화된 아이템과 함께 호출.

**Request:**

```json
{
  "query": "미니멀한 린넨 셔츠",
  "gender": "male",
  "analyzed_items": [
    {
      "id": "top",
      "category": "Top",
      "subcategory": "shirt",
      "name": "Minimal Linen Shirt",
      "color": "White",
      "colorFamily": "WHITE",
      "fit": "relaxed",
      "fabric": "linen",
      "searchQuery": "relaxed white linen shirt men",
      "season": "summer",
      "pattern": "solid"
    }
  ],
  "session_history": [
    {
      "sequence": 1,
      "prompt": "캐주얼한 여름 코디",
      "items": [
        { "category": "Top", "name": "Linen Shirt", "color": "White", "fit": "relaxed" }
      ]
    }
  ],
  "previous_context": {
    "items": [
      { "category": "Top", "name": "Linen Shirt", "color": "White", "fit": "relaxed" }
    ],
    "styleNode": "C",
    "moodTags": ["minimal", "clean"]
  }
}
```

**Response:**

```json
{
  "search_results": [
    {
      "item_id": "top",
      "products": [
        {
          "product_id": 12345,
          "score": 0.89,
          "match_reasons": [
            { "field": "style", "value": "Minimal Contemporary match" },
            { "field": "fabric", "value": "Linen" },
            { "field": "color", "value": "White family" }
          ]
        }
      ]
    }
  ],
  "enhanced_query": "미니멀 스타일, 린넨 소재, 릴랙스드 핏, 여름 캐주얼",
  "metadata": {
    "enhance_query": { "model": "gpt-4o-mini", "tokens_in": 320, "tokens_out": 85, "latency_ms": 450 },
    "search_products": { "qdrant_latency_ms": 12, "results_count": 14 },
    "total_latency_ms": 800,
    "estimated_cost_usd": 0.0002
  }
}
```

#### GET /health

```json
{
  "status": "ok",
  "qdrant": "connected",
  "litellm": "connected",
  "uptime_seconds": 86400
}
```

---

## 6. 벡터 검색 설계

### 검색 전략: dense vector + 기존 enum 스코어링

임베딩 API는 dense 벡터만 제공한다 (sparse 미지원).
기존 enum 스코어링(카테고리/색상/핏/소재 exact match)이 사실상 sparse 역할을 하므로,
**dense vector(의미 유사도) + enum score(정확 매칭)를 결합하여 실질적 하이브리드 검색**을 구현한다.

```
최종 스코어 = vector_score × 0.4 + enum_score × 0.6
```

- vector_score: Qdrant dense 유사도 (0~1)
- enum_score: 기존 13차원 가중치 스코어링 (Qdrant payload 기반 계산)

### Qdrant 컬렉션 구조

```
Collection: products
├─ vectors:
│   └─ "dense": 임베딩 API dense vector (768 dim 또는 모델 의존)
├─ payload (메타데이터):
│   ├─ product_id: int          (Supabase products.id)
│   ├─ category: string         (Outer, Top, Bottom, ...)
│   ├─ subcategory: string      (shirt, blazer, ...)
│   ├─ gender: string[]         (["male", "unisex"])
│   ├─ brand: string
│   ├─ platform: string
│   ├─ color_family: string
│   ├─ style_node: string
│   ├─ fit: string
│   ├─ fabric: string
│   ├─ season: string
│   ├─ pattern: string
│   ├─ mood_tags: string[]
│   ├─ price: int | null
│   ├─ in_stock: bool
│   └─ text: string             (임베딩 원본 텍스트)
```

payload에 enum 필드를 모두 포함하여, Qdrant에서 검색 후 AI 서버 내에서
enum 스코어링을 계산한다. Supabase 조회 없이 스코어링 가능.

### 검색 흐름

```
1. 아이템 searchQuery
   → 임베딩 API 호출 → dense vector

2. Qdrant 검색:
   - dense search (top 50, cosine similarity)
   - filter: { category, gender, in_stock, price range }

3. AI 서버 내 후처리:
   - 각 결과의 payload로 enum 스코어링 계산
   - final_score = vector_score × 0.4 + enum_score × 0.6
   - 정렬 → 다양성 적용 (브랜드 max 2, 플랫폼 max 3)
   - 아이템당 top 7 product_id 리턴

4. Next.js가 product_id로 Supabase 조회 → 프론트에 전달
```

### 온라인 임베딩: 임베딩 API 사용

100-1000 쿼리/일 규모에서 BGE-M3 셀프호스팅은 비효율 (~1.1GB RAM, 99.9% 유휴).
임베딩 API를 사용한다.

| 프로바이더 | 모델 | 한국어 | 가격 (1M 토큰) | 비고 |
|-----------|------|--------|---------------|------|
| Cohere | embed-v4 | 100+ 언어 | $0.10 | MTEB 최고 (65.2) |
| DeepInfra | BGE-M3 hosted | 네이티브 | ~$0.01 | BGE-M3 그대로 API화 |
| OpenAI | text-embedding-3-small | 양호 | $0.02 | 가장 저렴 |

**추천: Cohere embed-v4 또는 DeepInfra BGE-M3** — 한국어 품질 + 비용 효율.
1000쿼리/일 기준 월 $1 미만.

LiteLLM 프록시를 통해 호출하면 프로바이더 교체가 코드 변경 없이 가능:

```python
response = await litellm.aembedding(
    model="cohere/embed-v4",  # 또는 "deepinfra/BAAI/bge-m3"
    input=["relaxed white linen shirt men"]
)
vector = response.data[0].embedding
```

---

## 7. LLM 호출 경로

```
AI Server (FastAPI)
  │
  │ litellm.completion() 또는 HTTP
  │
  ▼
LiteLLM Proxy (localhost:4000)
  │
  ├─ model: "gpt-4o-mini"     → OpenAI API
  ├─ model: "bedrock/nova-*"  → AWS Bedrock
  └─ fallback 체인 설정 가능
```

### LiteLLM 설정

```yaml
# litellm-config.yaml
model_list:
  # 쿼리 개선용 (enhance_query)
  - model_name: "gpt-4o-mini"
    litellm_params:
      model: "openai/gpt-4o-mini"
      api_key: "${OPENAI_API_KEY}"

  # 임베딩 API
  - model_name: "embedding"
    litellm_params:
      model: "cohere/embed-v4"    # 또는 deepinfra/BAAI/bge-m3
      api_key: "${COHERE_API_KEY}"

  # 향후 Bedrock 추가
  # - model_name: "bedrock-nova"
  #   litellm_params:
  #     model: "bedrock/amazon.nova-lite-v1:0"
```

**참고:** Vision 모델은 Next.js에서 OpenAI SDK로 직접 호출. LiteLLM 경유 불필요.

---

## 8. 데이터 흐름

### 첫 분석 (이미지 업로드)

```
1. [유저] 이미지 + 프롬프트 업로드
2. [Next.js 프론트] → POST /api/analyze (Next.js 백엔드)
3. [Next.js 백엔드]
   a. 이미지를 R2에 업로드
   b. GPT-4o-mini Vision 직접 호출 → 룩 분해 + 아이템 추출 + 스타일/무드
   c. 분석 결과 analyses 테이블에 저장, 세션 생성
   d. session_history 조회 (첫 턴이므로 빈 배열)
   e. AI 서버용 request 구성:
      { query, gender, analyzed_items: [...], session_history: [], previous_context: null }
   f. → POST AI서버/recommend
4. [AI 서버]
   a. enhance_query: 첫 턴이라 query 정리만
   b. search_products: 아이템별 Qdrant 검색 → product_id + score
   c. generate_response: 다양성 적용 + 매칭 이유 생성
   d. collect_metadata: 기록
   e. → response 리턴 (product_id 목록)
5. [Next.js 백엔드]
   a. AI 응답 수신
   b. product_id 목록으로 Supabase products 조회 (가격, 이미지, 링크 등)
   c. 검색 결과를 analyses에 업데이트
   d. → 프론트에 analysisId + 결과 전달
6. [Next.js 프론트] /result/[analysisId] 페이지로 이동
```

### 리파인 (텍스트만)

```
1. [유저] "좀 더 캐주얼하게" 입력
2. [Next.js 프론트] → POST /api/refine (Next.js 백엔드)
3. [Next.js 백엔드]
   a. session_history 조회 (이전 턴들)
   b. previous_context 구성 (현재 아이템/스타일/무드)
   c. AI 서버용 request 구성:
      { query: "좀 더 캐주얼하게", gender,
        analyzed_items: [...이전 아이템...],
        session_history: [{seq:1, prompt:..., items:...}],
        previous_context: {items, styleNode, moodTags} }
   d. → POST AI서버/recommend
4. [AI 서버]
   a. enhance_query: 맥락 반영 → "미니멀 스타일, 면/저지, 릴랙스드, 캐주얼"
   b. search_products: 개선된 쿼리로 Qdrant 검색
   c. generate_response: 이전과 다른 결과 보장
   d. collect_metadata
   e. → response 리턴 (product_id 목록)
5. [Next.js 백엔드]
   a. product_id로 Supabase 조회
   b. 새 analyses 레코드 저장 (parent_analysis_id 연결)
   c. 세션 업데이트 (analysis_count++)
   d. → 프론트에 새 analysisId + 결과 전달
6. [Next.js 프론트] /result/[newAnalysisId]로 이동
```

---

## 9. 임베딩 파이프라인

### 오프라인 배치 (상품 임베딩)

26K 상품 텍스트를 임베딩 API로 벡터화하여 Qdrant에 적재.

```
상품 데이터 (Supabase)
  → 텍스트 조합: "{brand} {name} {description} {material} {keywords}"
  → 임베딩 API (Cohere/DeepInfra) → dense vector
  → Qdrant upsert (product_id, dense vector, payload with enum fields)
```

**생성 방식**: TBD (GPU 배치로 BGE-M3 셀프 / 임베딩 API 배치 호출)
- 임베딩 API 배치: 26K × ~100 토큰 = ~2.6M 토큰 → Cohere $0.26, DeepInfra $0.03
- BGE-M3 셀프: GPU 인스턴스에서 수 분 (Phase 0 재활용 가능)

**갱신 주기**: 크롤링 후 신규/변경 상품만 incremental upsert

### 온라인 쿼리 (검색 시 실시간)

```
유저 쿼리 텍스트 (searchQuery)
  → 임베딩 API → dense vector
  → Qdrant search (dense + metadata filter)
```

임베딩 API 호출은 LiteLLM 프록시를 경유하여 프로바이더 교체 용이.

---

## 10. 에러 처리 및 폴백

| 장애 | 처리 |
|------|------|
| AI 서버 전체 다운 | Next.js가 기존 enum 검색 로직 (`/api/search-products`) 폴백 사용 |
| LiteLLM 프록시 다운 | AI 서버 /health에서 감지 → 503 리턴 → Next.js 폴백 |
| Qdrant 다운 | 검색 실패 → 503 리턴 → Next.js 폴백 |
| 임베딩 API 타임아웃 | 10초 타임아웃 → 재시도 1회 → 실패 시 에러 리턴 |
| LLM API 타임아웃 (enhance_query) | 15초 타임아웃 → 실패 시 원본 쿼리로 검색 진행 (graceful degradation) |
| Qdrant 검색 결과 0건 | 필터 완화하여 재검색 (category만으로) → 여전히 0건이면 빈 결과 리턴 |
| Vision LLM 실패 (Next.js) | Next.js에서 처리. 텍스트만으로 아이템 추출 후 AI 서버 호출 |

### 폴백 전략: 기존 enum 검색 유지

AI 서버 전체 장애 시 Next.js의 기존 `/api/search-products` 로직을 폴백으로 사용.
AI 서버 이관 후에도 기존 코드는 삭제하지 않고 폴백 경로로 유지한다.

---

## 11. NOT in scope

이 설계에서 다루지 않는 것:

- **임베딩 배치 생성 방식** — 별도 논의 (GPU 셀프 vs 임베딩 API 배치)
- **임베딩 프로바이더 최종 선정** — Cohere embed-v4 vs DeepInfra BGE-M3 비교 테스트 필요
- **LangGraph 전환 시점** — MVP는 plain async, 복잡해지면 Functional API로 전환
- **LangSmith 연동** — Phase 4에서 별도 진행
- **Cohere Reranker** — Phase 4에서 별도 진행
- **FashionSigLIP 이미지 임베딩** — Phase 5 장기 과제
- **Next.js EC2 이관** — 별도 인프라 작업
- **CI/CD 파이프라인** — 별도 DevOps 작업
- **부하 테스트 / 성능 최적화** — MVP 이후
- **모니터링 / 알림 설정** — MVP 이후
