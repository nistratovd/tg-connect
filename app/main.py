from fastapi import FastAPI

from app.api.telegram_compat import router as telegram_compat_router

app = FastAPI(title="TG Connect")
app.include_router(telegram_compat_router)
