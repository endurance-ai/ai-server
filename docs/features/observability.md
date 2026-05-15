# 관측성 — Langfuse

> Langfuse self-host 로 LLM/embedding/파이프라인 호출 trace 를 단일 SoT 에 수집.

## 토폴로지

```
[AI 서버 (FastAPI)]   ──@observe──▶  [Langfuse SDK]   ──HTTP──▶  [langfuse-web (EC2)]   ◀──   [langfuse-db (Postgres)]
                                                       
[LiteLLM proxy]        ──callback──▶  [Langfuse]    (LLM 호출 자동 trace)
```

## Langfuse `@observe`

`app/observability/langfuse.py` — `@observe` 래퍼.

- `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` 미설정 시 **자동 no-op** (테스트/dev 안전)
- 적용 위치: 파이프라인 진입점(`run_pipeline`) + 각 step (`embed_step`, `search_step`, `diversify_step`)

```python
@observe(name="recommend_pipeline")
async def run_pipeline(req: RecommendRequest) -> RecommendResponse:
    state = PipelineState.from_request(req)
    state = await embed_step(state)         # @observe(name="pipeline.embed")
    state = await search_step(state)        # @observe(name="pipeline.search")
    state = await diversify_step(state)     # @observe(name="pipeline.diversify")
    return state.to_response()
```

Langfuse UI에 다음과 같이 보임:

```
recommend_pipeline (4.2s)
├─ pipeline.embed (1.1s)
├─ pipeline.search (89ms)
└─ pipeline.diversify (2ms)
```

## LiteLLM 자동 trace

`aws-infra/kiko-ai-servers/portal-ai/config/litellm.yaml`:

```yaml
litellm_settings:
  success_callback: ["langfuse"]
  failure_callback: ["langfuse"]
```

→ `gpt-4o-mini`, `nova-lite` 등 모든 LLM 호출이 자동 trace. 코드 수정 0줄.

`kikoai/app` 의 Vision 분석(`/api/find/analyze-post`) 도 LiteLLM 경유로 호출되면 동일하게 trace 됨 — `kikoai/app/.env`에 `LITELLM_BASE_URL` + `LITELLM_API_KEY` 설정 시 활성.

## 환경변수

| 키 | 값 | 비고 |
|----|----|------|
| `LANGFUSE_HOST` | `http://langfuse-web:3000` | docker network 내부 |
| `LANGFUSE_PUBLIC_KEY` | `pk-lf-...` | Langfuse UI 에서 발급 |
| `LANGFUSE_SECRET_KEY` | `sk-lf-...` | 동일 |

## 첫 셋업 (운영)

1. EC2 docker-compose up → `http://<EIP>:3000` 접속
2. **Sign up** (첫 가입자 = admin) — `NEXTAUTH_SECRET`/`SALT` 가 `.env` 에 설정돼있어야 함
3. **New Project** → 이름 `kiko.ai`
4. **Settings → API Keys → Create new** → public/secret 발급
5. EC2 `.env` 의 `LANGFUSE_PUBLIC_KEY`/`SECRET_KEY` 채우기
6. `docker compose restart ai-server litellm` → 양쪽 컨테이너가 새 키로 trace 송출

## 확인

```bash
# 수동 LLM 호출 → Langfuse UI 의 "Traces" 탭에 들어오는지
curl -X POST http://<EIP>:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'

# AI 서버 호출 → recommend_pipeline trace 확인
curl -X POST http://<EIP>:8000/recommend \
  -H "X-Internal-Token: $INTERNAL_API_TOKEN" \
  -d @sample_request.json
```

## 대화 이벤트 로그 이벤트 타입 (SPEC-CONVERSATION-LOG-001)

`app/observability/event_payloads.py` 에 20개의 TypedDict 정의. `emit(event_type, ...)` 로 fire-and-forget INSERT.

| # | 이벤트 타입 | 발생 위치 | 비고 |
|---|------------|---------|------|
| 1–19 | `user_text`, `user_photo`, `user_callback`, `intent_routed`, `vision_done`, `search_done`, `diversify_done`, `card_sent`, `card_clicked`, `bot_text`, `taste_update`, `node_error` 등 | 그래프 노드 + webhook intake | SPEC-CONVERSATION-LOG-001 |
| 20 | `tool_call` | `app/agents/react_loop.py` | SPEC-AGENT-V2-REACT REQ-AGENT-OBS-001. ReAct loop 내 매 tool dispatch(성공/실패) 후 emit. payload: `tool_name`, `iteration_no`, `latency_ms`, `error`, `args_summary`, `result_summary`. Langfuse span tag: `tool.<tool_name>` |

`tool_call` 이벤트는 `AGENT_V2_REACT_ENABLED=true` 시에만 발생. V1 토폴로지에서는 emit 없음.

## PII / 마스킹 (백로그)

`@observe` 가 함수 인자를 자동 캡처 — `RecommendRequest.image_url`, `searchQuery` 등 사용자 행동 데이터 포함. 운영 단계 진입 시점에 다음 중 택 1:

- `@observe(capture_input=False)` 로 입력 캡처 비활성 + 명시적 `langfuse_context.update_current_trace(input={...})` 로 비식별 필드만 기록
- Langfuse 측 데이터 보존 정책 단축 (예: 30일)

## 향후

- LLM 호출 비용 트래킹 (Langfuse 가 token usage 자동 집계)
- 검색 품질 점수와 trace 연결 (`search_quality_logs` 테이블 조인)
- A/B 실험 (v5a vs v5b) trace 분리 — `langfuse_context.update_current_trace(metadata={"variant": "v5a"})`
