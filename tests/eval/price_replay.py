"""가격 조건 툴 호출 재생 (PR #321, 2026-10-18).

dev 로그(ai.log_conversation_event tool_call, 8/19 이후)에서 max_price/min_price 가 붙은
search_products / refine_search 호출을 그대로 dispatch 에 다시 넣는다.
  - refine 은 같은 스레드 직전 search_products 의 text_query 를 last_query 로 심는다. 직전 검색이
    없는 7건은 제외.
  - 성별은 프로필 조회를 unisex 로 고정(두 모드 동일).
  - RPC 는 두 모드 모두 직접 SQL 로 부른다(PostgREST 대신). old = public 함수(가격 키 제거),
    new = 이 PR 의 SQL 을 pg_temp 에 만든 함수. 공용 함수는 바꾸지 않는다.
사용 (dev-ai 컨테이너, 2026-10-18 PR #321 측정):
  python tests/eval/price_replay.py --make-sql > /tmp/tmpfuncs.sql   # 로컬 저장소에서 pg_temp 함수 SQL 생성
  # price_replay.py · price_replay_calls.json · tmpfuncs.sql 을 컨테이너 /tmp 로 복사한 뒤
  MODE=old PYTHONPATH=/app python /tmp/price_replay.py
  MODE=new PYTHONPATH=<브랜치 코드> python /tmp/price_replay.py
결과: /tmp/price_replay_<MODE>.json
입력 price_replay_calls.json 은 아래 _EXPORT_SQL 로 dev DB 에서 뽑았다(2026-10-18, 62건).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time

MODE = os.environ.get("MODE", "")
_PP = os.environ.get("PYTHONPATH", "")
if _PP:
    sys.path[:] = [p for p in sys.path if p != "/app"]
    sys.path.insert(0, _PP)

# 실행마다 다른 chat_id 를 쓴다. 세션은 Postgres(ai.user_session)에 남아, 같은 id 로 두 모드를
# 돌리면 나중 실행이 먼저 실행의 직전 결과 · 노출 기록을 물려받는다(2026-10-18 첫 측정에서 발생).
CHAT_BASE = int(os.environ.get("CHAT_BASE") or (9_180_000_000 + (int(time.time()) % 1_000_000) * 100))

_EXPORT_SQL = """
select json_agg(t) from (
  select e.id, e.thread_id, e.created_at, e.payload->>'tool_name' tool, e.payload->>'args_summary' args,
         e.payload->>'result_summary' res, prev.args prev_args
  from ai.log_conversation_event e
  left join lateral (
    select p.payload->>'args_summary' args from ai.log_conversation_event p
    where p.thread_id = e.thread_id and p.id < e.id and p.event_type = 'tool_call'
      and p.payload->>'tool_name' = 'search_products'
    order by p.id desc limit 1) prev on true
  where e.event_type = 'tool_call' and e.payload->>'tool_name' in ('search_products', 'refine_search')
    and e.payload->>'args_summary' ~ '"(max|min)_price"' and e.created_at > timestamp '2026-08-19'
  order by e.id) t
"""


def _make_sql() -> str:
    """sql/functions 의 두 RPC 를 pg_temp.new_<이름> 으로 바꾼 SQL (공용 함수는 건드리지 않는다)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    out = []
    for name in ("search_products_hybrid_v1", "search_products_v6"):
        sql = (root / "sql" / "functions" / f"{name}.sql").read_text()
        m = re.search(rf"CREATE OR REPLACE FUNCTION public\.{name}.*?\$function\$;", sql, re.S)
        out.append(m.group(0).replace(f"public.{name}", f"pg_temp.new_{name}"))
    return "\n\n".join(out) + "\n"


def _clean(args: dict) -> dict:
    out = {}
    for k, v in args.items():
        if isinstance(v, str) and re.fullmatch(r"(list|dict)\[\d+\]", v):
            continue  # args_summary 가 잘라낸 값
        out[k] = v
    return out


async def main():
    import psycopg

    from app.core.config import settings
    from app.providers import db_pool

    await db_pool.init_pool(settings.DB_DSN)
    from app.infrastructure.cache import chat_state
    from app.infrastructure.repositories.brand_embedding_cache import warm_cache as w3
    from app.infrastructure.repositories.brand_node_cache import warm_cache as w2
    from app.infrastructure.repositories.style_node import warm_cache as w1

    await chat_state.warm_pool()
    await w1()
    await w2()
    await w3()

    import app.agents.tools.refine_search as rs
    import app.agents.tools.search_products as sp
    from app.agents.last_query import set_last_query
    from app.providers.database import DatabaseProvider

    print("code:", sp.__file__, flush=True)
    conn = await psycopg.AsyncConnection.connect(settings.DB_DSN, autocommit=True)
    if MODE == "new":
        await conn.execute(open("/tmp/tmpfuncs.sql").read())
    lock = asyncio.Lock()
    rpc_ms: list[int] = []

    async def rpc(fn_name, params):
        p = dict(params)
        if MODE == "old":
            p.pop("p_min_price", None)
            p.pop("p_max_price", None)
            target = f"public.{fn_name}"
        else:
            target = f"pg_temp.new_{fn_name}"
        emb = p.pop("query_embedding")
        named = ", ".join(f"{k} => %({k})s" for k in p)
        sql = f"SELECT * FROM {target}(%(query_embedding)s::halfvec{', ' + named if named else ''})"
        p["query_embedding"] = emb
        async with lock:
            t0 = time.perf_counter()
            cur = await conn.execute(sql, p)
            cols = [d.name for d in cur.description]
            rows = [dict(zip(cols, r)) for r in await cur.fetchall()]
            rpc_ms.append(int((time.perf_counter() - t0) * 1000))
        return rows

    DatabaseProvider.rpc = staticmethod(rpc)
    sp._lookup_profile_gender = lambda _ctx: "unisex"

    calls = json.load(open(os.environ.get("CALLS", "/tmp/price_replay_calls.json")))
    out = []
    for i, c in enumerate(calls):
        args = _clean(json.loads(c["args"]))
        if c["tool"] == "refine_search" and not c["prev_args"]:
            continue
        chat_id = CHAT_BASE + i
        ctx = {"chat_id": chat_id, "user_key": f"replay:{chat_id}", "text_query": args.get("text_query") or "x"}
        if c["tool"] == "refine_search":
            prev = _clean(json.loads(c["prev_args"]))
            set_last_query(chat_id, prev.get("text_query") or "fashion")
            ctx["text_query"] = prev.get("text_query") or "x"
        lo = args.get("min_price")
        hi = args.get("max_price")
        n0 = len(rpc_ms)
        t0 = time.perf_counter()
        err = None
        try:
            fn = sp.dispatch if c["tool"] == "search_products" else rs.dispatch
            res = await fn(args, ctx)
        except Exception as exc:  # noqa: BLE001
            res, err = {}, f"{type(exc).__name__}:{exc}"[:160]
        lat = int((time.perf_counter() - t0) * 1000)
        tops = res.get("top_candidates") or []
        bad = 0
        for t in tops:
            pr = t.get("price")
            if pr is None:
                continue
            if (hi is not None and float(pr) > float(hi)) or (lo is not None and float(pr) < float(lo)):
                bad += 1
        row = {
            "id": c["id"],
            "tool": c["tool"],
            "args": args,
            "logged": c["res"],
            "count": res.get("candidates_count"),
            "error": err or res.get("error"),
            "lat_ms": lat,
            "rpc_ms": rpc_ms[n0:],
            "out_of_bounds_top": bad,
        }
        out.append(row)
        print(json.dumps(row, ensure_ascii=False)[:300], flush=True)
    json.dump(out, open(f"/tmp/price_replay_{MODE}.json", "w"), ensure_ascii=False)
    await conn.close()


if __name__ == "__main__":
    if "--make-sql" in sys.argv:
        print(_make_sql(), end="")
    else:
        asyncio.run(main())
