"""App config API — 모바일 버전 게이트용.

GET /v1/app/config — iOS 최소/최신 버전 + App Store URL (무인증).

모바일 앱이 실행 시 이 값을 받아 설치 버전과 비교한다:
  - 설치 < min_version    → 강제 업데이트(차단 모달)
  - min ≤ 설치 < latest   → 권장 업데이트(닫기 가능 모달)

값 변경은 아래 상수만 수정 → dev 머지 → 자동배포로 전체 사용자에 반영된다
(별도 앱 릴리스 불필요 — legal.py 의 약관 버전 상수와 동일 패턴).
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/v1/app", tags=["app"])

# App Store ID = eas.json submit.production.ios.ascAppId. itms-apps 스킴은
# App Store 앱을 바로 연다(웹 브라우저 우회).
_IOS_STORE_URL = "itms-apps://apps.apple.com/app/id6787153872"

# 강제/권장 업데이트 기준 버전. 값만 올리면 배포로 반영.
_IOS_MIN_VERSION = "1.0.0"
_IOS_LATEST_VERSION = "1.1.1"


class AppPlatformConfig(BaseModel):
    min_version: str
    latest_version: str
    store_url: str


class AppConfigResponse(BaseModel):
    ios: AppPlatformConfig


@router.get("/config", response_model=AppConfigResponse)
async def app_config() -> AppConfigResponse:
    """모바일 버전 게이트 설정. 인증 불필요 — 로그인 전에도 조회 가능해야 한다."""
    return AppConfigResponse(
        ios=AppPlatformConfig(
            min_version=_IOS_MIN_VERSION,
            latest_version=_IOS_LATEST_VERSION,
            store_url=_IOS_STORE_URL,
        )
    )
