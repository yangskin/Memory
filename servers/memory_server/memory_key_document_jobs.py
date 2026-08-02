from __future__ import annotations

from pathlib import Path
from typing import Any

from .memory_config import MemoryConfig
from .memory_durable_jobs import DurableJobQueue, DurableQueueCorruption, default_worker_id
from .memory_events import append_event
from .memory_identity import canonical_identity
from .memory_key_documents import KEY_DOCUMENT_KEYS, rebuild_key_documents
from .memory_locks import LockTimeoutError, file_lock
from .memory_record_index import record_corpus_watermark
from .memory_result import error_result, ok_result

_STATE_REL = Path(".ai-memory") / "key_document_rebuild_jobs.json"
_WORKER_REL = Path(".ai-memory") / "key_document_rebuild_worker.lock"


def _safe_append_event(config: MemoryConfig, event_type: str, payload: dict[str, Any], *, status: str = "ok") -> dict[str, str] | None:
    try:
        append_event(config, event_type, payload, status=status)
    except Exception as exc:  # noqa: BLE001 - audit logging is derived, never the durable intent
        return {"code": "event_log_deferred", "message": f"{type(exc).__name__}: {exc}"}
    return None


def _queue(config: MemoryConfig) -> DurableJobQueue:
    return DurableJobQueue(config, "key-documents", state_rel=_STATE_REL)


def _worker_path(config: MemoryConfig) -> Path:
    return config.repo_root / _WORKER_REL


def _clean_targets(targets: list[str] | None) -> list[str]:
    values = targets or list(KEY_DOCUMENT_KEYS)
    cleaned = [str(item).strip() for item in values if str(item).strip() in KEY_DOCUMENT_KEYS]
    return list(dict.fromkeys(cleaned)) or list(KEY_DOCUMENT_KEYS)


def _scope_key(*, user: str | None, renderer: str, guard_prefer_llm: bool) -> str:
    return f"user={user or ''}|renderer={renderer}|guard_llm={int(bool(guard_prefer_llm))}"


def corpus_watermark(config: MemoryConfig) -> str:
    return record_corpus_watermark(config)


def _merge_payload(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    merged["targets"] = _clean_targets(
        list(existing.get("targets") or []) + list(incoming.get("targets") or [])
    )
    for key in ("user", "renderer", "guard_prefer_llm", "phase", "layer", "trigger", "reason"):
        if incoming.get(key) is not None:
            merged[key] = incoming[key]
    merged["source_watermark"] = incoming.get("source_watermark")
    return merged


def enqueue_key_document_rebuild(
    config: MemoryConfig,
    *,
    targets: list[str],
    user: str | None,
    renderer: str,
    guard_prefer_llm: bool,
    phase: str | None = None,
    layer: str | None = None,
    trigger: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Persist or coalesce a crash-recoverable key-document rebuild intent."""

    selected_targets = _clean_targets(targets)
    user = canonical_identity(user) if user else None
    watermark = corpus_watermark(config)
    payload = {
        "targets": selected_targets,
        "user": user,
        "renderer": renderer,
        "guard_prefer_llm": bool(guard_prefer_llm),
        "phase": phase,
        "layer": layer,
        "trigger": trigger,
        "reason": reason,
        "source_watermark": watermark,
    }
    try:
        queued = _queue(config).enqueue(
            kind="key_document_rebuild",
            payload=payload,
            dedupe_key=_scope_key(user=user, renderer=renderer, guard_prefer_llm=guard_prefer_llm),
            merge=_merge_payload,
        )
    except (DurableQueueCorruption, OSError, ValueError) as exc:
        return error_result("queue_unavailable", f"key-document rebuild could not be queued: {exc}", queued=False)
    if not queued.get("ok"):
        return queued
    job = queued.get("job") or {}
    event_warning = _safe_append_event(
        config,
        "key_document_rebuild_queued",
        {
            "job_id": queued.get("job_id"),
            "coalesced": bool(queued.get("coalesced")),
            "targets": (job.get("payload") or {}).get("targets", selected_targets),
        },
    )
    response = ok_result(
        "key-document rebuild job coalesced" if queued.get("coalesced") else "key-document rebuild job queued",
        queued=True,
        coalesced=bool(queued.get("coalesced")),
        recovered=bool(queued.get("recovered")),
        job_id=queued.get("job_id"),
        targets=(job.get("payload") or {}).get("targets", selected_targets),
        user=user,
        source_watermark=watermark,
    )
    if event_warning:
        response["warnings"] = [event_warning]
    return response


def _summarize_result(result: dict[str, Any], *, stale: bool) -> dict[str, Any]:
    return {
        "ok": bool(result.get("ok")),
        "error": result.get("error"),
        "request_id": result.get("request_id"),
        "written": sorted((result.get("written") or {}).keys()),
        "errors": sorted((result.get("errors") or {}).keys()),
        "stale_at_publish": stale,
    }


def _job_payload(job: dict[str, Any]) -> dict[str, Any]:
    payload = job.get("payload")
    if isinstance(payload, dict) and payload:
        return dict(payload)
    # Version-1 queues stored payload fields directly on the job. Keeping this
    # read path makes upgrades resumable instead of discarding in-flight work.
    return {
        key: job.get(key)
        for key in (
            "targets",
            "user",
            "renderer",
            "guard_prefer_llm",
            "phase",
            "layer",
            "trigger",
            "reason",
            "source_watermark",
        )
        if key in job
    }


def drain_key_document_rebuild_jobs(
    config: MemoryConfig,
    *,
    max_jobs: int = 1,
    worker_id: str | None = None,
) -> dict[str, Any]:
    """Process durable jobs while keeping all worker failures off the MCP path."""

    worker = worker_id or default_worker_id()
    processed: list[dict[str, Any]] = []
    limit = max(1, int(max_jobs or 1))
    lease_seconds = max(5.0, float(config.worker.get("lease_seconds", 120)))
    retry_base = max(0.0, float(config.worker.get("retry_base_seconds", 2)))
    queue = _queue(config)
    try:
        with file_lock(config.repo_root, _worker_path(config), timeout=0.1):
            for _ in range(limit):
                try:
                    job = queue.claim(worker_id=worker, lease_seconds=lease_seconds)
                except (DurableQueueCorruption, OSError, ValueError) as exc:
                    return error_result("queue_unavailable", str(exc), processed=processed)
                if job is None:
                    break
                payload = _job_payload(job)
                lease_token = str(job.get("lease_token") or "")
                try:
                    with queue.lease_guard(str(job.get("job_id")), lease_token, lease_seconds=lease_seconds) as lease_state:
                        result = rebuild_key_documents(
                            config,
                            targets=_clean_targets(payload.get("targets") if isinstance(payload.get("targets"), list) else []),
                            user=str(payload["user"]) if payload.get("user") is not None else None,
                            renderer=str(payload.get("renderer") or "deterministic"),
                            guard_prefer_llm=bool(payload.get("guard_prefer_llm", False)),
                        )
                    if lease_state.get("lost"):
                        result = error_result(
                            "lease_lost",
                            "key-document rebuild completed without a valid durable-job lease; it will be retried",
                            lease_errors=list(lease_state.get("errors") or []),
                        )
                except Exception as exc:  # noqa: BLE001 - worker boundary must isolate all failures
                    result = error_result("worker_exception", f"key-document rebuild raised: {type(exc).__name__}: {exc}")
                stale = corpus_watermark(config) != str(payload.get("source_watermark") or "")
                summary = _summarize_result(result, stale=stale)
                if result.get("ok"):
                    finish = queue.succeed(str(job.get("job_id")), lease_token, result=summary)
                else:
                    finish = queue.fail(
                        str(job.get("job_id")),
                        lease_token,
                        error=str(result.get("error") or "key-document rebuild failed"),
                        retry_base_seconds=retry_base,
                        result=summary,
                    )
                item = {
                    "job_id": job.get("job_id"),
                    "ok": bool(result.get("ok") and finish.get("ok")),
                    "stale_at_publish": stale,
                    "targets": payload.get("targets"),
                    "result": result,
                    "queue_update": finish,
                }
                processed.append(item)
                _safe_append_event(
                    config,
                    "key_document_rebuild_job_finished",
                    {key: value for key, value in item.items() if key != "result"},
                    status="ok" if item["ok"] else "error",
                )
                if stale and item["ok"]:
                    enqueue_key_document_rebuild(
                        config,
                        targets=_clean_targets(payload.get("targets") if isinstance(payload.get("targets"), list) else []),
                        user=str(payload["user"]) if payload.get("user") is not None else None,
                        renderer=str(payload.get("renderer") or "deterministic"),
                        guard_prefer_llm=bool(payload.get("guard_prefer_llm", False)),
                        phase=str(payload.get("phase") or "") or None,
                        layer=str(payload.get("layer") or "") or None,
                        trigger="stale_requeue",
                        reason="source watermark changed while rebuild job was running",
                    )
    except LockTimeoutError as exc:
        return error_result("worker_busy", str(exc), processed=processed)
    try:
        queue.prune(keep_completed=int(config.worker.get("history_limit", 500)))
    except (DurableQueueCorruption, OSError, ValueError):
        pass
    return ok_result("key-document rebuild queue drained", processed=len(processed), jobs=processed)


def read_key_document_rebuild_jobs(config: MemoryConfig) -> dict[str, Any]:
    result = _queue(config).read()
    if not result.get("ok"):
        return result
    compatible: dict[str, Any] = {}
    for job_id, value in (result.get("jobs") or {}).items():
        job = dict(value) if isinstance(value, dict) else {}
        view = _job_payload(job)
        view.update(job)
        compatible[str(job_id)] = view
    result["jobs"] = compatible
    return result


__all__ = [
    "corpus_watermark",
    "drain_key_document_rebuild_jobs",
    "enqueue_key_document_rebuild",
    "read_key_document_rebuild_jobs",
]
