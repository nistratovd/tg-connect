from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.admin.routes import router as admin_router
from app.api.telegram_compat import router as telegram_compat_router
from app.middleware.security import SecurityMiddleware
from app.observability.routes import router as observability_router
from app.webhooks.bitrix_forwarder import process_pending_deliveries, router as bitrix_forwarder_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:  # noqa: ARG001 - FastAPI передает приложение в lifespan.
    """После рестарта добираем persistent outbox, который не был доставлен."""
    await process_pending_deliveries()
    yield


app = FastAPI(title="TG Connect", lifespan=lifespan)
app.add_middleware(SecurityMiddleware)
app.include_router(admin_router)
app.include_router(telegram_compat_router)
app.include_router(bitrix_forwarder_router)
app.include_router(observability_router)
