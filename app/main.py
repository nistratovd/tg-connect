from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.admin.routes import router as admin_router
from app.api.telegram_compat import router as telegram_compat_router
from app.middleware.security import SecurityMiddleware
from app.observability.routes import router as observability_router
from app.services.telegram_runner import telegram_runner
from app.services.wireguard import wireguard_manager
from app.webhooks.bitrix_forwarder import process_pending_deliveries, router as bitrix_forwarder_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:  # noqa: ARG001 - FastAPI передает приложение в lifespan.
    """Восстанавливает outbox и запускает long polling runners для настроенных ботов."""
    await wireguard_manager.ensure_started()
    await process_pending_deliveries()
    await telegram_runner.start()
    try:
        yield
    finally:
        await telegram_runner.stop()
        await wireguard_manager.stop()


app = FastAPI(title="TG Connect", lifespan=lifespan)
app.add_middleware(SecurityMiddleware)
app.include_router(admin_router)
app.include_router(telegram_compat_router)
app.include_router(bitrix_forwarder_router)
app.include_router(observability_router)
