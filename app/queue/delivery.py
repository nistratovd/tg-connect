from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.storage.db import connect, init_db

TERMINAL_STATUSES = {"delivered", "dead_letter", "canceled"}
ACTIVE_STATUSES = {"queued", "processing", "delivered"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


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


class DatabaseDeliveryQueue:
    """Persistent outbox/dead-letter queue в SQLite базе данных."""

    def __init__(self, max_items: int = 1000) -> None:
        self.max_items = max_items
        self._lock = RLock()
        init_db()

    def enqueue(self, endpoint: str, bot_key: str, payload: dict[str, Any]) -> DeliveryQueueItem:
        update_id = payload.get("update_id")
        with self._lock:
            existing = self.find_by_update(bot_key, update_id)
            if existing and existing.status in ACTIVE_STATUSES:
                return existing
            item = DeliveryQueueItem(endpoint=endpoint, bot_key=bot_key, payload=payload, update_id=update_id)
            with connect() as conn:
                conn.execute(
                    """
                    INSERT INTO delivery_queue(id, endpoint, bot_key, payload, update_id, attempts, status, error, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._to_record(item),
                )
                self._trim(conn)
            self._mirror_legacy_queue_file()
            return item

    def get(self, item_id: str) -> DeliveryQueueItem | None:
        with connect() as conn:
            row = conn.execute("SELECT * FROM delivery_queue WHERE id = ?", (item_id,)).fetchone()
        return self._from_row(row) if row else None

    def find_by_update(self, bot_key: str, update_id: Any) -> DeliveryQueueItem | None:
        if update_id is None:
            return None
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM delivery_queue WHERE bot_key = ? AND update_id = ? ORDER BY created_at DESC LIMIT 1",
                (bot_key, str(update_id)),
            ).fetchone()
        return self._from_row(row) if row else None

    def mark_processing(self, item_id: str) -> DeliveryQueueItem | None:
        item = self.get(item_id)
        if item is None or item.status == "canceled":
            return None
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

    def cancel_queued(self, item_id: str) -> DeliveryQueueItem | None:
        item = self.get(item_id)
        if item is None or item.status != "queued":
            return None
        return self._update_item(item_id, status="canceled", error="Отменено администратором", updated_at=utc_now())

    def pending(self, limit: int = 50) -> list[DeliveryQueueItem]:
        return self._select("WHERE status = 'queued' ORDER BY created_at ASC LIMIT ?", limit)

    def recent(self, limit: int = 50) -> list[DeliveryQueueItem]:
        return self._select("ORDER BY updated_at DESC LIMIT ?", limit)

    def dead_letters(self, limit: int = 50) -> list[DeliveryQueueItem]:
        return self._select("WHERE status = 'dead_letter' ORDER BY updated_at DESC LIMIT ?", limit)

    def stats(self) -> dict[str, int]:
        stats = {"queued": 0, "processing": 0, "delivered": 0, "dead_letter": 0, "canceled": 0}
        with connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS count FROM delivery_queue GROUP BY status").fetchall()
        for row in rows:
            stats[str(row["status"])] = int(row["count"])
        return stats

    def clear(self) -> None:
        init_db()
        with self._lock, connect() as conn:
            conn.execute("DELETE FROM delivery_queue")
        self._mirror_legacy_queue_file()

    def _update_item(self, item_id: str, **changes: Any) -> DeliveryQueueItem | None:
        with self._lock, connect() as conn:
            current = conn.execute("SELECT * FROM delivery_queue WHERE id = ?", (item_id,)).fetchone()
            if current is None:
                return None
            item = self._from_row(current).model_copy(update=changes)
            conn.execute(
                """
                UPDATE delivery_queue
                SET endpoint = ?, bot_key = ?, payload = ?, update_id = ?, attempts = ?, status = ?, error = ?, created_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (*self._to_record(item)[1:], item.id),
            )
            self._mirror_legacy_queue_file()
            return item

    def _select(self, clause: str, limit: int) -> list[DeliveryQueueItem]:
        with connect() as conn:
            rows = conn.execute(f"SELECT * FROM delivery_queue {clause}", (limit,)).fetchall()
        return [self._from_row(row) for row in rows]

    def _trim(self, conn: Any) -> None:
        conn.execute(
            "DELETE FROM delivery_queue WHERE id NOT IN (SELECT id FROM delivery_queue ORDER BY updated_at DESC LIMIT ?)",
            (self.max_items,),
        )


    def _mirror_legacy_queue_file(self) -> None:
        path = os.getenv("DELIVERY_QUEUE_PATH")
        if not path:
            return
        queue_path = Path(path)
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        items = [item.model_dump(mode="json") for item in self.recent(self.max_items)]
        queue_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _to_record(item: DeliveryQueueItem) -> tuple[Any, ...]:
        return (
            item.id,
            item.endpoint,
            item.bot_key,
            json.dumps(item.payload, ensure_ascii=False),
            None if item.update_id is None else str(item.update_id),
            item.attempts,
            item.status,
            item.error,
            item.created_at.isoformat(),
            item.updated_at.isoformat(),
        )

    @staticmethod
    def _from_row(row: Any) -> DeliveryQueueItem:
        payload = dict(row)
        payload["payload"] = json.loads(payload["payload"])
        payload["created_at"] = _dt(payload["created_at"])
        payload["updated_at"] = _dt(payload["updated_at"])
        return DeliveryQueueItem.model_validate(payload)


# Backward-compatible alias.
FileDeliveryQueue = DatabaseDeliveryQueue
InMemoryDeliveryQueue = DatabaseDeliveryQueue

delivery_queue = DatabaseDeliveryQueue()
