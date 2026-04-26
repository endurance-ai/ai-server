# Portal.ai AI Server — 부트스트랩 가이드

> 새 세션/프로젝트에서 이 문서 하나만 보고 AI 서버 구축을 시작할 수 있도록 작성된 가이드.
> 설계 스펙, 참고 프로젝트, 포팅 대상 코드, 인프라 설정을 한 곳에 정리.

---

## 1. 뭘 만드는가

패션 추천 서비스(portal.ai)의 **검색 + 리파인 파이프라인**을 담당하는 FastAPI AI 서버.

- Next.js(프론트+백)에서 Vision 분석 완료 후, 구조화된 아이템을 AI 서버에 전달
- AI 서버는 쿼리 개선(리파인) + Qdrant 벡터 검색 + 결과 정리 후 product_id 리턴
- Next.js가 product_id로 Supabase 조회 → 프론트에 전달

**설계 스펙 (필독):**
`/Users/hansangho/Desktop/fashion-ai/docs/superpowers/specs/2026-04-10-ai-server-design.md`

---

## 2. 아키텍처 요약

```
EC2 (같은 인스턴스, Docker Compose)
├─ FastAPI AI Server (port 8000) — 검색 + 리파인
├─ LiteLLM Proxy (port 4000)    — LLM/임베딩 라우팅 + 모니터링 웹 UI
├─ Qdrant (port 6333)           — 26K 상품 벡터 검색
└─ nginx (port 443)             — HTTPS 종단

Next.js (Vercel, 추후 EC2 이관)
├─ Vision 분석 (GPT-4o-mini 직접 호출)
├─ DB 조회/저장 (Supabase)
├─ 세션 관리 + AI 서버 호출
└─ product_id로 상품 조회 → 프론트 전달
```

---

## 3. 새 프로젝트 구조 (권장)

```
portal-ai-server/
├── app/
│   ├── main.py                 # FastAPI 앱 + 미들웨어 + CORS
│   ├── api/
│   │   ├── recommend.py        # POST /recommend (메인 엔드포인트)
│   │   └── health.py           # GET /health
│   ├── pipeline/
│   │   ├── enhance.py          # Step 1: 쿼리 개선 (LLM)
│   │   ├── search.py           # Step 2: Qdrant 벡터 검색 + enum 스코어링
│   │   ├── response.py         # Step 3: 결과 정리 + 다양성 적용
│   │   └── metadata.py         # Step 4: 토큰/비용/latency 기록
│   ├── scoring/
│   │   ├── enum_scorer.py      # 13차원 enum 스코어링 (TS → Python 포팅)
│   │   ├── style_adjacency.py  # 스타일 노드 유사도 맵
│   │   ├── color_adjacency.py  # 색상 인접 맵
│   │   └── weights.py          # 스코어링 가중치 상수
│   ├── providers/
│   │   ├── llm.py              # LiteLLM 클라이언트
│   │   ├── embedding.py        # 임베딩 API 클라이언트
│   │   └── vector.py           # Qdrant 클라이언트
│   └── models/
│       ├── request.py          # RecommendRequest (Pydantic)
│       └── response.py         # RecommendResponse (Pydantic)
├── scripts/
│   └── batch_embed.py          # 26K 상품 임베딩 배치 스크립트
├── docker-compose.yml
├── Dockerfile
├── litellm-config.yaml
├── nginx.conf
├── pyproject.toml              # 또는 requirements.txt
└── .env.example
```

---

## 4. 참고 프로젝트

### 4-1. seed-lognia (회사 RAG 서버, 구조 참고용)

**경로:** `/Users/hansangho/Desktop/seed-lognia`

seed-lognia는 LangGraph + FastAPI 기반 RAG/AgenticRAG 서버.
portal.ai AI 서버는 LangGraph를 쓰지 않지만, FastAPI 구조와 프로바이더 패턴을 참고.

| 참고 대상 | 파일 | 가져올 것 |
|-----------|------|----------|
| FastAPI 앱 구조 | `app/main.py` | 미들웨어, CORS, 라우터 등록 패턴 |
| Qdrant 클라이언트 | `app/services/vector.py` | AsyncQdrantClient 사용법, 검색 패턴 |
| 리랭커 | `app/integrations/rerankers/litellm_proxy.py` | LiteLLM /rerank 호출 (Phase 4용) |
| 프로바이더 패턴 | `app/core/providers.py` | 싱글톤 DI 패턴 |
| Pydantic 스키마 | `app/schemas/` | request/response 모델 구조 |
| 프롬프트 관리 | `app/prompts/` | 프롬프트 파일 분리 패턴 |
| Docker 설정 | `Dockerfile` | Python FastAPI 컨테이너 빌드 |
| 환경 변수 | `.env.example` | 필요한 env 변수 목록 참고 |

**주의:** seed-lognia의 LangGraph 그래프(`app/graphs/`)는 참고만. portal.ai MVP는 plain async 함수.

### 4-2. aws-infra (인프라 설정)

**경로:** `/Users/hansangho/Desktop/aws-infra`

| 참고 대상 | 파일 | 용도 |
|-----------|------|------|
| LiteLLM Docker Compose | `portal-ai-servers/portal-litellm/docker/docker-compose.yml` | LiteLLM 컨테이너 설정 참고 |
| LiteLLM 설정 | `portal-ai-servers/portal-litellm/config/litellm.yaml` | 모델 라우팅 설정 참고 |
| LiteLLM 환경 변수 | `portal-ai-servers/portal-litellm/env/.env.example` | API 키 등 |
| GPU 배치 스크립트 | `portal-ai-servers/portal-gpu-batch/scripts/` | Spot 인스턴스 관련 (Phase 0) |
| 셋업 스크립트 | `portal-ai-servers/portal-litellm/scripts/setup.sh` | EC2 초기 설정 참고 |

### 4-3. fashion-ai (현재 Next.js 프로젝트, 로직 포팅 원본)

**경로:** `/Users/hansangho/Desktop/fashion-ai`

---

## 5. 포팅 대상 코드 (TypeScript → Python)

AI 서버의 핵심 로직은 현재 fashion-ai Next.js 프로젝트에 TypeScript로 구현되어 있다.
이 코드를 Python으로 포팅해야 한다.

### 5-1. 검색 엔진 (가장 중요, 818줄)

**파일:** `fashion-ai/src/app/api/search-products/route.ts`

포팅할 핵심 로직:
- 13차원 가중치 스코어링 (WEIGHTS 객체, line 78-95)
- 스코어 계산 함수 (line 572-687)
- 다양성 필터 — 브랜드 max 2, 플랫폼 max 3 (line 796-815)
- 서브카테고리 티어 정렬 — exact > name match > similar > none (line 770-794)
- 가격 hard filter (line 758-768)

```
WEIGHTS = {
  subcategory: 0.25, subcategorySimilar: 0.10, nameMatch: 0.20,
  keywordsEach: 0.05 (max 3), colorFamily: 0.20, colorAdjacent: 0.10,
  stylePrimary: 0.30, styleSecondary: 0.15, fit: 0.15, fabric: 0.15,
  moodTagEach: 0.05 (max 3), season: 0.15, pattern: 0.15, brandDna: 0.20
}
```

**참고:** AI 서버에서는 이 enum 스코어링을 Qdrant payload 기반으로 계산한다.
Supabase를 직접 조회하지 않음. Qdrant에 상품 upsert 시 payload에 enum 필드를 모두 포함.

### 5-2. Enum 정의 파일들

| 파일 | 줄 수 | 내용 |
|------|-------|------|
| `src/lib/enums/product-enums.ts` | 125 | SUBCATEGORIES, COLOR_FAMILIES, FIT, FABRIC 등 |
| `src/lib/enums/style-adjacency.ts` | 67 | 15개 스타일 노드 간 유사도 맵 (0.0~1.0) |
| `src/lib/enums/color-adjacency.ts` | 40 | 16색 인접 맵 |
| `src/lib/enums/korean-vocab.ts` | 715 | 523개 한국어→영어 패션 용어 매핑 |
| `src/lib/enums/season-pattern.ts` | 63 | season 5종 + pattern 10종 |
| `src/lib/fashion-genome.ts` | 260 | 15개 스타일 노드 + 12개 감도 태그 |

**포팅 방법:** 이 파일들은 대부분 상수 정의이므로 Python dict/enum으로 직접 변환.
검색 로직에서 참조하는 구조가 동일해야 한다.

### 5-3. 프롬프트 (enhance_query용)

`src/lib/prompts/prompt-search.ts` (145줄) — 텍스트 전용 검색 프롬프트.
enhance_query 스텝에서 리파인 맥락을 반영한 쿼리 개선 프롬프트를 새로 작성해야 하지만,
기존 프롬프트의 enum 제약 부분은 참고.

---

## 6. 핵심 의존성

```toml
# pyproject.toml (또는 requirements.txt)
[tool.poetry.dependencies]
python = "^3.11"
fastapi = "^0.115"
uvicorn = "^0.34"
litellm = "^1.60"            # LLM + 임베딩 호출
qdrant-client = "^1.17"      # Qdrant 벡터 검색
pydantic = "^2.10"           # request/response 모델
httpx = "^0.28"              # async HTTP (LiteLLM 프록시 호출)
```

**사용하지 않는 것:**
- `langchain`, `langchain-*` — 불필요한 래퍼
- `langgraph` — MVP에서는 plain async, 나중에 필요 시 추가
- `sentence-transformers`, `FlagEmbedding` — 임베딩은 API로 (셀프호스팅 안 함)

---

## 7. Docker Compose 뼈대

```yaml
services:
  ai-server:
    build: .
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

---

## 8. 구현 순서 (권장)

### Phase A: 프로젝트 세팅 + 헬스체크

1. 프로젝트 생성 (pyproject.toml, Dockerfile)
2. FastAPI 앱 뼈대 (main.py, /health 엔드포인트)
3. Docker Compose로 ai-server + litellm + qdrant 로컬 기동 확인
4. LiteLLM 설정 (litellm-config.yaml — gpt-4o-mini + embedding 모델)

**완료 기준:** `curl localhost:8000/health` → `{"status": "ok", "qdrant": "connected", "litellm": "connected"}`

### Phase B: Qdrant + 임베딩 배치

1. Qdrant 컬렉션 생성 스크립트 (collection: products, dense vector)
2. Supabase에서 26K 상품 데이터 export
3. 임베딩 배치 스크립트 작성 (scripts/batch_embed.py)
   - 상품 텍스트 조합 → 임베딩 API 호출 → Qdrant upsert
   - payload에 enum 필드 모두 포함 (category, subcategory, fit, fabric, color_family, style_node, season, pattern, mood_tags, brand, platform, gender, price, in_stock)
4. 배치 실행 + Qdrant 대시보드에서 데이터 확인

**완료 기준:** Qdrant에 26K 포인트 적재, 검색 쿼리 테스트 성공

### Phase C: 검색 파이프라인

1. enum 스코어링 로직 포팅 (route.ts → scoring/enum_scorer.py)
   - WEIGHTS, 스타일 유사도, 색상 인접, 서브카테고리 매칭
2. Qdrant 검색 함수 (providers/vector.py)
   - dense search + metadata filter
3. 파이프라인 조립 (pipeline/search.py)
   - 임베딩 API → Qdrant 검색 → enum 스코어링 → 다양성 필터
4. POST /recommend 엔드포인트 구현
5. 테스트: fashion-ai의 검색 디버거와 결과 비교

**완료 기준:** /recommend에 analyzed_items 보내면 product_id + score 리턴

### Phase D: 쿼리 개선 (리파인)

1. enhance_query 프롬프트 작성
   - 세션 히스토리 + 이전 컨텍스트 → 개선된 검색 쿼리
2. pipeline/enhance.py 구현 (LiteLLM → GPT-4o-mini)
3. /recommend에 enhance_query 스텝 연결
4. 리파인 시나리오 테스트:
   - "좀 더 캐주얼하게" → 이전과 다른 결과가 나오는지 확인

**완료 기준:** 리파인 쿼리가 맥락을 반영한 검색 결과 생성

### Phase E: Next.js 연동

1. fashion-ai의 /api/search-products를 AI 서버 호출로 전환
2. Next.js에서 request 구성 로직 구현 (세션 히스토리 + analyzed_items 조립)
3. AI 서버 응답의 product_id로 Supabase 조회 → 기존 프론트에 전달
4. 폴백 로직: AI 서버 장애 시 기존 enum 검색 사용
5. E2E 테스트

**완료 기준:** 유저가 기존과 동일한 UX로 검색 + 리파인 가능

---

## 9. 환경 변수

```bash
# .env.example
# LiteLLM
OPENAI_API_KEY=sk-...
COHERE_API_KEY=...              # 또는 DEEPINFRA_API_KEY

# Qdrant
QDRANT_HOST=qdrant              # Docker 내부 (로컬: localhost)
QDRANT_PORT=6333

# LiteLLM Proxy
LITELLM_BASE_URL=http://litellm:4000

# Supabase (배치 스크립트에서 상품 데이터 export용)
SUPABASE_URL=https://...
SUPABASE_SERVICE_ROLE_KEY=...
```

---

## 10. 핵심 결정 사항 요약

| 항목 | 결정 | 이유 |
|------|------|------|
| 프레임워크 | plain async (LangGraph 안 씀) | 일직선 파이프라인에 그래프 오버헤드 불필요 |
| 임베딩 | API 호출 (Cohere/DeepInfra) | 1000쿼리/일에 셀프호스팅 비효율 |
| 벡터 DB | Qdrant (Docker) | Supabase 부하 분리 + 전용 벡터 검색 |
| 검색 전략 | dense vector + enum 스코어링 | dense(의미) + enum(정확) = 실질적 하이브리드 |
| Vision 분석 | Next.js에서 직접 (AI 서버 안 함) | 단순 API 콜, UI에 직접 사용 |
| 세션 관리 | Next.js가 DB 조회 → request에 담아 전달 | AI 서버 stateless 유지 |
| LLM 라우팅 | LiteLLM 프록시 (같은 인스턴스) | 웹 UI 모니터링 + 모델 교체 용이 |
| 폴백 | 기존 enum 검색 로직 유지 | AI 서버 장애 시 Next.js에서 처리 |

---

## 11. 주의사항

- **enum 스코어링 로직은 정확히 포팅해야 한다.** 가중치, 유사도 맵, 다양성 규칙이 검색 품질에 직결.
  반드시 fashion-ai의 검색 디버거(`/admin/search-debugger`)로 결과를 비교 검증할 것.
- **Qdrant payload에 enum 필드를 빠뜨리면 스코어링이 깨진다.** batch_embed 시 모든 필드 포함 확인.
- **임베딩 프로바이더는 아직 미확정.** Cohere embed-v4 vs DeepInfra BGE-M3 중 테스트 후 결정.
  LiteLLM 경유하면 코드 변경 없이 교체 가능.
- **t4g.medium (4GB RAM) 제약.** 메모리 배분: AI Server 1.5GB + LiteLLM 512MB + Qdrant 512MB + OS 1GB.
- **AWS 프로필: `portal-ai`.** EC2, 보안그룹 등 모두 이 프로필로 작업.
