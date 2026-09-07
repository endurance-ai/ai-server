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

    @staticmethod
    def _parse_vector(raw: Any) -> list[float] | None:
        """pgvector/halfvec 컬럼 값 → list[float] 정규화.

        PostgREST 직렬화는 보통 "[0.1, 0.2, ...]" 텍스트로 떨어지지만, 드라이버/
        세팅에 따라 list/JSON array 로 오는 경로도 있어 둘 다 흡수한다.
        NULL/빈값/파싱 실패 → None."""
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
    async def get_product_embedding(cls, product_id: int) -> list[float] | None:
        """`public.product_embeddings.embedding` 단건 조회 (PostgREST select).

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
        return cls._parse_vector(rows[0].get("embedding"))

    @classmethod
    async def get_brand_centroid_embedding(cls, brand_names: list[str], *, limit: int = 300) -> list[float] | None:
        """브랜드 상품 이미지 임베딩(product_embeddings)의 centroid(평균) 벡터.

        brand-similar 경로용 — "X 같은 옷" 요청 시 X 브랜드 상품들의 임베딩
        평균을 앵커 벡터로 써서 결이 비슷한 상품을 유사도로 뽑는다. 같은
        FashionSigLIP L2 이미지 공간이라 v6 image-space 랭킹과 호환된다
        (override_embedding 경로 = 핀 상품 앵커와 동일 취급).

        `brand_names` 는 brand_node_cache 가 resolve 한 canonical 명(중복 노드
        포함). 행 없음/파싱 실패 → None (호출부 fail-open: 일반 텍스트 폴백).
        """
        if not brand_names:
            return None
        try:
            client = await cls.get_client()
            prod = await client.from_("products").select("id").in_("brand", brand_names).limit(limit).execute()
        except Exception:
            return None
        ids = [r.get("id") for r in (prod.data or []) if r.get("id") is not None]
        if not ids:
            return None
        try:
            res = await client.from_("product_embeddings").select("embedding").in_("product_id", ids).execute()
        except Exception:
            return None

        acc: list[float] | None = None
        n = 0
        for row in res.data or []:
            v = cls._parse_vector(row.get("embedding"))
            if v is None:
                continue
            if acc is None:
                acc = list(v)
                n = 1
            elif len(v) == len(acc):
                for i, x in enumerate(v):
                    acc[i] += x
                n += 1
        if acc is None or n == 0:
            return None
        return [x / n for x in acc]

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
