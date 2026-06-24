from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from app.queue.delivery import delivery_queue
from app.services.bot_registry import registry
from app.services.idempotency import idempotency_store
from app.services.telegram_runner import telegram_runner

router = APIRouter(tags=["observability"])


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready() -> dict[str, object]:
    await registry.load_active_bots()
    polling = telegram_runner.status()
    return {"status": "ok", "active_bots": len(registry.active_configs()), "long_polling_bots": sum(1 for item in polling.values() if item.running)}


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics() -> str:
    queue_stats = delivery_queue.stats()
    idempotency_size = await idempotency_store.size()
    active_bots = len(registry.active_configs())
    lines = [
        "# HELP tg_connect_active_bots Active bot configurations loaded in registry.",
        "# TYPE tg_connect_active_bots gauge",
        f"tg_connect_active_bots {active_bots}",
        "# HELP tg_connect_delivery_queue_items Delivery queue item count by status.",
        "# TYPE tg_connect_delivery_queue_items gauge",
    ]
    lines.extend(f'tg_connect_delivery_queue_items{{status="{status}"}} {count}' for status, count in queue_stats.items())
    polling = telegram_runner.status()
    lines.extend(
        [
            "# HELP tg_connect_long_polling_bots Long polling bot runners by state.",
            "# TYPE tg_connect_long_polling_bots gauge",
            f'tg_connect_long_polling_bots{{state="running"}} {sum(1 for item in polling.values() if item.running)}',
            f'tg_connect_long_polling_bots{{state="stopped"}} {sum(1 for item in polling.values() if not item.running)}',
            "# HELP tg_connect_idempotency_records In-memory idempotency records kept by legacy store.",
            "# TYPE tg_connect_idempotency_records gauge",
            f"tg_connect_idempotency_records {idempotency_size}",
            "",
        ]
    )
    return "\n".join(lines)
