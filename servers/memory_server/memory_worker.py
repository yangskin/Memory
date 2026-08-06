from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .memory_compactor import recover_compaction_transactions
from .memory_config import MemoryConfig
from .memory_encoding import audit_memory_encoding
from .memory_key_document_jobs import drain_key_document_rebuild_jobs
from .memory_record_index import ensure_index_fresh
from .memory_record_io import _atomic_write_text
from .memory_reflection_jobs import curate_project_reflections, drain_project_reflection_jobs
from .memory_task_graph_jobs import drain_task_graph_settlement_jobs

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_step(name: str, callable_: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        result = callable_()
        if not isinstance(result, dict):
            return {"ok": False, "error": f"{name} returned a non-object"}
        # Worker status is a heartbeat, not a second memory store. Never copy
        # full record lists, LLM proposals or queue histories into it.
        summary = {
            key: result[key]
            for key in (
                "ok",
                "error",
                "message",
                "processed",
                "recovered",
                "conflicts",
                "healthy",
                "stats",
                "corpus_watermark",
                "indexed_sources",
            )
            if key in result
        }
        return summary
    except Exception as exc:  # noqa: BLE001 - the worker must never kill MCP request handling
        logger.exception("background memory worker step failed: %s", name)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


class MemoryBackgroundWorker:
    """Daemon worker whose entire failure domain is separate from MCP tools."""

    def __init__(self, config_provider: Callable[[], MemoryConfig]) -> None:
        self._config_provider = config_provider
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()
        self._cycle_lock = threading.Lock()
        self._last_index_check = 0.0
        self._last_encoding_audit = 0.0
        self._last_curator = 0.0
        self._cycles = 0

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        with self._lifecycle_lock:
            if self.running:
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="memory-background-worker", daemon=True)
            self._thread.start()

    def stop(self, *, timeout: float = 1.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=max(0.0, timeout))

    def _status_path(self, config: MemoryConfig) -> Path:
        return config.repo_root / ".ai-memory" / "worker-status.json"

    def _write_status(self, config: MemoryConfig, payload: dict[str, Any]) -> None:
        import json

        try:
            _atomic_write_text(
                self._status_path(config),
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                fsync_strict=False,
            )
        except Exception as exc:  # noqa: BLE001 - diagnostics cannot affect the worker or MCP
            logger.debug("worker status write suppressed: %s", exc)

    def run_once(self, config: MemoryConfig | None = None) -> dict[str, Any]:
        # CLI、测试钩子与后台线程可能同时触发单轮运行；同一 worker 实例
        # 必须串行执行周期任务，避免重复 curator/巡检和时间戳竞争。
        with self._cycle_lock:
            return self._run_once_unlocked(config)

    def _run_once_unlocked(self, config: MemoryConfig | None = None) -> dict[str, Any]:
        current = config or self._config_provider()
        maximum = max(1, int(current.worker.get("max_jobs_per_tick", 4)))
        now_monotonic = time.monotonic()
        steps: dict[str, Any] = {
            "compaction_recovery": _safe_step(
                "compaction_recovery", lambda: recover_compaction_transactions(current)
            ),
            "task_graph": _safe_step(
                "task_graph", lambda: drain_task_graph_settlement_jobs(current, max_jobs=maximum)
            ),
            "reflection": _safe_step(
                "reflection", lambda: drain_project_reflection_jobs(current, max_jobs=maximum)
            ),
            "key_documents": _safe_step(
                "key_documents", lambda: drain_key_document_rebuild_jobs(current, max_jobs=maximum)
            ),
        }
        if now_monotonic - self._last_index_check >= 60.0:
            steps["index"] = _safe_step("index", lambda: ensure_index_fresh(current))
            if steps["index"].get("ok"):
                self._last_index_check = now_monotonic
        if now_monotonic - self._last_encoding_audit >= 3600.0:
            steps["encoding"] = _safe_step("encoding", lambda: audit_memory_encoding(current))
            if steps["encoding"].get("ok"):
                self._last_encoding_audit = now_monotonic
        curator_interval = max(60.0, float(current.reflection.get("curator_interval_hours", 24)) * 3600.0)
        if current.reflection.get("curator_enabled", True) and now_monotonic - self._last_curator >= curator_interval:
            steps["curator"] = _safe_step("curator", lambda: curate_project_reflections(current))
            if steps["curator"].get("ok"):
                self._last_curator = now_monotonic
        self._cycles += 1
        status = {
            "ok": all(bool(value.get("ok")) for value in steps.values()),
            "state": "running",
            "heartbeat_at": _now(),
            "cycles": self._cycles,
            "config_hash": current.config_hash,
            "steps": steps,
        }
        self._write_status(current, status)
        return status

    def _run(self) -> None:
        try:
            grace_applied = False
            while not self._stop.is_set():
                try:
                    config = self._config_provider()
                except Exception as exc:  # noqa: BLE001
                    logger.exception("background worker config reload failed: %s", exc)
                    if self._stop.wait(1.0):
                        return
                    continue
                if not grace_applied:
                    grace = max(0.0, float(config.worker.get("startup_grace_seconds", 2.0)))
                    grace_applied = True
                    if self._stop.wait(grace):
                        return
                try:
                    if not config.worker.get("enabled", True):
                        self._write_status(
                            config,
                            {"ok": True, "state": "disabled", "heartbeat_at": _now(), "config_hash": config.config_hash},
                        )
                    else:
                        self.run_once(config)
                except Exception as exc:  # noqa: BLE001 - one bad cycle must not kill the resident worker
                    logger.exception("background memory worker cycle failed: %s", exc)
                    self._write_status(
                        config,
                        {
                            "ok": False,
                            "state": "cycle_failed",
                            "heartbeat_at": _now(),
                            "config_hash": config.config_hash,
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                    )
                try:
                    poll = max(0.2, float(config.worker.get("poll_seconds", 1.0)))
                except (TypeError, ValueError, OverflowError):
                    poll = 1.0
                if self._stop.wait(poll):
                    return
        except Exception as exc:  # noqa: BLE001 - last-resort daemon containment
            logger.exception("background memory worker terminated unexpectedly: %s", exc)
            try:
                config = self._config_provider()
                self._write_status(
                    config,
                    {"ok": False, "state": "failed", "heartbeat_at": _now(), "error": f"{type(exc).__name__}: {exc}"},
                )
            except Exception:
                pass


__all__ = ["MemoryBackgroundWorker"]
