-- search_products_v6.sql — v6 검색 RPC 캐노니컬 정의 (배포 시 자동 적용 대상)
--
-- ⚠️ SOURCE OF TRUTH: 이 파일이 dev DB 의 search_products_v6 단일 정의다.
--   함수 시그니처/본문을 바꿔야 하면 이 파일만 수정하면 배포가 자동 적용한다
--   (deploy.ai.sh → docker run one-off → psql). 더 이상 psql 수동 적용에
--   의존하지 않는다. 본문은 kiko.ai-app migration 082 verbatim (2026-05-20 fanout fix).
--
-- 7/10 사고 재발 방지 (SPEC-SEARCH-V6-001):
--   사고 원인 = 코드가 새 시그니처로 호출하는데 DB 함수는 옛날 시그니처 그대로.
--   Postgres 는 인자 리스트가 다르면 CREATE OR REPLACE 가 "교체"가 아니라
--   "오버로드 추가"라, 옛날/새 함수가 공존 → RPC 가 엉뚱한 쪽으로 resolve.
--   → 아래 DO 블록이 search_products_v6 의 **모든 오버로드를 먼저 DROP** 한 뒤
--     캐노니컬 1개만 재생성. 매 배포마다 "정확히 이 시그니처 하나"를 보장한다.
--   전체가 BEGIN/COMMIT 로 원자 적용 → 동시 쿼리는 옛/새 커밋 상태만 관찰 (중간 공백 없음).

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

-- ── 캐노니컬 정의 (082 verbatim JOIN fix) ─────────────────────────────────
CREATE OR REPLACE FUNCTION search_products_v6(
  query_embedding   halfvec(768),
  p_style_node_id   bigint  DEFAULT NULL,
  p_category        text    DEFAULT NULL,
  p_subcategory     text    DEFAULT NULL,
  p_brand_names     text[]  DEFAULT NULL,
  p_limit           int     DEFAULT 30
)
RETURNS TABLE (
  id            bigint,
  brand         text,
  name          text,
  price         integer,
  image_url     text,
  product_url   text,
  platform      text,
  subcategory   text,
  distance      double precision,
  degraded      boolean
)
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
  v_target_family text := NULL;
  v_node_count    integer := 0;
  v_node_fam_cnt  integer := 0;
BEGIN
  -- ── p_category → family lookup (정규화 유지) ──────────────────────
  -- p_category 는 호출자(Vision/agent/admin)가 임의 case 로 보낼 수 있어
  -- lower(trim()) 정규화로 cc 의 매칭 row 한 개를 찾는다 (LIMIT 1 이므로
  -- fanout 없음). cc 가 verbatim seed 라 모든 변형이 들어있어 매칭이 안정.
  IF p_category IS NOT NULL THEN
    SELECT cc.family INTO v_target_family
    FROM category_canonical cc
    WHERE lower(trim(cc.raw_category)) = lower(trim(p_category))
    LIMIT 1;
    IF v_target_family IS NULL THEN
      v_target_family := 'other';
    END IF;
  END IF;

  -- family gate 는 v_target_family 가 구체 family 일 때만 작동.
  -- NULL → p_category 미지정 / 'other' → 매핑 없음 둘 다 gate 비활성.

  -- ── rung 1 count: EXACT node + family gate ────────────────────────
  IF p_style_node_id IS NOT NULL THEN
    SELECT count(*) INTO v_node_fam_cnt
    FROM products p
    JOIN brand_nodes bn ON bn.id = p.brand_node_id
    JOIN product_embeddings pe ON pe.product_id = p.id
    LEFT JOIN category_canonical cc
      ON cc.raw_category = p.category   -- 082: verbatim 매칭
    WHERE bn.primary_style_node_id = p_style_node_id
      AND p.in_stock = true
      AND (
        p_category IS NULL
        OR v_target_family IS NULL
        OR v_target_family = 'other'
        OR COALESCE(cc.family, 'other') = v_target_family
      )
      AND (p_subcategory IS NULL OR p.subcategory = p_subcategory)
      AND (p_brand_names IS NULL OR bn.brand_name = ANY(p_brand_names));
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
        ON cc.raw_category = p.category   -- 082: verbatim 매칭
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
    ON cc.raw_category = p.category       -- 082: verbatim 매칭
  WHERE p.in_stock = true
    AND (
      p_category IS NULL
      OR v_target_family IS NULL
      OR v_target_family = 'other'
      OR COALESCE(cc.family, 'other') = v_target_family
    )
    AND (p_subcategory IS NULL OR p.subcategory = p_subcategory)
    AND (p_brand_names IS NULL OR bn.brand_name = ANY(p_brand_names));

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
        ON cc.raw_category = p.category   -- 082: verbatim 매칭
      WHERE p.in_stock = true
        AND (
          p_category IS NULL
          OR v_target_family IS NULL
          OR v_target_family = 'other'
          OR COALESCE(cc.family, 'other') = v_target_family
        )
        AND (p_subcategory IS NULL OR p.subcategory = p_subcategory)
        AND (p_brand_names IS NULL OR bn.brand_name = ANY(p_brand_names))
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
    WHERE p.in_stock = true
      AND (p_subcategory IS NULL OR p.subcategory = p_subcategory)
      AND (p_brand_names IS NULL OR bn.brand_name = ANY(p_brand_names))
    ORDER BY pe.embedding <=> query_embedding ASC, p.created_at DESC
    LIMIT p_limit;
END;
$$;

COMMENT ON FUNCTION search_products_v6 IS
  'v6 embedding-first retrieval (SPEC-SEARCH-V6-001 §4/§13 + 073 family '
  'gate + 082 verbatim JOIN fix). FILTER1 EXACT primary_style_node → '
  'FILTER2 canonical FAMILY (category_canonical, verbatim raw_category JOIN '
  '— 082 으로 fanout 제거) + in_stock + embedding → cosine `<=>` ASC, '
  'created_at DESC tie. Ladder F: rung1 node+family (degraded=false) → '
  'rung2 node dropped/family kept (degraded=true) → rung3 cosine-only.';

COMMIT;
