-- search_products_v6.sql — v6 검색 RPC 캐노니컬 정의 (배포 시 자동 적용 대상)
--
-- ⚠️ SOURCE OF TRUTH: 이 파일이 dev DB 의 search_products_v6 단일 정의다.
--   함수 시그니처/본문을 바꿔야 하면 이 파일만 수정하면 배포가 자동 적용한다
--   (deploy.ai.sh → docker compose run → psql). 더 이상 수동 psql/DBeaver 적용에
--   의존하지 않는다.
--
-- 7/10 사고 재발 방지:
--   사고 원인 = 코드가 새 시그니처로 호출하는데 DB 함수는 옛날 시그니처 그대로.
--   Postgres 는 인자 리스트가 다르면 CREATE OR REPLACE 가 "교체"가 아니라
--   "오버로드 추가"라, 옛날/새 함수가 공존 → RPC 가 엉뚱한 쪽으로 resolve.
--   (color 파라미터 추가로 6-인자 → 7-인자가 되며 6-인자가 남은 게 바로 그 사례.)
--   → 아래 DO 블록이 search_products_v6 의 **모든 오버로드를 먼저 DROP** 한 뒤
--     캐노니컬 1개만 재생성. 매 배포마다 "정확히 이 시그니처 하나"를 보장한다.
--   전체가 BEGIN/COMMIT 로 원자 적용 → 동시 쿼리는 옛/새 커밋 상태만 관찰 (중간 공백 없음).
--
-- ── 함수 사양 (SPEC-SEARCH-V6-COLOR) ──────────────────────────────────────
-- 색 필터 (`p_color_family`) 파라미터. Vision v2 가 뽑는 canonical 16 family
-- (BLACK / WHITE / GREY / NAVY / BLUE / BEIGE / BROWN / GREEN / RED / PINK /
-- PURPLE / ORANGE / YELLOW / CREAM / KHAKI / MULTI) 를 그대로 넘기면 됨.
-- 하위 호환: `p_color_family text DEFAULT NULL`. NULL 이면 필터 disable →
-- 기존 caller (색 안 넘기는 코드경로) 는 byte-identical.
-- 검증: 적용 후 `\df search_products_v6` 시그니처에 `p_color_family` 나오면 성공.
--
-- ── 색 출처 이관: products.color → product_features (2026-07-29) ──────────
-- 매칭이 `UPPER(p.color) = UPPER(p_color_family)` 에서
-- `pf.feature_metadata->>'primary_color' = UPPER(p_color_family)` 로 바뀌었다.
--
-- 이유: 크롤러가 뽑던 products.color 는 VLM primary_color 와 일치율이 54.8%
--   (71,775 / 131,058) 밖에 안 됐다 — 즉 색 하드 필터가 45% 확률로 틀린
--   기준을 적용하고 있었다. product_features.feature_metadata->>'primary_color'
--   는 Qwen3-VL 이 이미지에서 직접 뽑은 값이고 **정확히 위 16 family enum**
--   이라 (BLACK/WHITE/GREY/BLUE/BROWN/CREAM/NAVY/GREEN/BEIGE/KHAKI/PINK/RED/
--   YELLOW/PURPLE/MULTI/ORANGE) drop-in 교체가 된다.
--   → 호출부의 alias 보정(multi→multicolor, gray→grey)도 불필요해져 제거됨.
--
-- JOIN 은 반드시 LEFT: 검색 실모수(in_stock + product_embeddings) 82,397 중
--   product_features 보유는 79,283 (96.2%). INNER 로 묶으면 나머지 3,114 개가
--   색 필터를 안 쓰는 쿼리에서까지 통째로 사라진다.
--   색 술어 자체는 strict (features 없으면 탈락) — AI 서버의 relax 재시도가
--   `p_color_family` 를 떨어뜨려 이 리콜을 회수한다 (search_service.py).
--   인덱스: idx_pf_primary_color (migration 095). 기존 jsonb_path_ops GIN 은
--   `->>` 등치 비교를 타지 못하므로 표현식 btree 가 따로 필요하다.
--
-- ── 성별 필터 (`p_gender`) ──────────────────────────────────────────────
-- 이력: 2026-07-16 도입 / 2026-07-29 VLM 우선 3단 다리 / 2026-08-03 순위 역전
--       / **2026-08-03 단일 출처로 축약**
--
-- 호출자(AI 서버)는 men/women 만 보낸다: unisex 요청/미확인은 NULL(필터 off).
-- 골든셋 2차 실측 문제(남성 쿼리에 여성 상품 누수) 해소용 상품 레벨 하드 필터.
-- 시맨틱 제약이므로 어느 rung 에서도 완화하지 않는다 (in_stock 과 동급).
--
--   AND (p_gender IS NULL OR p.gender && ARRAY[p_gender, 'unisex'])
--
-- 다단 CASE 와 fail-open 은 걷어냈다. gender 출처를 VLM(product_features) 으로
-- 이관했다가 성능 미달로 크롤러에 회귀시켰고, migration 104 가
-- `chk_products_gender_required` 를 VALIDATE 해 **p.gender 의 non-NULL ·
-- non-empty · canonical(men/women/unisex) 이 DB 차원에서 보장**된다.
-- 따라서 `&&` 가 NULL 을 낼 수 없고 폴백이 방어할 대상도 없다.
--
-- ⚠️ `LEFT JOIN product_features pf` 는 **그대로 둔다** — p_color_family 필터가
--    `pf.feature_metadata->>'primary_color'` 를 쓴다. color 는 계속 VLM 소관이다.
--
-- 되돌려야 한다면: 104 를 DROP 하고 p.gender 에 NULL 이 다시 생길 수 있는
--    상태로 가는 경우에만 3단 다리가 필요해진다.

BEGIN;

-- ── 모든 search_products_v6 오버로드 제거 (옛 6-인자 포함) ──────────────────
DO $$
DECLARE
  r record;
BEGIN
  FOR r IN
    SELECT oid::regprocedure AS sig
    FROM pg_proc
    WHERE proname = 'search_products_v6'
      AND pronamespace = 'public'::regnamespace
  LOOP
    EXECUTE 'DROP FUNCTION ' || r.sig::text;
  END LOOP;
END $$;

-- ── 캐노니컬 정의 (8-arg: p_color_family + p_gender hard filters) ──────────
CREATE OR REPLACE FUNCTION public.search_products_v6(
  query_embedding halfvec,
  p_style_node_id bigint DEFAULT NULL::bigint,
  p_category      text DEFAULT NULL::text,
  p_subcategory   text DEFAULT NULL::text,
  p_brand_names   text[] DEFAULT NULL::text[],
  p_color_family  text DEFAULT NULL::text,  -- 16 canonical family (BLACK/GREY/…)
  p_gender        text DEFAULT NULL::text,  -- NEW: 'men'|'women' (unisex 상품은 항상 포함)
  p_limit         integer DEFAULT 30
)
 RETURNS TABLE(id bigint, brand text, name text, price integer, image_url text, product_url text, platform text, subcategory text, distance double precision, degraded boolean)
 LANGUAGE plpgsql
 STABLE
AS $function$
DECLARE
  v_target_family text := NULL;
  v_node_count    integer := 0;
  v_node_fam_cnt  integer := 0;
BEGIN
  -- ── p_category → family lookup (정규화 유지) ──────────────────────
  IF p_category IS NOT NULL THEN
    SELECT cc.family INTO v_target_family
    FROM category_canonical cc
    WHERE lower(trim(cc.raw_category)) = lower(trim(p_category))
    LIMIT 1;
    IF v_target_family IS NULL THEN
      v_target_family := 'other';
    END IF;
  END IF;

  -- ── rung 1 count: EXACT node + family gate ────────────────────────
  IF p_style_node_id IS NOT NULL THEN
    SELECT count(*) INTO v_node_fam_cnt
    FROM products p
    JOIN brand_nodes bn ON bn.id = p.brand_node_id
    JOIN product_embeddings pe ON pe.product_id = p.id
    LEFT JOIN category_canonical cc
      ON cc.raw_category = p.category
    LEFT JOIN product_features pf
      ON pf.product_id = p.id
    WHERE bn.primary_style_node_id = p_style_node_id
      AND p.in_stock = true
      AND (
        p_category IS NULL
        OR v_target_family IS NULL
        OR v_target_family = 'other'
        OR COALESCE(cc.family, 'other') = v_target_family
      )
      AND (p_subcategory IS NULL OR p.subcategory = p_subcategory)
      AND (p_brand_names IS NULL OR bn.brand_name = ANY(p_brand_names))
      AND (p_color_family IS NULL
           OR pf.feature_metadata->>'primary_color' = UPPER(p_color_family))
      AND (
        p_gender IS NULL
        OR p.gender && ARRAY[p_gender, 'unisex']
      );
  END IF;

  IF p_style_node_id IS NOT NULL AND v_node_fam_cnt > 0 THEN
    -- ── rung 1: EXACT node + family gate (NOT degraded) ────────────
    RETURN QUERY
      SELECT p.id, p.brand, p.name, p.price, p.image_url, p.product_url,
             p.platform, p.subcategory,
             (pe.embedding <=> query_embedding)::double precision AS distance,
             false AS degraded
      FROM products p
      JOIN brand_nodes bn ON bn.id = p.brand_node_id
      JOIN product_embeddings pe ON pe.product_id = p.id
      LEFT JOIN category_canonical cc
        ON cc.raw_category = p.category
      LEFT JOIN product_features pf
        ON pf.product_id = p.id
      WHERE bn.primary_style_node_id = p_style_node_id
        AND p.in_stock = true
        AND (
          p_category IS NULL
          OR v_target_family IS NULL
          OR v_target_family = 'other'
          OR COALESCE(cc.family, 'other') = v_target_family
        )
        AND (p_subcategory IS NULL OR p.subcategory = p_subcategory)
        AND (p_brand_names IS NULL OR bn.brand_name = ANY(p_brand_names))
        AND (p_color_family IS NULL
             OR pf.feature_metadata->>'primary_color' = UPPER(p_color_family))
        AND (
          p_gender IS NULL
          OR p.gender && ARRAY[p_gender, 'unisex']
        )
      ORDER BY pe.embedding <=> query_embedding ASC, p.created_at DESC
      LIMIT p_limit;
    RETURN;
  END IF;

  -- ── rung 2: node filter dropped, family gate KEPT (degraded) ─────
  SELECT count(*) INTO v_node_count
  FROM products p
  JOIN product_embeddings pe ON pe.product_id = p.id
  LEFT JOIN brand_nodes bn ON bn.id = p.brand_node_id
  LEFT JOIN category_canonical cc
    ON cc.raw_category = p.category
  LEFT JOIN product_features pf
    ON pf.product_id = p.id
  WHERE p.in_stock = true
    AND (
      p_category IS NULL
      OR v_target_family IS NULL
      OR v_target_family = 'other'
      OR COALESCE(cc.family, 'other') = v_target_family
    )
    AND (p_subcategory IS NULL OR p.subcategory = p_subcategory)
    AND (p_brand_names IS NULL OR bn.brand_name = ANY(p_brand_names))
    AND (p_color_family IS NULL
         OR pf.feature_metadata->>'primary_color' = UPPER(p_color_family))
    AND (
      p_gender IS NULL
      OR p.gender && ARRAY[p_gender, 'unisex']
    );

  IF v_node_count > 0 THEN
    RETURN QUERY
      SELECT p.id, p.brand, p.name, p.price, p.image_url, p.product_url,
             p.platform, p.subcategory,
             (pe.embedding <=> query_embedding)::double precision AS distance,
             true AS degraded
      FROM products p
      JOIN product_embeddings pe ON pe.product_id = p.id
      LEFT JOIN brand_nodes bn ON bn.id = p.brand_node_id
      LEFT JOIN category_canonical cc
        ON cc.raw_category = p.category
      LEFT JOIN product_features pf
        ON pf.product_id = p.id
      WHERE p.in_stock = true
        AND (
          p_category IS NULL
          OR v_target_family IS NULL
          OR v_target_family = 'other'
          OR COALESCE(cc.family, 'other') = v_target_family
        )
        AND (p_subcategory IS NULL OR p.subcategory = p_subcategory)
        AND (p_brand_names IS NULL OR bn.brand_name = ANY(p_brand_names))
        AND (p_color_family IS NULL
             OR pf.feature_metadata->>'primary_color' = UPPER(p_color_family))
        AND (
          p_gender IS NULL
          OR p.gender && ARRAY[p_gender, 'unisex']
        )
      ORDER BY pe.embedding <=> query_embedding ASC, p.created_at DESC
      LIMIT p_limit;
    RETURN;
  END IF;

  -- ── rung 3: node + family BOTH dropped (still degraded) ──────────
  RETURN QUERY
    SELECT p.id, p.brand, p.name, p.price, p.image_url, p.product_url,
           p.platform, p.subcategory,
           (pe.embedding <=> query_embedding)::double precision AS distance,
           true AS degraded
    FROM products p
    JOIN product_embeddings pe ON pe.product_id = p.id
    LEFT JOIN brand_nodes bn ON bn.id = p.brand_node_id
    LEFT JOIN product_features pf
      ON pf.product_id = p.id
    WHERE p.in_stock = true
      AND (p_subcategory IS NULL OR p.subcategory = p_subcategory)
      AND (p_brand_names IS NULL OR bn.brand_name = ANY(p_brand_names))
      AND (p_color_family IS NULL
           OR pf.feature_metadata->>'primary_color' = UPPER(p_color_family))
      AND (
        p_gender IS NULL
        OR p.gender && ARRAY[p_gender, 'unisex']
      )
    ORDER BY pe.embedding <=> query_embedding ASC, p.created_at DESC
    LIMIT p_limit;
END;
$function$;

COMMIT;

-- PostgREST 스키마 캐시 리로드 (2026-07-15 사고 재발 방지): 오버로드
-- DROP/재생성 후 캐시가 stale 이면 기존 param-set 호출이 PGRST203
-- (ambiguous overload) 로 전면 실패한다 — 실사고: 7-param 교체 직후
-- 6-key 호출 전부 다운, NOTIFY 수동 발사로 복구. 함수 적용과 같은
-- 세션에서 즉시 리로드해 무중단을 보장한다.
NOTIFY pgrst, 'reload schema';
