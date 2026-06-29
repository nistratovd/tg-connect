from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from aiogram import Bot
from pydantic import ValidationError

from app.models.bot_config import BotConfig
from app.security.secrets import encrypt_config_payload
from app.storage.db import connect, init_db
from app.security_utils import mask_sensitive
from app.services.wireguard import wireguard_manager

_MAX_EVENTS = 100


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def load_admin_bot_configs() -> list[BotConfig]:
    init_db()
    with connect() as conn:
        rows = conn.execute("SELECT payload FROM admin_bots ORDER BY name ASC").fetchall()
    configs: list[BotConfig] = []
    for row in rows:
        try:
            configs.append(BotConfig.model_validate(json.loads(row["payload"])))
        except (json.JSONDecodeError, ValidationError):
            continue
    return configs


def save_admin_bot_config(config: BotConfig) -> None:
    init_db()
    payload = encrypt_config_payload(config.model_dump(mode="json"))
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO admin_bots(id, name, payload, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                payload = excluded.payload,
                updated_at = excluded.updated_at
            """,
            (
                config.id,
                config.name,
                json.dumps(payload, ensure_ascii=False, default=str),
                config.created_at.isoformat(),
                config.updated_at.isoformat(),
            ),
        )
    _mirror_legacy_state_file()


def get_admin_bot_config(bot_id: str) -> BotConfig | None:
    for config in load_admin_bot_configs():
        if config.id == bot_id:
            return config
    return None


def record_event(direction: str, bot_key: str, status: str, payload: Any | None = None, error: str | None = None) -> None:
    init_db()
    event_id = secrets.token_hex(8)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO admin_events(id, created_at, direction, bot_key, status, payload, error)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                utc_now().isoformat(),
                direction,
                mask_sensitive(bot_key),
                status,
                json.dumps(mask_sensitive(payload), ensure_ascii=False, default=str) if payload is not None else None,
                mask_sensitive(error),
            ),
        )
        conn.execute(
            "DELETE FROM admin_events WHERE id NOT IN (SELECT id FROM admin_events ORDER BY created_at DESC LIMIT ?)",
            (_MAX_EVENTS,),
        )
    _mirror_legacy_state_file()


def recent_events(limit: int = 50) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        rows = conn.execute("SELECT * FROM admin_events ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        event = dict(row)
        if event.get("payload"):
            event["payload"] = json.loads(event["payload"])
        events.append(event)
    return events


def _mirror_legacy_state_file() -> None:
    path = os.getenv("ADMIN_STATE_PATH")
    if not path:
        return
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        bots = [json.loads(row["payload"]) for row in conn.execute("SELECT payload FROM admin_bots ORDER BY name ASC").fetchall()]
        events = [dict(row) for row in conn.execute("SELECT * FROM admin_events ORDER BY created_at DESC LIMIT ?", (_MAX_EVENTS,)).fetchall()]
    for event in events:
        if event.get("payload"):
            event["payload"] = json.loads(event["payload"])
    state_path.write_text(json.dumps({"bots": bots, "events": events}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

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
