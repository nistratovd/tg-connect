from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

DEFAULT_DELIVERY_QUEUE_PATH = "data/delivery_queue.json"
TERMINAL_STATUSES = {"delivered", "dead_letter"}
ACTIVE_STATUSES = {"queued", "processing", "delivered"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def delivery_queue_path() -> Path:
    return Path(os.getenv("DELIVERY_QUEUE_PATH", DEFAULT_DELIVERY_QUEUE_PATH))


class DeliveryQueueItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: secrets.token_hex(8))
    endpoint: str
    bot_key: str
    payload: dict[str, Any]
    update_id: int | None = None
    attempts: int = 0
    status: str = "queued"
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class FileDeliveryQueue:
    """Persistent outbox/dead-letter queue для Telegram → Битрикс доставки.

    Очередь хранит элементы в JSON-файле, поэтому состояние не теряется при
    рестарте процесса. API синхронный и защищен process-local lock; для
    нескольких инстансов следует заменить backend на PostgreSQL/Redis/RabbitMQ.
    """

    def __init__(self, max_items: int = 1000) -> None:
        self.max_items = max_items
        self._lock = RLock()

    def enqueue(self, endpoint: str, bot_key: str, payload: dict[str, Any]) -> DeliveryQueueItem:
        update_id = payload.get("update_id")
        with self._lock:
            existing = self.find_by_update(bot_key, update_id)
            if existing and existing.status in ACTIVE_STATUSES:
                return existing
            item = DeliveryQueueItem(endpoint=endpoint, bot_key=bot_key, payload=payload, update_id=update_id)
            items = [item, *self._load_items()]
            self._save_items(items[: self.max_items])
            return item

    def get(self, item_id: str) -> DeliveryQueueItem | None:
        return next((item for item in self._load_items() if item.id == item_id), None)

    def find_by_update(self, bot_key: str, update_id: Any) -> DeliveryQueueItem | None:
        if update_id is None:
            return None
        return next(
            (
                item
                for item in self._load_items()
                if item.bot_key == bot_key and str(item.update_id) == str(update_id)
            ),
            None,
        )

    def mark_processing(self, item_id: str) -> DeliveryQueueItem | None:
        return self._update_item(item_id, status="processing", updated_at=utc_now())

    def mark_delivered(self, item_or_id: DeliveryQueueItem | str, attempts: int) -> DeliveryQueueItem | None:
        item_id = item_or_id.id if isinstance(item_or_id, DeliveryQueueItem) else item_or_id
        return self._update_item(item_id, status="delivered", attempts=attempts, error=None, updated_at=utc_now())

    def mark_failed(self, item_or_id: DeliveryQueueItem | str, attempts: int, error: str | None) -> DeliveryQueueItem | None:
        item_id = item_or_id.id if isinstance(item_or_id, DeliveryQueueItem) else item_or_id
        return self._update_item(item_id, status="dead_letter", attempts=attempts, error=error, updated_at=utc_now())

    def retry_dead_letter(self, item_id: str) -> DeliveryQueueItem | None:
        item = self.get(item_id)
        if item is None or item.status != "dead_letter":
            return None
        return self._update_item(item_id, status="queued", error=None, updated_at=utc_now())

    def pending(self, limit: int = 50) -> list[DeliveryQueueItem]:
        return [item for item in self._load_items() if item.status == "queued"][:limit]

    def recent(self, limit: int = 50) -> list[DeliveryQueueItem]:
        return self._load_items()[:limit]

    def dead_letters(self, limit: int = 50) -> list[DeliveryQueueItem]:
        return [item for item in self._load_items() if item.status == "dead_letter"][:limit]

    def stats(self) -> dict[str, int]:
        items = self._load_items()
        return {
            "queued": sum(1 for item in items if item.status == "queued"),
            "processing": sum(1 for item in items if item.status == "processing"),
            "delivered": sum(1 for item in items if item.status == "delivered"),
            "dead_letter": sum(1 for item in items if item.status == "dead_letter"),
        }

    def clear(self) -> None:
        with self._lock:
            path = delivery_queue_path()
            if path.exists():
                path.unlink()

    def _update_item(self, item_id: str, **changes: Any) -> DeliveryQueueItem | None:
        with self._lock:
            items = self._load_items()
            updated: DeliveryQueueItem | None = None
            for index, item in enumerate(items):
                if item.id == item_id:
                    updated = item.model_copy(update=changes)
                    items[index] = updated
                    break
            if updated is not None:
                self._save_items(items)
            return updated

    def _load_items(self) -> list[DeliveryQueueItem]:
        path = delivery_queue_path()
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return TypeAdapter(list[DeliveryQueueItem]).validate_python(payload)
        except (json.JSONDecodeError, ValidationError):
            return []

    def _save_items(self, items: list[DeliveryQueueItem]) -> None:
        path = delivery_queue_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [item.model_dump(mode="json") for item in items]
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# Backward-compatible alias: тесты и импортирующий код могут продолжать использовать старое имя.
InMemoryDeliveryQueue = FileDeliveryQueue

delivery_queue = FileDeliveryQueue()
