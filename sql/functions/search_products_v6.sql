-- SPEC-SEARCH-V6-COLOR — search_products_v6 RPC + p_color_family hard filter
--
-- 색 필터 (`p_color_family`) 파라미터 추가. Vision v2 가 뽑는 canonical 16 family
-- (BLACK / WHITE / GREY / NAVY / BLUE / BEIGE / BROWN / GREEN / RED / PINK /
-- PURPLE / ORANGE / YELLOW / CREAM / KHAKI / MULTI) 를 그대로 넘기면 됨.
--
-- 매칭: `UPPER(p.color) = UPPER(p_color_family)`
--   ├── 'Black' / 'BLACK' 다 잡음 (카탈로그 대소문자 분열 대응)
--   └── 'Charcoal' / 'Ivory' 등 family enum 밖 색은 못 잡음 (임베딩이 근처
--       색을 어차피 랭킹 상위로 뽑아줌 — 실측 후 필요하면 color_canonical
--       테이블로 정식화)
--
-- 하위 호환: `p_color_family text DEFAULT NULL`. NULL 이면 필터 disable →
-- 기존 caller (색 안 넘기는 코드경로) 는 byte-identical.
--
-- Fallback: 색 필터가 pool 을 지나치게 좁혀도 별도 rung 없음. 임팩트 측정 후
-- 필요하면 rung 4 (color dropped) 추가 검토.
--
-- 적용: DBeaver 나 `sudo -u postgres psql` 로 전체 파일 실행.
-- 적용 후 pg_proc 시그니처에 `p_color_family` 나타나면 성공.

CREATE OR REPLACE FUNCTION public.search_products_v6(
  query_embedding halfvec,
  p_style_node_id bigint DEFAULT NULL::bigint,
  p_category      text DEFAULT NULL::text,
  p_subcategory   text DEFAULT NULL::text,
  p_brand_names   text[] DEFAULT NULL::text[],
  p_color_family  text DEFAULT NULL::text,  -- NEW: 16 canonical family (BLACK/GREY/…)
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
      AND (p_color_family IS NULL OR UPPER(p.color) = UPPER(p_color_family));  -- NEW
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
        AND (p_color_family IS NULL OR UPPER(p.color) = UPPER(p_color_family))  -- NEW
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
  WHERE p.in_stock = true
    AND (
      p_category IS NULL
      OR v_target_family IS NULL
      OR v_target_family = 'other'
      OR COALESCE(cc.family, 'other') = v_target_family
    )
    AND (p_subcategory IS NULL OR p.subcategory = p_subcategory)
    AND (p_brand_names IS NULL OR bn.brand_name = ANY(p_brand_names))
    AND (p_color_family IS NULL OR UPPER(p.color) = UPPER(p_color_family));  -- NEW

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
      WHERE p.in_stock = true
        AND (
          p_category IS NULL
          OR v_target_family IS NULL
          OR v_target_family = 'other'
          OR COALESCE(cc.family, 'other') = v_target_family
        )
        AND (p_subcategory IS NULL OR p.subcategory = p_subcategory)
        AND (p_brand_names IS NULL OR bn.brand_name = ANY(p_brand_names))
        AND (p_color_family IS NULL OR UPPER(p.color) = UPPER(p_color_family))  -- NEW
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
      AND (p_color_family IS NULL OR UPPER(p.color) = UPPER(p_color_family))  -- NEW
    ORDER BY pe.embedding <=> query_embedding ASC, p.created_at DESC
    LIMIT p_limit;
END;
$function$;
