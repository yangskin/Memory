"""Daemon Outbox uploader; independent from local MCP correctness."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Callable

from .memory_sync_client import MemoryHubClient
from .memory_sync_config import SharedMemoryConfig
from .memory_sync_store import SyncStore

logger = logging.getLogger(__name__)


class MemorySyncWorker:
    def __init__(self, config_provider: Callable[[], object]) -> None:
        self._provider, self._stop, self._wake = config_provider, threading.Event(), threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="memory-sync-worker", daemon=True)
            self._thread.start()

    def stop(self, *, timeout: float = 1.0) -> None:
        self._stop.set(); self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    def wake(self) -> None:
        self._wake.set()

    def run_once(self) -> None:
        runtime = self._provider(); config: SharedMemoryConfig = runtime.shared_memory
        if not config.active or not config.upload_enabled:
            return
        store = SyncStore(runtime.repo_root / ".ai-memory" / "shared-sync.db")
        rows = store.due_events(config.upload_batch_size)
        if not rows:
            return
        import json
        events = [json.loads(row["payload_json"]) for row in rows]
        status, response = MemoryHubClient(config).upload(events)
        if status == 200:
            store.acknowledge(list(response.get("accepted", [])) + list(response.get("duplicates", [])))
            for rejected in response.get("rejected", []):
                store.reject(str(rejected.get("event_id")), str(rejected.get("code") or "rejected"))
            return
        for row in rows:
            delay = min(config.upload_retry_max_seconds, 2 ** min(int(row["attempts"]), 8))
            store.retry(row["event_id"], str(response.get("error") or f"http_{status}"), (datetime.now(UTC) + timedelta(seconds=delay)).isoformat())

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:
                logger.debug("shared sync cycle failed: %s", type(exc).__name__)
            config = self._provider().shared_memory
            self._wake.wait(config.upload_interval_seconds)
            self._wake.clear()