"""Snapshot / digest / review / rollback compile views.

Compile orchestration entry-point; pairs with :mod:`memory_compiler` (the
public ``memory_compile`` / ``memory_get_runtime_digest`` shell). Each
``compile_*`` view here takes the already-collected record list and
returns the same ``ok/error`` envelope the dispatch layer expects.

This module also hosts the formerly-split helper layer (targets,
scoring, render, writer) — they were tiny leaf modules with no external
consumers, so keeping them here removes one import indirection per
compile call without changing public behaviour.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .memory_compiler_cache import (
    find_compile_cache_entry,
    load_compile_cache_entries,
    record_usage_stats,
)
from .memory_config import MemoryConfig
from .memory_corpus import CompilableRecord, compact_body as _compact_body, iter_compilable_records
from .memory_events import append_event
from .memory_paths import PathManager, PathSecurityError
from .memory_result import error_result, ok_result
from .memory_scoring import build_reference_counts, load_usage_stats, parse_timestamp, score_record


# ──────────────────────────────────────────────────────────────────────
# Compile targets (formerly memory_compile_targets.py)
# ──────────────────────────────────────────────────────────────────────

STANDARD_TARGETS = {"runtime_digest", "task_handoff", "system_digest", "publish_queue"}
SNAPSHOT_TARGETS = {"daily_snapshot", "weekly_snapshot", "monthly_snapshot"}
ROLE_TARGETS = {"rollback_context", "review_queue", "dao_digest", "fa_digest", "shu_digest"}
SUPPORTED_TARGETS = STANDARD_TARGETS | SNAPSHOT_TARGETS | ROLE_TARGETS
SUPPORTED_BODY_MODES = {"compact", "full"}
DEFAULT_BODY_MODE = "compact"
DEFAULT_INCLUDE_SCOPES = ["shared", "personal"]
DEFAULT_INCLUDE_STATUSES = ["validated", "published"]
DEFAULT_CONTEXT_SCOPES = ["shared", "personal", "session", "task_or_branch", "project_shared", "org_shared"]
DIGEST_LEVELS = {
    "dao_digest": "dao",
    "fa_digest": "fa",
    "shu_digest": "shu",
}


def slug_compile_value(value: str, *, fallback: str) -> str:
    normalized = value.replace("\\", "/")
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", normalized).strip("-._")
    return slug or fallback


def compiled_path(target: str, *, user: str | None = None, task_id: str | None = None, branch: str | None = None) -> str:
    if target == "daily_snapshot":
        label = slug_compile_value(task_id or "daily", fallback="daily")
        return f"memory-bank/compiled/snapshots/daily/{label}.md"
    if target == "weekly_snapshot":
        label = slug_compile_value(task_id or "weekly", fallback="weekly")
        return f"memory-bank/compiled/snapshots/weekly/{label}.md"
    if target == "monthly_snapshot":
        label = slug_compile_value(task_id or "monthly", fallback="monthly")
        return f"memory-bank/compiled/snapshots/monthly/{label}.md"
    if target == "review_queue":
        return "memory-bank/compiled/review/review-queue.md"
    if target == "dao_digest":
        return "memory-bank/compiled/runtime/dao-digest.md"
    if target == "fa_digest":
        return "memory-bank/compiled/runtime/fa-digest.md"
    if target == "shu_digest":
        return "memory-bank/compiled/runtime/shu-digest.md"
    if target == "rollback_context":
        if task_id:
            return f"memory-bank/compiled/runtime/task/{slug_compile_value(task_id, fallback='task')}-rollback.md"
        if branch:
            return f"memory-bank/compiled/runtime/branch/{slug_compile_value(branch, fallback='branch')}-rollback.md"
        if user:
            return f"memory-bank/compiled/runtime/people/{slug_compile_value(user, fallback='user')}-rollback.md"
        return "memory-bank/compiled/runtime/rollback-context.md"
    if target == "task_handoff":
        task_slug = slug_compile_value(task_id or "handoff", fallback="handoff")
        return f"memory-bank/compiled/runtime/task/{task_slug}-handoff.md"
    if target == "publish_queue":
        return "memory-bank/compiled/publish/publish-queue.md"
    if target == "system_digest":
        return "memory-bank/compiled/runtime/system-digest.md"
    if task_id:
        return f"memory-bank/compiled/runtime/task/{slug_compile_value(task_id, fallback='task')}.md"
    if branch:
        return f"memory-bank/compiled/runtime/branch/{slug_compile_value(branch, fallback='branch')}.md"
    if user:
        return f"memory-bank/compiled/runtime/people/{slug_compile_value(user, fallback='user')}-digest.md"
    return "memory-bank/compiled/runtime/system-digest.md"


# ──────────────────────────────────────────────────────────────────────
# Render helpers (formerly memory_compile_render.py)
# ──────────────────────────────────────────────────────────────────────


def bullet_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) if value else "none"
    return str(value)


def render_record(record: CompilableRecord, *, body_mode: str) -> list[str]:
    metadata = record.metadata
    if body_mode == "full":
        tags = [str(tag) for tag in metadata.get("tags", []) if str(tag)]
        lines = [
            f"### {record.title}",
            "",
            f"- id: `{metadata.get('id')}`",
            f"- path: `{record.path}`",
            f"- kind: `{metadata.get('record_kind')}`",
            f"- scope/status: `{metadata.get('scope')}` / `{metadata.get('status')}`",
            f"- author: `{metadata.get('author')}`",
            f"- task_id: `{bullet_value(metadata.get('task_id'))}`",
            f"- branch: `{bullet_value(metadata.get('branch'))}`",
            f"- tags: `{bullet_value(tags)}`",
            "",
            record.body,
            "",
        ]
    else:
        lines = [
            f"### {record.title}",
            "",
            f"- id: `{metadata.get('id')}`",
            f"- source: `{record.path}`",
            f"- status: `{metadata.get('status')}`",
            "",
            _compact_body(record),
            "",
        ]
    return lines


def legacy_memory_lines(config: MemoryConfig, *, user: str | None) -> list[str]:
    rel_paths = ["memory-bank/activeContext.md", "memory-bank/progress.md"]
    lines: list[str] = []
    manager = PathManager(config)
    for rel_path in rel_paths:
        try:
            resolved = manager.resolve(rel_path, must_exist=True, must_be_file=True)
        except Exception:
            continue
        try:
            snippet = resolved.read_text(encoding="utf-8", errors="replace").strip().splitlines()[:6]
        except OSError:
            continue
        lines.append(f"### `{rel_path}`")
        lines.append("")
        lines.extend(f"> {line}" for line in snippet if line.strip())
        lines.append("")
    return lines


def render_compile_markdown(
    *,
    config: MemoryConfig,
    target: str,
    records: list[CompilableRecord],
    user: str | None,
    task_id: str | None,
    branch: str | None,
    include_scopes: list[str],
    include_statuses: list[str],
    preferred_tags: list[str],
    body_mode: str,
) -> str:
    title_by_target = {
        "runtime_digest": "Runtime Digest",
        "task_handoff": "Task Handoff",
        "system_digest": "System Digest",
        "publish_queue": "Publish Queue",
    }
    title = title_by_target[target]
    lines = [
        f"# {title}",
        "",
        "> Generated deterministically from Markdown + Front Matter records. This file is a rebuildable view, not truth source.",
        "",
        "## Filters",
        "",
        f"- target: `{target}`",
        f"- user: `{bullet_value(user)}`",
        f"- task_id: `{bullet_value(task_id)}`",
        f"- branch: `{bullet_value(branch)}`",
        f"- include_scopes: `{bullet_value(include_scopes)}`",
        f"- include_statuses: `{bullet_value(include_statuses)}`",
        f"- preferred_tags: `{bullet_value(preferred_tags)}`",
        f"- body_mode: `{body_mode}`",
        "",
        "## Included Records",
        "",
    ]

    if target == "runtime_digest":
        legacy_lines = legacy_memory_lines(config, user=user)
        if legacy_lines:
            lines.extend(["## Legacy Memory Files", ""])
            lines.extend(legacy_lines)
            lines.append("")

    if not records:
        lines.extend(["No records matched the compile filters.", ""])
    else:
        for record in records:
            lines.extend(render_record(record, body_mode=body_mode))

    lines.extend(["## Source References", ""])
    if not records:
        lines.append("- none")
    else:
        for record in records:
            lines.append(f"- `{record.metadata.get('id')}` -> `{record.path}`")
    lines.append("")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# Scoring helpers (formerly memory_compile_scoring.py)
# ──────────────────────────────────────────────────────────────────────


def record_time(record: CompilableRecord) -> datetime | None:
    metadata = record.metadata
    for key in ("occurred_at", "valid_from", "updated_at", "created_at"):
        parsed = parse_timestamp(metadata.get(key))
        if parsed is not None:
            return parsed
    return None


def record_sort_key(record: CompilableRecord) -> tuple[int, str, str]:
    status = str(record.metadata.get("status", ""))
    scope = str(record.metadata.get("scope", ""))
    status_rank = {"published": 0, "validated": 1, "candidate": 2, "raw": 3, "archived": 4}.get(status, 9)
    scope_rank = {"shared": 0, "personal": 1, "archive": 2, "local": 3}.get(scope, 9)
    return status_rank, f"{scope_rank}:{record.title.lower()}", str(record.metadata.get("id", ""))


def record_sort_with_score(record: CompilableRecord, score_data: dict[str, Any]) -> tuple[float, float, str]:
    timestamp = record_time(record)
    epoch = timestamp.timestamp() if timestamp is not None else 0.0
    return (-float(score_data.get("total", 0.0)), -epoch, record.title.lower())


def scored_records(config: MemoryConfig, records: list[CompilableRecord]) -> list[tuple[CompilableRecord, dict[str, Any]]]:
    usage_stats = load_usage_stats(config)
    reference_counts = build_reference_counts(records)
    now = datetime.now(timezone.utc)
    scored = [
        (
            record,
            score_record(
                record.metadata,
                usage_entry=usage_stats.get(str(record.metadata.get("id", "")), {}),
                reference_count=reference_counts.get(str(record.metadata.get("id", "")), 0),
                now=now,
            ),
        )
        for record in records
    ]
    scored.sort(key=lambda item: record_sort_with_score(item[0], item[1]))
    return scored


def summary_with_score(record: CompilableRecord, score_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(record.metadata.get("id", "")),
        "title": record.title,
        "path": record.path,
        "record_kind": record.metadata.get("record_kind"),
        "scope": record.metadata.get("scope"),
        "status": record.metadata.get("status"),
        "cognitive_level": record.metadata.get("cognitive_level"),
        "memory_tier": score_data.get("effective_memory_tier"),
        "importance_score": score_data.get("total"),
    }


# ──────────────────────────────────────────────────────────────────────
# Compile-output writer (formerly memory_compile_writer.py)
# ──────────────────────────────────────────────────────────────────────


def cache_key(
    target: str,
    *,
    user: str | None,
    task_id: str | None,
    branch: str | None,
    hint: str | None = None,
) -> str:
    parts = [target]
    if hint:
        parts.append(slug_compile_value(hint, fallback="entry"))
    elif task_id:
        parts.append(slug_compile_value(task_id, fallback="task"))
    elif branch:
        parts.append(slug_compile_value(branch, fallback="branch"))
    elif user:
        parts.append(slug_compile_value(user, fallback="user"))
    else:
        parts.append("system")
    return "-".join(parts) + ".json"


def write_compiled_view(
    config: MemoryConfig,
    *,
    target: str,
    rel_path: str,
    content: str,
    included: list[CompilableRecord],
    user: str | None,
    task_id: str | None,
    branch: str | None,
    body_mode: str,
    cache_hint: str | None = None,
    cache_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manager = PathManager(config)
    try:
        resolved = manager.resolve(rel_path, must_exist=False, must_be_file=False)
    except PathSecurityError as exc:
        return error_result("path_not_allowed", str(exc))

    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
    except OSError as exc:
        return error_result("write_failed", f"failed to write compiled memory: {exc}")

    included_ids = [str(record.metadata.get("id")) for record in included]
    used_at = datetime.now(timezone.utc).isoformat()
    record_usage_stats(config, included, used_at, target=target)
    cache_path = config.repo_root / ".ai-memory" / "compile-cache" / cache_key(
        target,
        user=user,
        task_id=task_id,
        branch=branch,
        hint=cache_hint,
    )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_payload = {
        "target": target,
        "path": rel_path,
        "included_record_ids": included_ids,
        "included_record_paths": [record.path for record in included],
        "body_mode": body_mode,
    }
    if cache_extra:
        cache_payload.update(cache_extra)
    cache_path.write_text(json.dumps(cache_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    append_event(
        config,
        "memory_compile",
        {
            "target": target,
            "path": rel_path,
            "user": user,
            "task_id": task_id,
            "branch": branch,
            "body_mode": body_mode,
            "included_record_ids": included_ids,
        },
    )
    return ok_result(
        "memory compiled",
        target=target,
        path=rel_path,
        content=content,
        body_mode=body_mode,
        included_record_ids=included_ids,
        included_record_paths=[record.path for record in included],
    )


# ── Time helpers ───────────────────────────────────────────────────────


def reference_time(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    parsed = parse_timestamp(value)
    if parsed is not None:
        return parsed
    raise ValueError(f"invalid ISO timestamp/date: {value}")


def time_window(target: str, as_of: datetime) -> tuple[datetime, datetime, str]:
    end = as_of
    if target == "daily_snapshot":
        start = as_of.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1) - timedelta(microseconds=1)
        label = start.strftime("%Y-%m-%d")
        return start, end, label
    if target == "weekly_snapshot":
        start = (as_of - timedelta(days=as_of.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7) - timedelta(microseconds=1)
        iso_year, iso_week, _ = start.isocalendar()
        label = f"{iso_year}-W{iso_week:02d}"
        return start, end, label
    if target == "monthly_snapshot":
        start = as_of.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            month_end = start.replace(year=start.year + 1, month=1)
        else:
            month_end = start.replace(month=start.month + 1)
        end = month_end - timedelta(microseconds=1)
        label = start.strftime("%Y-%m")
        return start, end, label
    raise ValueError(f"unsupported snapshot target: {target}")


# ── Shared bullet renderer ─────────────────────────────────────────────


def format_record_bullets(
    records: list[tuple[CompilableRecord, dict[str, Any]]], *, limit: int = 10
) -> list[str]:
    if not records:
        return ["- none"]
    lines: list[str] = []
    for record, score_data in records[:limit]:
        lines.append(
            f"- `{record.metadata.get('id')}` | {record.title} | "
            f"{record.metadata.get('record_kind')} | score={score_data.get('total')} | "
            f"tier={score_data.get('effective_memory_tier')}"
        )
    return lines


# ── Snapshot target ────────────────────────────────────────────────────


def _maybe_generate_snapshot_narrative(
    config: MemoryConfig,
    *,
    target: str,
    label: str,
    records: list[CompilableRecord],
) -> dict[str, Any]:
    """Run the v0.10.0 ``snapshot_narrative`` capability under the runner.

    Returns a status envelope.  When the LLM is disabled / unavailable /
    fails, ``ok`` is still True from the runner's perspective (callers
    can read the inner ``status`` to decide whether to retry); the
    snapshot body itself is always written deterministically and the
    only effect of failure is that no ``## Narrative (LLM)`` section is
    spliced in.
    """

    from .memory_llm_runner import run_llm_capability

    record_payload = [
        {
            "id": str(record.metadata.get("id", "")),
            "title": str(record.title or ""),
            "record_kind": str(record.metadata.get("record_kind", "")),
            "body": str(getattr(record, "content", "") or ""),
        }
        for record in records
    ]

    def _invoke(client):
        from .memory_llm import LLMRequestError
        from .memory_snapshot_narrative import generate_snapshot_narrative

        outcome = generate_snapshot_narrative(
            client,
            record_payload,
            target=target,
            label=label,
        )
        if not outcome.ok:
            raise LLMRequestError(outcome.error or "snapshot_narrative failed")
        return {
            "ok": True,
            "section": outcome.injected_section,
            "narrative": outcome.narrative,
            "model": outcome.model,
            "cache_hit": outcome.cache_hit,
            "record_count": outcome.record_count,
        }

    envelope = run_llm_capability(
        config,
        "snapshot_narrative",
        _invoke,
        fallback=lambda: {"ok": True, "section": "", "fallback": True},
    )
    payload = envelope.value if isinstance(envelope.value, dict) else {}
    return {
        "ok": bool(envelope.ok),
        "status": envelope.status,
        "fallback_used": bool(envelope.fallback_used),
        "section": str(payload.get("section") or "") if isinstance(payload, dict) else "",
        "narrative": str(payload.get("narrative") or "") if isinstance(payload, dict) else "",
        "model": str(payload.get("model") or "") if isinstance(payload, dict) else "",
        "cache_hit": bool(payload.get("cache_hit")) if isinstance(payload, dict) else False,
        "record_count": int(payload.get("record_count") or 0) if isinstance(payload, dict) else 0,
        "error": envelope.error,
        "envelope": envelope.to_dict(),
    }


def compile_snapshot_target(
    config: MemoryConfig,
    *,
    target: str,
    records: list[CompilableRecord],
    user: str | None,
    task_id: str | None,
    branch: str | None,
    body_mode: str,
    as_of: str | None,
    narrative: bool = False,
) -> dict[str, Any]:
    try:
        reference = reference_time(as_of)
    except ValueError as exc:
        return error_result("invalid_input", str(exc))
    window_start, window_end, label = time_window(target, reference)
    in_window = [
        record
        for record in records
        if (timestamp := record_time(record)) is not None and window_start <= timestamp <= window_end
    ]
    scored = scored_records(config, in_window)
    top_changes = [
        item
        for item in scored
        if str(item[0].metadata.get("record_kind")) in {"decision", "procedure", "incident", "system_rule"}
    ]
    top_reused = sorted(
        scored,
        key=lambda item: (
            -int(item[1].get("usage", {}).get("compile_hit_count", 0)),
            -float(item[1].get("total", 0.0)),
            item[0].title.lower(),
        ),
    )
    open_items = [
        item
        for item in scored
        if str(item[0].metadata.get("status")) in {"raw", "candidate", "degraded"}
        or bool(item[0].metadata.get("conflicts_with"))
    ]
    cache_targets = (
        {"daily_snapshot"}
        if target == "weekly_snapshot"
        else {"weekly_snapshot"}
        if target == "monthly_snapshot"
        else set()
    )
    derived_snapshots: list[str] = []
    if cache_targets:
        for entry in load_compile_cache_entries(config, targets=cache_targets):
            entry_start = parse_timestamp(entry.get("window_start"))
            if entry_start is None or not (window_start <= entry_start <= window_end):
                continue
            snapshot_id = str(entry.get("snapshot_id", "")).strip()
            if snapshot_id:
                derived_snapshots.append(snapshot_id)
    snapshot_id = f"{target}:{label}"
    title = {
        "daily_snapshot": f"Daily Snapshot {label}",
        "weekly_snapshot": f"Weekly Snapshot {label}",
        "monthly_snapshot": f"Monthly Snapshot {label}",
    }[target]
    lines = [
        f"# {title}",
        "",
        "> Generated deterministically from source records. This snapshot is rebuildable and does not mutate source memory.",
        "",
        "## Window",
        "",
        f"- snapshot_id: `{snapshot_id}`",
        f"- window_start: `{window_start.isoformat()}`",
        f"- window_end: `{window_end.isoformat()}`",
        f"- matched_records: `{len(in_window)}`",
        "",
        "## Derived From Records",
        "",
    ]
    lines.extend(f"- `{record.metadata.get('id')}` -> `{record.path}`" for record in in_window[:20] or [])
    if not in_window:
        lines.append("- none")
    lines.extend(["", "## Top Changes", ""])
    lines.extend(format_record_bullets(top_changes, limit=10))
    lines.extend(["", "## Top Reused Memories", ""])
    lines.extend(format_record_bullets(top_reused, limit=10))
    lines.extend(["", "## Open Questions", ""])
    lines.extend(format_record_bullets(open_items, limit=10))
    candidate_heading = "## Candidate For Weekly" if target == "daily_snapshot" else "## Candidate For Monthly"
    lines.extend(["", candidate_heading, ""])
    lines.extend(format_record_bullets(scored, limit=10))
    if target != "daily_snapshot":
        lines.extend(["", "## Derived From Snapshots", ""])
        if derived_snapshots:
            lines.extend(f"- `{snapshot}`" for snapshot in derived_snapshots)
        else:
            lines.append("- none")
    lines.append("")
    rel_path = compiled_path(target, task_id=label)
    content = "\n".join(lines)

    # v0.10.0 §15.3 — optional LLM-generated executive summary for
    # weekly / monthly snapshots.  The deterministic body above is the
    # source of truth; the narrative is spliced in front of it via
    # :func:`memory_snapshot_narrative.inject_narrative`, which is
    # idempotent and additive (re-running without ``narrative=True``
    # produces an identical body).
    narrative_outcome: dict[str, Any] | None = None
    if narrative and target in {"weekly_snapshot", "monthly_snapshot"}:
        narrative_outcome = _maybe_generate_snapshot_narrative(
            config,
            target=target,
            label=label,
            records=in_window,
        )
        if narrative_outcome and narrative_outcome.get("ok"):
            section = str(narrative_outcome.get("section") or "")
            if section.strip():
                from .memory_snapshot_narrative import inject_narrative

                content = inject_narrative(content, section)

    result = write_compiled_view(
        config,
        target=target,
        rel_path=rel_path,
        content=content,
        included=in_window,
        user=user,
        task_id=task_id,
        branch=branch,
        body_mode=body_mode,
        cache_hint=label,
        cache_extra={
            "snapshot_id": snapshot_id,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "derived_from_snapshot_ids": derived_snapshots,
            "summary": {
                "top_changes": [summary_with_score(record, score_data) for record, score_data in top_changes[:5]],
                "top_reused_memories": [
                    summary_with_score(record, score_data) for record, score_data in top_reused[:5]
                ],
                "open_questions": [summary_with_score(record, score_data) for record, score_data in open_items[:5]],
            },
        },
    )
    if result.get("ok"):
        result["snapshot_id"] = snapshot_id
        result["window_start"] = window_start.isoformat()
        result["window_end"] = window_end.isoformat()
        result["derived_from_snapshot_ids"] = derived_snapshots
    if narrative_outcome is not None and isinstance(result, dict):
        result["narrative"] = narrative_outcome
    return result


# ── dao / fa / shu cognitive-level digests ────────────────────────────


def compile_level_digest(
    config: MemoryConfig,
    *,
    target: str,
    records: list[CompilableRecord],
    user: str | None,
    task_id: str | None,
    branch: str | None,
    body_mode: str,
) -> dict[str, Any]:
    level = DIGEST_LEVELS[target]
    filtered = [
        record
        for record in records
        if str(record.metadata.get("cognitive_level", "")) == level
        and str(record.metadata.get("status", "")) in {"validated", "published"}
    ]
    scored = scored_records(config, filtered)
    title = f"{level.upper()} Digest"
    lines = [
        f"# {title}",
        "",
        "> Deterministic cognitive-level digest.",
        "",
        f"- level: `{level}`",
        f"- records: `{len(filtered)}`",
        "",
        "## Included Records",
        "",
    ]
    for record, score_data in scored:
        lines.extend(
            [
                f"### {record.title}",
                "",
                f"- id: `{record.metadata.get('id')}`",
                f"- path: `{record.path}`",
                f"- status: `{record.metadata.get('status')}`",
                f"- importance_score: `{score_data.get('total')}`",
                "",
                _compact_body(record),
                "",
            ]
        )
    if not scored:
        lines.append("No records matched the compile filters.\n")
    return write_compiled_view(
        config,
        target=target,
        rel_path=compiled_path(target),
        content="\n".join(lines),
        included=filtered,
        user=user,
        task_id=task_id,
        branch=branch,
        body_mode=body_mode,
    )


# ── Review queue ──────────────────────────────────────────────────────


def compile_review_queue(
    config: MemoryConfig,
    *,
    records: list[CompilableRecord],
    user: str | None,
    task_id: str | None,
    branch: str | None,
    body_mode: str,
) -> dict[str, Any]:
    scored = scored_records(
        config,
        [
            record
            for record in records
            if str(record.metadata.get("status", "")) in {"raw", "candidate", "validated", "published", "degraded"}
        ],
    )
    hottest = [item for item in scored if item[1].get("effective_memory_tier") == "hot"]
    newest_rules = [
        item
        for item in scored
        if str(item[0].metadata.get("record_kind")) in {"decision", "procedure", "system_rule"}
    ]
    discarded = [item for item in scored if str(item[0].metadata.get("status")) in {"degraded", "archived"}]
    lines = [
        "# Review Queue",
        "",
        "> Deterministic review queue ordered by governance, usage, impact, novelty, conflict, and decay.",
        "",
        "## Most Important Now",
        "",
    ]
    lines.extend(format_record_bullets(scored, limit=10))
    lines.extend(["", "## Hot Tier", ""])
    lines.extend(format_record_bullets(hottest, limit=10))
    lines.extend(["", "## New Stable Rules", ""])
    lines.extend(format_record_bullets(newest_rules, limit=10))
    lines.extend(["", "## Discarded Paths", ""])
    lines.extend(format_record_bullets(discarded, limit=10))
    lines.append("")
    result = write_compiled_view(
        config,
        target="review_queue",
        rel_path=compiled_path("review_queue"),
        content="\n".join(lines),
        included=[record for record, _score_data in scored[:20]],
        user=user,
        task_id=task_id,
        branch=branch,
        body_mode=body_mode,
    )
    if result.get("ok"):
        result["ranked"] = [summary_with_score(record, score_data) for record, score_data in scored[:10]]
    return result


# ── Rollback context ──────────────────────────────────────────────────


def compile_rollback_context(
    config: MemoryConfig,
    *,
    records: list[CompilableRecord],
    user: str | None,
    task_id: str | None,
    branch: str | None,
    body_mode: str,
) -> dict[str, Any]:
    scoped = [
        record
        for record in records
        if (task_id is None or record.metadata.get("task_id") == task_id)
        and (branch is None or record.metadata.get("branch") in (None, branch))
    ]
    rollback_candidates = [
        record
        for record in scoped
        if str(record.metadata.get("record_kind", "")) in {"incident", "decision", "procedure"}
        or bool(record.metadata.get("supersedes"))
        or bool(record.metadata.get("conflicts_with"))
    ]
    scored = scored_records(config, rollback_candidates)
    lines = [
        "# Rollback Context",
        "",
        "> Deterministic rollback-oriented context view.",
        "",
        f"- task_id: `{bullet_value(task_id)}`",
        f"- branch: `{bullet_value(branch)}`",
        "",
        "## Rollback Chain",
        "",
    ]
    lines.extend(format_record_bullets(scored, limit=15))
    lines.extend(["", "## Source References", ""])
    if scored:
        lines.extend(f"- `{record.metadata.get('id')}` -> `{record.path}`" for record, _score_data in scored[:15])
    else:
        lines.append("- none")
    lines.append("")
    result = write_compiled_view(
        config,
        target="rollback_context",
        rel_path=compiled_path("rollback_context", user=user, task_id=task_id, branch=branch),
        content="\n".join(lines),
        included=rollback_candidates,
        user=user,
        task_id=task_id,
        branch=branch,
        body_mode=body_mode,
    )
    if result.get("ok"):
        result["ranked"] = [summary_with_score(record, score_data) for record, score_data in scored[:10]]
    return result


# ── Snapshot diff ─────────────────────────────────────────────────────


def memory_compare_snapshots(config: MemoryConfig, *, path: str, other_path: str) -> dict[str, Any]:
    left = find_compile_cache_entry(config, path)
    right = find_compile_cache_entry(config, other_path)
    if left is None:
        return error_result("not_found", f"compiled snapshot metadata not found: {path}", path=path)
    if right is None:
        return error_result("not_found", f"compiled snapshot metadata not found: {other_path}", path=other_path)
    left_ids = {str(item) for item in left.get("included_record_ids", []) if str(item).strip()}
    right_ids = {str(item) for item in right.get("included_record_ids", []) if str(item).strip()}
    try:
        records, _stats = iter_compilable_records(config)
    except (PathSecurityError, FileNotFoundError) as exc:
        return error_result("path_error", str(exc))
    by_id = {str(record.metadata.get("id", "")): record for record in records}

    def summarize(record_id: str) -> dict[str, Any]:
        record = by_id.get(record_id)
        if record is None:
            return {"id": record_id, "path": None, "title": None}
        return {
            "id": record_id,
            "path": record.path,
            "title": record.title,
            "record_kind": record.metadata.get("record_kind"),
        }

    added = [summarize(record_id) for record_id in sorted(right_ids - left_ids)]
    removed = [summarize(record_id) for record_id in sorted(left_ids - right_ids)]
    persisted = [summarize(record_id) for record_id in sorted(left_ids & right_ids)]
    return ok_result(
        "snapshots compared",
        left={"path": path, "snapshot_id": left.get("snapshot_id"), "target": left.get("target")},
        right={"path": other_path, "snapshot_id": right.get("snapshot_id"), "target": right.get("target")},
        added=added,
        removed=removed,
        persisted=persisted,
        stats={
            "left_records": len(left_ids),
            "right_records": len(right_ids),
            "added": len(added),
            "removed": len(removed),
            "persisted": len(persisted),
        },
    )


__all__ = [
    "reference_time",
    "time_window",
    "format_record_bullets",
    "compile_snapshot_target",
    "compile_level_digest",
    "compile_review_queue",
    "compile_rollback_context",
    "memory_compare_snapshots",
]
