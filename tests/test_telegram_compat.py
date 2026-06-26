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

    def __getattr__(self, name: str):
        async def method(**params):
            self.calls.append((name, params))
            return {"method": name, "params": params}

        return method


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
    response = client.post("/bottoken/unsupportedMethod", json={"chat_id": 100})

    assert response.status_code == 404
    assert response.json() == {"ok": False, "description": "Method unsupportedMethod is not supported"}


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


@pytest.mark.parametrize(
    ("method", "payload", "expected_call"),
    [
        ("forwardMessage", {"chat_id": 1, "from_chat_id": 2, "message_id": 3}, "forward_message"),
        ("copyMessage", {"chat_id": 1, "from_chat_id": 2, "message_id": 3}, "copy_message"),
        ("sendMediaGroup", {"chat_id": 1, "media": [{"type": "photo", "media": "file_id"}]}, "send_media_group"),
        ("sendVideo", {"chat_id": 1, "video": "file_id"}, "send_video"),
        ("sendAudio", {"chat_id": 1, "audio": "file_id"}, "send_audio"),
        ("sendVoice", {"chat_id": 1, "voice": "file_id"}, "send_voice"),
        ("sendAnimation", {"chat_id": 1, "animation": "file_id"}, "send_animation"),
        ("sendLocation", {"chat_id": 1, "latitude": 55.75, "longitude": 37.62}, "send_location"),
        ("sendContact", {"chat_id": 1, "phone_number": "+100", "first_name": "Test"}, "send_contact"),
        ("sendPoll", {"chat_id": 1, "question": "Q?", "options": ["A", "B"]}, "send_poll"),
        ("pinChatMessage", {"chat_id": 1, "message_id": 3}, "pin_chat_message"),
        ("unpinChatMessage", {"chat_id": 1}, "unpin_chat_message"),
        ("setMessageReaction", {"chat_id": 1, "message_id": 3}, "set_message_reaction"),
        ("getChat", {"chat_id": 1}, "get_chat"),
        ("getChatMember", {"chat_id": 1, "user_id": 7}, "get_chat_member"),
        ("getFile", {"file_id": "abc"}, "get_file"),
        ("setWebhook", {"url": "https://example.com/hook"}, "set_webhook"),
        ("deleteWebhook", {}, "delete_webhook"),
        ("getWebhookInfo", {}, "get_webhook_info"),
    ],
)
def test_requested_telegram_methods(client, fake_bot_factory, method, payload, expected_call):
    response = client.post(f"/bottoken/{method}", json=payload)

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert fake_bot_factory["token"].calls[-1] == (expected_call, payload)


def test_json_encoded_form_fields_are_parsed(client, fake_bot_factory):
    response = client.post(
        "/bottoken/sendPoll",
        data={"chat_id": "1", "question": "Q?", "options": '["A", "B"]'},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert fake_bot_factory["token"].calls[-1] == (
        "send_poll",
        {"chat_id": "1", "question": "Q?", "options": ["A", "B"]},
    )


def _signed_headers(secret: str, body: bytes, timestamp: int | None = None) -> dict[str, str]:
    import hmac
    import time
    from hashlib import sha256

    ts = str(timestamp or int(time.time()))
    signature = hmac.new(secret.encode(), ts.encode() + b"." + body, sha256).hexdigest()
    return {"x-tg-timestamp": ts, "x-tg-signature": signature}


def test_security_accepts_valid_hmac_signature(client, fake_bot_factory, monkeypatch):
    monkeypatch.setenv(
        "TELEGRAM_BOT_CONFIGS",
        '[{"id":"secure","name":"secure","telegram_bot_token":"321:securetoken","hmac_secret":"top-secret"}]',
    )
    body = b'{"chat_id":100,"text":"secure"}'

    response = client.post(
        "/botsecure/sendMessage",
        content=body,
        headers={"content-type": "application/json", **_signed_headers("top-secret", body)},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_security_rejects_invalid_hmac_signature(client, fake_bot_factory, monkeypatch):
    monkeypatch.setenv(
        "TELEGRAM_BOT_CONFIGS",
        '[{"id":"secure-bad","name":"secure-bad","telegram_bot_token":"322:securetoken","hmac_secret":"top-secret"}]',
    )

    response = client.post(
        "/botsecure-bad/sendMessage",
        json={"chat_id": 100, "text": "secure"},
        headers={"x-tg-timestamp": "1", "x-tg-signature": "bad"},
    )

    assert response.status_code == 401
    assert response.json() == {"ok": False, "error": "invalid_signature"}


def test_security_rejects_forbidden_ip(client, fake_bot_factory, monkeypatch):
    monkeypatch.setenv(
        "TELEGRAM_BOT_CONFIGS",
        '[{"id":"ipbot","name":"ipbot","telegram_bot_token":"323:securetoken","allowed_ips":["10.0.0.0/8"]}]',
    )

    response = client.post(
        "/botipbot/sendMessage",
        json={"chat_id": 100, "text": "secure"},
        headers={"x-forwarded-for": "192.168.1.10"},
    )

    assert response.status_code == 403
    assert response.json() == {"ok": False, "error": "forbidden"}


def test_security_rate_limits_by_bot_ip_and_method(client, fake_bot_factory, monkeypatch):
    monkeypatch.setenv(
        "TELEGRAM_BOT_CONFIGS",
        '[{"id":"limited","name":"limited","telegram_bot_token":"324:securetoken","rate_limit":1}]',
    )
    headers = {"x-forwarded-for": "203.0.113.10"}

    first = client.post("/botlimited/sendMessage", json={"chat_id": 100, "text": "one"}, headers=headers)
    second = client.post("/botlimited/sendMessage", json={"chat_id": 100, "text": "two"}, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json() == {"ok": False, "error": "rate_limited"}


def test_security_limits_request_body_size(client, fake_bot_factory, monkeypatch):
    monkeypatch.setenv(
        "TELEGRAM_BOT_CONFIGS",
        '[{"id":"small","name":"small","telegram_bot_token":"325:securetoken","max_request_body_bytes":10}]',
    )

    response = client.post("/botsmall/sendMessage", json={"chat_id": 100, "text": "too large"})

    assert response.status_code == 413
    assert response.json() == {"ok": False, "error": "request_too_large"}
