---
name: kiko-ops-trace
description: 운영 데이터로 에이전트 동작·버그 빈도를 확인할 때 사용. 한 턴 재구성, 툴 호출 빈도, 0건 검색, Reflexion 경로, 턴 종료 사유, Langfuse 트레이스 연결. "로그 봐봐", "몇 번 일어났어", "실제로 그래?", "세션 뜯어봐", 버그 주장·수정 전 빈도 확인.
---

# KIKO 운영 추적 쿼리

규칙: 버그를 주장하거나 고치기 전에 여기서 빈도를 먼저 센다(`.claude/rules/kiko/evidence-and-eval.md` 3절).

## 접속

- DB: dev-app EC2 → `docker exec db psql -U postgres -d kikoai`
- 서버 로그: dev-ai EC2 → `docker logs ai-server --since 1h`
- Langfuse: dev-ai의 `langfuse-web`(3000 포트). 이벤트의 `langfuse_trace` 컬럼으로 트레이스를 찾는다.

## 데이터 구조

`ai.log_conversation_event` (SPEC-CONVERSATION-LOG-001): `thread_id`, `turn_no`, `event_type`, `payload`(jsonb), `langfuse_trace`, `latency_ms`, `created_at`, `user_id`

| event_type | payload 주요 키 |
|---|---|
| `tool_call` | `tool_name`, `iteration_no`, `latency_ms`, `error`, `args_summary`, `result_summary` |
| `turn_summary` | `tool_sequence`, `iter_count`, `total_tokens`, `cost_usd`, `status`, `exit_reason` |
| `search_done` | `query`, `filters`, `is_refine`, `dense_count`, `top_k_product_ids` |
| `evaluator_run` | `score`, `retry_decision`, `iteration_no` |
| `card_sent` | `product_id`, `position`, `send_ok` |
| `intent_routed` | `intent` |

그 밖의 테이블: `ai.chat_messages`(대화 원문), `ai.card_impression`, `ai.search_outcomes`(검색↔아웃컴↔trace), `ai.user_taste_profile`.

## 쿼리

### 한 턴 재구성

```sql
select turn_no, event_type, latency_ms, left(payload::text, 300), langfuse_trace
from ai.log_conversation_event
where thread_id = '<thread_uuid>'
order by id;
```

### 툴별 호출 수·평균 지연·오류 (죽은 툴 찾기)

```sql
select payload->>'tool_name' tool, count(*),
       round(avg((payload->>'latency_ms')::int)) avg_ms,
       count(*) filter (where coalesce(payload->>'error','') <> '') errors
from ai.log_conversation_event
where event_type = 'tool_call' and created_at > now() - interval '14 days'
group by 1 order by 2 desc;
```

레지스트리 8개 툴 중 목록에 없는 툴이 기간 내 0회다. (#260: 14일 0회 툴 2개를 찾아 1개 제거, 1개 배선)

### 0건 검색 비율

```sql
select payload->>'tool_name', count(*) total,
       count(*) filter (where payload->>'result_summary' ~ '"candidates_count": 0[,}]') zero
from ai.log_conversation_event
where event_type = 'tool_call'
  and payload->>'tool_name' in ('search_products', 'refine_search')
  and created_at > now() - interval '30 days'
group by 1;
```

### Reflexion 경로 (fast-path vs LLM 채점)

```sql
select case when payload->>'retry_decision' like 'empty results%' then 'fastpath' else 'llm' end path,
       count(*), count(*) filter (where (payload->>'score')::float >= 0.6) ge_06
from ai.log_conversation_event
where event_type = 'evaluator_run'
group by 1;
```

#295 이후 0건 검색은 전부 fastpath여야 한다. `llm`이 새로 생기면 회귀다.

### 턴 종료 사유

```sql
select payload->>'status', payload->>'exit_reason', count(*)
from ai.log_conversation_event
where event_type = 'turn_summary' and created_at > now() - interval '14 days'
group by 1, 2 order by 3 desc;
```

`exhausted`가 늘면 iteration cap·deadline 소진이다.

## 해석 주의

- 팀 계정·테스트 세션을 먼저 제외한다. 실사용자 수가 작아(9월 17명) 테스트 몇 건이 비율을 크게 바꾼다.
- `result_summary`, `args_summary`는 잘린 텍스트다. 정확한 값이 필요하면 Langfuse 트레이스를 연다.
- 앰플리튜드 날짜는 US 리전 기준이라 KST와 하루 어긋날 수 있다.
- 결론을 문서에 쓸 때는 기간, 표본 수, 제외 조건을 함께 적는다.
