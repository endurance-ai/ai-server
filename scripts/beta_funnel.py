"""Per-user funnel against `ai.log_conversation_event`.

턴 단위 vision → search → card 송신 → card 클릭 펀널을 chat_id 별로 집계.
베타 1.0 측정용으로 만들었지만 cohort 하드코딩 없이 전체 풀을 보므로
이후 베타에도 그대로 재사용 가능.

Usage:
  DB_DSN=postgresql://... uv run python scripts/beta_funnel.py

Columns:
  turns   = 한 chat 의 distinct trace(=턴) 개수
  vis     = vision_done 이벤트가 있던 턴 수
  srch    = search_done 이벤트가 있던 턴 수
  card    = card_sent 이벤트가 있던 턴 수
  clk     = card_clicked 이벤트가 있던 턴 수
  card%   = card / turns  (검색이 카드 송신까지 도달한 비율)
  CTR%    = clk / card    (송신된 카드의 클릭률)
"""

import os
import sys

import psycopg

SQL = """
WITH turn AS (
  SELECT langfuse_trace AS t, chat_id,
         BOOL_OR(event_type = 'vision_done')   AS has_vision,
         BOOL_OR(event_type = 'search_done')   AS has_search,
         BOOL_OR(event_type = 'card_sent')     AS has_card,
         BOOL_OR(event_type = 'card_clicked')  AS has_click
  FROM ai.log_conversation_event
  WHERE langfuse_trace IS NOT NULL
  GROUP BY langfuse_trace, chat_id
)
SELECT chat_id,
       COUNT(*) AS turns,
       SUM(has_vision::int) AS vision,
       SUM(has_search::int) AS search,
       SUM(has_card::int)   AS cards,
       SUM(has_click::int)  AS clicks,
       ROUND(100.0*SUM(has_card::int)/NULLIF(COUNT(*),0),1)              AS card_rate,
       ROUND(100.0*SUM(has_click::int)/NULLIF(SUM(has_card::int),0),1)   AS ctr
FROM turn
GROUP BY chat_id
ORDER BY turns DESC;
"""


def main() -> int:
    dsn = os.environ.get("DB_DSN")
    if not dsn:
        print("ERROR: DB_DSN env 가 필요합니다 (postgresql://...).", file=sys.stderr)
        return 2

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(SQL)
        hdr = ["chat_id", "turns", "vis", "srch", "card", "clk", "card%", "CTR%"]
        print(" ".join(f"{h:>10}" for h in hdr))
        print(" ".join("-" * 10 for _ in hdr))
        for r in cur.fetchall():
            cells = [str(x if x is not None else 0) for x in r]
            print(" ".join(f"{c:>10}" for c in cells))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
