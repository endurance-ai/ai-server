"""In-App Purchase API.

GET  /v1/iap/products   — 구독 상품 목록
POST /v1/iap/verify     — StoreKit 2 JWS 트랜잭션 검증 + 구독 활성화
POST /v1/iap/restore    — 기존 구독 복원
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from jose import JWTError
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from app.api.deps import get_current_user_id
from app.core.apple_iap import TransactionInfo, decode_apple_jws, parse_transaction
from app.core.di import provide_db_pool

router = APIRouter(prefix="/v1/iap", tags=["iap"])

# ── 상품 카탈로그 (App Store Connect와 동일한 product_id 사용) ──────────────

_PRODUCTS = [
    {
        "product_id": "com.kiko.subscription.basic.monthly",
        "tier": "basic",
        "display_name": "Basic 월간",
        "duration_days": 30,
        "price_krw": 9900,
    },
    {
        "product_id": "com.kiko.subscription.pro.monthly",
        "tier": "pro",
        "display_name": "Pro 월간",
        "duration_days": 30,
        "price_krw": 19900,
    },
    {
        "product_id": "com.kiko.subscription.pro.yearly",
        "tier": "pro",
        "display_name": "Pro 연간",
        "duration_days": 365,
        "price_krw": 169000,
    },
]

_PRODUCT_MAP: dict[str, dict] = {p["product_id"]: p for p in _PRODUCTS}


# ── Schemas ───────────────────────────────────────────────────────────────────


class IapProduct(BaseModel):
    product_id: str
    tier: str
    display_name: str
    duration_days: int
    price_krw: int


class IapProductsResponse(BaseModel):
    products: list[IapProduct]


class VerifyRequest(BaseModel):
    jws_transaction: str


class VerifyResponse(BaseModel):
    tier: str
    tier_expires_at: str | None
    transaction_id: str


class RestoreRequest(BaseModel):
    jws_transactions: list[str]


class RestoreResponse(BaseModel):
    restored: bool
    tier: str
    tier_expires_at: str | None


# ── Helpers ───────────────────────────────────────────────────────────────────


def _decode_transaction(jws: str) -> TransactionInfo:
    try:
        payload = decode_apple_jws(jws)
        return parse_transaction(payload)
    except (JWTError, KeyError, ValueError) as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"유효하지 않은 JWS 트랜잭션: {e}",
        ) from e


async def _activate_subscription(
    pool: AsyncConnectionPool,
    user_id: UUID,
    txn: TransactionInfo,
    notification_type: str | None = None,
) -> tuple[str, datetime | None]:
    """트랜잭션 기록 + user_profiles 티어 업데이트. (tier, tier_expires_at) 반환."""
    product = _PRODUCT_MAP.get(txn.product_id)
    tier = product["tier"] if product else "basic"

    # expires_date가 없으면 duration_days로 계산
    if txn.expires_date:
        expires_at = txn.expires_date
    elif product:
        expires_at = datetime.now(UTC) + timedelta(days=product["duration_days"])
    else:
        expires_at = None

    async with pool.connection() as conn, conn.cursor() as cur:
        # 트랜잭션 기록 (중복 무시)
        await cur.execute(
            """
            INSERT INTO ai.iap_transactions
                (user_id, apple_txn_id, apple_original_txn_id, product_id,
                 tier, purchase_date, expires_date, environment, notification_type)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (apple_txn_id) DO NOTHING
            """,
            (
                user_id,
                txn.transaction_id,
                txn.original_transaction_id,
                txn.product_id,
                tier,
                txn.purchase_date,
                expires_at,
                txn.environment,
                notification_type,
            ),
        )
        # 유저 티어 업데이트 (더 높거나 만료일이 더 늦을 때만)
        await cur.execute(
            """
            UPDATE ai.user_profiles
            SET tier = %s,
                tier_expires_at = %s,
                updated_at = now()
            WHERE user_id = %s
              AND (tier_expires_at IS NULL OR tier_expires_at < %s)
            """,
            (tier, expires_at, user_id, expires_at or datetime.now(UTC)),
        )

    return tier, expires_at


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/products", response_model=IapProductsResponse, status_code=status.HTTP_200_OK)
async def get_iap_products(
    _: UUID = Depends(get_current_user_id),
) -> IapProductsResponse:
    """구독 가능한 IAP 상품 목록."""
    return IapProductsResponse(products=[IapProduct(**p) for p in _PRODUCTS])


@router.post("/verify", response_model=VerifyResponse, status_code=status.HTTP_200_OK)
async def verify_transaction(
    body: VerifyRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> VerifyResponse:
    """StoreKit 2 JWS 트랜잭션 검증 후 구독 활성화."""
    txn = _decode_transaction(body.jws_transaction)
    tier, expires_at = await _activate_subscription(pool, user_id, txn)
    return VerifyResponse(
        tier=tier,
        tier_expires_at=expires_at.isoformat() if expires_at else None,
        transaction_id=txn.transaction_id,
    )


@router.post("/restore", response_model=RestoreResponse, status_code=status.HTTP_200_OK)
async def restore_purchases(
    body: RestoreRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> RestoreResponse:
    """기존 구독 트랜잭션 목록으로 구독 복원."""
    if not body.jws_transactions:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="jws_transactions 필드가 비어 있습니다.",
        )

    best_tier: str = "free"
    best_expires: datetime | None = None

    for jws in body.jws_transactions:
        txn = _decode_transaction(jws)
        tier, expires_at = await _activate_subscription(pool, user_id, txn, notification_type="RESTORE")
        # 만료일이 가장 늦은 구독을 대표값으로
        if expires_at and (best_expires is None or expires_at > best_expires):
            best_tier = tier
            best_expires = expires_at

    restored = best_expires is not None and best_expires > datetime.now(UTC)
    return RestoreResponse(
        restored=restored,
        tier=best_tier if restored else "free",
        tier_expires_at=best_expires.isoformat() if best_expires else None,
    )
