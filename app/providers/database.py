from typing import Any, ClassVar

from supabase._async.client import AsyncClient, create_client

from app.core.config import settings


class DatabaseProvider:
    """DB async 클라이언트 (싱글톤). 검색 RPC + 상품 메타 조회."""

    _client: ClassVar[AsyncClient | None] = None

    @classmethod
    async def get_client(cls) -> AsyncClient:
        if cls._client is None:
            cls._client = await create_client(
                settings.DB_URL,
                settings.DB_TOKEN,
            )
        return cls._client

    @classmethod
    async def check_connection(cls) -> bool:
        """DB 연결 확인. service role로 가벼운 쿼리 수행."""
        try:
            client = await cls.get_client()
            # 가벼운 SELECT — products 테이블 (읽기 전용, 항상 존재하는 관계)
            await client.from_("products").select("id").limit(1).execute()
            return True
        except Exception:
            return False

    @classmethod
    async def rpc(cls, fn_name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """RPC 함수 호출 헬퍼. data 리스트만 반환."""
        client = await cls.get_client()
        res = await client.rpc(fn_name, params).execute()
        return res.data or []

    @classmethod
    async def get_product_embedding(cls, product_id: int) -> list[float] | None:
        """`public.product_embeddings.embedding` 단건 조회 (PostgREST select).

        pgvector 컬럼은 PostgREST 직렬화에서 보통 "[0.1, 0.2, ...]" 형태의 텍스트
        로 떨어진다. 이미 list/tuple/JSON array 로 오는 경로(드라이버/세팅 차이
        가능)도 일관되게 list[float] 로 정규화한다.

        반환: 임베딩 list[float] / 행 없음 또는 NULL → None / 파싱 실패 → None.
        호출부는 fail-open(None 처리) 으로 기존 텍스트 경로 폴백.
        """
        try:
            client = await cls.get_client()
            res = (
                await client.from_("product_embeddings")
                .select("embedding")
                .eq("product_id", product_id)
                .limit(1)
                .execute()
            )
        except Exception:
            return None

        rows = res.data or []
        if not rows:
            return None
        raw = rows[0].get("embedding")
        if raw is None:
            return None

        if isinstance(raw, list):
            try:
                return [float(x) for x in raw]
            except (TypeError, ValueError):
                return None

        if isinstance(raw, str):
            stripped = raw.strip().lstrip("[").rstrip("]")
            if not stripped:
                return None
            try:
                return [float(x) for x in stripped.split(",")]
            except ValueError:
                return None

        return None

    @classmethod
    async def get_product_category(cls, product_id: int) -> str | None:
        """`public.products.category` 단건 조회 (PostgREST select).

        refine_search 의 `#id` anchor 경로에서 쓰임. 이전 턴의
        `ctx.vision_category` 가 새로운 핀 상품과 카테고리가 다르면
        v6 RPC family gate 가 잘못된 family 로 필터링해서 결과가
        오염되는 문제 (예: 직전 니트 검색 → 청바지 핀 → 결과 니트들로 채워짐).

        반환: 카테고리 문자열 / 행 없음 또는 NULL → None / 실패 → None.
        호출부는 fail-open(None 처리) 으로 기존 ctx 카테고리 폴백.
        """
        try:
            client = await cls.get_client()
            res = await client.from_("products").select("category").eq("id", product_id).limit(1).execute()
        except Exception:
            return None

        rows = res.data or []
        if not rows:
            return None
        raw = rows[0].get("category")
        if raw is None:
            return None
        s = str(raw).strip()
        return s or None

    @classmethod
    async def close(cls) -> None:
        # supabase-py async 클라이언트는 명시적 close 불필요 (httpx 재사용)
        cls._client = None
