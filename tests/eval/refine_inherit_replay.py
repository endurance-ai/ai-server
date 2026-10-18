"""refine 이 직전 검색 조건을 이어받는지 재생 측정 (2026-10-18).

dev 로그에서 search_products 바로 뒤에 온 refine_search 48건(9/1 이후)을 같은 순서로 다시
돌린다. 같은 chat_id 로 직전 search_products 인자 → refine_search 인자를 dispatch 에 넣고,
refine 결과 상위 15개 중 직전 검색 category 와 같은 품목군(category_canonical.family)인 상품의
비율을 센다. 직전 검색 category 가 품목군으로 해석되지 않으면(other) 비율 계산에서 뺀다.

사용 (dev-ai 컨테이너):
  CALLS=/tmp/refine_inherit_calls.json OUT=/tmp/refine_inherit_<tag>.json \
    PYTHONPATH=<코드> python /tmp/refine_inherit_replay.py
입력 refine_inherit_calls.json 은 같은 디렉터리의 _EXPORT_SQL 로 뽑았다(2026-10-18, 48건).
성별 프로필은 unisex 로 고정한다. RPC 는 배포된 PostgREST 를 그대로 쓴다.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time

_PP = os.environ.get("PYTHONPATH", "")
if _PP:
    sys.path[:] = [p for p in sys.path if p != "/app"]
    sys.path.insert(0, _PP)

# 실행마다 다른 chat_id 를 쓴다. 세션은 Postgres(ai.user_session)에 남아, 같은 id 로 두 모드를
# 돌리면 나중 실행이 먼저 실행의 직전 결과 · 노출 기록을 물려받는다(2026-10-18 첫 측정에서 발생).
CHAT_BASE = int(os.environ.get("CHAT_BASE") or (9_190_000_000 + (int(time.time()) % 1_000_000) * 100))

_EXPORT_SQL = """
select json_agg(t) from (
  select e.id, e.thread_id, e.created_at, e.payload->>'args_summary' args, e.payload->>'result_summary' res,
         prev.a prev_args
  from ai.log_conversation_event e
  join lateral (
    select p.payload->>'tool_name' tn, p.payload->>'args_summary' a from ai.log_conversation_event p
    where p.thread_id = e.thread_id and p.id < e.id and p.event_type = 'tool_call'
      and p.payload->>'tool_name' in ('search_products', 'refine_search')
    order by p.id desc limit 1) prev on prev.tn = 'search_products'
  where e.event_type = 'tool_call' and e.payload->>'tool_name' = 'refine_search' and e.created_at >= '2026-09-01'
  order by e.id) t
"""


def _clean(args: dict) -> dict:
    return {k: v for k, v in args.items() if not (isinstance(v, str) and re.fullmatch(r"(list|dict)\[\d+\]", v))}


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
    from app.infrastructure.memory.session import get_store
    from app.infrastructure.repositories.category_family import to_canonical_family
    from app.infrastructure.repositories.subcategory_vocab import normalize_subcategory

    def _want_family(cat: str | None) -> str | None:
        # search_service 게이트와 같은 해석: subcategory 로 잡히면 그 family, 아니면 canonical family.
        if not cat:
            return None
        _sub, fam = normalize_subcategory(cat)
        return fam or to_canonical_family(cat)

    print("code:", sp.__file__, flush=True)
    sp._lookup_profile_gender = lambda _ctx: "unisex"
    conn = await psycopg.AsyncConnection.connect(settings.DB_DSN, autocommit=True)

    calls = json.load(open(os.environ.get("CALLS", "/tmp/refine_inherit_calls.json")))
    out = []
    for i, c in enumerate(calls):
        prev = _clean(json.loads(c["prev_args"]))
        args = _clean(json.loads(c["args"]))
        chat_id = CHAT_BASE + i
        ctx = {"chat_id": chat_id, "user_key": f"replay:{chat_id}", "text_query": prev.get("text_query") or "x"}
        err = None
        try:
            await sp.dispatch(dict(prev), dict(ctx))
            t0 = time.perf_counter()
            res = await rs.dispatch(dict(args), dict(ctx, text_query="refine"))
            lat = int((time.perf_counter() - t0) * 1000)
        except Exception as exc:  # noqa: BLE001
            res, lat, err = {}, -1, f"{type(exc).__name__}:{exc}"[:160]
        ids = []
        for card in (get_store().get_or_create(chat_id).last_results or [])[:15]:
            d = card if isinstance(card, dict) else card.model_dump()
            v = d.get("id") or d.get("product_id")
            if v is not None and str(v).isdigit():
                ids.append(int(v))
        want = _want_family(prev.get("category"))
        fams = []
        if ids:
            cur = await conn.execute(
                "select p.id, coalesce(cc.family, 'other') from products p "
                "left join category_canonical cc on cc.raw_category = p.category where p.id = any(%s)",
                (ids,),
            )
            fam_by_id = dict(await cur.fetchall())
            fams = [fam_by_id.get(x, "other") for x in ids]
        row = {
            "id": c["id"],
            "action": args.get("action"),
            "prev_category": prev.get("category"),
            "want_family": want,
            "n": len(ids),
            "family_match": (sum(1 for f in fams if f == want) if want and want != "other" else None),
            "lat_ms": lat,
            "error": err or res.get("error"),
        }
        out.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    json.dump(out, open(os.environ.get("OUT", "/tmp/refine_inherit_out.json"), "w"), ensure_ascii=False)
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
