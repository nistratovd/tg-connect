import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.webhooks import bitrix_forwarder


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


@pytest.fixture(autouse=True)
def forwarder_env(monkeypatch):
    monkeypatch.setenv("BITRIX_BOT_WEBHOOK_URLS", '{"internal":"https://bitrix.example/webhook"}')
    monkeypatch.setenv("BITRIX_FORWARD_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("BITRIX_FORWARD_RETRY_ATTEMPTS", "2")
    monkeypatch.setenv("BITRIX_FORWARD_RETRY_BACKOFF_SECONDS", "0")
    bitrix_forwarder.reset_bitrix_endpoint_cache()
    bitrix_forwarder.configure_delivery_client_factory(FakeAsyncClient)
    FakeAsyncClient.responses = []
    FakeAsyncClient.calls = []
    yield
    bitrix_forwarder.reset_delivery_client_factory()
    bitrix_forwarder.reset_bitrix_endpoint_cache()


@pytest.fixture
def client():
    return TestClient(app)


def test_forwards_message_update_to_configured_bitrix_endpoint(client):
    FakeAsyncClient.responses = [httpx.Response(200, json={"ok": True})]
    update = {
        "update_id": 1000,
        "message": {
            "message_id": 1,
            "date": 1710000000,
            "chat": {"id": 10, "type": "private"},
            "from": {"id": 20, "is_bot": False, "first_name": "User"},
            "text": "Привет",
        },
    }

    response = client.post("/webhooks/telegram/internal", json=update)

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert FakeAsyncClient.calls == [
        {"endpoint": "https://bitrix.example/webhook", "json": update, "timeout": 1.0}
    ]


@pytest.mark.parametrize(
    "update",
    [
        {
            "update_id": 1001,
            "callback_query": {
                "id": "cbq",
                "from": {"id": 20, "is_bot": False, "first_name": "User"},
                "chat_instance": "chat-instance",
                "data": "payload",
            },
        },
        {
            "update_id": 1002,
            "edited_message": {
                "message_id": 1,
                "date": 1710000000,
                "edit_date": 1710000010,
                "chat": {"id": 10, "type": "private"},
                "text": "Изменено",
            },
        },
        {
            "update_id": 1003,
            "inline_query": {
                "id": "inline",
                "from": {"id": 20, "is_bot": False, "first_name": "User"},
                "query": "q",
                "offset": "",
            },
        },
    ],
)
def test_forwards_supported_telegram_update_types(client, update):
    FakeAsyncClient.responses = [httpx.Response(200)]

    response = client.post("/webhooks/telegram/internal", json=update)

    assert response.status_code == 200
    assert FakeAsyncClient.calls[0]["json"] == update


def test_retries_delivery_errors_and_logs_failure(client, caplog):
    FakeAsyncClient.responses = [httpx.ConnectError("boom"), httpx.Response(500)]
    update = {"update_id": 1004}

    response = client.post("/webhooks/telegram/internal", json=update)

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert len(FakeAsyncClient.calls) == 2
    assert response.json()["result"]["queue_item_id"]
    assert "Не удалось доставить Telegram update в Битрикс" in caplog.text


def test_returns_404_when_bot_has_no_bitrix_endpoint(client):
    response = client.post("/webhooks/telegram/unknown", json={"update_id": 1005})

    assert response.status_code == 404
    assert response.json() == {
        "ok": False,
        "description": "Bitrix webhook URL is not configured for bot unknown",
    }
