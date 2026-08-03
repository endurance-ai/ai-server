---
id: SPEC-CHAT-STATE-REDIS-001
acceptance_version: 0.1.0
spec_version: 0.1.0
plan_version: 0.1.0
created: 2026-05-20
status: planned
---

# Acceptance Mapping — SPEC-CHAT-STATE-REDIS-001

> SPEC §Definition of Done 의 P1 항목 + manual smoke 시나리오(로컬/prod)를 실제 테스트 파일과 운영 검증 절차에 매핑한다.
>
> **Acceptance HISTORY**:
> - 2026-05-20 (v0.1.0): 초안 — 4 REQ-row + 회귀 grep + manual smoke 2 시나리오 (로컬 / dev-ai prod).
>
> **Test layout**: 2 new files under `tests/test_infrastructure/cache/` + `tests/test_agents/tools/`. 모든 단위/통합 테스트는 `fakeredis` 기반 (real redis 컨테이너 불필요) — CI 즉시 실행 가능.

---

## 1. P1 Requirements → Test Files

| REQ | DoD bullet | Test file | Test case(s) | Status |
|---|---|---|---|---|
| REQ-CHAT-STATE-001 | `get_cursor` / `set_cursor` 가 `kiko:cursor:{chat_id}` (TTL 24h) read/write; `send_hybrid_batch` 2 지점 헬퍼 교체; 모듈 글로벌 `_CARD_BATCH_CURSOR` 제거 | `tests/test_infrastructure/cache/test_chat_state.py` + `tests/test_agents/tools/test_respond_redis_integration.py` | `test_set_cursor_then_get_returns_value`, `test_get_cursor_unset_returns_zero`, `test_set_cursor_sets_ttl_24h`, `test_send_hybrid_batch_advances_cursor_in_redis`, `test_cards_more_reads_cursor_from_redis` | planned |
| REQ-CHAT-STATE-002 | `is_logged` / `mark_logged` / `clear_logged` 가 `kiko:imp:{chat_id}` (SET, TTL 7d, 새 검색 시 DEL) 관리; `_log_delivered_impressions` 4 지점 헬퍼 교체; 모듈 글로벌 `_LOGGED_IMPRESSION_IDS` 제거; id-less candidate fresh.append 보존 | `tests/test_infrastructure/cache/test_chat_state.py` + `tests/test_agents/tools/test_respond_redis_integration.py` | `test_mark_logged_then_is_logged_true`, `test_clear_logged_drops_set`, `test_mark_logged_sets_ttl_7d`, `test_is_logged_empty_pid_returns_false`, `test_log_delivered_impressions_dedupes_within_chat`, `test_fresh_search_clears_dedupe`, `test_id_less_candidate_passes_through` | planned |
| REQ-CHAT-STATE-003 | 5 헬퍼 전부 try/except + DEBUG 1줄 + 안전 default; caller try/except 없음; lifespan warm 실패가 startup 차단 안 함 | `tests/test_infrastructure/cache/test_chat_state.py` + `tests/test_agents/tools/test_respond_redis_integration.py` | `test_get_cursor_fail_open_returns_zero`, `test_set_cursor_fail_open_swallow`, `test_is_logged_fail_open_returns_false`, `test_mark_logged_fail_open_swallow`, `test_clear_logged_fail_open_swallow`, `test_warm_pool_returns_false_on_unreachable`, `test_redis_down_does_not_block_card_delivery` | planned |
| REQ-CHAT-STATE-004 | `REDIS_URL` 단일 게이트; 로컬 `docker-compose.yml` redis 서비스; `fakeredis` dev dep; prod dev-ai redis DB 1 (`redis://:${REDIS_AUTH}@redis:6379/1`) | `tests/test_infrastructure/cache/test_chat_state.py` (fakeredis fixture) + manual smoke (로컬 + prod) | `test_warm_pool_returns_false_on_unreachable` + manual scenarios (a)(b) below | planned |

---

## 2. Definition of Done Manual Scenarios

SPEC §Definition of Done 의 manual smoke — 자동화 가능한 부분은 위 표에 포함됨. 나머지는 로컬 + dev-ai bot 실측 procedure.

### (a) 로컬 docker-compose smoke

- **Automated coverage**: `test_warm_pool_returns_false_on_unreachable` (lifespan fail-open) + 단위/통합 테스트 전체 (fakeredis).
- **Manual verification**:
  ```bash
  # 1. 로컬 docker-compose 기동
  cd /Users/hansangho/Desktop/kikoai/ai
  docker compose up -d

  # 2. redis 컨테이너 healthy 확인
  docker ps --filter "name=kiko-ai-redis" --format "{{.Names}} {{.Status}}"
  # 기대: kiko-ai-redis Up X seconds (healthy)

  # 3. redis PING
  docker exec kiko-ai-redis redis-cli ping
  # 기대: PONG

  # 4. ai-server startup log 확인
  docker logs $(docker ps -q --filter "name=ai-server") 2>&1 | grep -iE "redis pool|redis warm"
  # 기대: "redis pool warmed (url=redis://****@redis:6379/1)" 또는 동치 INFO 로그

  # 5. 봇 시나리오 (로컬 DEV 토큰):
  #    - Telegram 에서 봇에 사진 1장 전송
  #    - 추천 카드 5장 수신
  #    - "더보기" 인라인 버튼 탭
  #    - 다음 5장 수신 (첫 5장과 다른 product_id)

  # 6. redis 키 확인
  docker exec kiko-ai-redis redis-cli -n 1 KEYS 'kiko:*'
  # 기대: kiko:cursor:{chat_id}, kiko:imp:{chat_id} 두 키 등장

  docker exec kiko-ai-redis redis-cli -n 1 GET 'kiko:cursor:{chat_id}'
  # 기대: 10 (5장 × 2 batch)

  docker exec kiko-ai-redis redis-cli -n 1 SMEMBERS 'kiko:imp:{chat_id}'
  # 기대: 10개 distinct product_id

  docker exec kiko-ai-redis redis-cli -n 1 TTL 'kiko:cursor:{chat_id}'
  # 기대: <= 86400 양수

  docker exec kiko-ai-redis redis-cli -n 1 TTL 'kiko:imp:{chat_id}'
  # 기대: <= 604800 양수
  ```

### (b) dev-ai prod smoke

- **Automated coverage**: 단위/통합 테스트 (fakeredis) + 회귀 grep test.
- **Manual verification** (dev-ai prod, `@kiko_fashion_ai_bot` PROD 토큰):
  ```bash
  # 1. 배포 후 ai-server 컨테이너 startup log
  ssh -i ~/Desktop/aws-infra/kikoai-key.pem ec2-user@54.116.116.225 \
    'docker logs ai-server 2>&1 | tail -100 | grep -iE "redis pool|redis warm"'
  # 기대: "redis pool warmed (url=redis://****@redis:6379/1)"

  # 2. 환경변수 확인
  ssh -i ~/Desktop/aws-infra/kikoai-key.pem ec2-user@54.116.116.225 \
    'docker exec ai-server env | grep -i REDIS_URL'
  # 기대: REDIS_URL=redis://:****@redis:6379/1 (auth masked or visible to operator)

  # 3. PROD 봇 시나리오 (PROD 토큰):
  #    - Telegram @kiko_fashion_ai_bot 에 사진 1장 전송
  #    - 추천 카드 수신
  #    - "더보기" 탭 → 다음 batch
  #    - 새 검색 ("청바지") → 새 카드 set

  # 4. redis 키 확인 (DB 1)
  ssh -i ~/Desktop/aws-infra/kikoai-key.pem ec2-user@54.116.116.225 \
    'docker exec redis redis-cli -a $REDIS_AUTH -n 1 KEYS "kiko:*"'
  # 기대: 활성 chat 별 cursor + imp 키

  # 5. DB 0 (Langfuse) 와 분리 확인
  ssh -i ~/Desktop/aws-infra/kikoai-key.pem ec2-user@54.116.116.225 \
    'docker exec redis redis-cli -a $REDIS_AUTH -n 0 KEYS "kiko:*"'
  # 기대: (empty list or set) — kiko 키는 DB 0 에 없음

  # 6. ai.card_impression 중복 row 검증 (단일 worker 환경, 멀티 worker 미도입)
  ssh -i ~/Desktop/aws-infra/kikoai-key.pem ec2-user@54.116.116.225 \
    'PGPASSWORD=$DB_TOKEN psql -h 172.31.59.31 -U postgres -d kikoai -c \
      "SELECT chat_id, product_id, COUNT(*) FROM ai.card_impression \
       WHERE created_at > NOW() - INTERVAL '\''1 hour'\'' \
       GROUP BY 1,2 HAVING COUNT(*) > 1 LIMIT 20;"'
  # 기대: 0 rows (단일 worker + Redis dedupe 작동)

  # 7. 24h 후 fail-open 빈도 모니터링
  ssh -i ~/Desktop/aws-infra/kikoai-key.pem ec2-user@54.116.116.225 \
    'docker logs ai-server --since 24h 2>&1 | grep -c "fail-open"'
  # 기대: 0 또는 매우 낮은 숫자 (redis 안정성 신호)
  ```

### (c) 컨테이너 재시작 paging 보존

- **Automated coverage**: 통합 테스트 `test_cards_more_reads_cursor_from_redis` (Redis-backed 보존).
- **Manual verification**:
  ```bash
  # 1. 사용자: 사진 전송 → 카드 → "더보기" 1회 탭 (cursor=10 으로 set)
  # 2. ai-server 재시작
  ssh -i ~/Desktop/aws-infra/kikoai-key.pem ec2-user@54.116.116.225 \
    'docker restart ai-server'
  # 3. 사용자: 같은 chat 에서 "더보기" 다시 탭
  # 4. 기대: 첫 5장 다시 안 보이고 cursor=10 부터 batch 시작 (TTL 24h 안)
  #    (재시작 전 pre-SPEC 동작: cursor 사라져 처음부터 — 회귀)
  ```

---

## 3. AST / Static Checks

| Check | Tool | File | Test case |
|---|---|---|---|
| 모듈 글로벌 `_CARD_BATCH_CURSOR` literal 이 `app/**/*.py` 어디에도 없음 | source scan | `app/**/*.py` | `test_no_module_global_chat_state_dicts` (plan §2.3) |
| 모듈 글로벌 `_LOGGED_IMPRESSION_IDS` literal 이 `app/**/*.py` 어디에도 없음 | source scan | `app/**/*.py` | `test_no_module_global_chat_state_dicts` |
| `reset_card_batch_cursor_for_tests` 심볼이 `app/**/*.py` 와 `tests/**/*.py` 어디에도 없음 | source scan | `app/**/*.py` + `tests/**/*.py` | `test_no_module_global_chat_state_dicts` (대상 디렉터리 확장) |
| `app/agents/tools/respond.py` 안에서 `redis.` 직접 호출 없음 (모든 redis 접근은 `chat_state.*` 헬퍼 경유) | source scan | `app/agents/tools/respond.py` | (optional, plan §2.3 에 추가 가능) |
| `chat_state` 헬퍼 5개 모두 try/except 로 래핑됨 (raise 가 propagate 되지 않음) | source review | `app/infrastructure/cache/chat_state.py` | (review-only — 단위 테스트 5개가 사실상 강제) |

---

## 4. Final Gates (target)

| Gate | Command | Target Result |
|---|---|---|
| ruff check | `uv run ruff check .` | PASS |
| ruff format | `uv run ruff format --check .` | PASS |
| pytest new | `uv run pytest tests/test_infrastructure/cache/test_chat_state.py tests/test_agents/tools/test_respond_redis_integration.py -q` | All planned cases pass |
| pytest overall | `uv run pytest -q` | No regression vs pre-SPEC baseline |
| Module-global grep | `grep -rnE "_CARD_BATCH_CURSOR\|_LOGGED_IMPRESSION_IDS\|reset_card_batch_cursor_for_tests" app/ tests/` | 빈 결과 (or 본 SPEC 의 test_no_module_global... 단 1건) |
| Manual scenario (a) 로컬 | docker-compose smoke | PASS observation |
| Manual scenario (b) dev-ai | prod smoke + DB query 0 dup | PASS observation |
| Manual scenario (c) 재시작 | container restart + paging 보존 | PASS observation |

---

## 5. Plan Deviations (filled at completion)

_To be populated at SPEC completion. Expected examples:_

- (anticipated) `send_hybrid_batch` 의 cursor read/write 지점이 정확히 2 vs 더 많음 — A1 검증 결과 기록.
- (anticipated) `redis>=5.0` vs `redis>=5.2` (실제 사용 중인 lockfile 버전 따라 조정).
- (anticipated) `fakeredis.aioredis.FakeRedis` import path 가 fakeredis 최신 버전에서 변경되었다면(`fakeredis.aioredis` → `fakeredis.aioredis_redis_py` 등) 그에 맞춰 fixture 시그니처 조정.
- (anticipated) dev-ai redis 의 maxmemory-policy 가 `noeviction` 일 경우 fail-open 빈도가 예상보다 높을 수 있음 — 운영 ticket 분리 기록.
- (anticipated) Langfuse 의 DB 0 와 kiko 의 DB 1 분리 동작 검증 결과 (DB 1 점유 여부 — A10 확인).
- (anticipated) `app/agents/tools/respond.py` 의 `_log_delivered_impressions` 가 `int(chat_id)` 변환 외 다른 type-coercion 패턴 보유 시 헬퍼 caller 측 조정 기록.

---

## 6. Status: PLANNED

SPEC-CHAT-STATE-REDIS-001 v0.1.0 ready for Run phase. 2 new test files planned (~12 단위 + ~6 통합 ≈ 18 새 케이스). 새 env var 1개(`REDIS_URL`), 새 runtime dep 1개(`redis>=5.0`), 새 dev dep 1개(`fakeredis>=2.0`), 새 docker-compose 서비스 1개(`redis:7-alpine` 로컬), aws-infra 환경변수 1줄 추가. Application surface 변경: NEW 1 모듈(`chat_state.py`) + MODIFIED 3 파일(`respond.py`, `main.py`, `config.py`) — 5 함수 추가 / 2 module-global dict 제거 / 1 reset 함수 제거 / 6 호출 지점 헬퍼 교체.
