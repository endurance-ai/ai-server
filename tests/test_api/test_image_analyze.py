"""POST /v1/image/analyze API tests."""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient

from app.api.deps import get_current_user_id
from app.channels.vision import (
    VisionItem,
    VisionMood,
    VisionMoodTag,
    VisionPosition,
    VisionResult,
    VisionStyleNode,
)
from app.main import app


@pytest.fixture
def authenticated_user():
    app.dependency_overrides[get_current_user_id] = lambda: uuid4()
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_user_id, None)


@pytest.mark.asyncio
async def test_image_analyze_returns_app_contract(
    client: AsyncClient,
    authenticated_user,
    monkeypatch: pytest.MonkeyPatch,
):
    async def _extract(_image_url: str) -> VisionResult:
        return VisionResult(
            isApparel=True,
            styleNode=VisionStyleNode(primary="C", secondary="D"),
            mood=VisionMood(
                tags=[
                    VisionMoodTag(label="Minimalist", score=95),
                    VisionMoodTag(label="", score=20),
                    VisionMoodTag(label="Street", score=80),
                ]
            ),
            items=[
                VisionItem(
                    name="Boxy Graphic Tee",
                    searchQueryKo="박시 크롭 블랙 그래픽 티셔츠 여성",
                    searchQuery="boxy cropped black graphic t-shirt women",
                    category="Top",
                    subcategory="t-shirt",
                    fit="boxy",
                    color="Black",
                    position=VisionPosition(top=42.5, left=48.0),
                )
            ],
        )

    monkeypatch.setattr("app.api.image.vision_extract", _extract)

    response = await client.post(
        "/v1/image/analyze",
        json={"image_url": "https://cdn.example.com/uploads/user/look.jpg"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "name": "Boxy Graphic Tee",
                "searchQueryKo": "박시 크롭 블랙 그래픽 티셔츠 여성",
                "searchQuery": "boxy cropped black graphic t-shirt women",
                "category": "Top",
                "subcategory": "t-shirt",
                "fit": "boxy",
                "color": "Black",
                "position": {"top": 42.5, "left": 48.0},
            }
        ],
        "mood_tags": ["Minimalist", "Street"],
        "style_node": "C",
    }


@pytest.mark.asyncio
async def test_image_analyze_requires_auth(client: AsyncClient):
    response = await client.post(
        "/v1/image/analyze",
        json={"image_url": "https://cdn.example.com/uploads/user/look.jpg"},
    )

    assert response.status_code in (401, 403)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "image_url",
    [
        "http://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
    ],
)
async def test_image_analyze_blocks_internal_url_before_extract(
    client: AsyncClient,
    authenticated_user,
    monkeypatch: pytest.MonkeyPatch,
    image_url: str,
):
    called = False

    async def _extract(_image_url: str) -> VisionResult:
        nonlocal called
        called = True
        return VisionResult()

    monkeypatch.setattr("app.api.image.vision_extract", _extract)

    response = await client.post(
        "/v1/image/analyze",
        json={"image_url": image_url},
    )

    assert response.status_code == 400
    assert response.json()["detail"].startswith("ssrf_blocked:")
    assert called is False


@pytest.mark.asyncio
async def test_image_analyze_serializes_empty_fallback(
    client: AsyncClient,
    authenticated_user,
    monkeypatch: pytest.MonkeyPatch,
):
    async def _extract(_image_url: str) -> VisionResult:
        return VisionResult()

    monkeypatch.setattr("app.api.image.vision_extract", _extract)

    response = await client.post(
        "/v1/image/analyze",
        json={"image_url": "https://cdn.example.com/uploads/user/not-apparel.jpg"},
    )

    assert response.status_code == 200
    assert response.json() == {"items": [], "mood_tags": [], "style_node": "C"}
