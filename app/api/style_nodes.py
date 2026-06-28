from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.infrastructure.repositories import style_node

router = APIRouter(prefix="/v1", tags=["style-nodes"])


class StyleNodeItem(BaseModel):
    id: int
    code: str
    name_en: str | None
    keywords_en: list[str]


class StyleNodesResponse(BaseModel):
    nodes: list[StyleNodeItem]
    warmed: bool


@router.get("/style-nodes", response_model=StyleNodesResponse)
async def list_style_nodes() -> StyleNodesResponse:
    """Return the active style-node taxonomy loaded by the AI server."""
    return StyleNodesResponse(
        warmed=style_node.is_warmed(),
        nodes=[
            StyleNodeItem(
                id=node.id,
                code=node.code,
                name_en=node.name_en,
                keywords_en=list(node.keywords_en),
            )
            for node in style_node.list_nodes()
        ],
    )
