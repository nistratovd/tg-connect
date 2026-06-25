import asyncio
import socket

from app.services.wireguard import WireGuardManager, WireGuardSettings, load_wireguard_settings


def test_wireguard_settings_from_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WIREGUARD_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_WIREGUARD_INTERFACE", "wg-tg")
    monkeypatch.setenv("TELEGRAM_WIREGUARD_CONFIG_PATH", "/etc/wireguard/tg.conf")
    monkeypatch.setenv("TELEGRAM_WIREGUARD_TELEGRAM_HOSTS", "api.telegram.org,example.org")
    monkeypatch.setenv("TELEGRAM_WIREGUARD_AUTO_UP", "yes")
    monkeypatch.setenv("TELEGRAM_WIREGUARD_EXTRA_ROUTES", "149.154.160.0/20,91.108.4.0/22")

    settings = load_wireguard_settings()

    assert settings.enabled is True
    assert settings.interface == "wg-tg"
    assert settings.config_path == "/etc/wireguard/tg.conf"
    assert settings.telegram_hosts == ("api.telegram.org", "example.org")
    assert settings.auto_up is True
    assert settings.extra_routes == ("149.154.160.0/20", "91.108.4.0/22")


def test_manager_adds_only_telegram_routes(monkeypatch):
    calls = []
    settings = WireGuardSettings(enabled=True, interface="wg-tg", telegram_hosts=("api.telegram.org",), auto_up=False)
    manager = WireGuardManager(settings_loader=lambda: settings)

    async def fake_run(command, loaded_settings, check=True):
        calls.append(command)

    async def fake_to_thread(func, *args, **kwargs):
        assert func is socket.getaddrinfo
        return [(socket.AF_INET, None, None, None, ("149.154.167.220", 443))]

    monkeypatch.setattr(manager, "_run", fake_run)
    monkeypatch.setattr("app.services.wireguard.asyncio.to_thread", fake_to_thread)

    asyncio.run(manager.ensure_started())

    assert calls == [("ip", "route", "replace", "149.154.167.220/32", "dev", "wg-tg")]
