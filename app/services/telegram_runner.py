from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import Update

from app.models.bot_config import BotConfig
from app.queue.delivery import delivery_queue
from app.services.bot_registry import BotRegistry, registry
from app.webhooks.bitrix_forwarder import enqueue_update, process_delivery_item

logger = logging.getLogger(__name__)

UpdateHandler = Callable[[str, Update], Awaitable[object]]
DeliveryProcessor = Callable[[str], Awaitable[object]]


@dataclass
class RunnerStatus:
    bot_id: str
    mode: str
    running: bool
    last_update_id: int | None = None
    last_error: str | None = None


class TelegramLongPollingRunner:
    """Управляет long polling задачами для ботов с telegram_update_mode=long_polling."""

    def __init__(
        self,
        bot_registry: BotRegistry,
        update_handler: UpdateHandler = enqueue_update,
        delivery_processor: DeliveryProcessor = process_delivery_item,
        poll_timeout_seconds: int | None = None,
        request_timeout_seconds: int | None = None,
        error_sleep_seconds: float | None = None,
    ) -> None:
        self._registry = bot_registry
        self._update_handler = update_handler
        self._delivery_processor = delivery_processor
        self._poll_timeout_seconds = poll_timeout_seconds or int(os.getenv("TELEGRAM_LONG_POLL_TIMEOUT_SECONDS", "30"))
        self._request_timeout_seconds = request_timeout_seconds or int(
            os.getenv("TELEGRAM_LONG_POLL_REQUEST_TIMEOUT_SECONDS", str(self._poll_timeout_seconds + 10))
        )
        if self._request_timeout_seconds <= self._poll_timeout_seconds:
            logger.warning(
                "TELEGRAM_LONG_POLL_REQUEST_TIMEOUT_SECONDS=%s не больше TELEGRAM_LONG_POLL_TIMEOUT_SECONDS=%s; "
                "используем %s, чтобы aiohttp не обрывал штатный long polling",
                self._request_timeout_seconds,
                self._poll_timeout_seconds,
                self._poll_timeout_seconds + 10,
            )
            self._request_timeout_seconds = self._poll_timeout_seconds + 10
        self._error_sleep_seconds = error_sleep_seconds or float(os.getenv("TELEGRAM_LONG_POLL_ERROR_SLEEP_SECONDS", "5"))
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._statuses: dict[str, RunnerStatus] = {}
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        self._stopping.clear()
        configs = await self._registry.load_active_bots()
        for config in configs:
            if config.telegram_update_mode != "long_polling":
                self._statuses[config.id] = RunnerStatus(bot_id=config.id, mode=config.telegram_update_mode, running=False)
                continue
            bot = self._registry.get_bot(config.id)
            if bot is None or config.id in self._tasks:
                continue
            self._statuses[config.id] = RunnerStatus(bot_id=config.id, mode="long_polling", running=True)
            self._tasks[config.id] = asyncio.create_task(self._poll_loop(config, bot), name=f"tg-polling-{config.id}")

    async def stop(self) -> None:
        self._stopping.set()
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        for status in self._statuses.values():
            status.running = False

    async def reload(self) -> None:
        await self.stop()
        await self.start()

    def status(self) -> dict[str, RunnerStatus]:
        return dict(self._statuses)

    async def poll_once(self, config: BotConfig, bot: Bot, offset: int | None = None) -> int | None:
        """Выполняет одну итерацию getUpdates и возвращает следующий offset."""
        updates = await bot.get_updates(
            offset=offset,
            timeout=self._poll_timeout_seconds,
            request_timeout=self._request_timeout_seconds,
        )
        next_offset = offset
        for update in updates:
            await self._update_handler(config.id, update)
            queued = delivery_queue.find_by_update(config.id, update.update_id)
            if queued is not None:
                await self._delivery_processor(queued.id)
            next_offset = update.update_id + 1
            status = self._statuses.setdefault(config.id, RunnerStatus(bot_id=config.id, mode="long_polling", running=True))
            status.last_update_id = update.update_id
            status.last_error = None
        return next_offset

    async def _poll_loop(self, config: BotConfig, bot: Bot) -> None:
        offset: int | None = None
        while not self._stopping.is_set():
            try:
                offset = await self.poll_once(config, bot, offset)
            except asyncio.CancelledError:
                raise
            except TelegramNetworkError as exc:
                logger.warning("Сетевая ошибка long polling для бота %s: %s", config.id, exc)
                status = self._statuses.setdefault(config.id, RunnerStatus(bot_id=config.id, mode="long_polling", running=True))
                status.last_error = str(exc) or exc.__class__.__name__
                await asyncio.sleep(self._error_sleep_seconds)
            except Exception as exc:  # noqa: BLE001 - polling loop должен переживать transient ошибки Telegram/сети.
                logger.exception("Ошибка long polling для бота %s", config.id)
                status = self._statuses.setdefault(config.id, RunnerStatus(bot_id=config.id, mode="long_polling", running=True))
                status.last_error = str(exc) or exc.__class__.__name__
                await asyncio.sleep(self._error_sleep_seconds)


telegram_runner = TelegramLongPollingRunner(registry)
