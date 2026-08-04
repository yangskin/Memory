from __future__ import annotations

from servers.memory_server.memory_sync_store import SyncStore


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