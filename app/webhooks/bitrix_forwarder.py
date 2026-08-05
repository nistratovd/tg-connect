from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

import httpx
from aiogram import Bot
from aiogram.client.default import Default
from aiogram.types import Update
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.queue.delivery import delivery_queue
from app.services.admin_store import get_admin_bot_config, load_admin_bot_configs, record_event
from app.services.idempotency import idempotency_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks/telegram", tags=["telegram-webhooks"])

_BITRIX_ENDPOINTS: dict[str, str] | None = None
_DELIVERY_CLIENT_FACTORY: Callable[[], httpx.AsyncClient] = httpx.AsyncClient


class BitrixForwarderError(Exception):
    def __init__(self, description: str, status_code: int = 400) -> None:
        super().__init__(description)
        self.description = description
        self.status_code = status_code


class DeliveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delivered: bool
    attempts: int
    status_code: int | None = None
    error: str | None = None


class ForwarderSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_seconds: float = Field(default=5.0, gt=0)
    retry_attempts: int = Field(default=3, ge=1)
    retry_backoff_seconds: float = Field(default=0.5, ge=0)


def reset_bitrix_endpoint_cache() -> None:
    global _BITRIX_ENDPOINTS
    _BITRIX_ENDPOINTS = None


def configure_delivery_client_factory(factory: Callable[[], httpx.AsyncClient]) -> None:
    """Переопределяет HTTP-клиент для тестов или DI-контейнера."""
    global _DELIVERY_CLIENT_FACTORY
    _DELIVERY_CLIENT_FACTORY = factory


def reset_delivery_client_factory() -> None:
    configure_delivery_client_factory(httpx.AsyncClient)


def get_forwarder_settings() -> ForwarderSettings:
    raw_settings = {
        "timeout_seconds": os.getenv("BITRIX_FORWARD_TIMEOUT_SECONDS", "5"),
        "retry_attempts": os.getenv("BITRIX_FORWARD_RETRY_ATTEMPTS", "3"),
        "retry_backoff_seconds": os.getenv("BITRIX_FORWARD_RETRY_BACKOFF_SECONDS", "0.5"),
    }
    try:
        return ForwarderSettings.model_validate(raw_settings)
    except ValidationError as exc:
        raise BitrixForwarderError("Invalid Bitrix forwarder settings", 500) from exc


def get_bitrix_endpoints() -> dict[str, str]:
    """Возвращает соответствие token/alias Telegram-бота и webhook URL Битрикс.

    Настройка хранится в переменной окружения BITRIX_BOT_WEBHOOK_URLS в формате JSON:
    {"bot_alias_or_token": "https://example.bitrix24.ru/rest/..."}.
    В production эту функцию можно заменить чтением из БД/конфигурационного сервиса.
    """
    global _BITRIX_ENDPOINTS
    if _BITRIX_ENDPOINTS is not None:
        return _BITRIX_ENDPOINTS

    raw_endpoints = os.getenv("BITRIX_BOT_WEBHOOK_URLS", "{}")
    try:
        endpoints = json.loads(raw_endpoints)
    except json.JSONDecodeError as exc:
        raise BitrixForwarderError("Invalid BITRIX_BOT_WEBHOOK_URLS JSON", 500) from exc

    if not isinstance(endpoints, dict) or not all(
        isinstance(bot_key, str) and isinstance(url, str) and url for bot_key, url in endpoints.items()
    ):
        raise BitrixForwarderError("BITRIX_BOT_WEBHOOK_URLS must be a JSON object with non-empty string values", 500)

    _BITRIX_ENDPOINTS = endpoints
    return _BITRIX_ENDPOINTS


def resolve_bitrix_endpoint(bot_key: str) -> str:
    admin_config = get_admin_bot_config(bot_key)
    if admin_config and admin_config.bitrix_webhook_url:
        return str(admin_config.bitrix_webhook_url)

    endpoint = get_bitrix_endpoints().get(bot_key)
    if endpoint is None:
        raise BitrixForwarderError(f"Bitrix webhook URL is not configured for bot {bot_key}", 404)
    return endpoint


_OMIT = object()


def _drop_aiogram_defaults(value: Any) -> Any:
    """Удаляет aiogram Default sentinels, которые не сериализуются Pydantic."""
    if isinstance(value, Default):
        return _OMIT
    if isinstance(value, Mapping):
        cleaned_mapping = {
            key: cleaned
            for key, item in value.items()
            if (cleaned := _drop_aiogram_defaults(item)) is not _OMIT
        }
        return cleaned_mapping or _OMIT
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            cleaned
            for item in value
            if (cleaned := _drop_aiogram_defaults(item)) is not _OMIT
        ]
    return value


def serialize_update(update: Update) -> dict[str, Any]:
    """Формирует JSON, максимально близкий к Telegram webhook update."""
    try:
        return update.model_dump(mode="json", by_alias=True, exclude_none=True)
    except ValueError as exc:
        if "aiogram.client.default.Default" not in str(exc):
            raise
        logger.warning("Telegram update содержит aiogram Default sentinel; сериализуем без этих служебных значений")
        payload = update.model_dump(mode="python", by_alias=True, exclude_none=True)
        cleaned_payload = _drop_aiogram_defaults(payload)
        if not isinstance(cleaned_payload, dict):
            raise
        return cleaned_payload


async def parse_update(request: Request, bot: Bot | None = None) -> Update:
    payload = await request.json()
    if not isinstance(payload, Mapping):
        raise BitrixForwarderError("Telegram update payload must be a JSON object")
    try:
        return Update.model_validate(payload, context={"bot": bot} if bot is not None else None)
    except ValidationError as exc:
        raise BitrixForwarderError("Invalid Telegram update payload") from exc


def resolve_bitrix_auth_headers(bot_key: str) -> dict[str, str]:
    """Возвращает заголовки аутентификации для доставки в Битрикс."""
    configs = load_admin_bot_configs()
    config = next(
        (
            item
            for item in configs
            if bot_key in {item.id, item.name, item.telegram_bot_token}
        ),
        None,
    )
    if config is None or not config.bitrix_auth_token:
        return {}
    return {"X-TG-Connect-Token": config.bitrix_auth_token}


async def deliver_to_bitrix(
    endpoint: str,
    payload: dict[str, Any],
    settings: ForwarderSettings | None = None,
    headers: Mapping[str, str] | None = None,
) -> DeliveryResult:
    settings = settings or get_forwarder_settings()
    last_error: str | None = None
    last_status_code: int | None = None

    async with _DELIVERY_CLIENT_FACTORY() as client:
        for attempt in range(1, settings.retry_attempts + 1):
            try:
                request_kwargs: dict[str, Any] = {"json": payload, "timeout": settings.timeout_seconds}
                if headers:
                    request_kwargs["headers"] = dict(headers)
                response = await client.post(endpoint, **request_kwargs)
                last_status_code = response.status_code
                if response.status_code >= 400:
                    raise httpx.HTTPStatusError(
                        f"HTTP {response.status_code}",
                        request=getattr(response, "_request", None) or httpx.Request("POST", endpoint),
                        response=response,
                    )
                return DeliveryResult(delivered=True, attempts=attempt, status_code=response.status_code)
            except (httpx.TimeoutException, httpx.HTTPError) as exc:
                last_error = str(exc) or exc.__class__.__name__
                logger.warning(
                    "Ошибка доставки Telegram update в Битрикс",
                    extra={"endpoint": endpoint, "attempt": attempt, "status_code": last_status_code, "error": last_error},
                )
                if attempt < settings.retry_attempts and settings.retry_backoff_seconds:
                    await asyncio.sleep(settings.retry_backoff_seconds * attempt)

    logger.error(
        "Не удалось доставить Telegram update в Битрикс",
        extra={"endpoint": endpoint, "attempts": settings.retry_attempts, "status_code": last_status_code, "error": last_error},
    )
    return DeliveryResult(
        delivered=False,
        attempts=settings.retry_attempts,
        status_code=last_status_code,
        error=last_error,
    )


async def process_delivery_item(item_id: str) -> DeliveryResult | None:
    """Доставляет один queued item в Битрикс и фиксирует итоговый статус."""
    item = delivery_queue.mark_processing(item_id)
    if item is None:
        return None
    result = await deliver_to_bitrix(item.endpoint, item.payload, headers=resolve_bitrix_auth_headers(item.bot_key))
    if result.delivered:
        delivery_queue.mark_delivered(item.id, result.attempts)
    else:
        delivery_queue.mark_failed(item.id, result.attempts, result.error)
    record_event("incoming", item.bot_key, "delivered" if result.delivered else "error", item.payload, result.error)
    return result


async def process_pending_deliveries(limit: int = 50) -> None:
    """Обрабатывает pending outbox после рестарта или ручного retry."""
    for item in delivery_queue.pending(limit):
        await process_delivery_item(item.id)


async def enqueue_update_payload(bot_key: str, payload: dict[str, Any]) -> DeliveryResult:
    """Ставит Telegram update payload в outbox с дедупликацией по update_id."""
    endpoint = resolve_bitrix_endpoint(bot_key)
    existing = delivery_queue.find_by_update(bot_key, payload.get("update_id"))
    if existing and existing.status in {"queued", "processing", "delivered"}:
        record_event("incoming", bot_key, "duplicate", payload)
        return DeliveryResult(delivered=True, attempts=existing.attempts, status_code=202)

    queue_item = delivery_queue.enqueue(endpoint, bot_key, payload)
    record_event("incoming", bot_key, "queued", payload)
    return DeliveryResult(delivered=True, attempts=0, status_code=202, error=queue_item.id)


async def enqueue_update(bot_key: str, update: Update) -> DeliveryResult:
    """Ставит aiogram Update в outbox. Используется webhook и long polling режимами."""
    return await enqueue_update_payload(bot_key, serialize_update(update))


async def forward_update(bot_key: str, request: Request, bot: Bot | None = None) -> DeliveryResult:
    update = await parse_update(request, bot=bot)
    return await enqueue_update(bot_key, update)


@router.post("/{bot_key}")
async def bitrix_telegram_webhook(bot_key: str, request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    try:
        result = await forward_update(bot_key, request)
        if result.error:
            background_tasks.add_task(process_delivery_item, result.error)
    except BitrixForwarderError as exc:
        record_event("incoming", bot_key, "error", error=exc.description)
        return JSONResponse(status_code=exc.status_code, content={"ok": False, "description": exc.description})
    except Exception as exc:  # noqa: BLE001 - endpoint webhook не должен падать без JSON-ответа.
        logger.exception("Непредвиденная ошибка обработки Telegram update", extra={"bot_key": bot_key})
        description = str(exc) or exc.__class__.__name__
        record_event("incoming", bot_key, "error", error=description)
        return JSONResponse(status_code=500, content={"ok": False, "description": description})

    payload = result.model_dump(exclude_none=True)
    queue_item_id = payload.pop("error", None)
    if queue_item_id:
        payload["queue_item_id"] = queue_item_id
    return JSONResponse(status_code=200, content={"ok": True, "result": payload})
