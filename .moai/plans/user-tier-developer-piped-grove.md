# developer tier인데도 토큰 캡에 걸리는 문제 — 진단 및 운영 조치

## Context

**문제**: 자체 앱(REST `/v1/chat`)에서 사용 중인 계정을 무제한으로 만들려고 `ai.user_profiles.tier`를
`developer`로 직접 UPDATE 했고, DB에 `developer`로 저장된 것도 확인했다. 그런데도 여전히 일일 토큰
캡(daily token cap)에 걸린다.

**원하는 결과**: 내 계정이 앱에서 실제로 캡 제한 없이(무제한) 동작하게 만든다. 범위는 운영 조치 위주,
코드 변경 최소. (Telegram 경로는 더 이상 사용하지 않으므로 무관.)

---

## 근본 원인 분석 (코드로 확정된 사실)

앱(REST) 경로에서 캡을 차단하는 지점은 **딱 하나**다 — `app/services/chat_service.py:544`
(`invoke_streaming` 안에서 `if cap_status.cap_reached:`). 이 값은 `get_app_cap_status`가 만든다:

```python
# app/services/chat_service.py:147-166
user_tier = await _get_app_user_tier(pool, user_id)   # SELECT tier FROM ai.user_profiles WHERE user_id=%s (오류 시 'free')
cap_tier  = _APP_TO_CAP_TIER.get(user_tier, "free")   # developer → developer
daily_cap = token_cap._tier_cap(cap_tier)             # developer → settings.CAP_TIER_DEVELOPER
cap_reached = DAILY_TOKEN_CAP_ENABLED and daily_cap > 0 and cap_used >= daily_cap
```

- `_tier_cap('developer')` == `settings.CAP_TIER_DEVELOPER` (`app/infrastructure/cache/token_cap.py:68-69`)
- 코드 기본값과 `.env.example`는 `CAP_TIER_DEVELOPER = 0` (= 무제한). 0이면 `daily_cap > 0`이 False라
  **절대 `cap_reached`가 될 수 없다.**

따라서 tier가 런타임에 `developer`로 읽히는데도 캡에 걸린다면, 가능한 원인은 **둘 중 하나뿐**이다:

1. **(유력) 배포 서버의 `CAP_TIER_DEVELOPER`가 0이 아님.** prod `.env`가 코드 기본값(0)을 non-zero로
   덮어써서 developer도 유한 캡을 가진다. → `daily_cap > 0` → 걸림.
2. **런타임이 이 세션의 user_id로 developer가 아닌 tier를 읽음.** 앱 로그인 세션이 실제로 해석하는
   `user_id`가, 내가 UPDATE한 행의 `user_id`와 다르거나(다른 계정/행), 앱이 내가 확인한 DB와 **다른 DB**를
   바라본다. 이 경우 `_get_app_user_tier`가 `free`/기타 tier를 돌려주고 → free 캡(500K)에 걸림.

> 참고: Redis(`kiko:tier`) / `POST /debug/cap/tier`는 Telegram 경로 전용이라 앱 캡과 무관하다.
> Redis가 다운되면 캡은 오히려 **비활성(fail-open)** 되므로, "걸린다"는 것은 Redis 문제도 아니다.

---

## 결정적 진단 (관찰 1번으로 원인 확정)

서버 접속 없이, 앱이 실제로 무엇을 읽는지 그대로 보여주는 단일 근거가 있다:
`POST /v1/chat/sessions`의 **첫 SSE `session` 이벤트**. 여기에 `user_tier`와 `daily_cap`,
`cap_remaining`이 그대로 실린다 (`AppCapStatus.session_payload`, `app/services/chat_service.py:80-89`).

로그인 토큰(JWT)과 배포 base URL로 한 번 호출해 `session` 이벤트만 보면 된다:

```bash
curl -N -X POST "$BASE_URL/v1/chat/sessions" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message":"cap 진단용 핑"}' | head -5
# → event: session
#    data: {"session_id":"...","user_tier":"???","daily_cap":???,"cap_remaining":???, ...}
```

판정:
- `user_tier == "developer"` **그리고** `daily_cap > 0` → **원인 1 (env)**. 서버 `CAP_TIER_DEVELOPER`가 0이 아님.
- `user_tier != "developer"` (예: `free`/`pro`) → **원인 2 (identity/DB)**. 앱 세션 user_id가 내가
  본 developer 행과 다르다.

---

## 조치 (원인별)

### 원인 1 — `CAP_TIER_DEVELOPER`가 non-zero (env)

배포 서버(aws-infra `portal-ai` docker-compose)의 env에서 값을 0으로 바꾸고 ai-server를 재기동한다.

```bash
# 서버에서 현재 값 확인
docker compose exec <ai-server-service> env | grep CAP_TIER_DEVELOPER
# .env(또는 compose env)에서 CAP_TIER_DEVELOPER=0 으로 수정 후
docker compose up -d <ai-server-service>   # 컨테이너 재기동으로 settings 재로딩
```

→ 재기동 후 `session` 이벤트가 `user_tier:"developer"`, `daily_cap:0`, `cap_remaining:null`이면 해결.
(effect는 developer tier 전체에 적용되지만 developer는 내부 전용 tier라 의도된 동작이다.)

### 원인 2 — 앱 세션의 user_id ≠ 내가 UPDATE한 행

내 실제 계정 행을 이메일로 찾아 그 `user_id`를 developer로 설정한다 (`ai.user_profiles`에 `email` 컬럼 존재):

```sql
SELECT user_id, email, tier, tier_expires_at, updated_at
FROM ai.user_profiles WHERE email = '<내 로그인 이메일>';
-- 위 user_id가 developer가 아니면 그 행을 갱신
UPDATE ai.user_profiles SET tier = 'developer', tier_expires_at = NULL, updated_at = now()
WHERE user_id = '<위에서 나온 user_id>';
```

- 앱이 **다른 DB**를 보는 경우: 앱 서버가 쓰는 `DB_DSN`(`migrations/env.py:23`가 읽는 값과 동일 소스)이,
  내가 psql로 확인한 접속 대상과 같은지 확인한다. 다르면 앱이 보는 DB에서 위 UPDATE를 수행한다.
- 갱신 후 `session` 이벤트로 `user_tier:"developer"` 확인.

> 캡 카운터(`cap_used`) 리셋은 불필요하다. developer는 `daily_cap=0`이라 카운터 값과 무관하게
> `cap_reached`가 항상 False가 된다.

---

## 검증 (end-to-end)

1. `POST /v1/chat/sessions`를 다시 호출해 첫 `session` 이벤트가
   `user_tier:"developer"`, `daily_cap:0`, `cap_remaining:null` 인지 확인.
2. 같은 스트림에서 `cap_reached` 이벤트가 **나오지 않고** 정상적으로 `text`/`product`/`done`까지
   진행되는지 확인.
3. (선택) 캡이 실제로 소진 직전이던 계정으로 재현이 안 되면, 원인 1이었음을 재확인.

---

## 변경 파일

- **운영 조치로 코드 변경 없음.** 대상은 배포 서버 env(`CAP_TIER_DEVELOPER=0`) 또는 DB 데이터(올바른
  user_id의 tier).
- 참조(읽기 전용): `app/services/chat_service.py:147-166`(캡 판정), `app/infrastructure/cache/token_cap.py:59-70`(tier→cap), `app/core/config.py:311-333`(CAP_TIER 기본값).

---

## 범위 밖 (사용자 선택: "내 계정만 지금 무제한")

아래는 이번에 손대지 않지만, 같은 문제가 재발하는 구조적 원인이므로 기록만 남긴다:
- tier 저장소 이원화: 앱=DB `user_profiles.tier`, Telegram=Redis `kiko:tier`. `.env.example:185`가
  `POST /debug/cap/tier`(Redis 전용)를 "등급 관리"로 안내해 오해를 유발한다.
- 앱 경로용 tier 설정 admin 엔드포인트가 없다(수동 DB UPDATE에 의존).
- `subscription_service.upsert_from_transaction`(`:121`)의 IAP sync가 수동 설정한 `developer`를
  구독 tier/`free`로 덮어쓸 수 있다.

🗿 MoAI <email@mo.ai.kr>
