"""큐레이션 구좌 시드 — ai.curation_sections 직접 적재.

어드민 페이지가 나오기 전까지 구좌 메타데이터의 소스다. 어드민이 생기면 이
스크립트는 초기 데이터 부트스트랩 용도로만 남는다.

`curation_refresh_loop` 는 auto 구좌(popular / trending-search / under-100)의
product_ids 만 UPDATE 하고 행을 만들거나 지우지 않으므로, 여기서 넣은 구좌
메타데이터는 리프레셔에 덮이지 않는다.

사용:
    # 실제 DB 쓰기 없이 무엇이 들어갈지와 살아남는 상품 수만 확인
    uv run python scripts/seed_curation_sections.py --dry-run

    # 적재
    uv run python scripts/seed_curation_sections.py

멱등: 전 구좌 upsert. 이 파일이 소유하지 않은 활성 구좌는 건드리지 않는다
(끄고 싶으면 --deactivate-unlisted).

auto 구좌는 상품 없이 껍데기만 넣는다. `refresh_auto_sections` 가 UPDATE 만
하고 INSERT 는 하지 않으므로 행이 먼저 존재해야 리프레셔가 상품을 채운다.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from psycopg_pool import AsyncConnectionPool  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.services.curation_refresh import (  # noqa: E402
    GENDER_MATCH_SQL,
    PRODUCT_FEATURES_JOIN,
    _quality_sql,
    _query_params,
)

# 브랜드 구좌 하나에서 한 브랜드가 차지할 수 있는 최대 상품 수. 라운드로빈으로
# 섞으므로 브랜드 9개 × 2 = 18 개까지 후보가 쌓이고, API 가 앞 20 개를 쓴다.
_PER_BRAND = 2
_BRAND_SECTION_POOL = 24

# app/api/curation.py `_PRODUCTS_PER_SECTION` 과 같은 값. 그 모듈을 import 하면
# langfuse 쪽 pydantic v1 shim 이 딸려 와 stderr 를 더럽혀서 값만 복제한다.
_PRODUCTS_PER_SECTION = 20


# ── 구좌 정의 ─────────────────────────────────────────────────────────────────
#
# section_id 는 `editorial-<슬러그>` 규칙을 따른다 — 어드민 페이지도 같은 규칙을
# 쓰면 자동/수기 구좌를 id 만 보고 구분할 수 있다.
#
# 아래 ID 목록은 운영자가 고른 원본이라 대조가 쉽도록 10개씩 묶어 둔다.
# fmt: off

_SUMMER_VACATION_WOMEN = [
    713929, 673348, 672995, 672866, 672249, 672167, 671798, 671477, 671304, 670983,
    670882, 663757, 663675, 663457, 663406, 663339, 662194, 662138, 662023, 661802,
    661650, 661510, 661295, 661269, 660074, 660014, 659947, 659860, 659348, 659242,
    659136, 658799, 658758, 658077, 658016, 657965, 657895, 657552, 657546, 657534,
    657525, 656824, 656438, 656423, 656340, 656199, 656195, 656145, 654487, 654342,
    654125, 653931, 653423, 653415, 653187, 653160, 652965, 652889, 652817, 652326,
    652263, 650704, 641562, 641410, 641326, 641032, 640779, 640310, 640073, 639953,
    639802, 639715, 639513, 632293, 629424, 628208, 625011, 624949, 624749, 624207,
    623242, 622531, 622502, 621817,
]

_SUMMER_VACATION_MEN = [
    714076, 670528, 659662, 657640, 624614, 641109, 588103, 583000, 586137, 543054,
    543190, 559207, 533935, 528302, 528294, 485670, 482471, 464709, 462552, 460821,
    460794, 607478, 559208, 528791, 528425, 528506, 528635, 521827, 528298, 457748,
    445573, 445737, 446045, 607477, 436923, 437241, 520309, 528297, 528423, 446604,
    607470, 445571, 436918,
]

# 원본은 "공용" 한 벌. ai.curation_sections.gender CHECK 는 women/men 만
# 받으므로 같은 목록으로 두 행을 만든다. 하이드레이션이 성별로 한 번 더
# 거르므로(GENDER_MATCH_SQL) 각 행에 실제로 뜨는 상품은 서로 다르다.
_BERMUDA = [
    663408, 663583, 661298, 661235, 661053, 656583, 654116, 659229, 659266, 659106,
    659004, 654161, 652894, 653154, 641566, 625020, 624221, 616147, 623537, 623665,
    623288, 623344, 623622, 713750, 713242, 713651, 713085, 713021, 729785, 729872,
]

_SWIMWEAR_WOMEN = [
    731617, 729416, 728502, 713048, 672976, 650671, 624832, 624085, 620140, 619446,
    590118, 731615, 729415, 728501, 650650, 624831, 624084, 620139, 619400, 590117,
    731604, 729400, 728494, 650646, 624083, 620138, 590116, 731600, 729399, 728493,
    650606, 624082, 619767, 580857,
]

_SWIMWEAR_MEN = [
    730796, 707752, 506714, 434986, 730795, 459655, 434985, 730794, 459653, 434984,
]

# fmt: on


# auto 구좌 — 상품은 refresh_auto_sections 가 채운다. 껍데기만 선점한다.
# _AUTO_IDS 3개가 모두 있어야 리프레셔가 전부 채운다.
AUTO_SECTIONS: list[dict[str, Any]] = [
    {
        "section_id": "popular",
        "title": "지금 인기",
        "subtitle": "이번 주 가장 많이 담긴",
        "sort_order": 1,
        "is_active": True,
    },
    {
        "section_id": "trending-search",
        "title": "요즘 뜨는 브랜드",
        "subtitle": "검색량이 빠르게 오르는 중",
        "sort_order": 2,
        "is_active": True,
    },
    {
        "section_id": "under-100",
        "title": "Under $100",
        "subtitle": "10만원 아래, 안목은 그대로",
        "sort_order": 3,
        "is_active": True,
    },
]


# editorial 구좌 — 상품 ID 직접 지정.
EDITORIAL_SECTIONS: list[dict[str, Any]] = [
    {
        "section_id": "editorial-summer-vacation",
        "display_type": "trending",
        "title": "시원한 여름 휴가 피스",
        "subtitle": None,
        "sort_order": 10,
        "is_active": True,
        "products": {"women": _SUMMER_VACATION_WOMEN, "men": _SUMMER_VACATION_MEN},
    },
    {
        "section_id": "editorial-bermuda-pants",
        "display_type": "trending",
        "title": "버뮤다 팬츠 셀렉션",
        "subtitle": None,
        "sort_order": 11,
        "is_active": True,
        "products": {"women": _BERMUDA, "men": _BERMUDA},
    },
    {
        "section_id": "editorial-swimwear",
        "display_type": "trending",
        "title": "스윔웨어 모아보기",
        "subtitle": None,
        "sort_order": 12,
        "is_active": True,
        "products": {"women": _SWIMWEAR_WOMEN, "men": _SWIMWEAR_MEN},
    },
    {
        "section_id": "editorial-vietnam-hotgirl",
        "title": "지금 뜨는 베트남 핫걸 ST",
        "subtitle": "사이공 트렌드세터의 여름 무드",
        "sort_order": 14,
        "is_active": False,  # 상품 미입력 — 확정되면 켠다
        "products": {"women": []},
    },
]


# 브랜드 구좌 — brand_nodes.id 를 시드 시점에 상품으로 전개한다.
# ID 없이 이름만 적힌 항목은 이름으로 매칭을 시도하고, 실패하면 해당 브랜드만
# 빠진다 (구좌 자체는 남는다).
BRAND_SECTIONS: list[dict[str, Any]] = [
    {
        "section_id": "editorial-brand-picks",
        "title": "브랜드 픽",
        "subtitle": None,
        "sort_order": 20,
        "is_active": True,
        "brands": {
            "women": {
                "ids": [2140, 5281, 5554, 5491, 5543, 5545, 5608, 5485],
                "names": ["Odlyworkshop"],
            },
            "men": {
                "ids": [5757, 5413],
                "names": ["Scuffers", "stussy"],
            },
        },
    },
    {
        "section_id": "editorial-hidden-brands",
        "title": "몰랐던 브랜드",
        "subtitle": None,
        "sort_order": 21,
        "is_active": True,
        "brands": {
            "women": {
                "ids": [5284, 5474, 3838, 5482, 2166, 5209, 5292],
                "names": [],
            },
            "men": {
                "ids": [2205],
                "names": ["Anytime loreak", "Aieul", "NWT", "müdule", "singularisca"],
            },
        },
    },
]

GENDERS = ("women", "men")


# ── 브랜드 → 상품 전개 ────────────────────────────────────────────────────────


async def _resolve_brand_names(cur: Any, names: list[str]) -> tuple[list[int], list[str]]:
    """이름만 적힌 브랜드를 brand_nodes.id 로 해석. (찾은 id, 못 찾은 이름).

    저장된 `brand_name_normalized` 는 신뢰할 수 없다 — 실측상 'Scuffers' 가
    'scuffersher', 'Anytime loreak' 이 공백을 남긴 'anytime loreak',
    'müdule' 이 움라우트를 남긴 'müdule' 로 들어가 있다. 그래서 `brand_name`
    을 조회 시점에 입력과 같은 규칙으로 정규화해 맞추고, 저장 컬럼은 보조
    조건으로만 둔다.
    """
    if not names:
        return [], []
    await cur.execute(
        """
        WITH input AS (
            SELECT raw, regexp_replace(lower(raw), '[^a-z0-9]+', '', 'g') AS norm
            FROM unnest(%s::text[]) AS n(raw)
        )
        SELECT DISTINCT ON (i.raw) i.raw, bn.id
        FROM input i
        JOIN public.brand_nodes bn
          ON regexp_replace(lower(bn.brand_name), '[^a-z0-9]+', '', 'g') = i.norm
          OR bn.brand_name_normalized = i.norm
        ORDER BY i.raw, length(bn.brand_name) ASC, bn.id ASC
        """,
        (names,),
    )
    found = {str(r[0]): int(r[1]) for r in await cur.fetchall()}
    missing = [n for n in names if n not in found]
    return list(found.values()), missing


async def _brand_breakdown(cur: Any, brand_ids: list[int], gender: str) -> list[tuple[int, str, int]]:
    """브랜드별로 품질 게이트를 통과하는 상품 수. 얇은 브랜드를 눈으로 잡으려는 것."""
    if not brand_ids:
        return []
    await cur.execute(
        f"""
        WITH eligible AS (
            SELECT p.brand_node_id AS bid, count(*) AS n
            FROM public.products p
            {PRODUCT_FEATURES_JOIN}
            WHERE p.brand_node_id = ANY(%(brand_ids)s)
              AND {_quality_sql()}
            GROUP BY p.brand_node_id
        )
        SELECT bn.id, bn.brand_name, COALESCE(e.n, 0)
        FROM public.brand_nodes bn
        LEFT JOIN eligible e ON e.bid = bn.id
        WHERE bn.id = ANY(%(brand_ids)s)
        ORDER BY COALESCE(e.n, 0) ASC, bn.brand_name ASC
        """,  # noqa: S608 -- 보간되는 값은 모두 모듈 소유 상수
        {**_query_params(gender), "brand_ids": brand_ids},
    )
    return [(int(r[0]), str(r[1]), int(r[2])) for r in await cur.fetchall()]


async def _expand_brands(cur: Any, brand_ids: list[int], gender: str, excluded: set[int]) -> list[int]:
    """브랜드별 인기 상위 상품을 라운드로빈으로 섞어 상품 ID 풀을 만든다.

    인기 점수는 `popular` auto 구좌와 같은 신호·가중치를 쓴다 (조회 + 3×저장
    + 4×아웃바운드 + ln(1+리뷰), curation_refresh.py:99). 인기 정의가 구좌마다
    갈리지 않게 하려는 것이다.

    `excluded` 는 앞 구좌가 이미 차지한 상품이다. API 가 sort_order 순으로
    `excluded_ids` 를 누적해 뒤 구좌에서 중복을 빼기 때문에(curation.py:173),
    여기서 미리 피하지 않으면 인기 브랜드일수록 `popular` 와 겹쳐 구좌가 반토막
    난다 — 실측상 브랜드 픽(남) 8개 중 4개가 popular 에 먹혔다.

    품질 게이트도 auto 구좌와 같은 `_quality_sql()` 을 쓴다 — 여기서 통과한
    상품만 하이드레이션(app/api/curation.py:183)도 통과한다.
    """
    if not brand_ids:
        return []
    await cur.execute(
        f"""
        WITH views AS (
            SELECT product_id, count(DISTINCT user_id)::float AS n
            FROM ai.product_views
            WHERE viewed_at > now() - interval '7 days'
            GROUP BY product_id
        ),
        saves AS (
            SELECT product_id::bigint AS product_id, count(DISTINCT user_id)::float AS n
            FROM ai.saves
            WHERE product_id ~ '^[0-9]+$'
              AND created_at > now() - interval '7 days'
            GROUP BY product_id::bigint
        ),
        outbound AS (
            SELECT (metadata->>'product_id')::bigint AS product_id,
                   count(DISTINCT user_id)::float AS n
            FROM ai.taste_signal_events
            WHERE signal_type = 'outbound'
              AND occurred_at > now() - interval '7 days'
              AND metadata->>'product_id' ~ '^[0-9]+$'
            GROUP BY (metadata->>'product_id')::bigint
        ),
        ranked AS (
            SELECT p.id AS product_id,
                   p.brand_node_id,
                   row_number() OVER (
                       PARTITION BY p.brand_node_id
                       ORDER BY (
                           COALESCE(v.n, 0) + 3 * COALESCE(sv.n, 0) + 4 * COALESCE(o.n, 0)
                               + ln(1 + COALESCE(p.review_count, 0))
                       ) DESC,
                       p.id DESC
                   ) AS r
            FROM public.products p
            {PRODUCT_FEATURES_JOIN}
            LEFT JOIN views v ON v.product_id = p.id
            LEFT JOIN saves sv ON sv.product_id = p.id
            LEFT JOIN outbound o ON o.product_id = p.id
            WHERE p.brand_node_id = ANY(%(brand_ids)s)
              AND NOT (p.id = ANY(%(excluded)s))
              AND {_quality_sql()}
        )
        SELECT product_id
        FROM ranked
        WHERE r <= %(per_brand)s
        ORDER BY r ASC, brand_node_id ASC
        LIMIT %(pool)s
        """,  # noqa: S608 -- 보간되는 값은 모두 모듈 소유 상수
        {
            **_query_params(gender),
            "brand_ids": brand_ids,
            "excluded": list(excluded),
            "per_brand": _PER_BRAND,
            "pool": _BRAND_SECTION_POOL,
        },
    )
    return [int(r[0]) for r in await cur.fetchall()]


# ── 하이드레이션 검증 ─────────────────────────────────────────────────────────


async def _hydratable_count(cur: Any, product_ids: list[int], gender: str) -> int:
    """API 가 실제로 카드로 띄울 수 있는 상품 수 (curation.py 하이드레이션과 동일 술어)."""
    if not product_ids:
        return 0
    await cur.execute(
        f"""
        SELECT count(*)
        FROM public.products p
        {PRODUCT_FEATURES_JOIN}
        WHERE p.id = ANY(%(ids)s)
          AND p.in_stock
          AND p.image_url IS NOT NULL AND btrim(p.image_url) <> ''
          AND p.price >= 5000
          AND {GENDER_MATCH_SQL}
        """,  # noqa: S608 -- 보간되는 값은 모두 모듈 소유 상수
        {"ids": product_ids, "gender": gender},
    )
    row = await cur.fetchone()
    return int(row[0]) if row else 0


# ── 적재 ──────────────────────────────────────────────────────────────────────


_UPSERT = """
    INSERT INTO ai.curation_sections
        (section_id, gender, slot_type, display_type, title, subtitle, product_ids,
         sort_order, is_active, updated_at)
    VALUES (%(section_id)s, %(gender)s, %(slot_type)s, %(display_type)s, %(title)s, %(subtitle)s,
            %(product_ids)s, %(sort_order)s, %(is_active)s, now())
    ON CONFLICT (section_id, gender) DO UPDATE SET
        slot_type = EXCLUDED.slot_type,
        display_type = EXCLUDED.display_type,
        title = EXCLUDED.title,
        subtitle = EXCLUDED.subtitle,
        product_ids = CASE
            WHEN EXCLUDED.slot_type = 'auto' THEN ai.curation_sections.product_ids
            ELSE EXCLUDED.product_ids
        END,
        sort_order = EXCLUDED.sort_order,
        is_active = EXCLUDED.is_active,
        updated_at = now()
"""


async def _build_rows(cur: Any) -> list[dict[str, Any]]:
    """DB 조회(브랜드 전개)까지 끝낸 최종 행 목록.

    sort_order 순으로 훑으면서 앞 구좌가 차지한 상품을 `claimed` 에 모으고,
    브랜드 구좌는 그걸 피해서 전개한다 — API 의 `excluded_ids` 누적과 같은
    순서다(curation.py:173).
    """
    rows: list[dict[str, Any]] = []
    claimed: dict[str, set[int]] = {gender: set() for gender in GENDERS}

    # auto 구좌의 상품은 리프레셔 소유다. upsert 가 보존하므로 DB 현재값을
    # 그대로 claimed 에 반영한다 — 이게 브랜드 구좌를 가장 많이 먹는 쪽이다.
    await cur.execute(
        "SELECT gender, product_ids FROM ai.curation_sections WHERE slot_type = 'auto' AND is_active",
    )
    for gender, product_ids in await cur.fetchall():
        if str(gender) in claimed:
            claimed[str(gender)].update(int(p) for p in (product_ids or []))

    for spec in AUTO_SECTIONS:
        for gender in GENDERS:
            rows.append(
                {
                    "section_id": spec["section_id"],
                    "gender": gender,
                    "slot_type": "auto",
                    "display_type": "default",
                    "title": spec["title"],
                    "subtitle": spec["subtitle"],
                    "product_ids": [],
                    "sort_order": spec["sort_order"],
                    "is_active": spec["is_active"],
                }
            )

    for spec in EDITORIAL_SECTIONS:
        for gender, product_ids in spec["products"].items():
            deduped = list(dict.fromkeys(product_ids))
            if spec["is_active"]:
                claimed[gender].update(deduped[:_PRODUCTS_PER_SECTION])
            rows.append(
                {
                    "section_id": spec["section_id"],
                    "gender": gender,
                    "slot_type": "editorial",
                    "display_type": spec.get("display_type", "default"),
                    "title": spec["title"],
                    "subtitle": spec["subtitle"],
                    "product_ids": deduped,
                    "sort_order": spec["sort_order"],
                    "is_active": spec["is_active"],
                }
            )

    for spec in BRAND_SECTIONS:
        for gender, brands in spec["brands"].items():
            resolved, missing = await _resolve_brand_names(cur, brands["names"])
            if missing:
                print(f"  ⚠ {spec['section_id']}/{gender}: 브랜드 미매칭 {missing}")
            brand_ids = list(dict.fromkeys([*brands["ids"], *resolved]))
            product_ids = await _expand_brands(cur, brand_ids, gender, claimed[gender])
            claimed[gender].update(product_ids)
            empty = [f"{name}({bid})" for bid, name, n in await _brand_breakdown(cur, brand_ids, gender) if n == 0]
            if empty:
                print(f"  ⚠ {spec['section_id']}/{gender}: 해당 성별 상품 0 개인 브랜드 {empty}")
            if not product_ids:
                print(f"  ⚠ {spec['section_id']}/{gender}: 전개된 상품 0 개 — 비활성으로 넣는다")
            rows.append(
                {
                    "section_id": spec["section_id"],
                    "gender": gender,
                    "slot_type": "editorial",
                    "display_type": spec.get("display_type", "default"),
                    "title": spec["title"],
                    "subtitle": spec["subtitle"],
                    "product_ids": product_ids,
                    "sort_order": spec["sort_order"],
                    "is_active": spec["is_active"] and bool(product_ids),
                }
            )

    return rows


async def _report(cur: Any, rows: list[dict[str, Any]]) -> None:
    # auto 구좌는 upsert 가 기존 product_ids 를 보존하므로(_UPSERT 의 CASE),
    # 계획상의 빈 배열이 아니라 DB 현재값을 보여줘야 오해가 없다.
    await cur.execute(
        "SELECT section_id, gender, product_ids FROM ai.curation_sections WHERE slot_type = 'auto'",
    )
    current_auto = {(str(r[0]), str(r[1])): [int(p) for p in (r[2] or [])] for r in await cur.fetchall()}

    print(f"\n{'section_id':<30} {'gender':<6} {'type':<9} {'ord':>3} {'act':<4} {'ids':>4} {'표시가능':>6}")
    print("-" * 76)
    for row in sorted(rows, key=lambda r: (r["sort_order"], r["section_id"], r["gender"])):
        if row["slot_type"] == "auto":
            row = {**row, "product_ids": current_auto.get((row["section_id"], row["gender"]), [])}
        live = await _hydratable_count(cur, row["product_ids"], row["gender"])
        flag = "on" if row["is_active"] else "off"
        warn = "  ← 12개 미만" if row["is_active"] and row["slot_type"] == "editorial" and live < 12 else ""
        print(
            f"{row['section_id']:<30} {row['gender']:<6} {row['slot_type']:<9} "
            f"{row['sort_order']:>3} {flag:<4} {len(row['product_ids']):>4} {live:>6}{warn}"
        )


async def _main(args: argparse.Namespace) -> int:
    dsn = args.dsn or settings.DB_DSN
    if not dsn:
        print("DB_DSN is not set (use --dsn or export DB_DSN)", file=sys.stderr)
        return 2

    async with AsyncConnectionPool(dsn, min_size=1, max_size=4, open=False) as pool:
        await pool.wait()
        async with pool.connection() as conn, conn.cursor() as cur:
            rows = await _build_rows(cur)
            await _report(cur, rows)

            if args.dry_run:
                print("\n--dry-run — DB 쓰기 없음")
                return 0

            for row in rows:
                await cur.execute(_UPSERT, row)

            if args.deactivate_unlisted:
                owned = [f"{r['section_id']}:{r['gender']}" for r in rows]
                await cur.execute(
                    """
                    UPDATE ai.curation_sections
                    SET is_active = false, updated_at = now()
                    WHERE NOT ((section_id || ':' || gender) = ANY(%s)) AND is_active
                    """,
                    (owned,),
                )
                print(f"\n미등재 구좌 비활성화: {cur.rowcount} 행")

            await conn.commit()

    print(f"\n✅ upsert {len(rows)} 행")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="curation sections hardcoded seed")
    parser.add_argument("--dry-run", action="store_true", help="DB 쓰기 없이 적재 계획과 표시 가능 수만 출력")
    parser.add_argument(
        "--deactivate-unlisted",
        action="store_true",
        help="이 파일에 없는 활성 구좌를 is_active=false 로 내림",
    )
    parser.add_argument("--dsn", default="", help="Postgres DSN (기본: settings.DB_DSN)")
    return asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
