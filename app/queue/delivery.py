from __future__ import annotations

import secrets
from collections import deque
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DeliveryQueueItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: secrets.token_hex(8))
    endpoint: str
    bot_key: str
    payload: dict[str, Any]
    attempts: int = 0
    status: str = "queued"
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class InMemoryDeliveryQueue:
    """Минимальная outbox/dead-letter очередь для прозрачной эксплуатации.

    Доставка пока выполняется синхронно в HTTP-request lifecycle, но каждый update
    фиксируется в очереди, получает итоговый статус и сохраняется в DLQ при ошибке.
    """

    def __init__(self, max_items: int = 500) -> None:
        self._items: deque[DeliveryQueueItem] = deque(maxlen=max_items)
        self._dead_letters: deque[DeliveryQueueItem] = deque(maxlen=max_items)

    def enqueue(self, endpoint: str, bot_key: str, payload: dict[str, Any]) -> DeliveryQueueItem:
        item = DeliveryQueueItem(endpoint=endpoint, bot_key=bot_key, payload=payload)
        self._items.appendleft(item)
        return item

    def mark_delivered(self, item: DeliveryQueueItem, attempts: int) -> None:
        item.status = "delivered"
        item.attempts = attempts
        item.error = None
        item.updated_at = utc_now()

    def mark_failed(self, item: DeliveryQueueItem, attempts: int, error: str | None) -> None:
        item.status = "dead_letter"
        item.attempts = attempts
        item.error = error
        item.updated_at = utc_now()
        self._dead_letters.appendleft(item)

    def recent(self, limit: int = 50) -> list[DeliveryQueueItem]:
        return list(self._items)[:limit]

    def dead_letters(self, limit: int = 50) -> list[DeliveryQueueItem]:
        return list(self._dead_letters)[:limit]

    def stats(self) -> dict[str, int]:
        queued = sum(1 for item in self._items if item.status == "queued")
        delivered = sum(1 for item in self._items if item.status == "delivered")
        failed = sum(1 for item in self._items if item.status == "dead_letter")
        return {"queued": queued, "delivered": delivered, "dead_letter": failed}

    def clear(self) -> None:
        self._items.clear()
        self._dead_letters.clear()


delivery_queue = InMemoryDeliveryQueue()
