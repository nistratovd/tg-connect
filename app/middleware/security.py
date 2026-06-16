from __future__ import annotations

import hmac
import ipaddress
import logging
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from hashlib import sha256
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.models.bot_config import BotConfig
from app.security_utils import mask_sensitive
from app.services.bot_registry import BotRegistryError, registry

logger = logging.getLogger(__name__)

_SIGNATURE_HEADER = "x-tg-signature"
_TIMESTAMP_HEADER = "x-tg-timestamp"
_DEFAULT_RATE_WINDOW_SECONDS = 60

RateLimitKey = tuple[str, str, str]


class SecurityError(Exception):
    def __init__(self, status_code: int, code: str, log_message: str) -> None:
        super().__init__(log_message)
        self.status_code = status_code
        self.code = code
        self.log_message = log_message


class SecurityMiddleware(BaseHTTPMiddleware):
    """Защищает Telegram-compatible endpoints на уровне HTTP-запроса."""

    def __init__(self, app: Any) -> None:
        super().__init__(app)
        self._hits: dict[RateLimitKey, deque[float]] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        route_info = self._parse_bot_route(request.url.path)
        if route_info is None:
            return await call_next(request)

        token_or_alias, method = route_info
        try:
            await registry.load_active_bots()
            config = registry.get_config(token_or_alias)
            if config is not None:
                body = await self._read_limited_body(request, config)
                self._check_ip(request, config)
                self._check_timestamp(request, config)
                self._check_signature(request, config, body)
                self._check_rate_limit(request, config, method)
        except BotRegistryError as exc:
            logger.warning("Ошибка реестра ботов при проверке безопасности: %s", mask_sensitive(str(exc)))
            return self._error_response(503, "security_unavailable")
        except SecurityError as exc:
            logger.warning("Запрос отклонен middleware безопасности: %s", mask_sensitive(exc.log_message))
            return self._error_response(exc.status_code, exc.code)
        except Exception:  # noqa: BLE001 - наружу возвращается унифицированная ошибка без деталей.
            logger.exception("Внутренняя ошибка middleware безопасности")
            return self._error_response(500, "security_error")

        return await call_next(request)

    @staticmethod
    def _parse_bot_route(path: str) -> tuple[str, str] | None:
        if not path.startswith("/bot"):
            return None
        rest = path.removeprefix("/bot")
        if "/" not in rest:
            return None
        token_or_alias, method = rest.split("/", 1)
        if not token_or_alias or not method:
            return None
        return token_or_alias, method.split("/", 1)[0]

    @staticmethod
    async def _read_limited_body(request: Request, config: BotConfig) -> bytes:
        max_bytes = config.max_request_body_bytes
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > max_bytes:
            raise SecurityError(413, "request_too_large", f"body is too large for bot {config.id}")
        body = await request.body()
        if len(body) > max_bytes:
            raise SecurityError(413, "request_too_large", f"body is too large for bot {config.id}")
        return body

    @staticmethod
    def _check_ip(request: Request, config: BotConfig) -> None:
        if not config.allowed_ips:
            return
        client_ip = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        if not client_ip and request.client:
            client_ip = request.client.host
        try:
            ip = ipaddress.ip_address(client_ip)
            allowed = any(ip in ipaddress.ip_network(item, strict=False) for item in config.allowed_ips)
        except ValueError as exc:
            raise SecurityError(403, "forbidden", f"invalid or forbidden ip for bot {config.id}") from exc
        if not allowed:
            raise SecurityError(403, "forbidden", f"forbidden ip {client_ip} for bot {config.id}")

    @staticmethod
    def _check_timestamp(request: Request, config: BotConfig) -> None:
        if not config.require_hmac_signature:
            return
        raw_timestamp = request.headers.get(_TIMESTAMP_HEADER)
        try:
            timestamp = int(raw_timestamp or "")
        except ValueError as exc:
            raise SecurityError(401, "invalid_signature", f"invalid timestamp for bot {config.id}") from exc
        if abs(int(time.time()) - timestamp) > config.timestamp_tolerance_seconds:
            raise SecurityError(401, "invalid_signature", f"expired timestamp for bot {config.id}")

    @staticmethod
    def _check_signature(request: Request, config: BotConfig, body: bytes) -> None:
        if not config.require_hmac_signature:
            return
        signature = request.headers.get(_SIGNATURE_HEADER, "")
        payload = request.headers[_TIMESTAMP_HEADER].encode() + b"." + body
        expected = hmac.new(config.effective_hmac_secret.encode(), payload, sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise SecurityError(401, "invalid_signature", f"invalid signature for bot {config.id}")

    def _check_rate_limit(self, request: Request, config: BotConfig, method: str) -> None:
        if config.rate_limit is None:
            return
        client_ip = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        if not client_ip and request.client:
            client_ip = request.client.host
        now = time.monotonic()
        key = (config.id, client_ip, method)
        hits = self._hits[key]
        while hits and now - hits[0] >= _DEFAULT_RATE_WINDOW_SECONDS:
            hits.popleft()
        if len(hits) >= config.rate_limit:
            raise SecurityError(429, "rate_limited", f"rate limit exceeded for bot {config.id}")
        hits.append(now)

    @staticmethod
    def _error_response(status_code: int, code: str) -> JSONResponse:
        return JSONResponse(status_code=status_code, content={"ok": False, "error": code})
