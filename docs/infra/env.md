# 환경변수

> `.env` 에 들어가는 전체 키. 코드 grep 기반 실측. 변경 시 `.env.example` 동기화 필수.

## 필수 (운영)

| 키 | 용도 | 노출 |
|----|------|------|
| `SUPABASE_URL` | Supabase 프로젝트 URL | 서버 전용 |
| `SUPABASE_SERVICE_ROLE_KEY` | RLS 우회 — `search_products_v5` RPC 호출 | 서버 전용 |
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

## Telegram 메신저 채널 (SPEC-MSG-001)

| 키 | 용도 | 기본 |
|----|------|-----|
| `MESSENGER_BACKEND` | 활성 어댑터 (`telegram` \| `bluebubbles` \| `sendblue`) | `telegram` |
| `TELEGRAM_BOT_TOKEN` | Bot API 인증 토큰 (`@BotFather` 발급) | 필수 |
| `TELEGRAM_WEBHOOK_SECRET` | `X-Telegram-Bot-Api-Secret-Token` 검증값 (랜덤 32+ chars) | 필수 |
| `TELEGRAM_PUBLIC_URL` | webhook 등록용 공개 HTTPS URL (dev: Cloudflare Tunnel) | 필수 |
| `TELEGRAM_API_BASE` | Bot API base URL (셀프호스트 테스트 시 override) | `https://api.telegram.org` |
| `TELEGRAM_BOT_USERNAME` | 봇 username (로그/health 노출용) | `kiko_fashion_ai_bot` |
| `VISION_MODEL` | Vision 추출에 사용할 LiteLLM 모델 명칭 | `gpt-4o-mini` |
| `BOT_LANGUAGE` | 봇 응답 언어 (`ko` / `en`) | `ko` |
| `SESSION_TTL_SECONDS` | 인메모리 세션 만료 시간 (초) | `1800` |

> dev 환경에서는 Cloudflare Tunnel(`cloudflared tunnel --url http://localhost:8000`)로 `TELEGRAM_PUBLIC_URL` 확보.

## Langfuse

| 키 | 용도 | 설정 시점 |
|----|------|----------|
| `LANGFUSE_HOST` | Langfuse 서버 base URL (Docker network: `http://langfuse-web:3000`) | docker-compose 시작 시 |
| `LANGFUSE_PUBLIC_KEY` | `pk-lf-...` | Langfuse UI 첫 로그인 후 발급 |
| `LANGFUSE_SECRET_KEY` | `sk-lf-...` | 동일 |

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

`app/channels/vision.py` + `vision_prompt.py` — `portal/app` `analyze.ts` 와 동일 JSON 스키마 (styleNode/sensitivityTags/mood/palette/style/items[]).

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
| `EVALUATOR_MODEL` | `gpt-4o-mini` | LLM-evaluator 모델 (LiteLLM 경유) |
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

## 앱 메타

| 키 | 기본 |
|----|-----|
| `PROJECT_NAME` | `portal-ai-server` |
| `VERSION` | `0.1.0` (`pyproject.toml` 와 별개) |
| `ENVIRONMENT` | `development` |
| `DEBUG` | `False` |

## EC2 docker-compose 측 (LiteLLM/Langfuse 컨테이너 용)

`aws-infra/portal-ai-servers/portal-ai/env/.env` 참조. 위의 AI 서버 키 외 추가:

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
