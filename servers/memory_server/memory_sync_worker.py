"""Daemon Outbox uploader; independent from local MCP correctness."""

from __future__ import annotations

import logging
import hashlib
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

from .memory_sync_client import MemoryHubClient
from .memory_sync_config import SharedMemoryConfig
from .memory_sync_store import SyncStore

logger = logging.getLogger(__name__)
_WORKERS: dict[Path, "MemorySyncWorker"] = {}


def wake_sync_worker(repo_root: Path) -> None:
    worker = _WORKERS.get(repo_root.resolve())
    if worker is not None:
        worker.wake()


class MemorySyncWorker:
    def __init__(self, config_provider: Callable[[], object]) -> None:
        self._provider, self._stop, self._wake = config_provider, threading.Event(), threading.Event()
        self._thread: threading.Thread | None = None
        self._next_refresh = 0.0

    def start(self) -> None:
        if self._thread is None:
            runtime = self._provider()
            _WORKERS[Path(runtime.repo_root).resolve()] = self
            self._thread = threading.Thread(target=self._run, name="memory-sync-worker", daemon=True)
            self._thread.start()

    def stop(self, *, timeout: float = 1.0) -> None:
        self._stop.set(); self._wake.set()
        if self._thread:
            self._thread.join(timeout)
        try:
            runtime = self._provider()
            _WORKERS.pop(Path(runtime.repo_root).resolve(), None)
        except Exception:
            pass

    def wake(self) -> None:
        self._wake.set()

    def run_once(self) -> None:
        runtime = self._provider(); config: SharedMemoryConfig = runtime.shared_memory
        if not config.active:
            return
        store = SyncStore(runtime.repo_root / ".ai-memory" / "shared-sync.db")
        token_hash = hashlib.sha256(str(config.token).encode("utf-8")).hexdigest()
        disabled = store.get_state("remote_auth_disabled")
        if disabled and disabled.get("token_hash") == token_hash:
            return
        if disabled:
            store.delete_state("remote_auth_disabled")
        rows = store.claim_due_events(config.upload_batch_size) if config.upload_enabled else []
        if rows:
            import json
            grouped_rows: dict[str, list[dict[str, object]]] = {}
            for row in rows:
                grouped_rows.setdefault(str(row.get("user_id") or config.user_id), []).append(row)
            for user_id, user_rows in grouped_rows.items():
                upload_config = config if user_id == config.user_id else SharedMemoryConfig(**{**config.__dict__, "user_id": user_id})
                events = [json.loads(str(row["payload_json"])) for row in user_rows]
                status, response = MemoryHubClient(upload_config).upload(events)
                self._handle_upload_response(store, config, user_rows, status, response, token_hash)
        self._refresh_context(runtime, config, store)

    @staticmethod
    def _handle_upload_response(store: SyncStore, config: SharedMemoryConfig, rows: list[dict[str, object]], status: int, response: dict[str, object], token_hash: str) -> None:
        if status == 200:
            acknowledged = {str(event_id) for event_id in list(response.get("accepted", [])) + list(response.get("duplicates", []))}
            store.acknowledge(sorted(acknowledged))
            rejected_ids: set[str] = set()
            for rejected in response.get("rejected", []):
                event_id = str(rejected.get("event_id") or "")
                if event_id:
                    rejected_ids.add(event_id)
                    store.reject(event_id, str(rejected.get("code") or "rejected"))
            for row in rows:
                event_id = str(row["event_id"])
                if event_id not in acknowledged | rejected_ids:
                    store.retry(event_id, "unacknowledged_response", datetime.now(UTC).isoformat())
        elif status in {400, 401, 403, 404, 413, 422}:
            for row in rows:
                store.reject(str(row["event_id"]), str(response.get("error") or f"http_{status}"))
            if status in {401, 403}:
                store.put_state("remote_auth_disabled", {"token_hash": token_hash, "status": status})
        else:
            for row in rows:
                delay = min(config.upload_retry_max_seconds, 2 ** min(int(row["attempts"]), 8))
                store.retry(str(row["event_id"]), str(response.get("error") or f"http_{status}"), (datetime.now(UTC) + timedelta(seconds=delay)).isoformat())

    def _refresh_context(self, runtime: object, config: SharedMemoryConfig, store: SyncStore) -> None:
        if config.read_enabled and time.monotonic() >= self._next_refresh:
            self._next_refresh = time.monotonic() + config.background_refresh_seconds
            args = store.get_state("default_context_args")
            if args:
                try:
                    from .memory_shared_context import get_shared_context

                    get_shared_context(store, config, args, force_refresh=True)
                except Exception as exc:  # cache refresh is always best effort
                    logger.debug("shared context refresh failed: %s", type(exc).__name__)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:
                logger.debug("shared sync cycle failed: %s", type(exc).__name__)
            config = self._provider().shared_memory
            self._wake.wait(config.upload_interval_seconds)
            self._wake.clear()