from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class IdempotencyRecord:
    key: str
    created_at_monotonic: float
    ttl_seconds: float

    @property
    def expires_at_monotonic(self) -> float:
        return self.created_at_monotonic + self.ttl_seconds


class InMemoryIdempotencyStore:
    """Простое TTL-хранилище обработанных Telegram update_id.

    Хранилище защищает Битрикс от повторной доставки одного и того же update,
    если Telegram повторит webhook-запрос. Для production его можно заменить на
    Redis/PostgreSQL без изменения публичного API сервиса.
    """

    def __init__(self, default_ttl_seconds: float = 24 * 60 * 60) -> None:
        self.default_ttl_seconds = default_ttl_seconds
        self._records: dict[str, IdempotencyRecord] = {}
        self._lock = asyncio.Lock()

    async def seen_or_mark(self, namespace: str, value: Any, ttl_seconds: float | None = None) -> bool:
        """Возвращает True, если ключ уже встречался, иначе атомарно запоминает его."""
        if value is None:
            return False
        key = f"{namespace}:{value}"
        ttl = ttl_seconds or self.default_ttl_seconds
        now = time.monotonic()
        async with self._lock:
            self._purge_expired(now)
            if key in self._records:
                return True
            self._records[key] = IdempotencyRecord(key=key, created_at_monotonic=now, ttl_seconds=ttl)
            return False

    async def size(self) -> int:
        async with self._lock:
            self._purge_expired(time.monotonic())
            return len(self._records)

    async def clear(self) -> None:
        async with self._lock:
            self._records.clear()

    def _purge_expired(self, now: float) -> None:
        expired = [key for key, record in self._records.items() if record.expires_at_monotonic <= now]
        for key in expired:
            self._records.pop(key, None)


idempotency_store = InMemoryIdempotencyStore()
