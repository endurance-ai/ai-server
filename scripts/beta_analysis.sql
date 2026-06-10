-- 베타 4목표 분석 SQL 모음 (1개월 베타 기간 가정).
--
-- 컨벤션:
--   - rec_id ≡ ai.log_conversation_event.langfuse_trace (베타 한정 alias).
--     상세는 docs/features/observability.md 참조.
--   - 분석 단위 session ≡ thread_id (webhook 진입 시 신규 발급, 카드 콜백 시
--     30일 윈도우로 재사용).
--   - user-turn ≡ webhook 진입 1회 (= user_text / user_photo / user_callback
--     이벤트 1행).
--   - 목표 1 만족도 SQL 은 `card_feedback` 이벤트가 카탈로그에 추가된 후 활성화.
--     현재는 placeholder 로 남겨둠 (기획 확정 후 PR 별도).
--
-- 실행 환경: dev-app Postgres (read replica 권장, prod 직접 조회 가능).
-- 모든 쿼리는 read-only.

-- ===========================================================================
-- 목표 4 — 캡 도달률 + status 분포 (turn_summary 1행/턴)
-- ===========================================================================
-- "추천 1턴이 어떻게 끝났는가" 분포. responded 가 다수여야 정상.
-- stuck 비율이 높으면 ReAct iter cap 또는 도구 신뢰성 문제.
-- error 는 webhook 처리 중 예외 — 0 에 수렴해야 함.
SELECT
    payload->>'status' AS status,
    COUNT(*) AS turns,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct
FROM ai.log_conversation_event
WHERE event_type = 'turn_summary'
  AND created_at >= NOW() - INTERVAL '30 days'
GROUP BY 1
ORDER BY 2 DESC;


-- 목표 4 (이어서) — ReAct iter 분포 (responded 만)
-- 평균 iter 가 4 이상이면 도구 호출 과다 = 비용/latency 압박.
-- 분위수 p50/p90/p99 로 long tail 식별.
SELECT
    AVG((payload->>'iter_count')::int) AS avg_iter,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY (payload->>'iter_count')::int) AS p50,
    percentile_cont(0.9) WITHIN GROUP (ORDER BY (payload->>'iter_count')::int) AS p90,
    percentile_cont(0.99) WITHIN GROUP (ORDER BY (payload->>'iter_count')::int) AS p99
FROM ai.log_conversation_event
WHERE event_type = 'turn_summary'
  AND payload->>'status' = 'responded'
  AND created_at >= NOW() - INTERVAL '30 days';


-- 목표 4 (이어서) — thread 당 user-turn 수 분포 (15턴 캡 검증)
-- "15턴이 현실에서 적절한가" 의 핵심 분포. p90 가 10 미만이면 15 캡은 사실상
-- 비활성. p90 가 15 초과면 캡 도달이 잦으므로 상향 검토.
WITH per_thread AS (
    SELECT thread_id, COUNT(*) AS user_turns
    FROM ai.log_conversation_event
    WHERE event_type IN ('user_text', 'user_photo', 'user_callback')
      AND created_at >= NOW() - INTERVAL '30 days'
    GROUP BY thread_id
)
SELECT
    COUNT(*) AS thread_count,
    AVG(user_turns) AS avg_turns,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY user_turns) AS p50,
    percentile_cont(0.9) WITHIN GROUP (ORDER BY user_turns) AS p90,
    percentile_cont(0.99) WITHIN GROUP (ORDER BY user_turns) AS p99,
    MAX(user_turns) AS max_turns,
    SUM(CASE WHEN user_turns >= 15 THEN 1 ELSE 0 END) AS over_15_count,
    ROUND(100.0 * SUM(CASE WHEN user_turns >= 15 THEN 1 ELSE 0 END) / COUNT(*), 2) AS over_15_pct
FROM per_thread;


-- ===========================================================================
-- 목표 2 — 펀널: thread 별 이벤트 시퀀스 + drop 지점
-- ===========================================================================
-- 단계별 도달률. 어디서 drop 이 큰지 = 막힘 후보 지점.
-- intent_routed → vision_done → search_done → diversify_done → card_sent
-- 순으로 줄어드는 게 정상; 특정 단계가 절반 이하로 떨어지면 거기 문제.
WITH stage_counts AS (
    SELECT
        thread_id,
        BOOL_OR(event_type = 'user_text' OR event_type = 'user_photo') AS reached_intake,
        BOOL_OR(event_type = 'intent_routed') AS reached_intent,
        BOOL_OR(event_type = 'vision_done') AS reached_vision,
        BOOL_OR(event_type = 'search_done') AS reached_search,
        BOOL_OR(event_type = 'diversify_done') AS reached_diversify,
        BOOL_OR(event_type = 'card_sent') AS reached_cards,
        BOOL_OR(event_type = 'card_clicked') AS reached_click
    FROM ai.log_conversation_event
    WHERE created_at >= NOW() - INTERVAL '30 days'
    GROUP BY thread_id
)
SELECT
    SUM(reached_intake::int) AS s1_intake,
    SUM(reached_intent::int) AS s2_intent,
    SUM(reached_vision::int) AS s3_vision,
    SUM(reached_search::int) AS s4_search,
    SUM(reached_diversify::int) AS s5_diversify,
    SUM(reached_cards::int) AS s6_cards,
    SUM(reached_click::int) AS s7_click,
    ROUND(100.0 * SUM(reached_click::int) / NULLIF(SUM(reached_cards::int), 0), 2) AS ctr_pct
FROM stage_counts;


-- 목표 2 (이어서) — 첫 입력 타입 분포 (image vs text vs URL)
-- 유저가 카탈로그를 어떻게 시작하는지. 이미지 위주면 Vision 신뢰성이 핵심,
-- 텍스트 위주면 enhance_query / search_products 가 핵심.
WITH first_event_per_thread AS (
    SELECT DISTINCT ON (thread_id)
        thread_id,
        event_type,
        created_at
    FROM ai.log_conversation_event
    WHERE event_type IN ('user_text', 'user_photo', 'user_callback')
      AND created_at >= NOW() - INTERVAL '30 days'
    ORDER BY thread_id, created_at ASC
)
SELECT
    event_type AS first_input_type,
    COUNT(*) AS threads,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct
FROM first_event_per_thread
GROUP BY 1
ORDER BY 2 DESC;


-- ===========================================================================
-- 목표 3 — CTR (목표 1 만족도와 함께 추천 품질 보조 지표)
-- ===========================================================================
-- 노출 대비 클릭 비율. day 별 추세로 추천 알고리즘 변화 효과 추적.
-- card_clicked 는 redirect proxy 가 emit (rec_id = card_impression.langfuse_trace).
WITH impressions AS (
    SELECT
        DATE_TRUNC('day', shown_at) AS day,
        COUNT(*) AS imp_count
    FROM ai.card_impression
    WHERE shown_at >= NOW() - INTERVAL '30 days'
    GROUP BY 1
),
clicks AS (
    SELECT
        DATE_TRUNC('day', created_at) AS day,
        COUNT(*) AS click_count
    FROM ai.log_conversation_event
    WHERE event_type = 'card_clicked'
      AND created_at >= NOW() - INTERVAL '30 days'
    GROUP BY 1
)
SELECT
    i.day::date AS day,
    i.imp_count,
    COALESCE(c.click_count, 0) AS click_count,
    ROUND(100.0 * COALESCE(c.click_count, 0) / NULLIF(i.imp_count, 0), 2) AS ctr_pct
FROM impressions i
LEFT JOIN clicks c USING (day)
ORDER BY day DESC;


-- 목표 3 (이어서) — rec_id 단위 CTR (어떤 추천 batch 가 클릭 잘 받았나)
-- 상위/하위 batch 의 쿼리/카테고리 비교로 추천 알고리즘 약점 파악.
WITH per_rec AS (
    SELECT
        ci.langfuse_trace AS rec_id,
        COUNT(*) AS card_count,
        COUNT(*) FILTER (WHERE ci.click_status = 'clicked') AS clicked_count
    FROM ai.card_impression ci
    WHERE ci.shown_at >= NOW() - INTERVAL '30 days'
      AND ci.langfuse_trace IS NOT NULL
    GROUP BY 1
)
SELECT
    rec_id,
    card_count,
    clicked_count,
    ROUND(100.0 * clicked_count / NULLIF(card_count, 0), 2) AS rec_ctr_pct
FROM per_rec
ORDER BY rec_ctr_pct DESC NULLS LAST
LIMIT 50;


-- ===========================================================================
-- 목표 1 — 만족도 (PLACEHOLDER, 기획 후 활성화)
-- ===========================================================================
-- 👍/👎 batch 단위 voting (`card_feedback` 이벤트) 카탈로그 추가 후 활성화.
-- 임시로 빈 결과 반환. PR 별도.
--
-- SELECT
--     payload->>'value' AS value,
--     COUNT(*) AS votes,
--     ROUND(100.0 * COUNT(*) FILTER (WHERE payload->>'value' = 'up')
--           / NULLIF(COUNT(*), 0), 2) AS satisfaction_pct
-- FROM ai.log_conversation_event
-- WHERE event_type = 'card_feedback'
--   AND created_at >= NOW() - INTERVAL '30 days'
-- GROUP BY 1;
SELECT 'card_feedback event not yet implemented — see ROADMAP' AS note;


-- ===========================================================================
-- 보조 — 일자별 활성 유저 + thread 볼륨 (운영 sanity check)
-- ===========================================================================
SELECT
    DATE_TRUNC('day', created_at)::date AS day,
    COUNT(DISTINCT user_key) AS dau,
    COUNT(DISTINCT thread_id) AS threads,
    COUNT(*) AS events
FROM ai.log_conversation_event
WHERE created_at >= NOW() - INTERVAL '30 days'
GROUP BY 1
ORDER BY 1 DESC;
