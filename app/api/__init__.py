from fastapi import APIRouter

from app.api.auth import router as auth_router
from app.api.chat import router as chat_router
from app.api.debug import router as debug_router
from app.api.devices import router as devices_router
from app.api.feedback import router as feedback_router
from app.api.health import router as health_router
from app.api.legal import router as legal_router
from app.api.me import router as me_router
from app.api.recommend import router as recommend_router
from app.api.redirect import router as redirect_router
from app.api.webhooks.telegram import router as telegram_webhook_router

router = APIRouter()
router.include_router(health_router, tags=["system"])
router.include_router(auth_router)
router.include_router(chat_router)
router.include_router(me_router)
router.include_router(feedback_router)
router.include_router(devices_router)
router.include_router(legal_router)
router.include_router(recommend_router, tags=["recommend"])
router.include_router(telegram_webhook_router)
router.include_router(debug_router)
router.include_router(redirect_router, tags=["redirect"])
