from __future__ import annotations

import json
import os
import secrets
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from aiogram import Bot
from pydantic import TypeAdapter, ValidationError

from app.models.bot_config import BotConfig
from app.security.secrets import encrypt_config_payload
from app.security_utils import mask_sensitive
from app.services.wireguard import wireguard_manager

DEFAULT_ADMIN_STATE_PATH = "data/admin_state.json"
_MAX_EVENTS = 100


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def admin_state_path() -> Path:
    return Path(os.getenv("ADMIN_STATE_PATH", DEFAULT_ADMIN_STATE_PATH))


def _empty_state() -> dict[str, Any]:
    return {"bots": [], "events": []}


def load_state() -> dict[str, Any]:
    path = admin_state_path()
    if not path.exists():
        return _empty_state()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return _empty_state()
    if not isinstance(payload, dict):
        return _empty_state()
    payload.setdefault("bots", [])
    payload.setdefault("events", [])
    return payload


def save_state(state: dict[str, Any]) -> None:
    path = admin_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def load_admin_bot_configs() -> list[BotConfig]:
    state = load_state()
    try:
        return TypeAdapter(list[BotConfig]).validate_python(state.get("bots", []))
    except ValidationError:
        return []


def save_admin_bot_config(config: BotConfig) -> None:
    state = load_state()
    bots = [bot for bot in state.get("bots", []) if bot.get("id") != config.id]
    bots.append(encrypt_config_payload(config.model_dump(mode="json")))
    state["bots"] = sorted(bots, key=lambda item: item["name"])
    save_state(state)


def get_admin_bot_config(bot_id: str) -> BotConfig | None:
    for config in load_admin_bot_configs():
        if config.id == bot_id:
            return config
    return None


def record_event(direction: str, bot_key: str, status: str, payload: Any | None = None, error: str | None = None) -> None:
    state = load_state()
    events = deque(state.get("events", []), maxlen=_MAX_EVENTS)
    events.appendleft(
        {
            "id": secrets.token_hex(8),
            "created_at": utc_now().isoformat(),
            "direction": direction,
            "bot_key": mask_sensitive(bot_key),
            "status": status,
            "payload": mask_sensitive(payload),
            "error": mask_sensitive(error),
        }
    )
    state["events"] = list(events)
    save_state(state)


def recent_events(limit: int = 50) -> list[dict[str, Any]]:
    return load_state().get("events", [])[:limit]


async def check_telegram(token: str) -> tuple[bool, str]:
    await wireguard_manager.ensure_started()
    bot = Bot(token)
    try:
        me = await bot.get_me()
        return True, f"Telegram OK: @{me.username or me.id}"
    except Exception as exc:  # noqa: BLE001 - диагностический чек должен вернуть текст ошибки.
        return False, mask_sensitive(str(exc) or exc.__class__.__name__)
    finally:
        await bot.session.close()


async def check_bitrix(endpoint: str | None) -> tuple[bool, str]:
    if not endpoint:
        return False, "Endpoint Битрикс не задан"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(str(endpoint))
        if response.status_code < 500:
            return True, f"Битрикс ответил HTTP {response.status_code}"
        return False, f"Битрикс ответил HTTP {response.status_code}"
    except Exception as exc:  # noqa: BLE001 - диагностический чек должен вернуть текст ошибки.
        return False, mask_sensitive(str(exc) or exc.__class__.__name__)
