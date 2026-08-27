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

`tool_call` 이벤트는 매 turn 항상 발생 (ReAct 에이전트 영구 단일 토폴로지).

## User feedback scores (P0)

암묵 피드백 신호를 Langfuse v3 **numeric score** 로 변환해 **원본 추천 trace**(카드를 보낸 그 turn 의 trace)에 retro-attach 한다. 단일 소스: `app/observability/langfuse.py` 의 `emit_feedback_score(...)` (v3 `client.create_score(trace_id=, name=, value=, data_type="NUMERIC", comment=)`). 호출처는 `app/channels/implicit_feedback.py` 3곳.

| 신호 | 발생 함수 | 점수 |
|------|----------|------|
| ❤️ 번호 버튼 탭 (positive) | `record_click` | `user_feedback=1.0` |
| no-click 만료 (negative) | `attribute_expired_impressions` | `user_feedback=0.0` |
| 빠른 re-query (strong negative) | `detect_and_apply_re_query` | `user_feedback=0.0` **+** `re_query=1.0` (별도 필터 가능 신호) |

`comment` 에 `source=implicit_feedback.<signal> product_id=<hashed> brand=… attribution_window_s=…` 부착 (product_id 는 PII 규약상 `hash_id()` 적용).

**원본 trace 귀속 메커니즘**: click/no_click/re_query 는 *나중 webhook = 다른 trace* 에서 도착하므로 click 시점의 `current_langfuse_trace_id()` 를 쓰면 잘못된 trace 에 붙는다. `log_impressions` 가 임프레션을 INSERT 할 때 그 webhook 컨텍스트의 trace id 를 `ai.card_impression.langfuse_trace` 컬럼에 바인딩(migration `0006`). `record_click` 은 `UPDATE … RETURNING langfuse_trace`, `attribute_expired_impressions` 는 CTE `RETURNING … langfuse_trace`, re_query 는 재조회 product_id 들의 임프레션 행에서 distinct trace 를 역조회한다.

**임프레션 로깅 지점 (영구 토폴로지)**: `log_impressions` 는 라이브 카드 전달 단일 funnel 인 `respond` tool 의 `send_hybrid_batch` 성공 직후에서 호출된다(과거엔 그래프 미등록 `send_results` 노드에서만 호출돼 ReAct 경로에서 임프레션이 전혀 안 남던 갭이 있었음 — 수정됨). 새 검색(`offset==0`)이면 chat 별 dedupe 셋을 비워 같은 상품이 새 turn 에 다시 추천되면 새 trace 로 새 임프레션 행을 남기고, `cards:more` 페이지(`offset is None`)는 비우지 않아 동일 결과셋 내 중복 INSERT 를 막는다.

**추천 trace input/output (LLM-as-judge 입력)**: Langfuse LLM-as-judge 는 trace 의 `input`/`output` 을 읽어 채점하는데, `@observe` 는 span 만 만들고 trace I/O 는 비어 있었다(실측 `input=null output=null`). `respond` tool 의 turn 종료 지점에서 `update_current_trace(input=<유저 요청: query/vision/lang>, output=<추천 결과셋 top 15: product_id/brand/title + reply>)` 를 호출해 채점 가능한 형태로 채운다. 한 turn 당 정확히 1회(`_TRACE_IO_SET_KEY` ctx 가드 — genuine-completion + partial-delivery 재진입 양쪽), PII 제외(raw chat_id/from_user_id 미포함), fail-open(실패해도 전달 안 막음). `/recommend` API 경로는 별도 plumbing 필요 — 미적용.

**Fail-open**: 스코어 emit 실패는 피드백 경로/webhook 을 절대 깨지 않는다 — `conversation_log.py` 와 동일한 never-raise 규율(try/except → WARNING 로그, swallow). Langfuse 비활성(키 없음)·kill-switch off·trace id 부재 시 silent no-op.

**Kill-switch**: `LANGFUSE_FEEDBACK_SCORES` (기본 `true`). false 로 두면 `create_score()` 호출만 침묵, 피드백/taste 경로는 그대로.

배포 전 필수: dev-app Postgres 에 migration `0006` 적용(`langfuse_trace` 컬럼 추가, nullable·idempotent). 미적용 시 기존 코드 INSERT 가 컬럼 부재로 실패 → 임프레션 로깅 자체가 WARN no-op.

> **`current_langfuse_trace_id()` v3 API 정정**: v2→v3 SDK 마이그레이션 때 이 헬퍼가 v3 에 없는 v2 API(`get_current_observation()` / `langfuse_context`)를 호출 → 광범위 except 에 삼켜져 **항상 None 반환**하던 결함이 있었다. 그 결과 `ai.card_impression.langfuse_trace` 뿐 아니라 `ai.log_conversation_event.langfuse_trace` 교차참조(SPEC-CONVERSATION-LOG-001)도 v3 이후 전부 NULL 이었음. v3 `client.get_current_trace_id()` 단일 경로로 정정 → 두 서브시스템 동시 복구. 회귀 방지: 실 SDK `@observe` span 안에서 non-None 단언하는 특성 테스트(`tests/test_observability/test_trace_id_v3_api.py`).

## 베타 분석 컨벤션

**rec_id ≡ langfuse_trace** (베타 한정). 추천 batch 단위 식별자는 별도 컬럼을 두지 않고, 이미 `ai.card_impression.langfuse_trace` / `ai.log_conversation_event.langfuse_trace` / `card_clicked.langfuse_trace`(redirect proxy) / `turn_summary.payload.rec_id`(react_loop wrapper) 에 박혀 있는 Langfuse trace id 를 그대로 `rec_id` 로 부른다. 분석 SQL 은 `SELECT langfuse_trace AS rec_id …` alias 로 통일.

근거: trace 는 추천 batch 1회당 1개 발급되고 같은 batch 의 5장 카드가 동일 값을 공유한다(`@observe` 펜스 단위). 베타 종료 후 풀 분석이 필요하면 `ai.card_impression` 에 `rec_id uuid` 신설(migration 0009 후보) 로 옮길 수 있다 — 그 시점엔 lookup 키만 바뀌고 의미는 동일.

분석 쿼리 모음: `scripts/beta_analysis.sql`.

## PII / 마스킹 (백로그)

`@observe` 가 함수 인자를 자동 캡처 — `RecommendRequest.image_url`, `searchQuery` 등 사용자 행동 데이터 포함. 운영 단계 진입 시점에 다음 중 택 1:

- `@observe(capture_input=False)` 로 입력 캡처 비활성 + 명시적 `langfuse_context.update_current_trace(input={...})` 로 비식별 필드만 기록
- Langfuse 측 데이터 보존 정책 단축 (예: 30일)

## LLM 비용 원장 (llm_call 이벤트)

`accumulate_raw` / `accumulate_lc` 가 LLM 호출마다 `llm_call` 이벤트를 `ai.log_conversation_event` 에 기록한다.

- **애플리케이션 비용 원장**: `event_type = 'llm_call'` 행의 `payload.cost_usd` 합계. 실제 청구 검증의 최종 기준은 AWS Cost Explorer의 Bedrock Usage(gross, credit 제외)다.
- `payload.turn_id = '{thread_id}:{turn_no}'` — 한 턴의 모든 LLM 콜 join 키.
- `payload.cost_source`: `'litellm'` = LiteLLM의 `x-litellm-response-cost` 직접 사용 / `'fallback_rates'` = 로컬 요율 추정. 정상 운영 호출은 `litellm`이어야 한다.
- `payload.litellm_call_id`: LiteLLM spend log와 개별 호출을 대조하는 키. 신규 호출부터 저장한다.
- LangChain 호출은 원본 usage를 보존해 cache read/write 토큰을 모두 기록한다. 로컬 fallback 요율은 운영 cross-region Bedrock 요율이며, 응답 비용 헤더가 없을 때만 사용한다.
- `turn_summary.cost_usd` 는 턴 단위 롤업 — `llm_call` 합계와 일치해야 함.

AWS Cost Explorer는 반영 지연이 있으므로 완료된 UTC 날짜끼리 일별/모델별 합계를 대조한다. AWS account credit은 실제 사용량을 상쇄하는 결제 항목이므로 모델 사용비 비교에서는 제외한다.

Langfuse trace 메타데이터: `turn_id`, `cost_usd`, `llm_call_count`, `conversation_flow` (대화 흐름 breadcrumb).

분석 쿼리: `scripts/beta_analysis.sql` 비용 원장 섹션 (일별 요약 / 턴 분포 / 모델별 breakdown / llm_call ↔ turn_summary 대조).

## 향후

- ~~사용자 암묵 피드백 → trace score~~ **구현됨** (P0, 위 "User feedback scores" 절 참조)
- ~~LLM 호출 비용 트래킹~~ **구현됨** (위 "LLM 비용 원장" 절 참조)
- 검색 품질 점수와 trace 연결 (`search_quality_logs` 테이블 조인)
- A/B 실험 (v5a vs v5b) trace 분리 — `langfuse_context.update_current_trace(metadata={"variant": "v5a"})`
