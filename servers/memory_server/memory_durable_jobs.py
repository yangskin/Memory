from __future__ import annotations

import json
import os
import socket
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from contextlib import contextmanager

from .memory_config import MemoryConfig
from .memory_locks import file_lock
from .memory_record_io import _atomic_write_text
from .memory_request_id import new_request_id
from .memory_result import error_result, ok_result


class DurableQueueCorruption(RuntimeError):
    """Raised when neither the live queue nor its recovery copy is readable."""


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().isoformat()


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class DurableJobQueue:
    """Crash-recoverable JSON queue with leases, retries and a dead-letter set.

    Queue mutations are serialized by the repository lock. A claimed job remains
    durable and is only removed from the active queue after an explicit success.
    If the process disappears, a later process reclaims the job after its lease.
    """

    VERSION = 2

    def __init__(self, config: MemoryConfig, name: str, *, state_rel: Path | None = None) -> None:
        if not name or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for char in name.lower()):
            raise ValueError("queue name contains unsupported characters")
        self.config = config
        self.name = name.lower()
        self.path = config.repo_root / (state_rel or (Path(".ai-memory") / "jobs" / f"{self.name}.json"))
        self.backup_path = self.path.with_suffix(self.path.suffix + ".bak")

    def _empty(self) -> dict[str, Any]:
        return {
            "version": self.VERSION,
            "queue_name": self.name,
            "jobs": {},
            "queue": [],
            "dead_letter": [],
            "updated_at": _now(),
        }

    def _validate(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise DurableQueueCorruption("queue root must be an object")
        jobs = value.get("jobs")
        queue = value.get("queue")
        dead_letter = value.get("dead_letter", [])
        if not isinstance(jobs, dict) or not isinstance(queue, list) or not isinstance(dead_letter, list):
            raise DurableQueueCorruption("queue jobs, queue and dead_letter have invalid types")
        allowed_statuses = {"pending", "running", "done", "dead", "stale", "failed"}
        for job_id, job in jobs.items():
            if not isinstance(job_id, str) or not isinstance(job, dict):
                raise DurableQueueCorruption("queue job entries must map string ids to objects")
            status = str(job.get("status") or "")
            if status not in allowed_statuses:
                raise DurableQueueCorruption(f"queue job {job_id} has invalid status: {status}")
            if "payload" in job and not isinstance(job.get("payload"), dict):
                raise DurableQueueCorruption(f"queue job {job_id} payload must be an object")
            try:
                attempts = int(job.get("attempts") or 0)
                maximum = int(job.get("max_attempts") or self.config.worker.get("max_attempts", 4))
            except (TypeError, ValueError, OverflowError) as exc:
                raise DurableQueueCorruption(f"queue job {job_id} attempt counters are invalid") from exc
            if attempts < 0 or maximum < 1:
                raise DurableQueueCorruption(f"queue job {job_id} attempt counters are out of range")
        normalized_queue = [str(item) for item in queue]
        if len(normalized_queue) != len(set(normalized_queue)):
            raise DurableQueueCorruption("queue contains duplicate job ids")
        missing_queue_ids = sorted(set(normalized_queue) - set(jobs))
        if missing_queue_ids:
            raise DurableQueueCorruption(f"queue references missing jobs: {missing_queue_ids[:5]}")
        normalized_dead = [str(item) for item in dead_letter]
        if len(normalized_dead) != len(set(normalized_dead)) or any(item not in jobs for item in normalized_dead):
            raise DurableQueueCorruption("dead_letter contains duplicate or missing job ids")
        value["queue"] = normalized_queue
        value["version"] = self.VERSION
        value["queue_name"] = self.name
        value["dead_letter"] = normalized_dead
        value.setdefault("updated_at", _now())
        return value

    def _load_file(self, path: Path) -> dict[str, Any]:
        raw = path.read_text(encoding="utf-8", errors="strict")
        return self._validate(json.loads(raw))

    def _read_unlocked(self) -> tuple[dict[str, Any], bool]:
        if not self.path.exists() and not self.backup_path.exists():
            return self._empty(), False
        live_error: Exception | None = None
        if self.path.exists():
            try:
                return self._load_file(self.path), False
            except (OSError, UnicodeError, ValueError, DurableQueueCorruption) as exc:
                live_error = exc
        if self.backup_path.exists():
            try:
                recovered = self._load_file(self.backup_path)
                recovered["recovered_from_backup_at"] = _now()
                recovered["recovery_reason"] = str(live_error or "live queue missing")[:1000]
                return recovered, True
            except (OSError, UnicodeError, ValueError, DurableQueueCorruption) as backup_error:
                raise DurableQueueCorruption(
                    f"live queue and recovery copy are unreadable: live={live_error}; backup={backup_error}"
                ) from backup_error
        raise DurableQueueCorruption(f"live queue is unreadable and no recovery copy exists: {live_error}")

    def _write_unlocked(self, state: dict[str, Any]) -> None:
        state = self._validate(state)
        state["updated_at"] = _now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_file():
            try:
                live = self._load_file(self.path)
            except (OSError, UnicodeError, ValueError, DurableQueueCorruption):
                live = None
            if live is not None:
                _atomic_write_text(
                    self.backup_path,
                    json.dumps(live, ensure_ascii=False, indent=2, sort_keys=True),
                    fsync_strict=self.config.mcp_fsync_strict,
                )
        _atomic_write_text(
            self.path,
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
            fsync_strict=self.config.mcp_fsync_strict,
        )

    @staticmethod
    def _remove_from_queue(state: dict[str, Any], job_id: str) -> None:
        state["queue"] = [str(item) for item in state.get("queue", []) if str(item) != job_id]

    def _reclaim_expired_unlocked(self, state: dict[str, Any], *, now: datetime) -> list[str]:
        reclaimed: list[str] = []
        for job_id, job in list(state.get("jobs", {}).items()):
            if not isinstance(job, dict) or job.get("status") != "running":
                continue
            lease_expires = _parse_time(job.get("lease_expires_at"))
            if lease_expires is not None and lease_expires > now:
                continue
            job["last_error"] = "worker lease expired before completion"
            job["lease_owner"] = None
            job["lease_token"] = None
            job["lease_expires_at"] = None
            job["updated_at"] = now.isoformat()
            max_attempts = max(1, int(job.get("max_attempts") or self.config.worker.get("max_attempts", 4)))
            if int(job.get("attempts") or 0) >= max_attempts:
                job["status"] = "dead"
                job["finished_at"] = now.isoformat()
                if job_id not in state["dead_letter"]:
                    state["dead_letter"].append(job_id)
                self._remove_from_queue(state, job_id)
            else:
                job["status"] = "pending"
                job["available_at"] = now.isoformat()
                if job_id not in state["queue"]:
                    state["queue"].append(job_id)
            reclaimed.append(str(job_id))
        return reclaimed

    def enqueue(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        dedupe_key: str | None = None,
        merge: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
        max_attempts: int | None = None,
    ) -> dict[str, Any]:
        try:
            json.dumps({"kind": kind, "payload": payload, "dedupe_key": dedupe_key}, ensure_ascii=False)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"durable job payload must be JSON serializable: {exc}") from exc
        attempts = max(1, int(max_attempts or self.config.worker.get("max_attempts", 4)))
        with file_lock(self.config.repo_root, self.path):
            state, recovered = self._read_unlocked()
            self._reclaim_expired_unlocked(state, now=_now_dt())
            if dedupe_key:
                for job_id in state.get("queue", []):
                    job = state["jobs"].get(str(job_id))
                    if not isinstance(job, dict) or job.get("status") != "pending":
                        continue
                    if job.get("dedupe_key") != dedupe_key:
                        continue
                    existing_payload = dict(job.get("payload") or {})
                    merged_payload = merge(existing_payload, payload) if merge else existing_payload
                    try:
                        json.dumps(merged_payload, ensure_ascii=False)
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise ValueError(f"merged durable job payload must be JSON serializable: {exc}") from exc
                    job["payload"] = merged_payload
                    job["updated_at"] = _now()
                    job["config_hash"] = self.config.config_hash
                    self._write_unlocked(state)
                    return ok_result(
                        "durable job coalesced",
                        queued=True,
                        coalesced=True,
                        recovered=recovered,
                        job_id=job_id,
                        job=dict(job),
                    )
            job_id = f"{self.name[:4]}_{new_request_id().replace('-', '')}"
            now = _now()
            job = {
                "job_id": job_id,
                "kind": str(kind),
                "status": "pending",
                "payload": dict(payload),
                "dedupe_key": dedupe_key,
                "attempts": 0,
                "max_attempts": attempts,
                "available_at": now,
                "created_at": now,
                "updated_at": now,
                "config_hash": self.config.config_hash,
            }
            state["jobs"][job_id] = job
            state["queue"].append(job_id)
            self._write_unlocked(state)
        return ok_result(
            "durable job queued",
            queued=True,
            coalesced=False,
            recovered=recovered,
            job_id=job_id,
            job=dict(job),
        )

    def claim(self, *, worker_id: str, lease_seconds: float) -> dict[str, Any] | None:
        lease = max(1.0, float(lease_seconds))
        now = _now_dt()
        with file_lock(self.config.repo_root, self.path):
            state, recovered = self._read_unlocked()
            reclaimed = self._reclaim_expired_unlocked(state, now=now)
            selected: dict[str, Any] | None = None
            for job_id in list(state.get("queue", [])):
                job = state["jobs"].get(str(job_id))
                if not isinstance(job, dict) or job.get("status") != "pending":
                    self._remove_from_queue(state, str(job_id))
                    continue
                available = _parse_time(job.get("available_at")) or now
                if available > now:
                    continue
                token = new_request_id()
                job["status"] = "running"
                job["attempts"] = int(job.get("attempts") or 0) + 1
                job["started_at"] = job.get("started_at") or now.isoformat()
                job["claimed_at"] = now.isoformat()
                job["heartbeat_at"] = now.isoformat()
                job["lease_owner"] = worker_id
                job["lease_token"] = token
                job["lease_expires_at"] = (now + timedelta(seconds=lease)).isoformat()
                job["claimed_config_hash"] = self.config.config_hash
                job["updated_at"] = now.isoformat()
                self._remove_from_queue(state, str(job_id))
                selected = dict(job)
                break
            if selected is not None or reclaimed or recovered:
                self._write_unlocked(state)
            return selected

    def heartbeat(self, job_id: str, lease_token: str, *, lease_seconds: float) -> bool:
        now = _now_dt()
        with file_lock(self.config.repo_root, self.path):
            state, _recovered = self._read_unlocked()
            job = state.get("jobs", {}).get(job_id)
            if not isinstance(job, dict) or job.get("status") != "running" or job.get("lease_token") != lease_token:
                return False
            job["heartbeat_at"] = now.isoformat()
            job["lease_expires_at"] = (now + timedelta(seconds=max(1.0, float(lease_seconds)))).isoformat()
            job["updated_at"] = now.isoformat()
            self._write_unlocked(state)
            return True

    @contextmanager
    def lease_guard(self, job_id: str, lease_token: str, *, lease_seconds: float):
        """Renew a running lease while a blocking renderer/LLM call executes."""

        stop = threading.Event()
        state: dict[str, Any] = {"lost": False, "errors": []}
        interval = max(0.25, min(30.0, float(lease_seconds) / 3.0))

        def renew() -> None:
            while not stop.wait(interval):
                try:
                    if not self.heartbeat(job_id, lease_token, lease_seconds=lease_seconds):
                        state["lost"] = True
                        return
                except Exception as exc:  # noqa: BLE001 - reported to the owning worker on exit
                    state["errors"].append(f"{type(exc).__name__}: {exc}")

        thread = threading.Thread(target=renew, name=f"memory-lease-{job_id[-8:]}", daemon=True)
        thread.start()
        try:
            yield state
        finally:
            stop.set()
            thread.join(timeout=min(1.0, interval))
            # 在交还控制权前做一次同步 fencing 检查。后台心跳线程可能因
            # 文件系统错误而没能确认租约；这种情况下必须 fail closed，
            # 不能让调用方把一个已被其他进程回收的任务标记为成功。
            if not state["lost"]:
                try:
                    if not self.heartbeat(job_id, lease_token, lease_seconds=lease_seconds):
                        state["lost"] = True
                except Exception as exc:  # noqa: BLE001 - ownership cannot be proven
                    state["errors"].append(f"{type(exc).__name__}: {exc}")
                    state["lost"] = True

    def succeed(self, job_id: str, lease_token: str, *, result: dict[str, Any]) -> dict[str, Any]:
        return self._finish(job_id, lease_token, success=True, result=result, error=None)

    def fail(
        self,
        job_id: str,
        lease_token: str,
        *,
        error: str,
        retry_base_seconds: float,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._finish(
            job_id,
            lease_token,
            success=False,
            result=result or {},
            error=error,
            retry_base_seconds=retry_base_seconds,
        )

    def _finish(
        self,
        job_id: str,
        lease_token: str,
        *,
        success: bool,
        result: dict[str, Any],
        error: str | None,
        retry_base_seconds: float = 0.0,
    ) -> dict[str, Any]:
        try:
            json.dumps(result, ensure_ascii=False)
        except (TypeError, ValueError, OverflowError) as exc:
            return error_result("invalid_result", f"durable job result must be JSON serializable: {exc}")
        now = _now_dt()
        with file_lock(self.config.repo_root, self.path):
            state, _recovered = self._read_unlocked()
            job = state.get("jobs", {}).get(job_id)
            if not isinstance(job, dict):
                return error_result("job_missing", f"durable job not found: {job_id}")
            if job.get("status") != "running" or job.get("lease_token") != lease_token:
                return error_result("lease_lost", f"durable job lease is no longer owned: {job_id}")
            job["lease_owner"] = None
            job["lease_token"] = None
            job["lease_expires_at"] = None
            job["heartbeat_at"] = now.isoformat()
            job["updated_at"] = now.isoformat()
            job["result"] = dict(result)
            self._remove_from_queue(state, job_id)
            if success:
                job["status"] = "done"
                job["finished_at"] = now.isoformat()
                job["last_error"] = None
            else:
                job["last_error"] = str(error or "durable job failed")[:4000]
                attempts = int(job.get("attempts") or 0)
                max_attempts = max(1, int(job.get("max_attempts") or 1))
                if attempts >= max_attempts:
                    job["status"] = "dead"
                    job["finished_at"] = now.isoformat()
                    if job_id not in state["dead_letter"]:
                        state["dead_letter"].append(job_id)
                else:
                    delay = max(0.0, float(retry_base_seconds)) * (2 ** max(0, attempts - 1))
                    job["status"] = "pending"
                    job["available_at"] = (now + timedelta(seconds=delay)).isoformat()
                    state["queue"].append(job_id)
            self._write_unlocked(state)
            return ok_result("durable job state updated", job=dict(job))

    def read(self) -> dict[str, Any]:
        try:
            with file_lock(self.config.repo_root, self.path):
                state, recovered = self._read_unlocked()
                reclaimed = self._reclaim_expired_unlocked(state, now=_now_dt())
                if recovered or reclaimed:
                    self._write_unlocked(state)
        except DurableQueueCorruption as exc:
            return error_result("queue_corrupt", str(exc), queue_name=self.name)
        return ok_result(
            "durable queue read",
            queue_name=self.name,
            queue=list(state.get("queue", [])),
            jobs=dict(state.get("jobs", {})),
            dead_letter=list(state.get("dead_letter", [])),
            recovered=recovered,
            reclaimed=reclaimed,
        )

    def prune(self, *, keep_completed: int) -> dict[str, Any]:
        keep = max(0, int(keep_completed))
        with file_lock(self.config.repo_root, self.path):
            state, recovered = self._read_unlocked()
            completed = [
                (str(job_id), str(job.get("finished_at") or ""))
                for job_id, job in state.get("jobs", {}).items()
                if isinstance(job, dict) and job.get("status") in {"done", "stale"}
            ]
            remove = {job_id for job_id, _ in sorted(completed, key=lambda item: item[1], reverse=True)[keep:]}
            for job_id in remove:
                state["jobs"].pop(job_id, None)
            if remove or recovered:
                self._write_unlocked(state)
        return ok_result("durable queue pruned", removed=len(remove), recovered=recovered)


__all__ = ["DurableJobQueue", "DurableQueueCorruption", "default_worker_id"]
