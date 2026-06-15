from fastapi import FastAPI

from app.api.telegram_compat import router as telegram_compat_router
from app.webhooks.bitrix_forwarder import router as bitrix_forwarder_router

app = FastAPI(title="TG Connect")
app.include_router(telegram_compat_router)
app.include_router(bitrix_forwarder_router)
