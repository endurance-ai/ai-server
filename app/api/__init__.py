from fastapi import APIRouter

from app.api.health import router as health_router
from app.api.recommend import router as recommend_router

router = APIRouter()
router.include_router(health_router, tags=["system"])
router.include_router(recommend_router, tags=["recommend"])
