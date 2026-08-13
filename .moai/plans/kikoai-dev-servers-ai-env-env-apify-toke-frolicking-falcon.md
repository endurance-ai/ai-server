# APIFY_TOKEN 401 → 재발급 & 반영 Runbook

## Context

dev-ai 서버의 `kikoai-dev-servers/ai/env/.env`에 세팅된 `APIFY_TOKEN`
(`apify_api_eQ75...`)이 **401 Unauthorized**를 반환한다.

- 이 AI 서버는 IG 포스트 이미지를 Apify actor(`apify~instagram-post-scraper`)로 fetch하며,
  인증은 `Authorization: Bearer $APIFY_TOKEN` 헤더 방식이다 (`app/channels/instagram_apify.py:118`).
- 401/403은 **토큰 무효/폐기/오타** 신호다. (크레딧 소진은 402로 별도 처리 — `instagram_apify.py:130`)
  → 크레딧 문제가 아니라 **토큰 재발급**이 올바른 해결책.
- fail-open 설계라 토큰이 죽어도 IG fetch만 빈 배열 반환하고 서버는 계속 동작한다
  (원본 URL 폴백). 즉 서비스 중단은 아니지만 IG 이미지 인식이 안 됨.
- **보안**: 기존 토큰이 채팅에 노출됨 → 재발급 시 기존 토큰은 반드시 폐기(revoke).

> 주의: 토큰은 Apify 계정 로그인이 필요해 **AI(Claude)가 대신 발급할 수 없음.**
> 발급은 사용자가 브라우저에서 직접, 이후 서버 반영은 지원 가능.

## Step 1 — Apify 콘솔에서 새 토큰 발급 (사용자 직접)

1. https://console.apify.com 로그인
2. 우상단 계정 → **Settings → API & Integrations** (또는 Integrations 탭)
3. Personal API tokens 섹션:
   - 기존 토큰 옆 **⋯ → Revoke** (노출된 `apify_api_eQ75...` 폐기)
   - **+ Create a new token** → 이름(예: `kiko-ai-server-dev`) → 생성
   - 생성된 `apify_api_...` 토큰 복사 (한 번만 전체 노출됨)
4. (권장) 발급 전, 결제/크레딧이 살아있는지도 Billing에서 확인 —
   401이 아닌 402가 재발하면 그건 크레딧 문제.

## Step 2 — 새 토큰 유효성 검증 (선택, 반영 전)

로컬 또는 서버 쉘에서:

```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  -H "Authorization: Bearer <NEW_TOKEN>" \
  https://api.apify.com/v2/users/me
```

- `200` → 토큰 정상
- `401` → 토큰/헤더 문제 (복사 오류 등)

## Step 3 — dev-ai 서버 `.env` 반영 (사용자 직접 또는 지원)

대상: `kikoai-dev-servers/ai/env/.env` (aws-infra dev-ai 서버, **이 저장소 아님**)

```
APIFY_TOKEN=<NEW_TOKEN>
```

- 이 저장소엔 `.env.example:162`만 있고 실제 값은 서버에 있음.
- 값에 따옴표/공백 없이 넣기 (`instagram_apify.py:90`에서 `.strip()` 하긴 함).

## Step 4 — AI 서버 재기동 (env 리로드)

docker compose 스택이면:

```bash
docker compose --env-file kikoai-dev-servers/ai/env/.env up -d --force-recreate ai
# 또는 해당 서비스명으로 restart
```

- `settings.APIFY_TOKEN`은 pydantic-settings로 프로세스 시작 시 로드되므로
  **컨테이너 재기동 필요** (핫리로드 아님).

## Verification (end-to-end)

1. 서버 로그 tail 상태로 텔레그램 봇에 IG 포스트 URL DM 전송
2. 로그에서 `🔗 [apify-ig] 응답 ... status=200` 확인
   - `status=401/403` → 토큰 반영 안 됨 (Step 3/4 재확인)
   - `status=402` → 크레딧 소진 (토큰 아닌 결제 이슈)
3. 봇이 IG 이미지 기반 추천 카드를 정상 응답하면 완료.

## 참고 파일

- `app/channels/instagram_apify.py` — Apify fetch + 401/402 분기 로직
- `app/core/config.py:223` — `APIFY_TOKEN` 설정 필드
- `docs/infra/env.md:178` — env 문서
- `.env.example:162` — 로컬 예시
