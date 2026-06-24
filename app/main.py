from fastapi import FastAPI

from app.admin.routes import router as admin_router
from app.api.telegram_compat import router as telegram_compat_router
from app.middleware.security import SecurityMiddleware
from app.observability.routes import router as observability_router
from app.webhooks.bitrix_forwarder import router as bitrix_forwarder_router

app = FastAPI(title="TG Connect")
app.add_middleware(SecurityMiddleware)
app.include_router(admin_router)
app.include_router(telegram_compat_router)
app.include_router(bitrix_forwarder_router)
app.include_router(observability_router)
