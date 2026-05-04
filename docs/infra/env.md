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
