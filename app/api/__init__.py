from fastapi import APIRouter

from app.api.auth import router as auth_router
from app.api.brands import router as brands_router
from app.api.chat import router as chat_router
from app.api.curation import router as curation_router
from app.api.debug import router as debug_router
from app.api.devices import router as devices_router
from app.api.feedback import router as feedback_router
from app.api.health import router as health_router
from app.api.history import router as history_router
from app.api.iap import router as iap_router
from app.api.legal import router as legal_router
from app.api.me import router as me_router
from app.api.onboarding import router as onboarding_router
from app.api.products import router as products_router
from app.api.recommend import router as recommend_router
from app.api.redirect import router as redirect_router
from app.api.results import router as results_router
from app.api.saves import router as saves_router
from app.api.style_nodes import router as style_nodes_router
from app.api.subscription import router as subscription_router
from app.api.uploads import router as uploads_router
from app.api.webhooks.apple_notifications import router as apple_webhook_router
from app.api.webhooks.telegram import router as telegram_webhook_router

router = APIRouter()
router.include_router(health_router, tags=["system"])
router.include_router(auth_router)
router.include_router(chat_router)
router.include_router(me_router)
router.include_router(onboarding_router)
router.include_router(brands_router)
router.include_router(curation_router)
router.include_router(saves_router)
router.include_router(style_nodes_router)
router.include_router(products_router)
router.include_router(results_router)
router.include_router(history_router)
router.include_router(feedback_router)
router.include_router(devices_router)
router.include_router(legal_router)
router.include_router(iap_router)
router.include_router(subscription_router)
router.include_router(uploads_router)
router.include_router(recommend_router, tags=["recommend"])
router.include_router(telegram_webhook_router)
router.include_router(apple_webhook_router)
router.include_router(debug_router)
router.include_router(redirect_router, tags=["redirect"])
