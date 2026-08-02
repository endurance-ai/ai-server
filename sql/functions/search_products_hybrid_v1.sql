-- search_products_hybrid_v1.sql — 텍스트 쿼리 전용 하이브리드 검색 RPC
--
-- ⚠️ SOURCE OF TRUTH: 이 파일이 dev DB 의 search_products_hybrid_v1 단일 정의다.
--   deploy-dev.yml 이 sql/functions/*.sql 를 dev-app 에 psql 적용한다 (v6 와 동일 경로).
--
-- ── 왜 별도 RPC 인가 (SPEC-SEARCH-HYBRID-001) ────────────────────────────────
-- v6(search_products_v6)는 `product_embeddings`(이미지 임베딩) 단일 공간에
-- 코사인 랭킹한다. 텍스트 쿼리(embed_text)를 이미지 임베딩에 매칭하면 CLIP 계열
-- 특유의 modality gap 으로 distance 가 0.86~0.92 로 뭉쳐(스프레드 ~0.015) 랭킹
-- 신호가 흐리다. 반대로 상품 `product_features.text_embedding`(retrieval_text 의
-- 동일 FashionSigLIP 텍스트 임베딩)에 매칭하면 text↔text 라 distance 0.17~0.22
-- (스프레드 ~0.05)로 훨씬 분리력이 좋다.
--
-- 골든셋 172 쿼리 A/B (feature_metadata 구조화 precision@15):
--   * 순수 텍스트 교체는 폐기 — 컬러는 +0.15 이지만 소재/패턴(시각 텍스처)은
--     이미지가 이겨 회귀. 순수 텍스트 평균 0.68(최저).
--   * 두 공간 정규화 블렌드 w_txt=0.3 이 best-of-both: 컬러 0.85·소재 0.72·
--     패턴 0.87·평균 0.81 (순수 이미지 0.76 대비 +0.05). 소재/패턴은 두 순수
--     공간보다도 높다(신호가 서로 노이즈를 상쇄).
--
-- 구현: 두 kNN(이미지·텍스트)을 각자 HNSW 인덱스로 top p_pool 뽑아 union 한 뒤,
--   union 후보에 대해 두 거리를 모두 계산 → per-query min-max 정규화 → 블렌드
--   (1-w)·nzi + w·nzt 로 재정렬. SQL 내부에서 양쪽 거리를 정확히 계산하므로
--   서비스 2-쿼리(절단 리스트 + max 임퓨테이션, 0.79)보다 full 0.81 을 그대로 낸다.
--
-- 사진(이미지 업로드) 경로는 query_embedding 이 이미지 임베딩이라 text_embedding
--   블렌드가 cross-modal 로 미검증 → 여전히 v6 를 쓴다. 이 RPC 는 텍스트 경로 전용.
--
-- 게이트(카테고리 family / subcategory / brand / color / gender 하드필터 / style
--   node)는 v6 와 동일 시맨틱으로 두 kNN CTE 에 각각 적용한다. gender 3단 다리도
--   v6 와 동일(feature_metadata.gender → products.gender → fail-open).
--
-- product_features(text_embedding) 미보유 상품(카탈로그의 ~4%)은 텍스트 랭킹
--   대상이 아니고 최종 union JOIN 에서 빠진다 — 하이브리드는 enrich 완료 상품만
--   대상으로 한다(의도된 스코프). 이미지 단독 리콜이 필요하면 v6 를 쓴다.

BEGIN;

-- ── 모든 오버로드 제거 후 캐노니컬 1개 재생성 (v6 와 동일한 재발방지 패턴) ──
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT oid::regprocedure AS sig FROM pg_proc
    WHERE proname = 'search_products_hybrid_v1' AND pronamespace = 'public'::regnamespace
  LOOP
    EXECUTE 'DROP FUNCTION ' || r.sig::text;
  END LOOP;
END $$;

CREATE OR REPLACE FUNCTION public.search_products_hybrid_v1(
  query_embedding halfvec,
  p_style_node_id bigint DEFAULT NULL::bigint,
  p_category      text DEFAULT NULL::text,
  p_subcategory   text DEFAULT NULL::text,
  p_brand_names   text[] DEFAULT NULL::text[],
  p_color_family  text DEFAULT NULL::text,
  p_gender        text DEFAULT NULL::text,
  p_w_text        double precision DEFAULT 0.3,   -- 블렌드 텍스트 가중 (0=이미지, 1=텍스트)
  p_pool          integer DEFAULT 100,            -- 공간별 kNN 풀 크기
  p_limit         integer DEFAULT 50
)
 RETURNS TABLE(id bigint, brand text, name text, price integer, image_url text, product_url text, platform text, subcategory text, distance double precision, degraded boolean)
 LANGUAGE plpgsql
 STABLE
AS $function$
DECLARE
  v_target_family text := NULL;
BEGIN
  -- p_category → family lookup (v6 와 동일)
  IF p_category IS NOT NULL THEN
    SELECT cc.family INTO v_target_family
    FROM category_canonical cc
    WHERE lower(trim(cc.raw_category)) = lower(trim(p_category))
    LIMIT 1;
    IF v_target_family IS NULL THEN
      v_target_family := 'other';
    END IF;
  END IF;

  RETURN QUERY
  WITH img AS (
    -- 이미지 공간 kNN (idx_product_embeddings_hnsw, halfvec_cosine_ops)
    SELECT pe.product_id AS pid
    FROM products p
    JOIN product_embeddings pe ON pe.product_id = p.id
    LEFT JOIN brand_nodes bn ON bn.id = p.brand_node_id
    LEFT JOIN category_canonical cc ON cc.raw_category = p.category
    LEFT JOIN product_features pf ON pf.product_id = p.id
    WHERE p.in_stock = true
      AND (p_category IS NULL OR v_target_family IS NULL OR v_target_family = 'other'
           OR COALESCE(cc.family, 'other') = v_target_family)
      AND (p_subcategory IS NULL OR p.subcategory = p_subcategory)
      AND (p_brand_names IS NULL OR bn.brand_name = ANY(p_brand_names))
      AND (p_style_node_id IS NULL OR bn.primary_style_node_id = p_style_node_id)
      AND (p_color_family IS NULL OR pf.feature_metadata->>'primary_color' = UPPER(p_color_family))
      AND (p_gender IS NULL OR CASE
             WHEN pf.feature_metadata->>'gender' IS NOT NULL
               THEN pf.feature_metadata->>'gender' IN (p_gender, 'unisex')
             WHEN p.gender IS NOT NULL AND cardinality(p.gender) > 0
               THEN p.gender && ARRAY[p_gender, 'unisex']
             ELSE true END)
    ORDER BY pe.embedding::halfvec <=> query_embedding
    LIMIT p_pool
  ),
  txt AS (
    -- 텍스트 공간 kNN (idx_product_features_text_hnsw, halfvec_cosine_ops)
    SELECT pf.product_id AS pid
    FROM products p
    JOIN product_features pf ON pf.product_id = p.id
    LEFT JOIN brand_nodes bn ON bn.id = p.brand_node_id
    LEFT JOIN category_canonical cc ON cc.raw_category = p.category
    WHERE p.in_stock = true
      AND (p_category IS NULL OR v_target_family IS NULL OR v_target_family = 'other'
           OR COALESCE(cc.family, 'other') = v_target_family)
      AND (p_subcategory IS NULL OR p.subcategory = p_subcategory)
      AND (p_brand_names IS NULL OR bn.brand_name = ANY(p_brand_names))
      AND (p_style_node_id IS NULL OR bn.primary_style_node_id = p_style_node_id)
      AND (p_color_family IS NULL OR pf.feature_metadata->>'primary_color' = UPPER(p_color_family))
      AND (p_gender IS NULL OR CASE
             WHEN pf.feature_metadata->>'gender' IS NOT NULL
               THEN pf.feature_metadata->>'gender' IN (p_gender, 'unisex')
             WHEN p.gender IS NOT NULL AND cardinality(p.gender) > 0
               THEN p.gender && ARRAY[p_gender, 'unisex']
             ELSE true END)
    ORDER BY pf.text_embedding <=> query_embedding
    LIMIT p_pool
  ),
  un AS (
    SELECT pid FROM img UNION SELECT pid FROM txt
  ),
  dd AS (
    -- union 후보에 대해 두 거리를 모두 계산 (양쪽 index 없이 직접 계산 — 풀 작음)
    SELECT u.pid,
           (pe.embedding::halfvec <=> query_embedding)::double precision AS d_img,
           (pf.text_embedding <=> query_embedding)::double precision AS d_txt,
           p.brand, p.name, p.price, p.image_url, p.product_url, p.platform, p.subcategory
    FROM un u
    JOIN products p ON p.id = u.pid
    JOIN product_embeddings pe ON pe.product_id = u.pid
    JOIN product_features   pf ON pf.product_id = u.pid
  ),
  nn AS (
    -- per-query min-max 정규화 → [0,1] (스케일 상이한 두 거리를 동렬화)
    SELECT dd.*,
           (d_img - min(d_img) OVER()) / NULLIF(max(d_img) OVER() - min(d_img) OVER(), 0) AS nzi,
           (d_txt - min(d_txt) OVER()) / NULLIF(max(d_txt) OVER() - min(d_txt) OVER(), 0) AS nzt
    FROM dd
  )
  SELECT nn.pid AS id, nn.brand, nn.name, nn.price, nn.image_url, nn.product_url,
         nn.platform, nn.subcategory,
         ((1.0 - p_w_text) * COALESCE(nn.nzi, 0.0) + p_w_text * COALESCE(nn.nzt, 0.0)) AS distance,
         false AS degraded
  FROM nn
  ORDER BY (1.0 - p_w_text) * COALESCE(nn.nzi, 0.0) + p_w_text * COALESCE(nn.nzt, 0.0) ASC
  LIMIT p_limit;
END;
$function$;

COMMIT;

NOTIFY pgrst, 'reload schema';
