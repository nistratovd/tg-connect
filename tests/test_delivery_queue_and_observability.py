import asyncio

import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.queue.delivery import delivery_queue
from app.services.idempotency import idempotency_store
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


def setup_function():
    delivery_queue.clear()
    asyncio.run(idempotency_store.clear())
    bitrix_forwarder.configure_delivery_client_factory(FakeAsyncClient)
    bitrix_forwarder.reset_bitrix_endpoint_cache()
    FakeAsyncClient.responses = []
    FakeAsyncClient.calls = []


def teardown_function():
    bitrix_forwarder.reset_delivery_client_factory()
    bitrix_forwarder.reset_bitrix_endpoint_cache()


def test_duplicate_updates_are_not_delivered_twice(monkeypatch):
    monkeypatch.setenv("BITRIX_BOT_WEBHOOK_URLS", '{"internal":"https://bitrix.example/webhook"}')
    FakeAsyncClient.responses = [httpx.Response(200)]
    client = TestClient(app)
    update = {"update_id": 777, "message": {"message_id": 1, "date": 1710000000, "chat": {"id": 1, "type": "private"}, "text": "ping"}}

    first = client.post("/webhooks/telegram/internal", json=update)
    second = client.post("/webhooks/telegram/internal", json=update)

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(FakeAsyncClient.calls) == 1
    assert delivery_queue.stats()["delivered"] == 1


def test_failed_delivery_is_added_to_dead_letter(monkeypatch):
    monkeypatch.setenv("BITRIX_BOT_WEBHOOK_URLS", '{"dead":"https://bitrix.example/webhook"}')
    monkeypatch.setenv("BITRIX_FORWARD_RETRY_ATTEMPTS", "1")
    FakeAsyncClient.responses = [httpx.Response(500)]
    client = TestClient(app)

    response = client.post("/webhooks/telegram/dead", json={"update_id": 778})

    assert response.status_code == 200
    assert delivery_queue.stats()["dead_letter"] == 1
    assert delivery_queue.dead_letters()[0].bot_key == "dead"


def test_delivery_queue_persists_to_configured_file(monkeypatch, tmp_path):
    path = tmp_path / "persistent_queue.json"
    monkeypatch.setenv("DELIVERY_QUEUE_PATH", str(path))
    delivery_queue.clear()

    item = delivery_queue.enqueue("https://bitrix.example/webhook", "persist", {"update_id": 779})

    assert path.exists()
    assert delivery_queue.get(item.id).bot_key == "persist"


def test_admin_can_retry_dead_letter(monkeypatch):
    monkeypatch.setenv("BITRIX_BOT_WEBHOOK_URLS", '{"retry":"https://bitrix.example/webhook"}')
    monkeypatch.setenv("BITRIX_FORWARD_RETRY_ATTEMPTS", "1")
    FakeAsyncClient.responses = [httpx.Response(500)]
    client = TestClient(app)

    failed = client.post("/webhooks/telegram/retry", json={"update_id": 780})
    item = delivery_queue.dead_letters()[0]
    FakeAsyncClient.responses = [httpx.Response(200)]

    client.cookies.set("admin_session", "admin")
    retried = client.post(f"/admin/delivery/{item.id}/retry")

    assert failed.status_code == 200
    assert retried.status_code == 200
    assert delivery_queue.get(item.id).status == "delivered"


def test_health_and_metrics_endpoints(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_ALIASES", "{}")
    client = TestClient(app)

    live = client.get("/health/live")
    metrics = client.get("/metrics")

    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert metrics.status_code == 200
    assert "tg_connect_delivery_queue_items" in metrics.text
    assert "tg_connect_idempotency_records" in metrics.text
