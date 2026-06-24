from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Iterable
from typing import Any

from aiogram import Bot
from pydantic import TypeAdapter, ValidationError

from app.models.bot_config import BotConfig
from app.security.secrets import masked_telegram_token
from app.services.admin_store import load_admin_bot_configs

BotFactory = Callable[[str], Bot]
ConfigLoader = Callable[[], Iterable[BotConfig | dict[str, Any]]]


class BotRegistryError(Exception):
    """Ошибка загрузки или поиска конфигурации бота."""


class BotRegistry:
    """Потокобезопасный реестр активных конфигураций и экземпляров aiogram.Bot."""

    def __init__(self, config_loader: ConfigLoader | None = None, bot_factory: BotFactory = Bot) -> None:
        self._config_loader = config_loader or load_bot_configs_from_env
        self._bot_factory = bot_factory
        self._configs_by_id: dict[str, BotConfig] = {}
        self._configs_by_token: dict[str, BotConfig] = {}
        self._configs_by_name: dict[str, BotConfig] = {}
        self._bots_by_token: dict[str, Bot] = {}
        self._lock = asyncio.Lock()

    def set_bot_factory(self, bot_factory: BotFactory) -> None:
        self._bot_factory = bot_factory
        self._bots_by_token.clear()

    def set_config_loader(self, config_loader: ConfigLoader) -> None:
        self._config_loader = config_loader

    async def load_active_bots(self) -> list[BotConfig]:
        """Загружает включенные конфигурации и создает для них экземпляры Bot."""
        async with self._lock:
            configs = self._normalize_configs(self._config_loader())
            enabled_configs = [config for config in configs if config.enabled]
            bots_by_token = {
                config.telegram_bot_token: self._bots_by_token.get(config.telegram_bot_token)
                or self._bot_factory(config.telegram_bot_token)
                for config in enabled_configs
            }
            old_bots = [
                bot for token, bot in self._bots_by_token.items() if token not in bots_by_token
            ]
            self._replace_state(enabled_configs, bots_by_token)

        await self._close_bots(old_bots)
        return enabled_configs

    async def reload(self) -> list[BotConfig]:
        """Атомарно перечитывает настройки без полного рестарта сервиса."""
        return await self.load_active_bots()

    def get_config(self, token_or_id_or_alias: str) -> BotConfig | None:
        return (
            self._configs_by_token.get(token_or_id_or_alias)
            or self._configs_by_id.get(token_or_id_or_alias)
            or self._configs_by_name.get(token_or_id_or_alias)
        )

    def get_bot(self, token_or_id_or_alias: str) -> Bot | None:
        config = self.get_config(token_or_id_or_alias)
        if config is None:
            return None
        return self._bots_by_token.get(config.telegram_bot_token)

    def ensure_bot(self, token_or_id_or_alias: str) -> Bot:
        bot = self.get_bot(token_or_id_or_alias)
        if bot is None:
            raise BotRegistryError("Bot configuration is not found or disabled")
        return bot

    def active_configs(self) -> list[BotConfig]:
        return list(self._configs_by_id.values())

    def _replace_state(self, configs: list[BotConfig], bots_by_token: dict[str, Bot]) -> None:
        self._configs_by_id = {config.id: config for config in configs}
        self._configs_by_token = {config.telegram_bot_token: config for config in configs}
        self._configs_by_name = {config.name: config for config in configs}
        self._bots_by_token = bots_by_token

    @staticmethod
    def _normalize_configs(raw_configs: Iterable[BotConfig | dict[str, Any]]) -> list[BotConfig]:
        configs: list[BotConfig] = []
        for raw_config in raw_configs:
            config = raw_config if isinstance(raw_config, BotConfig) else BotConfig.model_validate(raw_config)
            if config.id in {item.id for item in configs}:
                raise BotRegistryError(f"Duplicate bot id: {config.id}")
            if config.name in {item.name for item in configs}:
                raise BotRegistryError(f"Duplicate bot alias: {config.name}")
            if config.telegram_bot_token in {item.telegram_bot_token for item in configs}:
                raise BotRegistryError(f"Duplicate bot token: {masked_telegram_token(config.telegram_bot_token)}")
            configs.append(config)
        return configs

    @staticmethod
    async def _close_bots(bots: Iterable[Bot]) -> None:
        for bot in bots:
            session = getattr(bot, "session", None)
            close = getattr(session, "close", None)
            if close is None:
                continue
            result = close()
            if asyncio.iscoroutine(result):
                await result


def load_bot_configs_from_env() -> list[BotConfig]:
    """Читает конфигурации из TELEGRAM_BOT_CONFIGS.

    Переменная должна содержать JSON-массив объектов BotConfig. Для обратной
    совместимости TELEGRAM_BOT_ALIASES преобразуется в минимальные конфигурации.
    """
    admin_configs = load_admin_bot_configs()

    raw_configs = os.getenv("TELEGRAM_BOT_CONFIGS")
    if raw_configs:
        try:
            payload = json.loads(raw_configs)
            return [*admin_configs, *TypeAdapter(list[BotConfig]).validate_python(payload)]
        except (json.JSONDecodeError, ValidationError) as exc:
            raise BotRegistryError("Invalid TELEGRAM_BOT_CONFIGS JSON") from exc

    raw_aliases = os.getenv("TELEGRAM_BOT_ALIASES", "{}")
    try:
        aliases = json.loads(raw_aliases)
    except json.JSONDecodeError as exc:
        raise BotRegistryError("Invalid TELEGRAM_BOT_ALIASES JSON") from exc

    if not isinstance(aliases, dict) or not all(
        isinstance(alias, str) and isinstance(token, str) for alias, token in aliases.items()
    ):
        raise BotRegistryError("TELEGRAM_BOT_ALIASES must be a JSON object with string values")

    return [
        *admin_configs,
        *(BotConfig(id=alias, name=alias, telegram_bot_token=token, bitrix_webhook_url=None)
        for alias, token in aliases.items()),
    ]


registry = BotRegistry()
