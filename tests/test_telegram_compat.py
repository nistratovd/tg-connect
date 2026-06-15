import pytest
from fastapi.testclient import TestClient

from app.api import telegram_compat
from app.main import app


class FakeBot:
    def __init__(self, token: str) -> None:
        self.token = token
        self.calls = []

    async def send_message(self, **params):
        self.calls.append(("send_message", params))
        return {"message_id": 42, "chat": {"id": params["chat_id"]}, "text": params["text"]}

    async def send_photo(self, **params):
        self.calls.append(("send_photo", params))
        return {"message_id": 43, "photo": params["photo"]}

    async def send_document(self, **params):
        self.calls.append(("send_document", params))
        return {"message_id": 44, "document": params["document"]}

    async def edit_message_text(self, **params):
        self.calls.append(("edit_message_text", params))
        return {"message_id": params.get("message_id"), "text": params["text"]}

    async def delete_message(self, **params):
        self.calls.append(("delete_message", params))
        return True

    async def answer_callback_query(self, **params):
        self.calls.append(("answer_callback_query", params))
        return True


@pytest.fixture(autouse=True)
def fake_bot_factory(monkeypatch):
    bots = {}

    def factory(token: str) -> FakeBot:
        bot = FakeBot(token)
        bots[token] = bot
        return bot

    monkeypatch.setenv("TELEGRAM_BOT_ALIASES", '{"internal":"123:abc"}')
    telegram_compat._BOT_ALIASES = None
    telegram_compat.configure_bot_factory(factory)
    yield bots
    telegram_compat.reset_bot_factory()
    telegram_compat._BOT_ALIASES = None


@pytest.fixture
def client():
    return TestClient(app)


def test_send_message_by_token(client, fake_bot_factory):
    response = client.post("/bot999:token/sendMessage", json={"chat_id": 100, "text": "Привет"})

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "result": {"message_id": 42, "chat": {"id": 100}, "text": "Привет"},
    }
    assert fake_bot_factory["999:token"].calls == [("send_message", {"chat_id": 100, "text": "Привет"})]


def test_send_message_by_alias(client, fake_bot_factory):
    response = client.post("/botinternal/sendMessage", json={"chat_id": "@channel", "text": "Hi"})

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert fake_bot_factory["123:abc"].token == "123:abc"


def test_validates_required_params(client):
    response = client.post("/bottoken/sendPhoto", json={"chat_id": 100})

    assert response.status_code == 400
    assert response.json()["ok"] is False
    assert "photo" in response.json()["description"]


def test_rejects_unsupported_method(client):
    response = client.post("/bottoken/forwardMessage", json={"chat_id": 100})

    assert response.status_code == 404
    assert response.json() == {"ok": False, "description": "Method forwardMessage is not supported"}


def test_edit_message_text_requires_target(client):
    response = client.post("/bottoken/editMessageText", json={"text": "Новый текст"})

    assert response.status_code == 400
    assert response.json() == {
        "ok": False,
        "description": "editMessageText requires either inline_message_id or both chat_id and message_id",
    }


@pytest.mark.parametrize(
    ("method", "payload", "expected_call"),
    [
        ("sendDocument", {"chat_id": 1, "document": "file_id"}, "send_document"),
        ("deleteMessage", {"chat_id": 1, "message_id": 2}, "delete_message"),
        ("answerCallbackQuery", {"callback_query_id": "cbq"}, "answer_callback_query"),
    ],
)
def test_supported_methods(client, fake_bot_factory, method, payload, expected_call):
    response = client.post(f"/bottoken/{method}", json=payload)

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert fake_bot_factory["token"].calls[-1] == (expected_call, payload)
