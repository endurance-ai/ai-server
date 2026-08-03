"""IAP 상품 카탈로그 (단일 소스).

App Store Connect 에 등록된 product_id 와 동일해야 한다. 명세상 iap_products
테이블(ASC 시드 캐시)이 있으나, MVP 는 코드 상수 카탈로그로 유지하고 응답만
명세 shape 으로 노출한다. tier / 기간 도출도 여기서 단일화한다.
"""

from __future__ import annotations

_EULA_URL = "https://www.apple.com/legal/internet-services/itunes/dev/stdeula/"
_TERMS_URL = "https://kiko.ai/legal/terms"

# period: ISO-8601 duration. duration_days: expiresDate 누락 시 폴백 계산용.
PRODUCTS: list[dict] = [
    {
        "product_id": "com.kiko.subscription.basic.monthly",
        "tier": "basic",
        "name": "Basic 월간",
        "description": "Basic 구독 (월간)",
        "period": "P1M",
        "price_tier": "tier_9900",
        "currency_hint": "KRW",
        "duration_days": 30,
    },
    {
        "product_id": "com.kiko.subscription.pro.monthly",
        "tier": "pro",
        "name": "Pro 월간",
        "description": "Pro 구독 (월간)",
        "period": "P1M",
        "price_tier": "tier_19900",
        "currency_hint": "KRW",
        "duration_days": 30,
    },
    {
        "product_id": "com.kiko.subscription.pro.yearly",
        "tier": "pro",
        "name": "Pro 연간",
        "description": "Pro 구독 (연간)",
        "period": "P1Y",
        "price_tier": "tier_169000",
        "currency_hint": "KRW",
        "duration_days": 365,
    },
]

PRODUCT_MAP: dict[str, dict] = {p["product_id"]: p for p in PRODUCTS}


def tier_for(product_id: str) -> str:
    """product_id → tier. 미지의 상품은 'basic' 으로 안전 폴백."""
    return PRODUCT_MAP.get(product_id, {}).get("tier", "basic")


def duration_days(product_id: str) -> int:
    return PRODUCT_MAP.get(product_id, {}).get("duration_days", 30)


def catalog_response() -> list[dict]:
    """명세 List IAP Products shape."""
    return [
        {
            "product_id": p["product_id"],
            "name": p["name"],
            "description": p["description"],
            "period": p["period"],
            "price_tier": p["price_tier"],
            "currency_hint": p["currency_hint"],
            "eula_url": _EULA_URL,
            "terms_url": _TERMS_URL,
        }
        for p in PRODUCTS
    ]
