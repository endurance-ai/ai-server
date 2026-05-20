from fastapi import APIRouter

from app.api.debug import router as debug_router
from app.api.health import router as health_router
from app.api.recommend import router as recommend_router
from app.api.webhooks.telegram import router as telegram_webhook_router

router = APIRouter()
router.include_router(health_router, tags=["system"])
router.include_router(recommend_router, tags=["recommend"])
router.include_router(telegram_webhook_router)
router.include_router(debug_router)
