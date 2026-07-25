"""add product_features (VLM enrichment: retrieval_text + feature_metadata + text_embedding)

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-23

- public.product_features: 상품 이미지 VLM 1회 분석 산출물 저장.
  카탈로그 도메인이므로 ai. 스키마가 아닌 public 에 두어
  product_embeddings 와 동일 패턴(product_id PK, ON DELETE CASCADE)으로
  검색 RPC(search_products_v6+)가 JOIN 할 수 있게 한다.
  crawler upsert(products)와 컬럼을 공유하지 않으므로 크롤링 경로 무영향.
- retrieval_text: 검색 최적화 문장 (임베딩 생성 + LLM 입력 겸용).
- feature_metadata: primary_color/fit/material/pattern/silhouette/style_tags
  등 구조화 피처(jsonb). 스키마 강제는 배치 코드의 Pydantic 이 담당.
- text_embedding: retrieval_text 의 FashionSigLIP 텍스트 임베딩(768).
  nullable — VLM 생성 커밋 후 임베딩 배치가 뒤따라 채운다
  (증분 조건 WHERE text_embedding IS NULL).
- feature_version(프롬프트+스키마 세대) / vlm_model(생성 모델) 분리로
  재생성·증분 배치 대상 조회를 인덱스로 지원.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.product_features (
            product_id       BIGINT PRIMARY KEY
                             REFERENCES public.products(id) ON DELETE CASCADE,

            -- VLM 산출물 (1회 호출, 2개 산출)
            retrieval_text   TEXT NOT NULL,
            feature_metadata JSONB NOT NULL,

            -- retrieval_text 텍스트 임베딩 (이미지와 동일 768-dim 공간)
            text_embedding   HALFVEC(768),
            embedding_model  TEXT,

            -- 추적성 (재생성/증분 배치 기준)
            feature_version  TEXT NOT NULL,
            vlm_model        TEXT NOT NULL,
            generated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # 벡터 검색 — product_embeddings HNSW 와 동일 파라미터
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_product_features_text_hnsw
            ON public.product_features
            USING hnsw (text_embedding halfvec_cosine_ops)
            WITH (m = 16, ef_construction = 200)
        """
    )
    # re-ranking 의 feature 조건 조회 (@> containment)
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_product_features_metadata_gin
            ON public.product_features
            USING gin (feature_metadata jsonb_path_ops)
        """
    )
    # 버전 마이그레이션/증분 배치 대상 조회
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_product_features_version
            ON public.product_features (feature_version)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.product_features")
