# Phase 1 — Social Login & Consumer Chat API

## Overview

텔레그램 Webhook 기반 봇에서 자체 iOS 앱으로 전환하기 위한 서버 사이드 구현.
소셜 로그인 → JWT 인증 → 채팅 REST API까지 전 범위를 포함한다.

---

## Changes

### 1. DB Migration (`migrations/versions/0009_add_user_auth_and_chat.py`)

신규 테이블 4개 (`ai` 스키마):

| 테이블 | 역할 |
|---|---|
| `ai.user_profiles` | 소셜 로그인 유저 정체성 (provider, email, tier, gender 등) |
| `ai.refresh_tokens` | Refresh token hash 저장 (revocation 지원) |
| `ai.chat_sessions` | 유저별 대화 세션 |
| `ai.chat_messages` | 세션별 메시지 (role: user/assistant, product_refs JSONB) |

기존 테이블 변경:
- `ai.user_session`, `ai.user_taste_profile` → `user_id UUID FK` 컬럼 추가 (nullable, 점진 마이그레이션)

`user_profiles` 주요 필드:
```sql
user_id       UUID PRIMARY KEY
provider      TEXT NOT NULL        -- 'google' | 'apple'
provider_id   TEXT NOT NULL        -- id_token sub claim
email         TEXT
display_name  TEXT
gender        TEXT                 -- 'male' | 'female' | 'other' | NULL
tier          TEXT DEFAULT 'free'  -- 'free' | 'basic' | 'pro' | 'premium'
tier_expires_at TIMESTAMPTZ
UNIQUE (provider, provider_id)
```

### 2. Social Auth (`app/core/social_auth/`)

- **`google.py`**: `google-auth` 라이브러리로 Google ID token 검증 (`verify_oauth2_token`)
- **`apple.py`**: Apple JWKS 조회 + `python-jose` RS256 검증

### 3. JWT (`app/core/jwt.py`)

- HS256, `JWT_SECRET` env var
- Access token: 1시간, payload `{sub: user_id, type: "access"}`
- Refresh token: 30일, raw 토큰 → SHA-256 hash → DB 저장 (revocation 지원)

### 4. Auth API (`app/api/auth.py`)

```
POST /auth/social    — id_token 검증 → user upsert → JWT pair 발급
POST /auth/refresh   — refresh_token → 새 access_token
POST /auth/revoke    — refresh_token revoke (204)
```

### 5. Chat Service (`app/services/chat_service.py`)

**`CaptureAdapter`** 패턴: 기존 LangGraph 그래프를 그대로 재사용하되, Telegram으로 보내는 대신 in-process에서 캡처.

- `send_text` → `self._texts` 수집
- `send_card` → `self._cards` 수집, `0` 반환 (non-None = 성공 신호)
- `get_reply()` → `BotReply(text, cards, closing_text)`

**유저 ID 브릿지**: `user_id (UUID)` → `chat_id (int)` 변환 (기존 SessionStore 호환)
```python
abs(int.from_bytes(user_id.bytes[:8], 'big')) % (2**62)
```

**Gender sync**: REST API 유저는 Telegram 버튼 플로우 없이 `user_profiles.gender`에서 읽어 taste profile에 미리 세팅. 미설정 시 기본값 `unisex`.

### 6. Chat API (`app/api/chat.py`)

```
POST /chat/sessions                       — 새 세션 + 첫 메시지
POST /chat/sessions/{session_id}/messages — 기존 세션에 메시지 추가
GET  /chat/sessions                       — 유저의 세션 목록
GET  /chat/sessions/{session_id}/messages — 세션 메시지 이력 (cursor pagination)
```

### 7. Bug Fixes

| 버그 | 원인 | 수정 |
|---|---|---|
| `products: []` | `CaptureAdapter.send_card` → `None` 반환, `send_results`/`respond`가 실패로 처리 | `0` 반환으로 변경 |
| `products: []` | gender 미설정 유저 → `awaiting_gender` 블로킹 | `_sync_gender_to_taste_profile` 추가, 미설정 시 unisex |
| 500 Internal Server Error | `product_refs: list[dict]` → psycopg3 JSONB 변환 불가 | `Jsonb(product_refs)` 래퍼 적용 |

### 8. New Dependencies (`pyproject.toml`)

```
google-auth>=2.29
python-jose[cryptography]>=3.3
```

### 9. Environment Variables (`.env`)

```
GOOGLE_CLIENT_ID=<iOS OAuth client ID>
APPLE_CLIENT_ID=com.kikoai.app
JWT_SECRET=<32+ chars random string>
ACCESS_TOKEN_EXPIRE_MINUTES=60
REFRESH_TOKEN_EXPIRE_DAYS=30
```

---

## Branch

`feat/social-login-user-identity`

## Telegram 호환성

기존 Telegram Webhook 흐름은 변경 없이 유지. 두 채널이 동일한 LangGraph 그래프를 공유한다.
