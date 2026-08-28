# Plan: GET /v1/history — session_id 선택적으로 변경

## Context

`GET /v1/history` (`app/api/history.py`)는 `session_id`를 필수 파라미터로 받아
해당 세션 내 result_set + product 이력만 반환한다.
세션 전환 시 새 세션에 데이터가 없으므로 히스토리 탭이 비어 보이는 문제.

목표: `session_id`를 선택적으로 바꿔, 미전달 시 해당 user_id의 전체 세션 이력 반환.

---

## 변경 파일

### `app/api/history.py` — 단일 파일 수정

**변경 1: session_id optional화 (line 72)**
```python
# Before
session_id: UUID = Query(..., description="세션 ID (단일 세션 한정)"),

# After
session_id: UUID | None = Query(default=None, description="세션 ID (미전달 시 전체 세션)"),
```

**변경 2: result_set 쿼리에서 session_id 조건 분기 (lines 93-100)**
```python
if include_rs:
    if session_id is not None:
        union_parts.append(
            """
            SELECT 'result_set' AS kind, s.created_at AS occurred_at, s.search_id::text AS ident
            FROM ai.searches s
            WHERE s.session_id = %s AND s.user_id = %s AND s.is_listed = TRUE
            """
        )
        params += [session_id, user_id]
    else:
        union_parts.append(
            """
            SELECT 'result_set' AS kind, s.created_at AS occurred_at, s.search_id::text AS ident
            FROM ai.searches s
            WHERE s.user_id = %s AND s.is_listed = TRUE
            """
        )
        params += [user_id]
```

**변경 3: product_views 쿼리에서 session_id 조건 분기 (lines 101-109)**
```python
if include_pv:
    if session_id is not None:
        union_parts.append(
            """
            SELECT 'product' AS kind, pv.viewed_at AS occurred_at, pv.view_id::text AS ident
            FROM ai.product_views pv
            WHERE pv.session_id = %s AND pv.user_id = %s
            """
        )
        params += [session_id, user_id]
    else:
        union_parts.append(
            """
            SELECT 'product' AS kind, pv.viewed_at AS occurred_at, pv.view_id::text AS ident
            FROM ai.product_views pv
            WHERE pv.user_id = %s
            """
        )
        params += [user_id]
```

---

## 인덱스 검토

- `ai.searches`에 `user_id` 기반 인덱스가 없다면 추가 필요 (검색 시 확인)
- `ai.product_views.idx_product_views_user_product ON (user_id, product_id, viewed_at DESC)` — 이미 존재, 활용 가능
- `ai.product_views.idx_product_views_user_session ON (user_id, session_id, viewed_at DESC)` — 세션 없는 쿼리에선 user_id만 쓰므로 위 인덱스 사용

---

## 기존 동작 유지

`session_id` 전달 시 동일하게 동작 (하위 호환).

---

## 검증

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest tests/ -v -k "history"
```

수동:
1. 세션 A에서 상품 방문 + 검색 → 세션 B로 전환 → `GET /v1/history` (session_id 없음) → 세션 A 항목 포함 확인
2. `GET /v1/history?session_id=<A>` → 세션 A 항목만 확인 (기존 동작)
