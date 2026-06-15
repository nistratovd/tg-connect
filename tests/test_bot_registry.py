import asyncio

import pytest

from app.models.bot_config import BotConfig
from app.services.bot_registry import BotRegistry, BotRegistryError


class FakeSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeBot:
    def __init__(self, token: str) -> None:
        self.token = token
        self.session = FakeSession()


def test_bot_config_normalizes_allowed_ips() -> None:
    config = BotConfig(
        id="main",
        name="support",
        telegram_bot_token="123:abc",
        bitrix_webhook_url="https://example.bitrix24.ru/rest/1/hook/",
        allowed_ips="127.0.0.1, 10.0.0.1",
        rate_limit=60,
    )

    assert config.allowed_ips == ["127.0.0.1", "10.0.0.1"]
    assert config.enabled is True


def test_registry_loads_active_bots_and_finds_by_token_id_or_alias() -> None:
    registry = BotRegistry(
        config_loader=lambda: [
            BotConfig(id="internal-1", name="support", telegram_bot_token="123:abc"),
            BotConfig(id="disabled", name="off", telegram_bot_token="456:def", enabled=False),
        ],
        bot_factory=FakeBot,
    )

    configs = asyncio.run(registry.load_active_bots())

    assert [config.id for config in configs] == ["internal-1"]
    assert registry.get_config("internal-1").name == "support"
    assert registry.get_config("support").telegram_bot_token == "123:abc"
    assert registry.get_config("123:abc").id == "internal-1"
    assert registry.get_bot("support").token == "123:abc"
    assert registry.get_bot("disabled") is None


def test_registry_reload_replaces_settings_and_closes_removed_bots() -> None:
    configs = [[BotConfig(id="old", name="old-alias", telegram_bot_token="123:abc")]]
    registry = BotRegistry(config_loader=lambda: configs[-1], bot_factory=FakeBot)
    asyncio.run(registry.load_active_bots())
    old_bot = registry.get_bot("old")

    configs.append([BotConfig(id="new", name="new-alias", telegram_bot_token="456:def")])
    asyncio.run(registry.reload())

    assert old_bot.session.closed is True
    assert registry.get_bot("old") is None
    assert registry.get_bot("new-alias").token == "456:def"


def test_registry_rejects_duplicate_aliases() -> None:
    registry = BotRegistry(
        config_loader=lambda: [
            BotConfig(id="one", name="support", telegram_bot_token="123:abc"),
            BotConfig(id="two", name="support", telegram_bot_token="456:def"),
        ],
        bot_factory=FakeBot,
    )

    with pytest.raises(BotRegistryError, match="Duplicate bot alias"):
        asyncio.run(registry.load_active_bots())
