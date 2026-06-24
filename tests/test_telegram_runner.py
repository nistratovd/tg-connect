import asyncio

import httpx
from aiogram.types import Update

from app.models.bot_config import BotConfig
from app.queue.delivery import delivery_queue
from app.services.bot_registry import BotRegistry
from app.services.telegram_runner import TelegramLongPollingRunner
from app.webhooks import bitrix_forwarder


class FakeBot:
    def __init__(self, updates):
        self.updates = updates
        self.calls = []

    async def get_updates(self, *, offset=None, timeout=None, request_timeout=None):
        self.calls.append({"offset": offset, "timeout": timeout, "request_timeout": request_timeout})
        updates, self.updates = self.updates, []
        return updates


class FakeAsyncClient:
    responses = []
    calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, endpoint, *, json, timeout):
        self.__class__.calls.append({"endpoint": endpoint, "json": json, "timeout": timeout})
        item = self.__class__.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def setup_function():
    delivery_queue.clear()
    bitrix_forwarder.configure_delivery_client_factory(FakeAsyncClient)
    bitrix_forwarder.reset_bitrix_endpoint_cache()
    FakeAsyncClient.responses = []
    FakeAsyncClient.calls = []


def teardown_function():
    bitrix_forwarder.reset_delivery_client_factory()
    bitrix_forwarder.reset_bitrix_endpoint_cache()


def test_bot_config_supports_long_polling_mode():
    config = BotConfig(id="poll", name="poll", telegram_bot_token="123:abc", telegram_update_mode="long_polling")

    assert config.telegram_update_mode == "long_polling"


def test_poll_once_enqueues_and_delivers_updates(monkeypatch):
    monkeypatch.setenv("BITRIX_BOT_WEBHOOK_URLS", '{"poll":"https://bitrix.example/webhook"}')
    monkeypatch.setenv("BITRIX_FORWARD_RETRY_ATTEMPTS", "1")
    FakeAsyncClient.responses = [httpx.Response(200)]
    update_payload = {
        "update_id": 900,
        "message": {
            "message_id": 1,
            "date": 1710000000,
            "chat": {"id": 10, "type": "private"},
            "text": "polling",
        },
    }
    update = Update.model_validate(update_payload)
    bot = FakeBot([update])
    runner = TelegramLongPollingRunner(BotRegistry(config_loader=lambda: []), poll_timeout_seconds=1)
    config = BotConfig(id="poll", name="poll", telegram_bot_token="123:abc", telegram_update_mode="long_polling")

    next_offset = asyncio.run(runner.poll_once(config, bot))

    assert next_offset == 901
    assert bot.calls == [{"offset": None, "timeout": 1, "request_timeout": 11}]
    assert FakeAsyncClient.calls == [
        {"endpoint": "https://bitrix.example/webhook", "json": update_payload, "timeout": 5.0}
    ]
    assert delivery_queue.find_by_update("poll", 900).status == "delivered"


def test_polling_request_timeout_is_above_long_poll_timeout(monkeypatch):
    monkeypatch.setenv("TELEGRAM_LONG_POLL_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("TELEGRAM_LONG_POLL_REQUEST_TIMEOUT_SECONDS", "30")

    runner = TelegramLongPollingRunner(BotRegistry(config_loader=lambda: []))

    assert runner._poll_timeout_seconds == 30
    assert runner._request_timeout_seconds == 40
