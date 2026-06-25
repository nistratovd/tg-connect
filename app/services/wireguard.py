from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
from dataclasses import dataclass, field
from typing import Iterable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WireGuardSettings:
    """Настройки маршрутизации Telegram API через WireGuard."""

    enabled: bool = False
    interface: str = "wg0"
    config_path: str | None = None
    telegram_hosts: tuple[str, ...] = ("api.telegram.org",)
    auto_up: bool = False
    route_allowed_ips: bool = True
    command_timeout_seconds: float = 15.0
    extra_routes: tuple[str, ...] = field(default_factory=tuple)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "да"}


def _env_csv(name: str, default: Iterable[str]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None:
        return tuple(default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def load_wireguard_settings() -> WireGuardSettings:
    return WireGuardSettings(
        enabled=_env_bool("TELEGRAM_WIREGUARD_ENABLED"),
        interface=os.getenv("TELEGRAM_WIREGUARD_INTERFACE", "wg0").strip() or "wg0",
        config_path=(os.getenv("TELEGRAM_WIREGUARD_CONFIG_PATH") or "").strip() or None,
        telegram_hosts=_env_csv("TELEGRAM_WIREGUARD_TELEGRAM_HOSTS", ("api.telegram.org",)),
        auto_up=_env_bool("TELEGRAM_WIREGUARD_AUTO_UP"),
        route_allowed_ips=_env_bool("TELEGRAM_WIREGUARD_ROUTE_ALLOWED_IPS", True),
        command_timeout_seconds=float(os.getenv("TELEGRAM_WIREGUARD_COMMAND_TIMEOUT_SECONDS", "15")),
        extra_routes=_env_csv("TELEGRAM_WIREGUARD_EXTRA_ROUTES", ()),
    )


class WireGuardManager:
    """Поднимает WireGuard и добавляет host routes только для Telegram API.

    Битрикс-запросы не используют этот менеджер: они продолжают идти через
    обычный системный маршрут, если администратор не настроил default route в ОС.
    """

    def __init__(self, settings_loader=load_wireguard_settings) -> None:
        self._settings_loader = settings_loader
        self._started = False
        self._lock = asyncio.Lock()

    async def ensure_started(self) -> None:
        settings = self._settings_loader()
        if not settings.enabled:
            return
        async with self._lock:
            if self._started:
                return
            if settings.auto_up:
                await self._run(("wg-quick", "up", settings.config_path or settings.interface), settings)
            if settings.route_allowed_ips:
                for network in await self._telegram_networks(settings):
                    await self._run(("ip", "route", "replace", network, "dev", settings.interface), settings)
            self._started = True
            logger.info("WireGuard-маршрутизация Telegram API включена через интерфейс %s", settings.interface)

    async def stop(self) -> None:
        settings = self._settings_loader()
        if not settings.enabled or not settings.auto_up or not self._started:
            return
        async with self._lock:
            await self._run(("wg-quick", "down", settings.config_path or settings.interface), settings, check=False)
            self._started = False

    async def _telegram_networks(self, settings: WireGuardSettings) -> list[str]:
        networks = list(settings.extra_routes)
        for host in settings.telegram_hosts:
            for family, _, _, _, sockaddr in await asyncio.to_thread(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM):
                ip = sockaddr[0]
                prefix = 32 if family == socket.AF_INET else 128
                networks.append(str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False)))
        return sorted(set(networks))

    async def _run(self, command: tuple[str, ...], settings: WireGuardSettings, check: bool = True) -> None:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=settings.command_timeout_seconds)
        if check and process.returncode != 0:
            raise RuntimeError(
                f"Команда {' '.join(command)} завершилась с кодом {process.returncode}: "
                f"{stderr.decode().strip() or stdout.decode().strip()}"
            )


wireguard_manager = WireGuardManager()
