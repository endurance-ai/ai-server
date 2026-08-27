# 추천 파이프라인

> `POST /recommend` 의 단일 진입점. Phase A(Qdrant) 폐기 후 v6 (Modal embed + dev-app Postgres RPC) 로 운영 중 (SPEC-SEARCH-V6-001).
>
> **SPEC-MSG-001 + SPEC-AGENT-001**: 앱/웹 채팅 채널도 동일 파이프라인(`app/pipeline/runner.py`)을 재사용. `POST /recommend` 는 현재 운영 미사용 (kikoai/app이 자체 v6 경로로 이전) — 코드·파이프라인은 그대로 존재하며 동일 `search_products_v6` RPC 를 사용.

## 데이터 흐름

```mermaid
flowchart TD
    REQ(["RecommendRequest<br/>{item, imageUrl, brandFilter, ...}"])
    REQ --> AUTH{X-Internal-Token<br/>검증}
    AUTH -->|valid| EMBED
    AUTH -->|invalid| E401([401 unauthorized])

    EMBED["embed_step<br/>Modal /embed (FashionSigLIP)"]
    ENHANCE["enhance_query_step<br/>LiteLLM (gpt-4o-mini)<br/>flag: ENHANCE_QUERY_ENABLED"]
    AUTH -->|valid| ENHANCE
    EMBED --> SEARCH
    ENHANCE --> SEARCH

    SEARCH["search_step<br/>v6 RPC search_products_v6<br/>(embedding-first; no query_text param)"]
    SEARCH --> DIVERSIFY

    DIVERSIFY["diversify_step<br/>brand cap + platform cap + tolerance"]
    DIVERSIFY --> RES(["RecommendResponse<br/>{itemId, results[], counts, latencyMs}"])

    classDef step fill:#1565c0,color:#fff
    classDef data fill:#2e7d32,color:#fff
    classDef ext fill:#6a1b9a,color:#fff
    classDef err fill:#c62828,color:#fff

    class EMBED,ENHANCE,SEARCH,DIVERSIFY step
    class REQ,RES data
    class AUTH ext
    class E401 err
```

## state machine

전 step 이 동일 시그니처 — `(state: PipelineState) -> PipelineState`.

`app/pipeline/state.py`:

```python
@dataclass
class PipelineState:
    request: RecommendRequest

    # 중간 산출물
    embedding: list[float] | None = None
    raw_candidates: list[dict[str, Any]] = field(default_factory=list)
    final_candidates: list[dict[str, Any]] = field(default_factory=list)

    # 측정
    counts: dict[str, int] = field(default_factory=dict)
    latency_ms: dict[str, int] = field(default_factory=dict)
    _step_starts: dict[str, float] = field(default_factory=dict)

    def start(self, step: str) -> None: ...
    def end(self, step: str) -> None: ...
```

## 각 step

### 1. `embed_step` — `app/pipeline/embed.py` (thin shim → `app/services/embed_service.py`)

```python
state.embedding = await EmbedProvider.embed_image_url(state.request.image_url)
```

| 항목 | 값 |
|------|---|
| 호출 | Modal `POST /embed` |
| 모델 | `Marqo/marqo-fashionSigLIP` |
| 출력 | 768-dim L2-normalized vector |
| 단건 latency (warm) | ~100~300ms (인퍼런스) + 네트워크 |
| 단건 latency (cold, scale-to-zero) | ~10~17초 |
| 실패 시 | `pipeline_failed` 502 → Next.js 가 v4 폴백 |

### 1.5 `enhance_query_step` — `app/pipeline/enhance_query.py` (SPEC-PIPELINE-001)

LLM(LiteLLM 프록시 경유 gpt-4o-mini)으로 raw `search_query` / `search_query_ko` 를 정제 쿼리로 변환한다. `embed_step` 과 `asyncio.gather` 로 병렬 실행 (PIPELINE_PARALLEL_ENABLED=true 기본).

> **SPEC-SEARCH-V6-001**: v6 는 embedding-first — `search_products_v6` 에 text 파라미터가 없으므로 enhance_query_step 의 출력은 RPC 로 전달되지 않는다. 모듈은 휴면 상태로 보존 (flag=off 기본 그대로).

| 항목 | 값 |
|------|---|
| feature flag | `ENHANCE_QUERY_ENABLED` (기본 `false` — 안전 롤아웃) |
| 모델 | `gpt-4o-mini` (LITELLM_BASE_URL 라우팅) |
| 타임아웃 | `ENHANCE_QUERY_TIMEOUT_MS=1500` (ms) |
| max_tokens | 200 / temperature 0.2 |
| 출력 | `state.enhanced_query`, `state.enhanced_query_ko`, `state.enhance_query_status` (`ok`/`fallback`/`disabled`/`skipped`) |
| 폴백 [HARD] | timeout / 5xx / 4xx / network / empty / parse_error / length_invalid 모두 raw 쿼리 사용 (raise 금지) |

### 2. `search_step` — `app/pipeline/search.py` (thin shim → `app/services/search_service.py` + `app/infrastructure/repositories/search_repository.py`)

> **SPEC-ARCH-AI-001 + SPEC-SEARCH-V6-001**: RPC 이름(`"search_products_v6"`)과 파라미터 빌드는 `SearchRepository`에 단일 소스. 응답 행은 `SearchRpcRowContract`로 검증 — 드리프트 시 `RpcContractError` + 구조화 ERROR 로그 + fail-open 빈 결과 (REQ-AI-006).

```python
rows = await SupabaseProvider.rpc("search_products_v6", {
    "query_embedding": embedding_to_pgvector(state.embedding),
    "p_style_node_id": None,
    "p_category": to_canonical_family(req.item.category),  # 20-token canonical family
    "p_subcategory": None,   # products.subcategory 100% NULL
    "p_brand_names": req.brand_filter,
    "p_limit": 50,
})
```

| 항목 | 값 |
|------|---|
| RPC | `search_products_v6` |
| 알고리즘 | embedding-first cosine distance ASC (pgroonga/RRF 제거됨) |
| top-K | 50 (`SEARCH_DEFAULT_K`) |
| 응답 | `distance` (cosine, ASC=better), `degraded` (bool). `score`/`dense_rank`/`sparse_rank` REMOVED |
| text path | `EmbedProvider.embed_text(text_query)` → Modal `POST /embed/text` (zero-dense stopgap 제거됨) |

상세: [`search-engine.md`](search-engine.md).

### 3. `diversify_step` — `app/pipeline/diversify.py` (thin shim → `app/services/diversify_service.py`)

```python
target = req.final_limit or _tolerance_to_target_count(req.tolerance)
brand_cap = settings.SEARCH_BRAND_CAP * 3 if req.brand_filter else settings.SEARCH_BRAND_CAP
platform_cap = settings.SEARCH_PLATFORM_CAP

# brand/platform 카운터로 buckets 채우면서 상위 N 선별
```

| 정책 | 값 (기본) |
|------|----------|
| 브랜드 캡 | `SEARCH_BRAND_CAP=2` (brand_filter 있으면 ×3 완화) |
| 플랫폼 캡 | `SEARCH_PLATFORM_CAP=3` |
| target count by tolerance | `0.0 → 10`, `0.5 → 15`, `1.0 → 20` (linear) |

`final_limit` 가 명시되면 tolerance 무시.

## 측정

`PipelineState.counts` / `latency_ms` 가 응답에 포함:

```json
{
  "itemId": "item-1",
  "results": [...],
  "counts": {"raw": 50, "after_diversify": 15, "final": 15},
  "latencyMs": {"embed": 1234, "search": 89, "diversify": 2}
}
```

Langfuse trace 의 `recommend_pipeline` span 하위에 `pipeline.embed`/`pipeline.search`/`pipeline.diversify` 가 자동 표시 — `@observe` 데코레이터.

## 향후 LangGraph 도입 시점

지금은 plain async 직선 파이프라인. 다음 분기가 실제 도입될 때 LangGraph 로 마이그레이션:

| 시그널 | 트리거 |
|-------|-------|
| confidence-fallback | 검색 confidence 낮으면 LLM 으로 쿼리 재작성 → 재검색 |
| Vision picker 자동화 | items 검출 애매 → LLM 으로 most prominent 선택 |
| strongMatches 0건 → general 자동 폴백 |
| A/B 분기 | search v5a vs v5b 비교 |

각 step 함수는 그대로 노드로 wrap 만 하면 됨 → 마이그레이션 비용 ~0.

## 에러 / 폴백 매트릭스

| 단계 | 실패 케이스 | 동작 |
|------|------------|------|
| 인증 | `X-Internal-Token` 불일치 | 401 |
| `image_url` 검증 | 화이트리스트 외 호스트 | 422 (Pydantic) |
| Modal /embed | 5xx / timeout / 스키마 mismatch | 502 (`pipeline_failed`) — Next.js 가 v4 폴백 트리거 |
| Supabase RPC | exception | 502 — 동일 |
| diversify | exception (이론상 없음) | 502 |

> **note**: sparse-only 폴백은 추후 검토 (현재 dense+sparse 결합 결과를 기준으로 검색 품질 튜닝 진행 중).
