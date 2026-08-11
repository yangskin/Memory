"""Durable SQLite Outbox and shared-context cache, using only stdlib."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SyncStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS outbox_events (
                    local_seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL, content_hash TEXT NOT NULL, user_id TEXT, created_at TEXT NOT NULL,
                    next_retry_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'pending', last_error TEXT, acknowledged_at TEXT);
                CREATE TABLE IF NOT EXISTS shared_cache (
                    cache_key TEXT PRIMARY KEY, payload_json TEXT NOT NULL, fetched_at TEXT NOT NULL,
                    expires_at TEXT, server_seq INTEGER);
                CREATE TABLE IF NOT EXISTS sync_state (
                    key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL);
            """)
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(outbox_events)")}
            if "user_id" not in columns:
                conn.execute("ALTER TABLE outbox_events ADD COLUMN user_id TEXT")
            conn.execute("UPDATE outbox_events SET state='pending' WHERE state='uploading'")

    def enqueue(self, event_id: str, payload: dict[str, Any], content_hash: str, user_id: str | None = None) -> bool:
        now = _now()
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO outbox_events(event_id,payload_json,content_hash,user_id,created_at,next_retry_at) VALUES(?,?,?,?,?,?)",
                (event_id, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), content_hash, user_id, now, now),
            )
            return cursor.rowcount == 1

    def due_events(self, limit: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM outbox_events WHERE state='pending' AND next_retry_at<=? ORDER BY local_seq LIMIT ?", (_now(), limit)).fetchall()
        return [dict(row) for row in rows]

    def claim_due_events(self, limit: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM outbox_events WHERE state='pending' AND next_retry_at<=? ORDER BY local_seq LIMIT ?", (_now(), limit)).fetchall()
            event_ids = [str(row["event_id"]) for row in rows]
            conn.executemany("UPDATE outbox_events SET state='uploading' WHERE event_id=? AND state='pending'", [(event_id,) for event_id in event_ids])
        return [dict(row) for row in rows]

    def acknowledge(self, event_ids: list[str]) -> None:
        if not event_ids:
            return
        with self._connect() as conn:
            conn.executemany("UPDATE outbox_events SET state='acknowledged',acknowledged_at=? WHERE event_id=?", [(_now(), event_id) for event_id in event_ids])

    def reject(self, event_id: str, code: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE outbox_events SET state='rejected',last_error=? WHERE event_id=?", (code, event_id))

    def retry(self, event_id: str, error: str, retry_at: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE outbox_events SET state='pending',attempts=attempts+1,last_error=?,next_retry_at=? WHERE event_id=?", (error[:300], retry_at, event_id))

    def put_state(self, key: str, value: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO sync_state(key,value_json,updated_at) VALUES(?,?,?)", (key, json.dumps(value, ensure_ascii=False), _now()))

    def get_state(self, key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value_json FROM sync_state WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(str(row["value_json"]))
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def delete_state(self, key: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sync_state WHERE key=?", (key,))

    def put_cache(self, key: str, payload: dict[str, Any], expires_at: str | None, server_seq: int | None = None) -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO shared_cache(cache_key,payload_json,fetched_at,expires_at,server_seq) VALUES(?,?,?,?,?)", (key, json.dumps(payload, ensure_ascii=False), _now(), expires_at, server_seq))

    def get_cache(self, key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM shared_cache WHERE cache_key=?", (key,)).fetchone()
        return dict(row) if row else None