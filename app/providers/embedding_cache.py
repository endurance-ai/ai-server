"""Text-query 임베딩 캐시 (ai.embedding_cache_text).

목적: Modal /embed/text 호출 결과를 PG에 영구 캐싱. 같은 모델·같은 정규화 텍스트
입력은 deterministic 으로 동일 벡터를 내므로 캐싱 안전.

설계 원칙:
- fail-open: 캐시 lookup/put 실패가 임베드 본 경로를 막지 않는다 (WARNING 로그만).
- 정규화는 보수적으로 (strip + casefold) — 의미 차이 보존.
- ON CONFLICT DO NOTHING — 동시 insert race-safe.
- hit_count / last_used_at 갱신은 별도 fire-and-forget (응답 latency 영향 0).

Migration: 0007_create_embedding_cache_text.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging

from app.providers.db_pool import get_pool

logger = logging.getLogger(__name__)

# 모델 / 정규화 버전 — caller 가 명시. 캐시 무효화는 이 값을 새로 쓰면 끝.
MODEL_VER_FASHIONSIGLIP = "fashionsiglip-v1"
NORMALIZE_VER = "v1"  # strip + casefold


def _normalize(text: str) -> str:
    """보수적 정규화: 앞뒤 공백 제거 + casefold (lower 보다 다국어 안전)."""
    return text.strip().casefold()


def _hash(normalized: str) -> bytes:
    return hashlib.sha256(normalized.encode("utf-8")).digest()


async def get_cached(
    text: str,
    model_ver: str = MODEL_VER_FASHIONSIGLIP,
) -> list[float] | None:
    """캐시 hit 시 768-dim 벡터 반환, miss 또는 에러 시 None.

    hit 시 hit_count++ / last_used_at=now() 는 fire-and-forget 으로 갱신
    (응답 지연 0).
    """
    norm = _normalize(text)
    h = _hash(norm)
    try:
        pool = get_pool()
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT embedding::text
                FROM ai.embedding_cache_text
                WHERE model_ver=%s AND normalize_ver=%s AND query_hash=%s
                """,
                (model_ver, NORMALIZE_VER, h),
            )
            row = await cur.fetchone()
            if not row:
                return None
            # pgvector text repr: "[0.1,0.2,...]"
            vec_str = row[0]
            embedding = [float(x) for x in vec_str.strip("[]").split(",")]
            _schedule_touch(model_ver, h)
            return embedding
    except Exception as exc:  # noqa: BLE001
        logger.warning("[embed_cache] get failed (fail-open): %r", exc)
        return None


async def put_cached(
    text: str,
    embedding: list[float],
    model_ver: str = MODEL_VER_FASHIONSIGLIP,
) -> None:
    """캐시 저장 (ON CONFLICT DO NOTHING). 실패는 silent (WARNING)."""
    norm = _normalize(text)
    h = _hash(norm)
    vec_literal = "[" + ",".join(f"{x:.8f}" for x in embedding) + "]"
    try:
        pool = get_pool()
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO ai.embedding_cache_text
                    (model_ver, normalize_ver, query_hash, query_text, embedding)
                VALUES (%s, %s, %s, %s, %s::vector)
                ON CONFLICT (model_ver, normalize_ver, query_hash) DO NOTHING
                """,
                (model_ver, NORMALIZE_VER, h, text, vec_literal),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[embed_cache] put failed (fail-open): %r", exc)


def _schedule_touch(model_ver: str, h: bytes) -> None:
    """hit_count++ / last_used_at=now() 비동기 fire-and-forget."""
    try:
        asyncio.create_task(_touch(model_ver, h))
    except RuntimeError:
        # 이벤트 루프 없음 (테스트 등) — 무시
        pass


async def _touch(model_ver: str, h: bytes) -> None:
    try:
        pool = get_pool()
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE ai.embedding_cache_text
                SET hit_count = hit_count + 1, last_used_at = now()
                WHERE model_ver=%s AND normalize_ver=%s AND query_hash=%s
                """,
                (model_ver, NORMALIZE_VER, h),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[embed_cache] touch failed (silent): %r", exc)
