"""App-facing gender vocabulary mapping.

앱/스펙 어휘는 `women | men` (products.gender, 온보딩, 큐레이션 쿼리 파라미터).
`ai.user_profiles.gender` CHECK 제약은 `male | female | other` (migration 0009).
DB 마이그레이션 없이 API 경계에서 양방향 매핑한다.
"""

from __future__ import annotations

APP_TO_DB: dict[str, str] = {"women": "female", "men": "male"}
DB_TO_APP: dict[str, str] = {"female": "women", "male": "men"}


def app_to_db(gender: str) -> str:
    """'women'/'men' → 'female'/'male'. Unknown values pass through unchanged."""
    return APP_TO_DB.get(gender, gender)


def db_to_app(gender: str | None) -> str | None:
    """'female'/'male' → 'women'/'men'. 'other'/None → None (큐레이션 성별 미확정)."""
    if gender is None:
        return None
    return DB_TO_APP.get(gender)
