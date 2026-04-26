from typing import Any, ClassVar

from supabase._async.client import AsyncClient, create_client

from app.core.config import settings


class SupabaseProvider:
    """Supabase async 클라이언트 (싱글톤). 검색 RPC + 상품 메타 조회."""

    _client: ClassVar[AsyncClient | None] = None

    @classmethod
    async def get_client(cls) -> AsyncClient:
        if cls._client is None:
            cls._client = await create_client(
                settings.SUPABASE_URL,
                settings.SUPABASE_SERVICE_ROLE_KEY,
            )
        return cls._client

    @classmethod
    async def check_connection(cls) -> bool:
        """Supabase 연결 확인. service role로 가벼운 쿼리 수행."""
        try:
            client = await cls.get_client()
            # 가벼운 SELECT — embedding coverage 뷰 (읽기 전용)
            await client.from_("product_embedding_coverage").select("platform").limit(1).execute()
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
    async def close(cls) -> None:
        # supabase-py async 클라이언트는 명시적 close 불필요 (httpx 재사용)
        cls._client = None
