# developer가 앱에서 캡에 걸리는 문제 — ai-server는 무죄, session 이벤트로 출처 확정

## Context (왜)

내부 developer 계정을 앱(REST/SSE)에서 쓰는데 tier별 일일 토큰 캡("비용 캡")에 계속 걸린다.
DB `ai.user_profiles.tier`는 `developer`로 확인됨. 텔레그램은 미사용, **앱 전용**.

## 확정된 사실 (증거 기반)

1. **env 오버라이드 없음** — 서버 컨테이너에서 `docker exec ai-server env | grep -iE 'CAP_TIER|DAILY_TOKEN'`
   → **빈 출력**. 따라서 전부 코드 기본값: `CAP_TIER_DEVELOPER=0`(무제한), `DAILY_TOKEN_CAP_ENABLED=True`
   (`app/core/config.py:311,321`).
2. **ai-server는 이 계정을 차단하지 않음** — `docker logs ai-server | grep "daily cap reached"` → **0건**.
   앱 경로의 캡 차단은 `chat_service.py:544`(메인 스트리밍) 한 곳뿐이며 차단 시 항상 로그를 남긴다.
   콜백 스트리밍(`chat_service.py:677`)엔 cap 차단 코드 자체가 없음(session만 방출).
3. 코드상 tier=developer → `_tier_cap('developer')=0` → `cap_reached=False` (`chat_service.py:157`).
   비용(USD) 기반 차단은 이 서버에 없음(`turn_cost.py`는 관측 전용).

⇒ **결론: 앱에 보이는 "캡"은 ai-server의 강제 차단이 아니다.**

## 남은 두 가지 원인 (둘 다 이 레포 밖)

ai-server는 스트림 첫 `session` 이벤트로 cap 메타데이터를 내려준다
(`chat_service.py:542` / `session_payload()` = `user_tier`, `daily_cap`, `cap_used`, `cap_remaining`).
developer면 `user_tier='developer'`, `daily_cap=0`, `cap_remaining=None`(null)이 내려간다.

- **원인 A — 프론트(kikoai/app) 렌더/게이트 버그**: `session` 이벤트가 developer/0/null을 정확히 내려줘도
  프론트가 이를 오해(예: `cap_remaining: null`을 0으로 취급, 또는 이전 free-tier payload 캐싱)해서
  "한도 초과" UI를 띄우는 경우. 참고: 디버그 엔드포인트는 무제한을 `remaining=-1`로 표현하는데
  (`debug.py:397`) 앱 payload는 `None`으로 표현 → 표현 불일치가 프론트 오처리 유발 가능성.
- **원인 B — user_id 불일치**: 앱 로그인 계정(OAuth provider+provider_id 기반 user_id, `auth.py:53`)이
  DB에서 developer로 바꾼 row의 user_id와 다름. 이 경우 앱은 다른(예: free) 티어로 결정되고,
  free 한도까지 쓰면 프론트가 카운트다운/차단. (단, 이 경우에도 서버가 실제 막았다면 `daily cap reached`
  로그가 있어야 하는데 없으므로, 서버 차단 전 프론트 자체 게이트일 가능성이 큼.)

## 결정적 확인 (구체 조치 — 진단 로그 1줄)

현재 앱 경로는 캡에 걸릴 때만 로그를 남겨서(`chat_service.py:545`), developer가 정상적으로 통과할 때
서버가 결정한 tier를 확인할 방법이 없다. `get_app_cap_status`에 **INFO 로그 한 줄**을 추가해
매 앱 세션의 결정 결과(runtime `user_id` + 해석된 tier + cap)를 남긴다.

### 변경 대상 (유일)

`app/services/chat_service.py` — `get_app_cap_status` (147-166), `return` 직전에 추가:

```python
logger.info(
    "[chat_service] cap_status user=%s tier=%s cap=%d used=%d reached=%s",
    user_id, user_tier, daily_cap, cap_used, cap_reached,
)
```

- 세션당 1회만 호출되므로 로그 스팸 아님. `logger`는 이미 모듈에 존재.
- 이 한 줄이 **런타임에서 앱이 실제 사용한 user_id와 그 tier**를 노출 → 원인 A/B를 즉시 판별.

### 검증 (재현 → grep)

1. 배포 후 developer 계정으로 앱 채팅 1회 실행.
2. `docker logs ai-server 2>&1 | grep "cap_status" | tail`
   - `tier=developer cap=0` → **서버 정상**. 버그는 kikoai/app 프론트(원인 A) → 그 레포에서 수정.
   - `tier=free …` → **user_id 불일치(원인 B)**. 로그의 `user=<uuid>` 를 DB에서 조회
     (`SELECT email, provider, tier FROM ai.user_profiles WHERE user_id='<uuid>'`) 하여,
     그 계정에 `UPDATE ai.user_profiles SET tier='developer'` 적용(이전엔 다른 row를 바꿨던 것).
3. 원인 확정 후 이 진단 로그는 유지(관측용) 또는 제거 — 운영 판단.

### 커밋 전 필수 (CLAUDE.md 규칙)

`uv run ruff check . && uv run ruff format --check . && uv run pytest` 통과 후 커밋.

## 예상 수정 위치

- 원인 A: `kikoai/app`(Next.js, `/Users/hansangho/Desktop/kikoai/app`) — cap UI 로직. **이 레포 아님.**
  (선택) ai-server 쪽에서 무제한 표현을 프론트 친화적으로 바꾸는 소폭 변경 가능:
  `session_payload`의 `cap_remaining`을 무제한일 때 `None` 대신 `-1`로 통일하거나 `"unlimited": true`
  플래그 추가 — 단, 프론트 계약 확인 후에만. 지금은 확정 불가라 보류.
- 원인 B: 코드 변경 없음. 올바른 user_id에 developer 티어 재적용(데이터 수정).

## 범위 밖 / 미변경

- 이 레포 소스 변경은 현재 **없음** (원인 확정 전). env도 변경 불필요(빈 = 기본값 = developer 무제한).
- 텔레그램 경로(Redis `kiko:tier`)는 미사용이라 무관.
