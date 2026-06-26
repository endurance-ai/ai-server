"""In-App Purchase API (명세 계약 — subscription 모델).

GET  /v1/iap/products   — 구독 상품 목록 (명세 shape)
POST /v1/iap/verify     — StoreKit 2 JWS 트랜잭션 검증 + 구독 활성화
POST /v1/iap/restore    — 기존 구독 복원
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from jose import JWTError
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from app.api.deps import get_current_user_id
from app.core.apple_iap import TransactionInfo, decode_apple_jws, parse_transaction
from app.core.di import provide_db_pool
from app.core.iap_catalog import catalog_response
from app.services import subscription_service
from app.services.subscription_service import SubState

router = APIRouter(prefix="/v1/iap", tags=["iap"])


# ── Schemas ───────────────────────────────────────────────────────────────────


class IapProduct(BaseModel):
    product_id: str
    name: str
    description: str
    period: str  # 'P1M' | 'P1Y'
    price_tier: str
    currency_hint: str
    eula_url: str
    terms_url: str


class SubscriptionPayload(BaseModel):
    product_id: str
    status: str  # 'active' | 'grace' | 'expired' | 'revoked'
    expires_at: str | None
    auto_renew: bool
    original_transaction_id: str


class VerifyRequest(BaseModel):
    signed_transaction_jws: str
    transaction_id: str | None = None  # 교차검증용(선택)


class VerifyResponse(BaseModel):
    subscription: SubscriptionPayload


class RestoreTransaction(BaseModel):
    signed_transaction_jws: str
    transaction_id: str | None = None


class RestoreRequest(BaseModel):
    transactions: list[RestoreTransaction]


class RestoreResponse(BaseModel):
    restored: bool
    subscription: SubscriptionPayload | None


# ── Helpers ───────────────────────────────────────────────────────────────────


def _decode_transaction(jws: str) -> TransactionInfo:
    try:
        return parse_transaction(decode_apple_jws(jws))
    except (JWTError, KeyError, ValueError) as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"유효하지 않은 JWS 트랜잭션: {e}",
        ) from e


def _to_payload(state: SubState) -> SubscriptionPayload:
    return SubscriptionPayload(
        product_id=state.product_id,
        status=state.status,
        expires_at=state.expires_at.isoformat() if state.expires_at else None,
        auto_renew=state.auto_renew,
        original_transaction_id=state.original_transaction_id,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/products", response_model=list[IapProduct], status_code=status.HTTP_200_OK)
async def get_iap_products(
    _: UUID = Depends(get_current_user_id),
) -> list[IapProduct]:
    """구독 가능한 IAP 상품 목록 (구매 전 고지용)."""
    return [IapProduct(**p) for p in catalog_response()]


@router.post("/verify", response_model=VerifyResponse, status_code=status.HTTP_200_OK)
async def verify_transaction(
    body: VerifyRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> VerifyResponse:
    """StoreKit 2 JWS 트랜잭션 검증 후 구독 활성화."""
    txn = _decode_transaction(body.signed_transaction_jws)
    state = await subscription_service.upsert_from_transaction(pool, user_id, txn)
    return VerifyResponse(subscription=_to_payload(state))


@router.post("/restore", response_model=RestoreResponse, status_code=status.HTTP_200_OK)
async def restore_purchases(
    body: RestoreRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> RestoreResponse:
    """기존 구독 트랜잭션 목록으로 구독 복원. 만료일이 가장 늦은 구독을 대표값으로."""
    if not body.transactions:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="transactions 필드가 비어 있습니다.",
        )

    best: SubState | None = None
    for item in body.transactions:
        txn = _decode_transaction(item.signed_transaction_jws)
        state = await subscription_service.upsert_from_transaction(pool, user_id, txn, notification_type="RESTORE")
        if state.expires_at and (best is None or best.expires_at is None or state.expires_at > best.expires_at):
            best = state

    restored = best is not None and best.status in ("active", "grace")
    return RestoreResponse(restored=restored, subscription=_to_payload(best) if best else None)
