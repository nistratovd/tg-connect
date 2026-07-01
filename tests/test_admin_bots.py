from fastapi.testclient import TestClient

from app.main import app
from app.models.bot_config import BotConfig
from app.security.secrets import load_master_key
from app.services.admin_store import get_admin_bot_config, load_admin_bot_configs, save_admin_bot_config


def test_admin_can_delete_bot(monkeypatch):
    monkeypatch.setenv("TG_CONNECT_MASTER_KEY", "test-master-key")
    load_master_key.cache_clear()
    save_admin_bot_config(BotConfig(id="delete-me", name="delete-me", telegram_bot_token="123:abc"))
    reload_calls = []

    async def fake_registry_reload():
        reload_calls.append("registry")
        return []

    async def fake_runner_reload():
        reload_calls.append("runner")

    monkeypatch.setattr("app.admin.routes.registry.reload", fake_registry_reload)
    monkeypatch.setattr("app.admin.routes.telegram_runner.reload", fake_runner_reload)

    client = TestClient(app)
    client.cookies.set("admin_session", "admin")

    response = client.post("/admin/bots/delete-me/delete", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/?message=%D0%91%D0%BE%D1%82%20%D1%83%D0%B4%D0%B0%D0%BB%D0%B5%D0%BD"
    assert get_admin_bot_config("delete-me") is None
    assert load_admin_bot_configs() == []
    assert reload_calls == ["registry", "runner"]


def test_admin_delete_missing_bot_shows_message():
    client = TestClient(app)
    client.cookies.set("admin_session", "admin")

    response = client.post("/admin/bots/missing/delete", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/?message=%D0%91%D0%BE%D1%82%20%D0%BD%D0%B5%20%D0%BD%D0%B0%D0%B9%D0%B4%D0%B5%D0%BD"
