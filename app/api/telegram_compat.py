from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any

from aiogram import Bot
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from app.models.bot_config import BotConfig
from app.services.admin_store import record_event
from app.services.bot_registry import BotRegistryError, registry

router = APIRouter()

_BOT_FACTORY: Callable[[str], Bot] = Bot
_DYNAMIC_BOT_CACHE: dict[str, Bot] = {}


class TelegramCompatError(Exception):
    def __init__(self, description: str, status_code: int = 400) -> None:
        super().__init__(description)
        self.description = description
        self.status_code = status_code


class TelegramMethodParams(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class SendMessageParams(TelegramMethodParams):
    chat_id: int | str
    text: str


class SendPhotoParams(TelegramMethodParams):
    chat_id: int | str
    photo: str


class SendDocumentParams(TelegramMethodParams):
    chat_id: int | str
    document: str


class EditMessageTextParams(TelegramMethodParams):
    text: str
    chat_id: int | str | None = None
    message_id: int | None = None
    inline_message_id: str | None = None


class DeleteMessageParams(TelegramMethodParams):
    chat_id: int | str
    message_id: int


class AnswerCallbackQueryParams(TelegramMethodParams):
    callback_query_id: str


_METHODS: dict[str, tuple[type[TelegramMethodParams], str]] = {
    "sendMessage": (SendMessageParams, "send_message"),
    "sendPhoto": (SendPhotoParams, "send_photo"),
    "sendDocument": (SendDocumentParams, "send_document"),
    "editMessageText": (EditMessageTextParams, "edit_message_text"),
    "deleteMessage": (DeleteMessageParams, "delete_message"),
    "answerCallbackQuery": (AnswerCallbackQueryParams, "answer_callback_query"),
}


def configure_bot_factory(factory: Callable[[str], Bot]) -> None:
    """Переопределяет фабрику Bot для тестов или DI-контейнера."""
    global _BOT_FACTORY
    _BOT_FACTORY = factory
    registry.set_bot_factory(factory)
    _DYNAMIC_BOT_CACHE.clear()


def reset_bot_factory() -> None:
    """Возвращает стандартную фабрику Bot."""
    configure_bot_factory(Bot)


def clear_bot_cache() -> None:
    _DYNAMIC_BOT_CACHE.clear()
    registry.set_bot_factory(_BOT_FACTORY)


def _dynamic_bot(token: str) -> Bot:
    if token not in _DYNAMIC_BOT_CACHE:
        _DYNAMIC_BOT_CACHE[token] = _BOT_FACTORY(token)
    return _DYNAMIC_BOT_CACHE[token]


async def get_bot(token_or_id_or_alias: str) -> Bot:
    try:
        await registry.load_active_bots()
    except BotRegistryError as exc:
        raise TelegramCompatError(str(exc), 500) from exc

    bot = registry.get_bot(token_or_id_or_alias)
    if bot is not None:
        return bot

    # Прямая передача токена сохраняет совместимость с Telegram Bot API proxy.
    return _dynamic_bot(token_or_id_or_alias)


def get_bot_config(token_or_id_or_alias: str) -> BotConfig | None:
    return registry.get_config(token_or_id_or_alias)


def serialize_result(result: Any) -> Any:
    if isinstance(result, BaseModel):
        return result.model_dump(mode="json", by_alias=True, exclude_none=True)
    if hasattr(result, "model_dump"):
        return result.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(result, list):
        return [serialize_result(item) for item in result]
    if isinstance(result, tuple):
        return [serialize_result(item) for item in result]
    if isinstance(result, Mapping):
        return {key: serialize_result(value) for key, value in result.items()}
    return result


async def read_params(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise TelegramCompatError("JSON payload must be an object")
        return payload

    form = await request.form()
    return dict(form.multi_items())


def validate_params(method: str, raw_params: dict[str, Any]) -> dict[str, Any]:
    if method not in _METHODS:
        raise TelegramCompatError(f"Method {method} is not supported", 404)

    params_model, _ = _METHODS[method]
    try:
        params = params_model.model_validate(raw_params)
    except ValidationError as exc:
        first_error = exc.errors()[0]
        field = ".".join(str(part) for part in first_error.get("loc", []))
        message = first_error.get("msg", "Invalid parameter")
        raise TelegramCompatError(f"Invalid parameter {field}: {message}") from exc

    if isinstance(params, EditMessageTextParams) and not params.inline_message_id:
        if params.chat_id is None or params.message_id is None:
            raise TelegramCompatError(
                "editMessageText requires either inline_message_id or both chat_id and message_id"
            )

    return params.model_dump(by_alias=False, exclude_none=True)


async def call_bot_method(bot: Bot, method: str, params: dict[str, Any]) -> Any:
    _, aiogram_method_name = _METHODS[method]
    handler = getattr(bot, aiogram_method_name, None)
    if handler is None:
        raise TelegramCompatError(f"Bot method {aiogram_method_name} is unavailable", 500)

    result = handler(**params)
    if inspect.isawaitable(result):
        return await result
    return result


def telegram_response(ok: bool, status_code: int = 200, **payload: Any) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"ok": ok, **payload})


@router.api_route("/bot{token}/{method}", methods=["GET", "POST"])
async def telegram_compat(token: str, method: str, request: Request) -> JSONResponse:
    try:
        raw_params = dict(request.query_params) if request.method == "GET" else await read_params(request)
        params = validate_params(method, raw_params)
        bot = await get_bot(token)
        result = await call_bot_method(bot, method, params)
        record_event("outgoing", token, "delivered", {"method": method, "params": params})
    except TelegramCompatError as exc:
        record_event("outgoing", token, "error", {"method": method}, exc.description)
        return telegram_response(False, exc.status_code, description=exc.description)
    except Exception as exc:  # noqa: BLE001 - совместимость с форматом ошибок Telegram Bot API.
        record_event("outgoing", token, "error", {"method": method}, str(exc) or exc.__class__.__name__)
        return telegram_response(False, 500, description="Internal server error")

    return telegram_response(True, result=serialize_result(result))
