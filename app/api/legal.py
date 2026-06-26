"""Legal consent API.

GET  /v1/legal/versions  — 현재/최신 약관 버전 + 내 동의 이력
POST /v1/legal/consents  — 약관 동의 기록
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from app.api.deps import get_current_user_id
from app.core.di import provide_db_pool

router = APIRouter(prefix="/v1/legal", tags=["legal"])

# 약관/처리방침 현재 버전 (변경 시 이 값 업데이트 → 30일 내 재동의 팝업)
_CURRENT_VERSIONS = {"tos": "v1.0", "privacy": "v1.0"}
_VALID_DOC_TYPES = {"tos", "privacy"}


# ── Schemas ───────────────────────────────────────────────────────────────────


class LegalVersionSet(BaseModel):
    tos: str
    privacy: str


class ConsentRecord(BaseModel):
    document_type: str
    version: str
    consented_at: str


class LegalVersionsResponse(BaseModel):
    current: LegalVersionSet
    latest: LegalVersionSet
    my_consents: list[ConsentRecord]


class RecordConsentRequest(BaseModel):
    document_type: str
    version: str


class RecordConsentResponse(BaseModel):
    consented_at: str


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/versions", response_model=LegalVersionsResponse, status_code=status.HTTP_200_OK)
async def get_legal_versions(
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> LegalVersionsResponse:
    """현재/최신 약관 버전과 사용자의 동의 이력 반환."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT DISTINCT ON (document_type)
                document_type, version, consented_at
            FROM ai.legal_consents
            WHERE user_id = %s
            ORDER BY document_type, consented_at DESC
            """,
            (user_id,),
        )
        rows = await cur.fetchall()

    my_consents = [ConsentRecord(document_type=r[0], version=r[1], consented_at=r[2].isoformat()) for r in rows]

    version_set = LegalVersionSet(**_CURRENT_VERSIONS)
    return LegalVersionsResponse(current=version_set, latest=version_set, my_consents=my_consents)


@router.post("/consents", response_model=RecordConsentResponse, status_code=status.HTTP_201_CREATED)
async def record_consent(
    body: RecordConsentRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> RecordConsentResponse:
    """약관 동의 기록."""
    if body.document_type not in _VALID_DOC_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"document_type must be one of {sorted(_VALID_DOC_TYPES)}",
        )

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.legal_consents (user_id, document_type, version)
            VALUES (%s, %s, %s)
            RETURNING consented_at
            """,
            (user_id, body.document_type, body.version),
        )
        row = await cur.fetchone()

    return RecordConsentResponse(consented_at=row[0].isoformat())
