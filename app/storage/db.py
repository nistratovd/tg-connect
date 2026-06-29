from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse, unquote

DEFAULT_DATABASE_URL = "sqlite:///data/tg_connect.db"


def database_url() -> str:
    if os.getenv("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    state_path = os.getenv("ADMIN_STATE_PATH") or os.getenv("DELIVERY_QUEUE_PATH")
    if state_path:
        return "sqlite:///" + str(Path(state_path).with_suffix(".sqlite3"))
    return DEFAULT_DATABASE_URL


def sqlite_path() -> Path:
    url = database_url()
    if not url.startswith("sqlite://"):
        raise RuntimeError("Сейчас поддерживается DATABASE_URL только формата sqlite:///path/to/db.sqlite3")
    parsed = urlparse(url)
    if parsed.netloc and parsed.netloc != ".":
        return Path(unquote(f"/{parsed.netloc}{parsed.path}"))
    raw_path = unquote(parsed.path)
    if raw_path.startswith("/"):
        raw_path = raw_path[1:]
    return Path(raw_path or "data/tg_connect.db")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = sqlite_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS admin_bots (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_admin_bots_name ON admin_bots(name);

            CREATE TABLE IF NOT EXISTS admin_events (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                direction TEXT NOT NULL,
                bot_key TEXT NOT NULL,
                status TEXT NOT NULL,
                payload TEXT,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_admin_events_created_at ON admin_events(created_at DESC);

            CREATE TABLE IF NOT EXISTS delivery_queue (
                id TEXT PRIMARY KEY,
                endpoint TEXT NOT NULL,
                bot_key TEXT NOT NULL,
                payload TEXT NOT NULL,
                update_id TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_delivery_queue_status_updated_at ON delivery_queue(status, updated_at DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_queue_bot_update ON delivery_queue(bot_key, update_id) WHERE update_id IS NOT NULL;
            """
        )
