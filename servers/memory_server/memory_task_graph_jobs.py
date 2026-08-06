"""Crash-recoverable task graph settlement and best-effort Hub upload."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .memory_config import MemoryConfig
from .memory_durable_jobs import DurableJobQueue, DurableQueueCorruption, default_worker_id
from .memory_identity import canonical_identity
from .memory_result import error_result, ok_result
from .memory_task_graph import build_task_graph_delta

_STATE_REL = Path(".ai-memory") / "jobs" / "task-graph-settlement.json"


def _queue(config: MemoryConfig) -> DurableJobQueue:
    return DurableJobQueue(config, "task-graph-settlement", state_rel=_STATE_REL)


def _merge(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    merged.update({key: value for key, value in incoming.items() if value is not None})
    triggers = [*list(existing.get("triggers") or []), *list(incoming.get("triggers") or [])]
    merged["triggers"] = list(dict.fromkeys(str(item) for item in triggers if str(item)))
    return merged


def enqueue_task_graph_settlement(
    config: MemoryConfig,
    *,
    task_id: str,
    user: str | None,
    trigger: str,
    branch: str | None = None,
) -> dict[str, Any]:
    task = str(task_id or "").strip()
    if not task:
        return error_result("invalid_input", "task graph settlement requires task_id", queued=False)
    payload = {
        "task_id": task,
        "user": canonical_identity(user) if user else None,
        "branch": branch,
        "triggers": [trigger],
    }
    try:
        return _queue(config).enqueue(
            kind="task_graph_settlement",
            payload=payload,
            dedupe_key=f"task:{task}",
            merge=_merge,
        )
    except (DurableQueueCorruption, OSError, ValueError) as exc:
        return error_result("queue_unavailable", f"task graph settlement could not be queued: {exc}", queued=False)


def _upload_delta(config: MemoryConfig, payload: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    if not config.shared_memory.enabled:
        return ok_result("shared memory is disabled", queued=False, disabled=True)
    from .memory_sync_protocol import build_memory_event
    from .memory_sync_store import SyncStore

    task_id = str(payload.get("task_id") or "")
    event = build_memory_event(
        {
            "operation": "checkpoint",
            "scope": "project_shared",
            "task_id": task_id,
            "task_phase": "task_done",
            "branch": payload.get("branch"),
            "graph_delta": delta,
            "agent_id": "memory-task-graph",
        },
        {"ok": True, "task_id": task_id},
        repo_root=config.repo_root,
    )
    event["event_id"] = str(uuid5(NAMESPACE_URL, f"memory-task-graph:{event['workspace_id']}:{delta['delta_id']}"))
    queued = SyncStore(config.repo_root / ".ai-memory" / "shared-sync.db").enqueue(
        event["event_id"], event, event["content_hash"]
    )
    if queued:
        from .memory_sync_worker import wake_sync_worker

        wake_sync_worker(config.repo_root)
    return ok_result("task graph event queued for Hub", queued=queued, event_id=event["event_id"])


def drain_task_graph_settlement_jobs(
    config: MemoryConfig,
    *,
    max_jobs: int = 1,
    worker_id: str | None = None,
) -> dict[str, Any]:
    queue = _queue(config)
    processed: list[dict[str, Any]] = []
    lease_seconds = max(5.0, float(config.worker.get("lease_seconds", 120)))
    retry_base = max(0.0, float(config.worker.get("retry_base_seconds", 2)))
    worker = worker_id or default_worker_id()
    for _ in range(max(1, int(max_jobs or 1))):
        try:
            job = queue.claim(worker_id=worker, lease_seconds=lease_seconds)
        except (DurableQueueCorruption, OSError, ValueError) as exc:
            return error_result("queue_unavailable", str(exc), processed=processed)
        if job is None:
            break
        payload = dict(job.get("payload") or {})
        task_id = str(payload.get("task_id") or "")
        token = str(job.get("lease_token") or "")
        try:
            with queue.lease_guard(str(job.get("job_id")), token, lease_seconds=lease_seconds) as lease_state:
                built = build_task_graph_delta(config, task_id=task_id)
                upload = _upload_delta(config, payload, built["graph_delta"]) if built.get("ok") else built
            result = upload if upload.get("ok") else upload
            if built.get("ok"):
                result = {**result, "task_id": task_id, "graph_delta": built["graph_delta"]}
            if lease_state.get("lost"):
                result = error_result("lease_lost", "task graph settlement lost its durable-job lease", task_id=task_id)
        except Exception as exc:  # noqa: BLE001 - background failure isolation boundary
            result = error_result("worker_exception", f"task graph settlement raised: {type(exc).__name__}: {exc}")
        if result.get("ok"):
            finish = queue.succeed(str(job.get("job_id")), token, result=result)
        else:
            finish = queue.fail(
                str(job.get("job_id")),
                token,
                error=str(result.get("error") or "task graph settlement failed"),
                retry_base_seconds=retry_base,
                result=result,
            )
        processed.append({"job_id": job.get("job_id"), "task_id": task_id, "ok": bool(result.get("ok") and finish.get("ok"))})
    try:
        queue.prune(keep_completed=int(config.worker.get("history_limit", 500)))
    except (DurableQueueCorruption, OSError, ValueError):
        pass
    return ok_result("task graph settlement queue drained", processed=len(processed), jobs=processed)


__all__ = ["drain_task_graph_settlement_jobs", "enqueue_task_graph_settlement"]