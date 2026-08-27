# 환경변수

> `.env` 에 들어가는 전체 키. 코드 grep 기반 실측. 변경 시 `.env.example` 동기화 필수.

## 필수 (운영)

| 키 | 용도 | 노출 |
|----|------|------|
| `DB_URL` | PostgREST 엔드포인트 base URL. 현재 dev-app EC2 의 nginx PostgREST shim (`http://172.31.59.31:3001`) 을 가리킴 — Supabase.com 미사용 (SPEC-INFRA-MIGRATE-001 P6 이후, P8 에서 SUPABASE_URL → DB_URL 리네임) | 서버 전용 |
| `DB_TOKEN` | PostgREST service JWT — `search_products_v6` RPC 호출 (구 SUPABASE_SERVICE_ROLE_KEY) | 서버 전용 |
| `MODAL_EMBED_URL` | Modal `/embed` 엔드포인트 base URL | 서버 전용 |
| `MODAL_EMBED_TOKEN` | Modal Bearer token (`EMBED_AUTH_TOKEN` 과 동일) | 서버 전용 |
| `LITELLM_BASE_URL` | LiteLLM proxy base URL | 서버 전용 |
| `LITELLM_MASTER_KEY` | LiteLLM 인증 키 | 서버 전용 |
| `INTERNAL_API_TOKEN` | Next.js → AI 서버 shared secret. 미설정 시 인증 스킵(dev 전용) | 서버 전용 |

## 보안 가드

| 키 | 용도 | 기본 |
|----|------|-----|
| `ALLOWED_IMAGE_HOSTS` | SSRF 방지 — `image_url` 호스트 화이트리스트 (콤마 구분, suffix 매칭). 비어있으면 검증 스킵 | `""` (dev 만 허용) |

운영 권장값:
```
ALLOWED_IMAGE_HOSTS=pub-dddeb1e14cdf428caa5cfbad8e1f98da.r2.dev,r2.cloudflarestorage.com
```

## APNs notification worker

The notification worker is a separate process and is disabled by default. Its
schedule, APNs credentials, sandbox/production routing, rollout, and mobile
contract are documented in [`../features/notifications.md`](../features/notifications.md).

| Key | Default | Purpose |
| --- | --- | --- |
| `NOTIFICATION_WORKER_ENABLED` | `false` | Worker kill switch |
| `APNS_TOPIC` | `com.kikoai.app` | APNs topic / iOS bundle identifier |
| `APNS_TEAM_ID` | `""` | Apple Developer Team ID |
| `APNS_SANDBOX_KEY_ID` | `""` | Development APNs signing key ID |
| `APNS_SANDBOX_AUTH_KEY_B64` / `_PATH` | `""` | Development `.p8`, base64 or mounted path |
| `APNS_PRODUCTION_KEY_ID` | `""` | Production APNs signing key ID |
| `APNS_PRODUCTION_AUTH_KEY_B64` / `_PATH` | `""` | Production `.p8`, base64 or mounted path |
| `NOTIFY_TIMEZONE` | `Asia/Seoul` | Policy timezone |
| `NOTIFY_WORKER_POLL_S` | `30` | Scheduler/delivery poll interval |
| `NOTIFY_DELIVERY_BATCH_SIZE` | `100` | Claimed deliveries per cycle |
| `NOTIFY_APNS_CONCURRENCY` | `20` | Maximum concurrent APNs requests |
| `NOTIFY_MAX_DELIVERY_ATTEMPTS` | `5` | Delivery attempt cap before expiry |

Do not place `.p8` material in client-visible env, logs, Git, or the mobile app.

## Uploads

`POST /v1/uploads` 가 S3 presigned PUT URL을 발급하고, 최종 `image_url`은 CloudFront base URL + object key로 반환한다. 클라이언트는 응답의 `upload_url`로 직접 PUT하며, PUT 요청에는 요청했던 `Content-Type` 헤더를 그대로 포함해야 한다.

| 키 | 기본 | 용도 |
|----|-----|------|
| `UPLOADS_S3_BUCKET` | `""` | 업로드 대상 S3 bucket |
| `UPLOADS_S3_REGION` | `ap-northeast-2` | S3 bucket region |
| `UPLOADS_S3_ACCESS_KEY_ID` | `""` | presign 전용 access key. 비어 있으면 `AWS_ACCESS_KEY_ID` fallback |
| `UPLOADS_S3_SECRET_ACCESS_KEY` | `""` | presign 전용 secret key. 비어 있으면 `AWS_SECRET_ACCESS_KEY` fallback |
| `UPLOADS_S3_SESSION_TOKEN` | `""` | 임시 자격증명 사용 시 session token. 비어 있으면 `AWS_SESSION_TOKEN` fallback |
| `UPLOADS_PUBLIC_BASE_URL` | `""` | CloudFront/public CDN base URL |
| `UPLOADS_KEY_PREFIX` | `uploads` | S3 object key prefix |
| `UPLOADS_MAX_SIZE_BYTES` | `10485760` | API가 허용하는 최대 이미지 크기(10MB). 초과 이미지는 클라이언트가 리사이즈 후 재요청 |
| `UPLOADS_PRESIGN_EXPIRES_SECONDS` | `300` | presigned URL TTL. 런타임에서 60~3600초로 clamp |

## 봇 기본 설정

| 키 | 용도 | 기본 |
|----|------|-----|
| `VISION_MODEL` | Vision 추출에 사용할 LiteLLM 모델 명칭 | `gpt-4o-mini` |
| `BOT_LANGUAGE` | 봇 응답 언어 (`ko` / `en`) | `ko` |
| `SESSION_TTL_SECONDS` | 인메모리 세션 만료 시간 (초) | `1800` |

## Langfuse

| 키 | 용도 | 설정 시점 |
|----|------|----------|
| `LANGFUSE_HOST` | Langfuse 서버 base URL (Docker network: `http://langfuse-web:3000`) | docker-compose 시작 시 |
| `LANGFUSE_PUBLIC_KEY` | `pk-lf-...` | Langfuse UI 첫 로그인 후 발급 |
| `LANGFUSE_SECRET_KEY` | `sk-lf-...` | 동일 |
| `LANGFUSE_SELECTIVE_MODE` | `false` | 비-LLM 노드 `@observe` 제거 비상 롤백 — `true` 시 4 LLM 노드만 trace |
| `LANGFUSE_FEEDBACK_SCORES` | `true` | 암묵 피드백(click/no_click/re_query) → 원본 추천 trace score retro-attach kill-switch. `false` 시 `create_score()` 만 침묵, 피드백/taste 경로는 그대로 |

상세: [`features/observability.md`](../features/observability.md).

## 검색 파라미터 (코드 기본값 — 보통 안 바꿈)

| 키 | 기본 |
|----|-----|
| `SEARCH_DEFAULT_K` | 50 — RPC top-k |
| `SEARCH_BRAND_CAP` | 2 — 다양성: 브랜드당 최대 (brand_filter 활성 시 ×3 완화) |
| `SEARCH_PLATFORM_CAP` | 3 — 다양성: 플랫폼당 최대 |
| `SEARCH_FINAL_LIMIT` | 15 — 최종 응답 개수 (`final_limit` 미명시 시 tolerance→target 사용) |

## LangGraph 응답 노드 (SPEC-AGENT-001)

`app/graphs/nodes/respond.py` 와 `ask_clarify.py` 에서 사용. 두 노드는 `langchain-openai.ChatOpenAI` 를 LiteLLM 프록시 (`LITELLM_BASE_URL + "/v1"`) 로 라우팅.

| 키 | 기본 | 용도 |
|----|-----|-----|
| `RESPONSE_MODEL` | `gpt-4o-mini` | `respond` / `ask_clarify` 모델 id (LiteLLM 경유) |
| `RESPONSE_TIMEOUT_MS` | `5000` | LLM 호출 timeout — 초과 시 하드코딩 fallback 텍스트 사용 |
| `RESPONSE_MAX_TOKENS` | `200` | `respond` 출력 토큰 cap (`ask_clarify` 는 코드 내 80 으로 별도 cap) |
| `ASK_CLARIFY_MIN_DESC_TOKENS` | `3` | vision 결과 description 토큰 수 < 임계 → `ask_clarify` 트리거 (REQ-AGENT-009, legacy v1) |
| `ASK_CLARIFY_AMBIGUOUS_LABELS` | `item,clothing,thing,piece` | 단일 모호 라벨 denylist — 매칭 시 `ask_clarify` 트리거 |
| `ASK_CLARIFY_MIN_QUERY_TOKENS` | `4` | v2: searchQuery 토큰 수 < 임계 → clarify 트리거 (rename of `MIN_DESC_TOKENS`) |
| `ASK_CLARIFY_AMBIGUOUS_SUBCATEGORIES` | `item,clothing,thing,piece` | v2: subcategory 모호 시 disambiguation 카드 |

## Vision 풍부 스키마 (SPEC-VISION-UNIFY-001)

`app/channels/vision.py` + `vision_prompt.py` — `kikoai/app` `analyze.ts` 와 동일 JSON 스키마 (styleNode/sensitivityTags/mood/palette/style/items[]).

| 키 | 기본 | 용도 |
|----|-----|-----|
| `VISION_SCHEMA_V2` | `true` | v2 풍부 스키마 ON/OFF (false 시 legacy minimal label/description 폴백) |

## 자가비평 루프 (SPEC-AGENTIC-CRITIQUE-001)

`app/graphs/nodes/evaluator.py` — `search → evaluator → send_results` Reflexion 루프. 빈 결과 fast-path (필터 drop, LLM 호출 없음) + LLM 평가 (점수 < threshold 시 `CritiqueDelta` 재시도).

| 키 | 기본 | 용도 |
|----|-----|-----|
| `SELF_CRITIQUE_ENABLED` | `true` | 루프 ON/OFF |
| `SELF_CRITIQUE_MAX_ITERATIONS` | `2` | 최대 재시도 횟수 (1차 search 제외) |
| `SELF_CRITIQUE_THRESHOLD` | `0.6` | 통과 점수 (0~1) |
| `SELF_CRITIQUE_TIMEOUT_S` | `30` | 전체 루프 wall-clock 가드 |
| `SELF_CRITIQUE_FASTPATH_DROP_FILTERS` | `min_price,max_price,exclude_keywords` | 빈 결과 시 drop 대상 필터 (콤마 구분) |
| `EVALUATOR_MODEL` | `nova-lite` | LLM-evaluator 모델 (LiteLLM 경유) |
| `EVALUATOR_MAX_TOKENS` | `400` | 평가 응답 cap |
| `EVALUATOR_TEMPERATURE` | `0.2` | 평가 sampling |
| `EVALUATOR_TIMEOUT_S` | `8` | 단일 평가 호출 timeout |

안전 가드 (FROZEN, env 무관): iteration cap / stagnation (점수 개선 없음) / score regression / wall-clock.

## Clarify 카드 (SPEC-CLARIFY-CARDS-001)

`app/channels/clarify.py` + `app/graphs/nodes/apply_clarify.py` — weak-vision 시 6 axes 결정형 인라인 키보드 (LLM 호출 없음). `clarify:*` callback → `session.boost_keywords` 누적 (sticky).

| 키 | 기본 | 용도 |
|----|-----|-----|
| `CLARIFY_CARDS_ENABLED` | `true` | 카드 모드 ON/OFF (false 시 legacy 텍스트 질문 폴백) |
| `CLARIFY_MAX_BUTTONS` | `5` | 카드당 최대 버튼 개수 |

## 응답 문장 분할 (noscroll benchmark P0)

`app/graphs/nodes/respond.py` — LLM/fallback 출력을 문장 단위로 분할 발화. 대화감 향상.

| 키 | 기본 | 용도 |
|----|-----|-----|
| `RESPONSE_SPLIT_ENABLED` | `true` | 문장 분할 ON/OFF |
| `RESPONSE_SPLIT_DELAY_MS` | `350` | 청크 간 딜레이 (ms) |
| `RESPONSE_SPLIT_MIN_CHARS` | `8` | 이 값 미만 조각은 다음 청크와 병합 |


## ReAct 에이전트 루프 (SPEC-AGENT-V2-CLEANUP-001 — 영구 단일 토폴로지)

`app/agents/react_loop.py` + `app/agents/llm_client.py` — ReAct 에이전트가 유일한 토폴로지. V3 4-Gap 강화(Gap1 memory / Gap2 Reflexion / Gap3 proactive / Gap4 dislike discount)는 모두 unconditional. 개별 ON/OFF 플래그(`AGENT_V2_REACT_ENABLED`, `AGENT_V3_*_ENABLED`)는 제거됨.

| 키 | 기본 | 용도 |
|----|-----|-----|
| `AGENT_LLM_MODEL` | `nova-lite` | ReAct LLM 모델 명칭 (LiteLLM 경유). 미설정(빈 문자열) 시 fail-closed |
| `AGENT_MAX_ITERATIONS` | `6` | 턴당 최대 tool call 반복 횟수 (REQ-AGENT-LOOP-ITERATION-001) |
| `AGENT_TURN_TOKEN_BUDGET` | `32000` | 턴당 누적 LLM token 상한. 초과 시 fallback respond (REQ-AGENT-PERF-TURN-BUDGET-001) |
| `AGENT_TOOL_TIMEOUT_S` | `5.0` | 단일 tool dispatch timeout (초, REQ-AGENT-FAILURE-TOOL-001) |
| `AGENT_LLM_TIMEOUT_S` | `5.0` | 단일 LLM ainvoke timeout (초) |
| `AGENT_LLM_MAX_RETRIES` | `2` | LLM transient 오류(5xx/throttle/timeout) 재시도 횟수 |
| `AGENT_TOOL_MAX_RETRIES` | `1` | tool dispatch transient 오류 재시도 횟수. terminal `respond` 는 재시도 0 고정 |
| `AGENT_RESPOND_TIMEOUT_S` | `30.0` | terminal `respond` 툴 전용 wall-clock timeout (초). `sendMediaGroup` + summary 전송 시간 반영 |
| `AGENT_V3_MEMORY_MAX_TOKENS` | `1500` | Gap1 메모리 주입 페이로드 token cap (char 근사 ×4). 유일하게 남은 V3 튜닝값 |

안전 가드 (env 무관 FROZEN): 3-consecutive identical tool call 무한루프 가드, JSON malform 1x retry → exhaustion, args validation (TypedDict).

> `AGENT_LLM_MODEL` 은 로그에 모델명만 노출 — API key 는 `LITELLM_MASTER_KEY` 경유, 직접 노출 없음.

Gap2 Reflexion은 기존 SPEC-AGENTIC-CRITIQUE-001 env를 재사용 (live dependency로 보존 필요):
- `SELF_CRITIQUE_MAX_ITERATIONS` — Reflexion 호출 횟수 상한
- `SELF_CRITIQUE_TIMEOUT_S` — turn_deadline과 함께 잔여-budget 계산 기준
- `EVALUATOR_MODEL` / `EVALUATOR_MAX_TOKENS` / `EVALUATOR_TEMPERATURE` / `EVALUATOR_TIMEOUT_S` — `evaluator._call_llm` 직접 참조

## Redis (SPEC-CHAT-STATE-REDIS-001)

| 키 | 기본 | 용도 |
|----|-----|------|
| `REDIS_URL` | `redis://localhost:6379/1` | pager cursor + impression dedupe 외부화. 로컬: docker-compose `redis:7-alpine` / 테스트: `fakeredis` / prod: dev-ai 기존 redis DB 1 (`redis://:${REDIS_AUTH}@redis:6379/1`). fail-open — redis 다운 시 추천 차단 없음 |

> `kiko:*` prefix 사용. DB 0은 Langfuse 전용이므로 반드시 DB 1 이상 지정.

## IG Apify 채널

| 키 | 기본 | 용도 |
|----|-----|------|
| `APIFY_TOKEN` | (필수, Apify 콘솔 발급) | Apify API 인증. IG 포스트 이미지 fetch (instagram_apify.py) |
| `APIFY_INSTAGRAM_ACTOR` | `apify~instagram-post-scraper` | 사용할 Apify Actor ID |
| `APIFY_SYNC_TIMEOUT_S` | `120` | Apify run 동기 완료 대기 (초) |
| `APIFY_FETCH_TIMEOUT_S` | `130` | Apify 전체 fetch 타임아웃 (초) |
| `APIFY_402_COOLOFF_S` | `600` | Apify 402 응답 시 재시도 금지 쿨다운 (초) |

## 임베딩 캐시

텍스트 임베딩을 Postgres에 캐싱해 Modal cold-start(~26s) 우회. migration 0007에서 테이블 생성. 별도 env flag 없음 — `DB_URL`/`DB_TOKEN` 재사용.

## 메인 큐레이션

| 키 | 기본 | 용도 |
|----|-----|------|
| `CURATION_REFRESH_ENABLED` | `true` | Notion 메타데이터 동기화와 auto 후보 갱신 백그라운드 루프 |
| `CURATION_REFRESH_INTERVAL_S` | `900` | Notion 동기화·실패 재시도 간격. 최소 15분으로 클램프 |
| `CURATION_SEASON` | `summer` | `summer`는 아우터·니트·겨울 키워드를 제외하고, `winter`는 기본 품질 게이트만 유지 |
| `NOTION_TOKEN` | (선택) | 큐레이션 운영 DB 읽기용 Notion integration token |
| `NOTION_CURATION_DB_ID` | (선택) | `GET /v1/curation` 메타데이터 원본 Notion database ID |

auto 상품은 KST 날짜별 1회 생성하며, Notion 메타데이터 동기화가 완료되지 않았거나 6개 성별·구좌 조합 중 일부가 실패하면 다음 루프에서 재시도한다. 앱은 Notion을 직접 읽지 않고 `ai.curation_sections` 캐시만 읽는다.

`popular`은 최근 7일의 순 사용자 조회·저장·외부 링크 클릭과 리뷰 수로 상품 점수를 만든 뒤, 브랜드별 상위 3개 상품 점수를 합산해 브랜드 순위를 계산한다. 상위 브랜드의 상품을 브랜드당 최대 2개씩 제공한다. `wiki.status`는 위키 데이터 품질 상태일 뿐 큐레이션 순위나 노출 자격에 사용하지 않는다. auto 구좌는 `popular` → `trending-search` → `under-100` 순서로 선정하며, 앞 구좌에서 선택한 상품은 뒤 구좌에서 제외한다. 로그인 사용자의 요청별 개인화 재정렬에도 같은 중복 제외를 적용한다.

## 앱 메타

| 키 | 기본 |
|----|-----|
| `PROJECT_NAME` | `kiko-ai-server` |
| `VERSION` | `0.1.0` (`pyproject.toml` 와 별개) |
| `ENVIRONMENT` | `development` |
| `DEBUG` | `False` |

## EC2 docker-compose 측 (LiteLLM/Langfuse 컨테이너 용)

`aws-infra/kiko-ai-servers/portal-ai/env/.env` 참조. 위의 AI 서버 키 외 추가:

| 키 | 용도 |
|----|------|
| `OPENAI_API_KEY` | LiteLLM 이 OpenAI 라우팅 시 |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | Bedrock 사용 시 (EC2 IAM Role 부착했으면 불필요) |
| `AWS_REGION_NAME` | Bedrock 리전 (`ap-northeast-1`) |
| `LITELLM_DATABASE_URL` | LiteLLM-DB Postgres 연결 |
| `POSTGRES_PASSWORD` | LiteLLM-DB root 비밀번호 |
| `LANGFUSE_DB_PASSWORD` | Langfuse-DB Postgres 비밀번호 |
| `LANGFUSE_PUBLIC_URL` | Langfuse 외부 접근 URL (NEXTAUTH_URL 로 사용) |
| `NEXTAUTH_SECRET` | Langfuse 인증 (32+ chars random) |
| `SALT` | Langfuse 인증 (32+ chars random) |

## 시크릿 노출 체크

- 모든 `*_KEY`, `*_TOKEN`, `*_PASSWORD`, `_SECRET` 은 서버 전용
- `.env` 파일은 `.gitignore` 에 `.env*` (단, `.env.example` 제외)
- AI 서버 컨테이너의 `docker inspect` 로 환경변수가 평문 노출되므로, EC2 OS 사용자 권한 관리 필수
- 운영 단계 → AWS Parameter Store / Secrets Manager 전환 예정
