# kiko-ai-server

kiko.ai 패션 추천 AI 서버 — FastAPI 기반 검색/리파인 파이프라인 + 앱/웹 채팅 채널.

`kikoai/app`(Next.js)이 IG 분석 + Vision 처리까지 끝낸 단일 아이템을 받아, **Modal에서 이미지 임베딩 → dev-app Postgres `search_products_v6` RPC (PostgREST nginx shim 경유) → 다양성 캡 → product_id[] 반환**.

앱/웹 채팅 채널: 사용자가 패션 이미지·Pinterest 링크를 보내면 → `POST /v1/chat/sessions/{id}/messages` (앱 JWT, SSE) → **LangGraph StateGraph** (`app/graphs/`) → 동일 파이프라인 → 카드 스트리밍 응답.

상세 문서:
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 전체 그림 + 토폴로지
- [docs/PATTERNS.md](docs/PATTERNS.md) — 코드 컨벤션
- [docs/features/](docs/features/) — pipeline / search-engine / observability
- [docs/infra/](docs/infra/) — env / deployment / cicd

## 책임 분리 (요약)

| 레이어 | 책임 |
|--------|------|
| dev-app EC2 / `kikoai/app` | R2, Vision(GPT-4o-mini), 세션(Auth.js), UI, v4 폴백. Next.js standalone 컨테이너 |
| **kikoai/ai (이 프로젝트)** | **검색 오케스트레이션, enhance_query, Langfuse trace, `/v1/chat` SSE + 채널 어댑터, 대화 이벤트 로그(SPEC-CONVERSATION-LOG-001), 경량 first-touch(SPEC-ONBOARD-LITE-001)** |
| kiko 앱 / 웹 클라이언트 | 채널 transport (메시지 입력/카드 렌더링). `/v1/auth` JWT + `/v1/chat` SSE 로 연결 |
| Modal | FashionSigLIP 임베딩 (단건 + 배치) |
| dev-app Postgres + nginx PostgREST shim | pgvector, `search_products_v6` RPC (embedding-first, distance ASC). SPEC-INFRA-MIGRATE-001 P6 이후 자체호스팅 (이전: Supabase). pgroonga/product_search_text DROPPED (SPEC-SEARCH-V6-001) |

> **2026-05-10 컷오버**: Supabase + Vercel pause. dev-app EC2 단독 운영. env 변수는 `DB_URL`/`DB_TOKEN` 으로 리네임 완료 (구 `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY`), nginx PostgREST shim (`http://172.31.59.31:3001`) 으로 라우팅.

## 디렉토리

```
app/
├── main.py              # FastAPI 앱 + lifespan + CORS
├── api/                 # 라우터 (chat SSE, auth, recommend, health, debug — 어드민 5개 엔드포인트 + SSRF 가드)
├── agents/              # ReAct 에이전트 루프 + 툴 레지스트리 (SPEC-AGENT-V2-CLEANUP-001 — 영구 단일 토폴로지)
│   ├── react_loop.py    # ReAct loop 엔진 (iteration cap / infinite-loop guard / token budget / timeout). Gap1 memory injection + Gap2 _maybe_reflexion(빈결과만) + Gap3 proactive directive (모두 unconditional)
│   ├── tool_registry.py # 8-tool REGISTRY + validate_args (단일 소스, str/float auto-cast). suggest_next_step 항상 등록
│   ├── llm_client.py    # ChatOpenAI 싱글톤 (bind_tools, LiteLLM proxy 경유)
│   ├── _memory_context.py  # Gap1: TasteProfile + 최근 N턴 요약 자동 주입 빌더 (항상 호출)
│   ├── _reflexion.py       # Gap2: evaluator 헬퍼 래핑 — in-loop Reflexion 평가 (빈결과 시에만 발동)
│   ├── pending_question.py # 봇 질문 ↔ 사용자 짧은 답변 pending-state 관리 (NEW)
│   ├── pending_gender.py   # SPEC-GENDER-PIN-001 (NEW) — 성별 카드 pending 스토어: 텍스트 검색 중 gender 미확인 시 args 스태시 → clarify:gender 콜백에서 팝 후 재검색
│   ├── last_query.py       # SPEC-GENDER-PIN-001 (NEW) — 마지막 성공 검색 쿼리 크로스턴 스토어: refine_search 가 이전 product query 재사용 (raw 리파인 지시어 임베딩 방지)
│   └── tools/           # 8-tool 래퍼: analyze_image, search_products, refine_search, update_taste, ask_user_clarification, get_recent_history, respond, suggest_next_step
├── channels/            # 채널 어댑터 (SPEC-MSG-001): adapter ABC, recommendation port, persona (kiko 페르소나 단일 소스), link_resolver, reset_keywords, session, lang, vision (+ vision_prompt, clarify, clarify_values, _jsonable), pre_messages, instagram_apify
├── graphs/              # LangGraph StateGraph (SPEC-AGENT-001): fashion_bot, state, routing
│   └── nodes/           # 노드: ingest, resolve_image, vision_node, pick_item, ask_clarify, apply_clarify, agent, intro (+ _first_touch / _trace.py 헬퍼). evaluator.py는 Gap2 헬퍼 보존 목적 존재(graph 미등록). 온보딩 카드 서브그래프는 SPEC-ONBOARD-LITE-001에서 제거
├── services/            # 비즈니스 서비스 레이어 (SPEC-ARCH-AI-001): embed_service, search_service, diversify_service, database_service
├── infrastructure/      # 인프라 레이어 (SPEC-ARCH-AI-001)
│   ├── repositories/    # SearchRepository (RPC name + param 단일 소스), search_rpc_contract (REQ-AI-006)
│   ├── cache/           # Redis-backed chat-state (SPEC-CHAT-STATE-REDIS-001): chat_state.py — cursor + impression dedupe, fail-open
│   └── memory/          # 메모리 저장소 — session, session_pg, taste_profile, taste_profile_pg (app/channels/에서 이전)
├── domain/              # 도메인 모델 — SearchResult, Candidate (app/models/과 분리된 도메인 타입)
├── pipeline/            # 검색 파이프라인 thin shim (SPEC-ARCH-AI-001): @observe 래핑 + 테스트 seam 재노출 → 실제 로직은 app/services/ 에 위치
├── providers/           # SupabaseProvider (PostgREST 클라이언트, 논리명 유지), EmbedProvider, LLMProvider, embedding_cache (PG 벡터 캐시, migration 0007 `ai.embedding_cache_text`)
├── observability/       # Langfuse @observe 래퍼 + build_callback_handler + conversation_log + event_payloads (SPEC-CONVERSATION-LOG-001)
├── models/              # Pydantic request/response
├── core/                # config (env) + di.py (DI 컨테이너 — provide_db_pool / provide_settings / provide_embed_provider, SPEC-ARCH-AI-001 REQ-AI-003) + types.py
```

## 기술 스택

| 영역 | 기술 |
|------|------|
| 프레임워크 | FastAPI + uvicorn |
| 에이전트 오케스트레이션 | **LangGraph** `>=1.1.10` (SPEC-AGENT-001) |
| LLM | LiteLLM proxy 경유. **ReAct agent LLM: Bedrock nova-lite (`AGENT_LLM_MODEL`, 기본 `nova-lite`) via LiteLLM** — `drop_params: true` 로 `tool_choice` 제거 (Bedrock 호환). `langchain-openai` (`ask_clarify` 노드 한정) |
| 임베딩 | Modal HTTP endpoint (FashionSigLIP). `POST /embed` (image) + **`POST /embed/text`** (text query — SPEC-SEARCH-V6-001) |
| 벡터 DB | **dev-app Postgres 16 + pgvector** (PostgREST nginx shim, Qdrant 미사용). pgroonga/product_search_text DROPPED with v5 — v6 is embedding-first |
| Observability | **Langfuse self-host v3** (`langfuse>=3,<4`, single-path v3 wiring — SPEC-OBSERVABILITY-002). `build_callback_handler` 는 키 존재 시 진짜 v3 `CallbackHandler` 반환 (LangGraph→nested LLM 브리지), 미설정 시 no-op 폴백. dev-ai 풀스택 self-host (web+worker+ClickHouse+Redis+MinIO+PG) |
| 스키마 | Pydantic v2 |
| HTTP | httpx (async) |
| 패키지 | uv |
| 린트 | ruff |

## 개발 명령어

```bash
uv sync                                              # 의존성 설치
uv run uvicorn app.main:app --reload --port 8000     # 로컬 실행
uv run ruff check . && uv run ruff format .          # 린트 + 포맷
uv run pytest                                        # 테스트
uv run pytest --cov=app --cov-report=term-missing    # 커버리지 측정 (목표 85%)
docker compose up -d                                 # 로컬 스택 (AI 서버만)
```

## Claude 작업 규칙

- **커밋 전 필수**: `uv run ruff check . && uv run ruff format --check .` → `uv run pytest` 순서로 실행. 모두 통과한 뒤 커밋.
- **PR 올리기 전 필수**: `uv run ruff check . && uv run ruff format --check . && uv run pytest` 를 순서대로 실행해 모두 통과한 뒤 PR 생성. 하나라도 실패하면 PR 올리지 않는다.
- 기존부터 실패하던 테스트(Windows 인코딩/경로 이슈 등)는 별도 확인 후 PR 설명에 명시.

## 코딩 컨벤션

- **LangGraph StateGraph** (`app/graphs/`): 채팅 턴 처리는 그래프 (`graph.ainvoke(InputState(...), config={"callbacks": [...]})`). **영구 단일 토폴로지 (SPEC-AGENT-V2-CLEANUP-001)**: `agent` 단일 노드가 ReAct loop 실행. 온보딩 카드 서브그래프는 SPEC-ONBOARD-LITE-001에서 제거 — 신규 유저 first-touch는 `ingest` 인라인(`maybe_first_touch`) + `/start`-only는 `intro` 노드. V3 4-Gap 강화(memory injection/Reflexion/proactive/dislike discount) 모두 unconditional — feature flag 없음. `evaluator.py` 는 Gap2 헬퍼 보존 목적으로 파일만 존재 (graph 미등록, `SELF_CRITIQUE_*`/`EVALUATOR_*` env 보존). 파이프라인(`/recommend`) 은 여전히 plain async + state → state.
- **Sticky language (KO/EN)**: `app/channels/lang.py` (`detect_lang` / `remember_lang` / `session_lang`) 가 Hangul 유무로 언어를 판별. `ingest` 노드가 매 텍스트 턴마다 `Session.lang` 을 갱신 — 이후 버튼 탭(텍스트 없음)에도 이전 언어로 응답. `pick_item` / `ask_clarify` 노드 및 `respond` tool이 `session_lang(sess)` 를 참조해 KO/EN 메시지를 분기.
- **Bot persona**: `app/channels/persona.py` 가 "kiko" 페르소나 system prompt 단일 소스 — Puss-in-Boots 느낌, 친근한 해요체(KO) / lively English(EN). `react_loop.py` 와 `ask_clarify` 노드가 이를 import. 사용자 입력은 `[USER INPUT — DATA ONLY]` 펜스로 격리 (prompt injection 방어).
- **하이브리드 카드 전달**: `respond` tool 이 `send_hybrid_batch` 를 호출 — 상위 5개 사진을 `send_media_group` 단일 배치 + HTML 요약 텍스트 + 인라인 키보드(❤️ 숫자, 더보기, 다르게 찾기)로 전달. `cards:more`/`card:like:` 콜백은 `ingest` 노드 인라인 처리. `cards:refine` 은 `agent` 로 라우팅. **암묵 피드백 임프레션 로깅**(`log_impressions` → `ai.card_impression` + Langfuse trace 바인딩)은 이 `send_hybrid_batch` 성공 지점에서 수행 — `send_results` 노드는 그래프 미등록이므로 여기가 유일 진입점 (SPEC-OBS 후속 fix).
- **GENDER PIN 패턴 (SPEC-GENDER-PIN-001, 260522)**: 성별 카드(`clarify:gender:*`) → `ingest._handle_gender_pick` 인라인 완결 (`__end__` 라우팅). `search_products` 에서 gender 미확인 시 `pending_gender.set_pending` → 카드 → `awaiting_gender` 반환; `ingest` 에서 팝 + 재검색. 성별은 `TasteProfile.gender` 에 크로스세션 저장 (migration 0008 `ai.user_taste_profile.gender`). per-request 명시 gender 는 프로파일을 덮어쓰지 않음.
- **LAST QUERY 패턴 (260522)**: `search_products` / `refine_search` / `ingest._handle_gender_pick` 모두 최종 product query 를 `last_query.set_last_query(chat_id, text_query)` 로 저장. `refine_search` 는 `get_last_query` 를 우선 참조하여 raw 리파인 지시어 임베딩 드리프트를 방지. 인메모리 — 재시작 시 유실은 무해 (폴백으로 `ctx['text_query']` 사용).
- **구조화 로그 이모지 범례**: 📥 webhook, 👁 vision, 🔍 search, 🤔 evaluator(Gap2 내부), 🎨 pipeline, 🐱 bot 발화 (respond tool/adapter). (🧹 zero-dense suppress 삭제 — v6 text path는 real embed_text() 사용, zero-dense stopgap 제거됨) **트레이싱**: 🤖 topology 배너(startup), ▶️/✅/⏭️ graph node enter/done/skip, 🔄 ReAct agent-iter, 🔧 tool dispatch, 🏁 agent 종료(respond), 🧠 v3:memory(Gap1), 🔬 v3:reflexion(Gap2), 💡 v3:proactive(Gap3), 🚫 v3:dislike(Gap4).
- **Port 패턴**: 채널 레이어와 파이프라인 간 결합도는 `Protocol` 기반 Port로 분리 (`app/channels/recommendation.py`). 그래프 노드는 `RecommendationPort`만 참조 — 파이프라인 구현은 lazy import
- Pydantic v2 모델로 request/response 정의
- LLM 호출은 LiteLLM 프록시 경유 (`LITELLM_BASE_URL`)
- 임베딩 호출은 Modal endpoint (`MODAL_EMBED_URL`)
- DB 쿼리는 PostgREST RPC 호출 (`supabase-py` async — 클라이언트 라이브러리는 유지, 엔드포인트는 dev-app nginx shim)
- ruff 린트+포맷 (line-length=120)

## 핵심 파일

| 파일 | 설명 |
|------|------|
| `app/main.py` | FastAPI 엔트리포인트 + lifespan (DB 클라이언트 워밍업 + **chat_state redis pool warm/close**, SPEC-CHAT-STATE-REDIS-001) |
| `app/api/recommend.py` | `POST /recommend` (X-Internal-Token 인증) |
| `app/api/health.py` | `/health` (liveness, no auth) + `/health/ready` (인증 + 의존 서비스 상태) |
| `app/api/chat.py` | `POST /v1/chat/...` (앱 JWT 인증, SSE 스트리밍) — `chat_service` 가 `StreamingAdapter` 로 동일 그래프 구동 |
| `app/api/debug.py` | **NEW** — 어드민 디버그 5개 엔드포인트 (INTERNAL_API_TOKEN 인증): `/debug/vision-analyze`, `/debug/resolve-url` (SSRF 가드), `/debug/rewrite-query`, `/debug/list-models`, `/debug/v6-trace` |
| `app/channels/adapter.py` | `MessengerAdapter` ABC. `send_chat_action(chat_id, action='typing') -> bool` 은 default no-op (`return False`) — 미구현 채널 어댑터 자동 skip (SPEC-AGENT-UX-P0-001 REQ-UX-003) |
| `app/channels/recommendation.py` | `RecommendationPort` Protocol + `ChannelRecommendationRequest/Result` DTO + `PipelineRecommendationPort` 구현 (채널-파이프라인 결합도 분리) |
| `app/agents/react_loop.py` | ReAct loop 엔진 — iteration cap / infinite-loop guard / token budget / per-tool timeout / tool_call event emit. Gap1 `build_memory_context` + Gap2 `_maybe_reflexion` (잔여-budget asyncio.wait_for 강제 취소, 260522: `evaluator_run` event emit 추가) + Gap3 `_PROACTIVE_DIRECTIVE` 모두 unconditional (SPEC-AGENT-V2-CLEANUP-001). `_build_ctx` 가 `vision_category` (REAL Vision garment category) 를 ctx 에 노출 — `search_products`/`refine_search` 가 이를 canonical family gate 에 사용 (SPEC-SEARCH-V6-001). 시스템 프롬프트 마지막 라인에 `[LANG=<ko\|en> — MUST reply in <Korean\|English>]` sticky directive 강제 주입 (SPEC-AGENT-UX-P0-001 REQ-UX-002). `search_products`/`refine_search`/`respond` dispatch 직전 `_fire_typing` (fire-and-forget, fail-open) 으로 typing indicator 1회 발사 (REQ-UX-003). **260522 SEARCH-FIRST 정책**: 신호 ≥2개 시 즉시 `search_products` (clarify 먼저 금지). **REFINE-vs-SEARCH 지침**: 동일 아이템 조정은 `refine_search` 사용 (price/brand/color delta). **GENDER (260522)**: 명시 신호 없으면 text_query에 gender 단어 OMIT (시스템이 downstream에서 unisex 추가). `awaiting_gender` 에러 수신 시 카드 가리키는 one-liner `respond` 후 종료. Vision `suggested_query:` 는 English-only 형태로 항상 English 선호 (gender 플립 방지) |
| `app/agents/tool_registry.py` | 8-tool REGISTRY + TypedDict args/result schema + `validate_args` (단일 소스). str/float 자동 캐스팅 — LLM 타입 실수로 loop 헛돌이 방지. `suggest_next_step` 항상 등록. `ask_user_clarification.axis` Literal 검증 추가 — 유효 6값 외 axis 는 `bad_axis:` 에러 즉시 반환으로 self-correction 지원 (P0-1) |
| `app/agents/pending_question.py` | **NEW** — 봇 질문 ↔ 사용자 짧은 답변 pending-state 관리. 다음 turn 에서 pending Q+A 를 ctx에 주입 후 클리어 |
| `app/agents/pending_gender.py` | **NEW (SPEC-GENDER-PIN-001)** — 성별 카드 pending 스토어. `search_products` 가 gender 미확인 시 검색 args 스태시 → `clarify:gender:*` 콜백이 pop 후 gender 적용하여 재검색. 인메모리(재시작 시 유실 무해). Multi-worker 시 Redis 이전 권장 |
| `app/agents/last_query.py` | **NEW (SPEC-GENDER-PIN-001 follow-up)** — 마지막 성공 검색 쿼리 크로스턴 스토어. `search_products` 가 최종 English+gender-pinned text_query 를 저장 → `refine_search` 가 다음 turn에서 raw 리파인 지시어("더 저렴하게") 대신 실제 product query 재사용 (임베딩 드리프트 방지). 인메모리. `chat_id → query` dict |
| `app/agents/llm_client.py` | ChatOpenAI 싱글톤 (bind_tools, LiteLLM proxy 경유). `AGENT_LLM_MODEL` 미설정 시 fail-closed (기본 `nova-lite`) |
| `app/agents/_memory_context.py` | Gap1: `build_memory_context(state, sess, ctx) -> str` — TasteProfile + 최근 N턴(기본 5) 요약 자동 주입, char-cap(`AGENT_V3_MEMORY_MAX_TOKENS`*4), `[MEMORY CONTEXT — SYSTEM DERIVED]` 펜스 |
| `app/agents/_reflexion.py` | Gap2: `evaluate_search_quality(...) -> dict` — `evaluator._call_llm`/`_build_fastpath_delta` 래핑, fail-open(score=1.0), 빈결과 fastpath |
| `app/agents/tools/suggest_next_step.py` | Gap3: 8번째 tool — `_adapter_ctx.get_adapter()` + `send_text_with_buttons` 재사용 (`terminates_loop=False`) |
| `app/agents/tools/search_products.py` | **SPEC-GENDER-PIN-001 (260522)**: gender resolution 로직 추가 — `_query_gender` (text_query 내 gender 토큰 탐지), `_lookup_profile_gender` (taste_profile 핀 조회), `_send_gender_card` (성별 선택 인라인 키보드 전송, 콜백 `clarify:gender:{men\|women\|unisex}`). gender 미확인 + 순수 텍스트 턴 → `pending_gender.set_pending` + 카드 전송 + `awaiting_gender` 에러 반환. `pipeline_exc_detail` (공유 헬퍼 — HTTP status/host 추출, host는 log-only, status는 `Result.error`에 포함). per-step 타이밍 로그 (`⏱ embed/rpc/divers ms`). 최종 text_query 를 `set_last_query` 에 저장 |
| `app/agents/tools/refine_search.py` | 260522: `get_last_query` 로 이전 turn product query 복원 후 base_query 로 사용 (raw 리파인 지시어 임베딩 드리프트 방지). refine 후 `set_last_query` 갱신 (체인 리파인 대응). `pipeline_exc_detail` 공유 헬퍼 import |
| `app/graphs/nodes/agent.py` | LangGraph `agent` 노드 — `run_react_loop` 래핑, state delta 반환 (SPEC-AGENT-V2-REACT) |
| `app/graphs/fashion_bot.py` | LangGraph StateGraph 빌드 + 모듈 수준 컴파일 캐시 (SPEC-AGENT-001). 단일 영구 토폴로지 — 플래그 분기 없음 (SPEC-AGENT-V2-CLEANUP-001). **SPEC-GENDER-PIN-001**: `_route_after_ingest_v2` 에서 `clarify:gender:*` 콜백 → `__end__` (ingest 인라인 완결) |
| `app/graphs/state.py` | `InputState`, `WorkingState`, `OutputState` Pydantic v2 모델. V2: `agent_iterations`, `tool_call_history`, `agent_status` 3 필드 추가 |
| `app/graphs/routing.py` | 조건부 엣지 순수 술어 (`_route_after_resolve`, `_is_weak_vision*`). 온보딩 진입 술어는 SPEC-ONBOARD-LITE-001에서 제거 — first-touch는 `fashion_bot._route_after_ingest_v2` + `ingest` 인라인 |
| `app/graphs/nodes/ingest.py` | **SPEC-GENDER-PIN-001 (260522)**: `_handle_gender_pick` 추가 — `clarify:gender:*` 콜백 인라인 처리: (1) taste_profile.gender 핀 (영속), (2) pending_gender 팝 + gender-appended text_query 로 `run_text_only_search` → `send_hybrid_batch` 인라인 전달, (3) `set_last_query` 저장. 이미지/URL/콜백 턴에서 `pending_question` 자동 클리어 (P1-4: 비텍스트 턴에 이전 clarify Q 재방출 버그 수정) |
| `app/graphs/nodes/intro.py` | 경량 first-touch 인트로 — 신규 유저(`onboarded_at IS NULL`)가 `/start`-only 첫 메시지일 때만 진입, 1회성 안내 + `onboarded_at` 기록 후 턴 종료. KO/EN 분기 (SPEC-ONBOARD-LITE-001) |
| `app/graphs/nodes/ask_clarify.py` | weak-vision 시 인라인 키보드 카드 생성 (SPEC-CLARIFY-CARDS-001, 6 axes, LLM 호출 없음) |
| `app/graphs/nodes/apply_clarify.py` | `clarify:*` callback 소비 → `session.boost_keywords` 누적 (SPEC-CLARIFY-CARDS-001) |
| `app/graphs/nodes/evaluator.py` | **graph 노드 아님** — Gap2 헬퍼 보존 전용. `_call_llm`/`_build_fastpath_delta` 만 `_reflexion.py` 가 래핑해 사용. `SELF_CRITIQUE_*`/`EVALUATOR_*` env 의존 |
| `app/channels/pre_messages.py` | **NEW (SPEC-AGENT-UX-P0-001 REQ-UX-004)** — 사전 안내 멘트 단일 소스 `PRE_MESSAGES` (4 키: vision/search/pinterest/analyze_image × KO/EN) + `fire_pre_message` helper (idempotent per-turn, fail-open). 4개 firing site: `app/graphs/nodes/vision.py` (key=vision, thread_id 마커), `app/agents/tools/search_products.py` & `refine_search.py` (key=search, ctx 마커 공유), `app/agents/tools/analyze_image.py` (key=analyze_image, ctx 마커). pinterest 키는 SPEC contract 로 보존 — pinterest_ingest 노드 부재 (SPEC-ONBOARD-LITE-001 §4) 라 런타임 firing 없음 |
| `app/channels/instagram_apify.py` | **NEW** — Apify 경유 IG 포스트 이미지 fetch. IG direct URL CDN 차단 우회. `APIFY_TOKEN` + `APIFY_INSTAGRAM_ACTOR` 환경변수, fail-open (fetch 실패 시 원본 URL 반환) |
| `app/channels/link_resolver.py` | Pinterest / pin.it og:image URL 해석. **직접 이미지 CDN fastpath** (P1-3): `_DIRECT_IMAGE_HOSTS` (unsplash/pinimg/cdninstagram/fbcdn/twimg/discordapp) → og:image 스크랩 없이 URL 그대로 반환 (SSRF 가드는 그대로 통과). 서브도메인 exact-prefix 매칭 (lookalike bypass 방지) |
| `app/channels/lang.py` | 언어 감지 헬퍼 — `detect_lang` / `remember_lang` / `session_lang`. Hangul 유무 기준 KO/EN 판별, `Session.lang` sticky 갱신 |
| `app/infrastructure/memory/session.py` | `SessionStore` Protocol + `InMemorySessionStore` 구현체. `set_store_factory/set_store/reset_store` 주입 지점 포함. `Session.lang: str = "en"` (sticky 언어 필드). 구 경로: `app/channels/session.py` (SPEC-ARCH-AI-001 이전) |
| `app/channels/vision.py` | LiteLLM 경유 Vision 패션 아이템 추출 — v2 schema (SPEC-VISION-UNIFY-001): `styleNode`/`sensitivityTags`/`mood`/`palette`/`style`/`items[]` (subcategory/fit/colorFamily/searchQuery/searchQueryKo). flag: `VISION_SCHEMA_V2` |
| `app/channels/vision_prompt.py` | Vision v2 schema 프롬프트 + JSON 스키마 정의 (kikoai/app `analyze.ts` 동치) |
| `app/channels/clarify.py` | clarify 카드 빌더 (6 axes: category_pick / formality / fit / occasion / subcategory_disambiguation / generic_fallback) |
| `app/channels/clarify_values.py` | clarify 카드 axis별 옵션 값 + 한글 라벨 매핑 |
| `app/channels/_jsonable.py` | 5-step JSON-serializable cascade 헬퍼 (session_pg / taste_profile_pg / conversation_log 공용 — SPEC-MEMORY-001 패턴 추출) |
| `app/channels/persona.py` | kiko 페르소나 system prompt 단일 소스 (`KIKO_PERSONA_SYSTEM_PROMPT`) — `react_loop.py` + `ask_clarify` 노드 공유 |
| `app/core/auth.py` | `verify_internal_token` FastAPI dependency |
| `app/pipeline/state.py` | PipelineState 정의 |
| `app/pipeline/embed.py` | thin @observe shim — 실제 로직은 `app/services/embed_service.py`에 위치 (SPEC-ARCH-AI-001) |
| `app/pipeline/enhance_query.py` | LLM 기반 sparse 쿼리 정제 (SPEC-PIPELINE-001, feature flag 기본 off) |
| `app/pipeline/search.py` | thin @observe shim — 실제 로직은 `app/services/search_service.py` + `app/infrastructure/repositories/search_repository.py`에 위치. 테스트 monkeypatch seam(`SupabaseProvider.rpc`) 재노출 (SPEC-ARCH-AI-001) |
| `app/pipeline/diversify.py` | thin @observe shim — 실제 로직은 `app/services/diversify_service.py`에 위치 (SPEC-ARCH-AI-001) |
| `app/pipeline/runner.py` | 파이프라인 조립 + `@observe` |
| `app/services/search_service.py` | 검색 오케스트레이션. v6 embedding-first — query_text/enhance_query RPC 경로 retired (모듈 보존, 휴면). `RpcContractError` 캐치 → 구조화 ERROR 로그 + fail-open 빈 결과 (REQ-AI-006, SPEC-ARCH-AI-001) |
| `app/services/diversify_service.py` | 브랜드/플랫폼 캡 + tolerance 산술. banker's rounding 포함 (`int(round(10 + t*10))`) (SPEC-ARCH-AI-001). `seen_ids: set[str]` product_id 레벨 dedup 가드 (falsy-id bypass) + **`seen_content: set[tuple]` 컨텐츠 레벨 dedup** (260522) — `(brand, name_norm, price)` 키로 동일 상품 다른 ID 중복 제거. `drops_dup` 카운터 — `[STEP 4.8]` 로그에 포함 (SPEC-AGENT-UX-P0-001 REQ-UX-001) |
| `app/services/embed_service.py` | Modal /embed 래핑 (SPEC-ARCH-AI-001) |
| `app/services/database_service.py` | `SupabaseProvider` pass-through 래퍼 (SPEC-ARCH-AI-001) |
| `app/infrastructure/cache/chat_state.py` | **NEW (SPEC-CHAT-STATE-REDIS-001)** — Redis-backed chat-state 단일 소스. 5 fail-open 헬퍼(`get_cursor`/`set_cursor`/`is_logged`/`mark_logged`/`clear_logged`) + `warm_pool`/`close_pool` lifespan hooks. Keys `kiko:cursor:{chat_id}` (TTL 24h) + `kiko:imp:{chat_id}` (SET, TTL 7d). `respond.py` 의 in-process pager-cursor dict + impression-dedupe dict 를 대체 — 멀티-워커 시 cursor 일관성 + impression 중복 INSERT 차단 보장. Redis 다운 시 추천 발사는 그대로 진행(fail-open: cursor=0, is_logged=False, write/del=no-op + DEBUG log). 단일 env `REDIS_URL` 로 로컬(docker-compose redis:7-alpine)/테스트(fakeredis)/prod(dev-ai redis DB 1) 분기 |
| `app/infrastructure/repositories/category_family.py` | **NEW (SPEC-SEARCH-V6-001)** — `CANONICAL_FAMILIES` (20 lowercase tokens) + `_VISION_ALIAS` + pure `to_canonical_family()`. v6 FILTER2 canonical family gate 단일 소스. `SearchRepository.build_params`가 `p_category`를 이 함수로 정규화 |
| `app/infrastructure/repositories/search_repository.py` | `SearchRepository` — `_RPC_NAME = "search_products_v6"` (단일 소스) + `build_params` (6-key: query_embedding, p_style_node_id, p_category, p_subcategory, p_brand_names, p_limit) + `search`. `embedding_to_pgvector` 공동 위치 (SPEC-ARCH-AI-001 REQ-AI-002, SPEC-SEARCH-V6-001) |
| `app/infrastructure/repositories/search_rpc_contract.py` | `SearchRpcRowContract` Pydantic 모델 (v6: `distance`+`degraded`, no score/dense_rank/sparse_rank) + `RpcContractError` + `validate_rpc_rows`. 드리프트 시 구조화 에러 (REQ-AI-006). 허용: id absent → 에러, distance/brand absent → 허용, extra 컬럼 허용 |
| `app/infrastructure/memory/session.py` | (구 `app/channels/session.py`) `SessionStore` Protocol + `InMemorySessionStore` (SPEC-ARCH-AI-001) |
| `app/infrastructure/memory/session_pg.py` | (구 `app/channels/session_pg.py`) Postgres 기반 세션 저장소 (SPEC-ARCH-AI-001) |
| `app/infrastructure/memory/taste_profile.py` | (구 `app/channels/taste_profile.py`) TasteProfile 도메인 모델 (SPEC-ARCH-AI-001). **SPEC-GENDER-PIN-001 (260522)**: `gender: str | None = None` 필드 추가 — `'men'`/`'women'`/`'unisex'` 또는 None (미설정). 크로스세션 영속. per-request 명시 gender 는 이 값을 덮어쓰지 않음 |
| `app/infrastructure/memory/taste_profile_pg.py` | (구 `app/channels/taste_profile_pg.py`) Postgres 기반 취향 프로파일 저장소 (SPEC-ARCH-AI-001). **SPEC-GENDER-PIN-001**: `_aget_or_create` / `_aupdate` SQL에 `gender` 컬럼 추가 (migration 0008 `ai.user_taste_profile.gender TEXT`). `_row_to_profile` 후방 호환 — 컬럼 없는 이전 row 는 None |
| `app/core/di.py` | DI 컨테이너 — `provide_db_pool` / `provide_settings` / `provide_embed_provider`. `db_pool` 모듈 글로벌 상태 위임 어댑터로 유지 (byte-identical, REQ-AI-003) |
| `app/core/types.py` | 공유 타입 — `ProductRow` 등 (SPEC-ARCH-AI-001) |
| `app/domain/search.py` | 도메인 타입 — `SearchResult`, `Candidate` (app/models/ DTO 와 분리) (SPEC-ARCH-AI-001) |
| `app/providers/database.py` | SupabaseProvider — PostgREST 클라이언트 (논리명 유지, async, lifespan 워밍업) |
| `app/providers/embedding.py` | Modal HTTP + 응답 스키마 검증. `embed_image_url` (image → 768-dim) + **`embed_text`** (text query → 768-dim, same FashionSigLIP L2 space, SPEC-SEARCH-V6-001). 260522: cache-lookup / Modal HTTP 각각 `⏱` 타이밍 로그 추가 (cold-start 병목 분리) |
| `app/providers/embedding_cache.py` | **NEW** — PG 벡터 캐시. `text_query → 768-dim` 재사용 (migration 0007 `ai.embedding_cache_text`). Modal cold-start ~26s 우회. hit 시 `EMBED_CACHE_HIT` 로그 |
| `app/providers/llm.py` | LiteLLM HTTP |
| `app/observability/langfuse.py` | `@observe` (no-op fallback) + langfuse env 자동 주입 + `current_langfuse_trace_id()` export + `emit_feedback_score()` (P0 암묵 피드백 → 원본 trace score retro-attach, fail-open) |
| `app/observability/conversation_log.py` | 대화 이벤트 로거 — `log_event()` (async, never raises) + `emit()` (fire-and-forget asyncio.create_task) + `_truncate()` payload cap. `MEMORY_BACKEND_IS_POSTGRES` 가드. 모든 graph 노드 + webhook intake 에서 호출 (SPEC-CONVERSATION-LOG-001) |
| `app/observability/event_payloads.py` | 20개 이벤트 타입 TypedDict 정의 (`user_text`, `user_photo`, `intent_routed`, `vision_done`, `search_done`, `diversify_done`, `card_sent`, `card_clicked`, `bot_text`, `taste_update`, `node_error`, `tool_call` 등 — SPEC-CONVERSATION-LOG-001 + SPEC-AGENT-V2-REACT) |
| `app/models/request.py` | RecommendRequest (alias + image_url SSRF 가드) |
| `app/models/response.py` | RecommendResponse (serialization_alias) |

## 검색 책임 경계 (B 옵션)

```
[Postgres RPC] embedding-first (cosine, distance ASC) → top-50  (v6 — SPEC-SEARCH-V6-001)
       ↓
[Python] 다양성 캡(브랜드/플랫폼) + tolerance + 최종 정렬 → top-15
```

## 환경 변수

`.env.example` 참조. 키는 `.env`에 (POC 단계 — 운영 시 Parameter Store 전환 예정).

주요 feature flag (상세는 `docs/infra/env.md`):
- `VISION_SCHEMA_V2` (기본 `true`) — Vision 풍부 스키마 (SPEC-VISION-UNIFY-001)
- `SELF_CRITIQUE_ENABLED` (기본 `true`) + `SELF_CRITIQUE_MAX_ITERATIONS` / `SELF_CRITIQUE_THRESHOLD` / `SELF_CRITIQUE_TIMEOUT_S` / `SELF_CRITIQUE_FASTPATH_DROP_FILTERS` + `EVALUATOR_MODEL` / `EVALUATOR_MAX_TOKENS` / `EVALUATOR_TEMPERATURE` / `EVALUATOR_TIMEOUT_S` (SPEC-AGENTIC-CRITIQUE-001)
- `CLARIFY_CARDS_ENABLED` (기본 `true`) + `CLARIFY_MAX_BUTTONS` (SPEC-CLARIFY-CARDS-001)
- `RESPONSE_SPLIT_ENABLED` (기본 `true`) + `RESPONSE_SPLIT_DELAY_MS` / `RESPONSE_SPLIT_MIN_CHARS` — 문장 단위 분할 발화 (noscroll benchmark P0)
- `LANGFUSE_FEEDBACK_SCORES` (기본 `true`) — P0 암묵 피드백 → 원본 추천 trace Langfuse score kill-switch (click/no_click/re_query). off 시 `create_score()` 만 침묵, 피드백 경로는 그대로
- `REDIS_URL` (기본 `redis://localhost:6379/1`) — SPEC-CHAT-STATE-REDIS-001 chat-state(pager cursor + impression dedupe) 외부화. 로컬 docker-compose `redis:7-alpine` / 테스트 fakeredis / prod dev-ai Langfuse redis 컨테이너 DB 1(`redis://:${REDIS_AUTH}@redis:6379/1`, Langfuse 는 DB 0). 키 prefix `kiko:*`. fail-open — Redis 다운이 추천을 막지 않음
- `APIFY_TOKEN` / `APIFY_INSTAGRAM_ACTOR` / `APIFY_SYNC_TIMEOUT_S` / `APIFY_FETCH_TIMEOUT_S` / `APIFY_402_COOLOFF_S` — IG 포스트 이미지 Apify fetch (`instagram_apify.py`). 상세는 `docs/infra/env.md § IG Apify 채널`
- **ReAct 에이전트 루프 (영구 단일 토폴로지, SPEC-AGENT-V2-CLEANUP-001)**: `AGENT_LLM_MODEL` (기본 `nova-lite` via LiteLLM, 미설정 시 fail-closed) + `AGENT_MAX_ITERATIONS` / `AGENT_TURN_TOKEN_BUDGET` / `AGENT_TOOL_TIMEOUT_S` / `AGENT_LLM_TIMEOUT_S` / `AGENT_LLM_MAX_RETRIES` / `AGENT_TOOL_MAX_RETRIES` / `AGENT_RESPOND_TIMEOUT_S`. V3 4-Gap(memory/Reflexion/proactive/dislike) 모두 unconditional — 개별 플래그 제거됨
  - `AGENT_V3_MEMORY_MAX_TOKENS` (기본 `1500`) — Gap1 메모리 주입 페이로드 token cap (char 근사: *4, 유일하게 남은 V3 튜닝값)

## 관련 프로젝트

| 프로젝트 | 경로 | 역할 |
|----------|------|------|
| kikoai/app | `/Users/hansangho/Desktop/kikoai/app` | Next.js 모놀리스 (caller + v4 폴백) |
| aws-infra | `/Users/hansangho/Desktop/aws-infra/kiko-ai-servers/portal-ai/` | EC2 docker-compose + Langfuse + Modal 인프라 |

## 인증 구조

AI 서버는 stateless. 인증 없음.
`kikoai/app`이 세션 + Auth.js v5 (Credentials Provider + bcrypt) 담당, AI 서버에 request body로 전달.
앱/웹 채팅 경로는 이 서버가 직접 `/v1/auth` 소셜 로그인 + JWT 발급/검증을 담당한다.
