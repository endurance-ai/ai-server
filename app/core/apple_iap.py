"""Apple StoreKit 2 JWS 검증 유틸리티.

Apple이 서명한 JWSTransaction / signedPayload를 디코드합니다.
x5c 헤더의 leaf cert 공개키(ES256)로 서명을 검증합니다.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography import x509
from jose import JWTError
from jose import jwt as jose_jwt


@dataclass
class TransactionInfo:
    transaction_id: str
    original_transaction_id: str
    product_id: str
    purchase_date: datetime
    expires_date: datetime | None
    environment: str  # 'Sandbox' | 'Production'


@dataclass
class RenewalInfo:
    """Apple signedRenewalInfo — 자동갱신/유예기간 상태."""

    auto_renew: bool
    grace_period_expires_date: datetime | None
    original_transaction_id: str | None


def decode_apple_jws(token: str) -> dict:
    """Apple JWS(ES256) 디코드 및 서명 검증."""
    header = jose_jwt.get_unverified_header(token)
    x5c = header.get("x5c", [])
    if not x5c:
        raise JWTError("x5c header missing in Apple JWS")

    cert_bytes = base64.b64decode(x5c[0])
    cert = x509.load_der_x509_certificate(cert_bytes)
    public_key = cert.public_key()

    return jose_jwt.decode(
        token,
        public_key,
        algorithms=["ES256"],
        options={"verify_aud": False},
    )


def parse_transaction(payload: dict) -> TransactionInfo:
    """JWS 트랜잭션 페이로드 → TransactionInfo."""
    expires_ms = payload.get("expiresDate")
    return TransactionInfo(
        transaction_id=str(payload["transactionId"]),
        original_transaction_id=str(payload["originalTransactionId"]),
        product_id=payload["productId"],
        purchase_date=datetime.fromtimestamp(payload["purchaseDate"] / 1000, tz=UTC),
        expires_date=datetime.fromtimestamp(expires_ms / 1000, tz=UTC) if expires_ms else None,
        environment=payload.get("environment", "Production"),
    )


def parse_renewal_info(payload: dict) -> RenewalInfo:
    """JWS 갱신정보 페이로드 → RenewalInfo.

    autoRenewStatus: 1=on, 0=off. gracePeriodExpiresDate: 결제 재시도 유예 만료(ms).
    """
    grace_ms = payload.get("gracePeriodExpiresDate")
    oti = payload.get("originalTransactionId")
    return RenewalInfo(
        auto_renew=str(payload.get("autoRenewStatus", "1")) == "1",
        grace_period_expires_date=datetime.fromtimestamp(grace_ms / 1000, tz=UTC) if grace_ms else None,
        original_transaction_id=str(oti) if oti else None,
    )
