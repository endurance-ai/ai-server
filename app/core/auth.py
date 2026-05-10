"""Internal shared-secret 인증.

kikoai/app(Next.js) → AI 서버 호출용. INTERNAL_API_TOKEN 미설정 시(dev) 검증 스킵.
운영에선 반드시 설정 — 미설정 + production 환경 조합은 startup에서 차단(아래 verify).
"""

from fastapi import Header, HTTPException, status

from app.core.config import settings


async def verify_internal_token(
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> None:
    """X-Internal-Token 헤더 검증. 토큰 미설정이면 dev로 간주하여 통과."""
    expected = settings.INTERNAL_API_TOKEN
    if not expected:
        return
    if x_internal_token != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unauthorized",
        )
