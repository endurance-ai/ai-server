# iOS Integration Guide — kiko AI Server

Base URL: `https://ai.kikoai.me` (production) / `http://localhost:8000` (local)

---

## Auth Flow

### 1. Google Sign In

```
iOS Google SDK → id_token
↓
POST /auth/social
↓
{ access_token, refresh_token, user_id }
```

### 2. Apple Sign In

```
iOS Apple SDK → id_token (identity token)
↓
POST /auth/social
↓
{ access_token, refresh_token, user_id }
```

---

## Endpoints

### POST /auth/social

소셜 로그인. provider가 처음이면 신규 유저 생성, 이미 있으면 기존 유저 반환 (upsert).

**Request**
```json
{
  "provider": "google",   // "google" | "apple"
  "id_token": "<SDK에서 받은 id_token>"
}
```

**Response 200**
```json
{
  "access_token": "eyJ...",
  "refresh_token": "abc123...",
  "token_type": "bearer",
  "user_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

**Errors**
- `401` — id_token 검증 실패 (만료, 잘못된 audience 등)
- `400` — 지원하지 않는 provider

---

### POST /auth/refresh

Access token 갱신. access_token 만료 시 호출.

**Request**
```json
{
  "refresh_token": "abc123..."
}
```

**Response 200**
```json
{
  "access_token": "eyJ..."
}
```

**Errors**
- `401` — refresh_token 무효 또는 revoke됨

---

### POST /auth/revoke

로그아웃. refresh_token을 무효화한다.

**Request**
```json
{
  "refresh_token": "abc123..."
}
```

**Response** `204 No Content`

---

### POST /chat/sessions

새 채팅 세션 시작 + 첫 메시지 전송. AI 응답과 추천 상품을 반환한다.

**Headers**
```
Authorization: Bearer <access_token>
Content-Type: application/json
```

**Request**
```json
{
  "message": "핏한 니트 추천해줘"
}
```

**Response 200**
```json
{
  "session_id": "04800c23-c0e9-459c-a247-2f50c093f06a",
  "reply_text": "✨ 마음에 들 만한 5개 추려봤어",
  "products": [
    {
      "image_url": "https://cdn.example.com/product.jpg",
      "caption": "<b>ZARA</b>\n💰 ₩89,000\nSlim fit ribbed knit"
    }
  ]
}
```

`products`는 검색 결과가 있을 때만 채워진다. AI가 질문을 하는 경우 빈 배열이 올 수 있다.

---

### POST /chat/sessions/{session_id}/messages

기존 세션에 메시지 추가.

**Headers**
```
Authorization: Bearer <access_token>
```

**Request**
```json
{
  "message": "더 저렴한 걸로"
}
```

**Response 200** — `POST /chat/sessions`와 동일 스키마

**Errors**
- `404` — session_id가 없거나 다른 유저 소유

---

### GET /chat/sessions

유저의 세션 목록 (최신순 50개).

**Headers**
```
Authorization: Bearer <access_token>
```

**Response 200**
```json
[
  {
    "session_id": "04800c23-...",
    "title": "핏한 니트 추천해줘",
    "last_message_at": "2026-06-23T12:15:00+00:00"
  }
]
```

---

### GET /chat/sessions/{session_id}/messages

세션 메시지 이력. cursor 기반 페이지네이션.

**Query params**
- `cursor` (optional) — 이전 응답의 `next_cursor` 값
- `limit` (optional, default 20, max 100)

**Response 200**
```json
{
  "messages": [
    {
      "message_id": "uuid",
      "role": "user",
      "content": "핏한 니트 추천해줘",
      "product_refs": null,
      "created_at": "2026-06-23T12:15:00+00:00"
    },
    {
      "message_id": "uuid",
      "role": "assistant",
      "content": "✨ 마음에 들 만한 5개 추려봤어",
      "product_refs": [
        {
          "image_url": "https://cdn.example.com/product.jpg",
          "caption": "<b>ZARA</b>\n💰 ₩89,000\nSlim fit ribbed knit"
        }
      ],
      "created_at": "2026-06-23T12:15:03+00:00"
    }
  ],
  "next_cursor": "uuid-of-last-message"
}
```

`next_cursor`가 `null`이면 마지막 페이지.

---

## Token Management

| 토큰 | 유효기간 | 저장소 |
|---|---|---|
| access_token | 1시간 | Keychain (메모리도 무방) |
| refresh_token | 30일 | Keychain (secure storage 필수) |

**갱신 전략 (권장)**
1. API 호출 시 `401` 수신
2. `POST /auth/refresh`로 새 access_token 발급
3. 원래 요청 재시도
4. refresh_token도 `401`이면 → 로그아웃 후 재로그인

---

## Onboarding 권장 플로우

```
1. 소셜 로그인 (Google/Apple)
2. 성별 선택 UI → PATCH /users/me (미구현, 추후 추가 예정)
   └ 미설정 시 서버가 자동으로 unisex 검색
3. 첫 채팅 진입
```

현재 `PATCH /users/me`는 미구현. 성별 미설정 유저는 자동으로 unisex 검색이 적용된다.

---

## product caption 렌더링

`caption` 필드는 HTML 태그를 포함할 수 있다 (`<b>`, `<a href>`).
iOS에서는 `NSAttributedString`으로 파싱하거나 `WKWebView`로 렌더링 권장.

```swift
// 예시: HTML → NSAttributedString
let data = caption.data(using: .utf8)!
let attrStr = try NSAttributedString(
    data: data,
    options: [.documentType: NSAttributedString.DocumentType.html],
    documentAttributes: nil
)
```

---

## Error Codes

| HTTP | 의미 |
|---|---|
| `400` | 잘못된 요청 (provider 불지원 등) |
| `401` | 인증 실패 (토큰 만료/무효) |
| `403` | Authorization 헤더 없음 |
| `404` | 리소스 없음 (세션 미존재, 타인 소유) |
| `422` | 입력값 유효성 실패 (빈 메시지 등) |
| `500` | 서버 내부 오류 |

에러 응답 형식:
```json
{ "detail": "에러 설명" }
```
