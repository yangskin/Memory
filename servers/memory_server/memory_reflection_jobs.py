from __future__ import annotations

from pathlib import Path
from typing import Any

from .memory_config import MemoryConfig
from .memory_durable_jobs import DurableJobQueue, DurableQueueCorruption, default_worker_id
from .memory_events import append_event
from .memory_identity import canonical_identity
from .memory_key_document_jobs import enqueue_key_document_rebuild
from .memory_record_io import iter_parsed_records
from .memory_reflection import publish_reflection_proposal, reflect_task
from .memory_result import error_result, ok_result

_STATE_REL = Path(".ai-memory") / "jobs" / "project-reflection.json"


def _safe_append_event(config: MemoryConfig, event_type: str, payload: dict[str, Any], *, status: str = "ok") -> dict[str, str] | None:
    try:
        append_event(config, event_type, payload, status=status)
    except Exception as exc:  # noqa: BLE001 - audit logging cannot invalidate durable queue state
        return {"code": "event_log_deferred", "message": f"{type(exc).__name__}: {exc}"}
    return None


def _queue(config: MemoryConfig) -> DurableJobQueue:
    return DurableJobQueue(config, "project-reflection", state_rel=_STATE_REL)


def _merge(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    merged.update({key: value for key, value in incoming.items() if value is not None})
    triggers = list(existing.get("triggers") or []) + list(incoming.get("triggers") or [])
    merged["triggers"] = list(dict.fromkeys(str(item) for item in triggers if str(item)))
    return merged


def enqueue_project_reflection(
    config: MemoryConfig,
    *,
    task_id: str,
    user: str | None,
    trigger: str,
    branch: str | None = None,
) -> dict[str, Any]:
    if not config.reflection.get("enabled", False):
        return ok_result("project reflection is disabled", queued=False, disabled=True)
    task = str(task_id or "").strip()
    if not task:
        return error_result("invalid_input", "project reflection requires task_id")
    payload = {
        "task_id": task,
        "user": canonical_identity(user) if user else None,
        "branch": branch,
        "triggers": [trigger],
    }
    try:
        result = _queue(config).enqueue(
            kind="project_reflection",
            payload=payload,
            dedupe_key=f"task:{task}",
            merge=_merge,
        )
    except (DurableQueueCorruption, OSError, ValueError) as exc:
        return error_result("queue_unavailable", f"project reflection could not be queued: {exc}", queued=False)
    if result.get("ok"):
        event_warning = _safe_append_event(
            config,
            "project_reflection_queued",
            {"task_id": task, "job_id": result.get("job_id"), "coalesced": result.get("coalesced")},
        )
        if event_warning:
            result.setdefault("warnings", []).append(event_warning)
    return result


def _prior_support(state: dict[str, Any]) -> dict[str, set[str]]:
    support: dict[str, set[str]] = {}
    for job in (state.get("jobs") or {}).values():
        if not isinstance(job, dict) or job.get("status") not in {"done", "stale"}:
            continue
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        if not result.get("ok"):
            continue
        task_id = str(result.get("task_id") or (job.get("payload") or {}).get("task_id") or "")
        for proposal in result.get("proposals", []) if isinstance(result.get("proposals"), list) else []:
            if (
                not isinstance(proposal, dict)
                or not proposal.get("fingerprint")
                or proposal.get("contradicts_record_ids")
                or proposal.get("action") == "REJECT"
                or not task_id
            ):
                continue
            support.setdefault(str(proposal["fingerprint"]), set()).add(task_id)
    return support


def drain_project_reflection_jobs(
    config: MemoryConfig,
    *,
    max_jobs: int = 1,
    worker_id: str | None = None,
) -> dict[str, Any]:
    if not config.reflection.get("enabled", False):
        return ok_result("project reflection is disabled", processed=0, jobs=[])
    queue = _queue(config)
    processed: list[dict[str, Any]] = []
    lease_seconds = max(5.0, float(config.worker.get("lease_seconds", 120)))
    retry_base = max(0.0, float(config.worker.get("retry_base_seconds", 2)))
    worker = worker_id or default_worker_id()
    for _ in range(max(1, int(max_jobs or 1))):
        try:
            state = queue.read()
            if not state.get("ok"):
                return state
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
                reflection = reflect_task(config, task_id=task_id, prior_support=_prior_support(state))
            if lease_state.get("lost"):
                original = reflection if isinstance(reflection, dict) else {}
                reflection = error_result(
                    "lease_lost",
                    "project reflection completed without a valid durable-job lease; it will be retried",
                    task_id=task_id,
                    lease_errors=list(lease_state.get("errors") or []),
                    proposals=list(original.get("proposals") or []),
                    published=list(original.get("published") or []),
                    model=original.get("model"),
                )
        except Exception as exc:  # noqa: BLE001 - background failure isolation boundary
            reflection = error_result("worker_exception", f"project reflection raised: {type(exc).__name__}: {exc}")
        if reflection.get("ok"):
            finish = queue.succeed(str(job.get("job_id")), token, result=reflection)
        else:
            finish = queue.fail(
                str(job.get("job_id")),
                token,
                error=str(reflection.get("error") or "project reflection failed"),
                retry_base_seconds=retry_base,
                result=reflection,
            )
        published = [item for item in reflection.get("published", []) if isinstance(item, dict) and item.get("ok") and not item.get("duplicate")]
        if published:
            enqueue_key_document_rebuild(
                config,
                targets=["teamContext", "progress", "techContext", "systemPatterns"],
                user=None,
                renderer="deterministic",
                guard_prefer_llm=False,
                trigger="project_reflection",
                reason=f"{len(published)} distilled project memories published",
            )
        item = {
            "job_id": job.get("job_id"),
            "task_id": task_id,
            "ok": bool(reflection.get("ok") and finish.get("ok")),
            "reflection": reflection,
            "queue_update": finish,
        }
        processed.append(item)
        _safe_append_event(
            config,
            "project_reflection_finished",
            {"job_id": job.get("job_id"), "task_id": task_id, "ok": item["ok"], "published": len(published)},
            status="ok" if item["ok"] else "error",
        )
    curator_result: dict[str, Any] | None = None
    if processed and config.reflection.get("curator_enabled", True):
        try:
            curator_result = curate_project_reflections(config)
        except Exception as exc:  # noqa: BLE001 - curator is another isolated derived step
            curator_result = error_result("curator_failed", f"{type(exc).__name__}: {exc}")
    try:
        queue.prune(keep_completed=int(config.reflection.get("history_limit", 200)))
    except (DurableQueueCorruption, OSError, ValueError):
        pass
    return ok_result(
        "project reflection queue drained",
        processed=len(processed),
        jobs=processed,
        curator=curator_result,
    )


def read_project_reflection_jobs(config: MemoryConfig) -> dict[str, Any]:
    return _queue(config).read()


def backfill_project_reflections(config: MemoryConfig, *, limit: int = 100, force: bool = False) -> dict[str, Any]:
    records, stats = iter_parsed_records(config)
    task_ids = sorted(
        {
            str(record.metadata.get("task_id"))
            for record in records
            if record.metadata.get("task_id")
            and record.metadata.get("provenance") != "background_reflection"
            and record.metadata.get("record_kind") not in {"archive_record", "snapshot_daily", "snapshot_weekly", "snapshot_monthly"}
        }
    )
    state = _queue(config).read()
    if not state.get("ok"):
        return state
    existing = {
        str((job.get("payload") or {}).get("task_id"))
        for job in (state.get("jobs") or {}).values()
        if isinstance(job, dict) and (job.get("payload") or {}).get("task_id")
    }
    queued: list[str] = []
    skipped: list[str] = []
    for task_id in task_ids:
        if len(queued) >= max(0, int(limit)):
            break
        if task_id in existing and not force:
            skipped.append(task_id)
            continue
        result = enqueue_project_reflection(
            config,
            task_id=task_id,
            user=None,
            branch=None,
            trigger="history_backfill",
        )
        if result.get("ok") and result.get("queued"):
            queued.append(task_id)
    return ok_result(
        "project reflection history backfill queued",
        queued=len(queued),
        task_ids=queued,
        skipped_existing=skipped,
        record_stats=stats,
    )


def curate_project_reflections(config: MemoryConfig) -> dict[str, Any]:
    state = _queue(config).read()
    if not state.get("ok"):
        return state
    groups: dict[str, list[tuple[str, dict[str, Any], str]]] = {}
    for job in (state.get("jobs") or {}).values():
        if not isinstance(job, dict) or job.get("status") not in {"done", "stale"}:
            continue
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        if not result.get("ok"):
            continue
        task_id = str(result.get("task_id") or (job.get("payload") or {}).get("task_id") or "")
        model = str(result.get("model") or "unknown")
        for proposal in result.get("proposals", []) if isinstance(result.get("proposals"), list) else []:
            if (
                isinstance(proposal, dict)
                and proposal.get("fingerprint")
                and not proposal.get("contradicts_record_ids")
                and proposal.get("action") != "REJECT"
                and task_id
            ):
                groups.setdefault(str(proposal["fingerprint"]), []).append((task_id, proposal, model))
    curator = config.reflection.get("curator") if isinstance(config.reflection.get("curator"), dict) else {}
    min_tasks = max(2, int(curator.get("min_distinct_tasks", config.reflection.get("publish_repeated_tasks", 2))))
    confidence_gate = float(curator.get("publish_confidence", config.reflection.get("publish_min_confidence", 0.95)))
    published: list[dict[str, Any]] = []
    retained = 0
    for fingerprint, items in groups.items():
        task_ids = sorted({task_id for task_id, _proposal, _model in items})
        representative = max(items, key=lambda item: float(item[1].get("confidence") or 0.0))
        proposal = dict(representative[1])
        if len(task_ids) < min_tasks or float(proposal.get("confidence") or 0.0) < confidence_gate:
            retained += 1
            continue
        all_support = [
            str(record_id)
            for _task_id, candidate, _model in items
            for record_id in candidate.get("supporting_record_ids", [])
        ]
        result = publish_reflection_proposal(
            config,
            proposal=proposal,
            task_id=representative[0],
            model=representative[2],
            additional_support_ids=all_support,
        )
        published.append({"fingerprint": fingerprint, "task_ids": task_ids, "result": result})
    return ok_result(
        "project reflection curator completed",
        groups=len(groups),
        retained=retained,
        published=published,
    )


__all__ = [
    "backfill_project_reflections",
    "curate_project_reflections",
    "drain_project_reflection_jobs",
    "enqueue_project_reflection",
    "read_project_reflection_jobs",
]
