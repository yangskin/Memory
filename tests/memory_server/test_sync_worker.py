from __future__ import annotations

from types import SimpleNamespace

from servers.memory_server.memory_sync_config import SharedMemoryConfig
from servers.memory_server.memory_sync_store import SyncStore
from servers.memory_server.memory_sync_worker import MemorySyncWorker


def _runtime(tmp_path):
    return SimpleNamespace(repo_root=tmp_path, shared_memory=SharedMemoryConfig(enabled=True, server_url="https://hub.example", project_id="project", token_env="TEST_MEMORY_HUB_TOKEN"))


def test_permanent_http_rejection_stops_retries(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_MEMORY_HUB_TOKEN", "mem_v1.tok_test.secret")
    runtime = _runtime(tmp_path)
    store = SyncStore(tmp_path / ".ai-memory" / "shared-sync.db")
    store.enqueue("evt_1", {"event_id": "evt_1"}, "sha256:one")
    monkeypatch.setattr("servers.memory_server.memory_sync_worker.MemoryHubClient.upload", lambda _self, _events: (401, {"error": "http_401"}))
    MemorySyncWorker(lambda: runtime).run_once()
    assert store.due_events(20) == []
    assert store.get_state("remote_auth_disabled") is not None


def test_partial_batch_response_retries_unacknowledged_events(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_MEMORY_HUB_TOKEN", "mem_v1.tok_test.secret")
    runtime = _runtime(tmp_path)
    store = SyncStore(tmp_path / ".ai-memory" / "shared-sync.db")
    store.enqueue("evt_1", {"event_id": "evt_1"}, "sha256:one")
    store.enqueue("evt_2", {"event_id": "evt_2"}, "sha256:two")
    monkeypatch.setattr("servers.memory_server.memory_sync_worker.MemoryHubClient.upload", lambda _self, _events: (200, {"accepted": ["evt_1"], "duplicates": [], "rejected": []}))
    MemorySyncWorker(lambda: runtime).run_once()
    assert store.due_events(20)[0]["event_id"] == "evt_2"


def test_queued_events_upload_with_creation_time_users(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_MEMORY_HUB_TOKEN", "mem_v1.tok_test.secret")
    runtime = SimpleNamespace(
        repo_root=tmp_path,
        shared_memory=SharedMemoryConfig(
            enabled=True,
            server_url="https://hub.example",
            project_id="project",
            user_id="deployment",
            token_env="TEST_MEMORY_HUB_TOKEN",
            read_enabled=False,
        ),
    )
    store = SyncStore(tmp_path / ".ai-memory" / "shared-sync.db")
    store.enqueue("evt_alice", {"event_id": "evt_alice"}, "sha256:alice", "alice")
    store.enqueue("evt_bob", {"event_id": "evt_bob"}, "sha256:bob", "bob")
    uploads: list[tuple[str, list[str]]] = []

    def upload(client, events):
        event_ids = [event["event_id"] for event in events]
        uploads.append((client.config.user_id, event_ids))
        return 200, {"accepted": event_ids, "duplicates": [], "rejected": []}

    monkeypatch.setattr("servers.memory_server.memory_sync_worker.MemoryHubClient.upload", upload)

    MemorySyncWorker(lambda: runtime).run_once()

    assert uploads == [("alice", ["evt_alice"]), ("bob", ["evt_bob"])]
    assert store.due_events(20) == []