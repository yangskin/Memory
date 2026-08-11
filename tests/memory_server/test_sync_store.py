from __future__ import annotations

import sqlite3

from servers.memory_server.memory_sync_store import SyncStore
from servers.memory_server.memory_sync_protocol import build_memory_event


def test_outbox_is_idempotent_and_survives_reopen(tmp_path) -> None:
    path = tmp_path / "shared-sync.db"
    store = SyncStore(path)
    assert store.enqueue("evt_1", {"event_id": "evt_1"}, "sha256:one")
    assert not store.enqueue("evt_1", {"event_id": "evt_1"}, "sha256:one")
    reopened = SyncStore(path)
    assert [item["event_id"] for item in reopened.due_events(20)] == ["evt_1"]
    reopened.acknowledge(["evt_1"])
    assert reopened.due_events(20) == []


def test_rejected_event_is_not_retried(tmp_path) -> None:
    store = SyncStore(tmp_path / "shared-sync.db")
    store.enqueue("evt_2", {"event_id": "evt_2"}, "sha256:two")
    store.reject("evt_2", "payload_too_large")
    assert store.due_events(20) == []


def test_claimed_event_recovers_after_restart(tmp_path) -> None:
    path = tmp_path / "shared-sync.db"
    store = SyncStore(path)
    store.enqueue("evt_3", {"event_id": "evt_3"}, "sha256:three")
    assert [item["event_id"] for item in store.claim_due_events(20)] == ["evt_3"]
    assert store.due_events(20) == []
    assert [item["event_id"] for item in SyncStore(path).due_events(20)] == ["evt_3"]


def test_outbox_migrates_existing_schema_and_binds_new_event_user(tmp_path) -> None:
    path = tmp_path / "shared-sync.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE outbox_events (
                local_seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                next_retry_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'pending', last_error TEXT, acknowledged_at TEXT
            )
        """)

    store = SyncStore(path)
    assert store.enqueue("evt_user", {"event_id": "evt_user"}, "sha256:user", "alice")

    assert store.due_events(20)[0]["user_id"] == "alice"


def test_event_protocol_prefers_persisted_canonical_record() -> None:
    event = build_memory_event(
        {"content": "untrusted", "scope": "personal"},
        {"id": "mem_1", "path": "memory-bank/people/test/mem_1.md"},
        {"content_markdown": "persisted", "scope": "project_shared", "record_kind": "handoff", "occurred_at": "2026-08-04T00:00:00+00:00"},
    )
    assert event["content_markdown"] == "persisted"
    assert event["scope"] == "project_shared"