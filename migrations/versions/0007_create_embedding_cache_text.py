"""create embedding_cache_text

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-20

Text-query 임베딩 캐시. Modal /embed/text 호출 결과(768-dim FashionSigLIP 벡터)
를 영구 캐싱해 cold-start 26초 / 정상 ~200ms 호출을 PG 인덱스 lookup (~3ms)으로
대체한다. 모델이 deterministic 이므로 (model_ver, normalize_ver, query_hash)
조합이 동일하면 벡터도 비트단위 동일 — 캐시 무효 위험 없음.

무효화 정책:
- 모델 가중치 교체: `model_ver` 새 값으로 caller 변경 → 옛 row 자연히 miss
- 정규화 로직 변경: `normalize_ver` 새 값으로 caller 변경 → 옛 row 자연히 miss
- 옛 row 정리는 별도 housekeeping(미구현, 누적량 보면서 결정)

pgvector 활성화: dev-app PG 는 search_products_v6 RPC 가 이미 pgvector 를
사용 중이라 extension 이 설치돼있음 — `CREATE EXTENSION IF NOT EXISTS` 는
no-op (CREATE 권한 불필요). CI testcontainer (pgvector/pgvector:pg16) 에서는
testcontainers 의 기본 user(test) 가 DB owner 라 활성화 가능.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # pgvector 활성화 — IF NOT EXISTS 라 dev-app(이미 설치) 에선 no-op,
    # CI testcontainers 에선 새로 활성화. fully-qualified `public.vector` 로
    # 같은 transaction 내 catalog snapshot 갱신 race 회피
    # (CI 에서 unqualified `vector` 가 catalog 갱신 race 로 not-found 발생).
    op.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai.embedding_cache_text (
            model_ver       text       NOT NULL,
            normalize_ver   text       NOT NULL,
            query_hash      bytea      NOT NULL,
            query_text      text       NOT NULL,
            embedding       public.vector(768) NOT NULL,
            hit_count       integer    NOT NULL DEFAULT 0,
            created_at      timestamptz NOT NULL DEFAULT now(),
            last_used_at    timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (model_ver, normalize_ver, query_hash)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai.embedding_cache_text")
