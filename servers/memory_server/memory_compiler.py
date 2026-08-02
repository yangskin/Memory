"""Memory compile orchestration.

Thin entry-point that drives a corpus scan + filter and dispatches to
the right view in :mod:`memory_compile_views` (which now also hosts the
formerly-split leaf helpers: targets, render, scoring, writer).

Back-compat re-exports keep `from .memory_compiler import …` working
for tests, ``server_dispatch``, and external callers.
"""

from __future__ import annotations

from typing import Any

from .memory_compile_views import (
    DEFAULT_BODY_MODE,
    DEFAULT_INCLUDE_SCOPES,
    DEFAULT_INCLUDE_STATUSES,
    DIGEST_LEVELS,
    SNAPSHOT_TARGETS,
    SUPPORTED_BODY_MODES,
    SUPPORTED_TARGETS,
    compile_level_digest as _compile_level_digest,
    compile_review_queue as _compile_review_queue,
    compile_rollback_context as _compile_rollback_context,
    compile_snapshot_target as _compile_snapshot_target,
    compiled_path as _compiled_path,
    memory_compare_snapshots,  # re-export for server_dispatch / tests
    record_sort_key as _record_sort_key,
    render_compile_markdown as _render_compile_markdown,
    write_compiled_view as _write_compiled_view,
)
from .memory_compiler_cache import (  # re-exports preserved for callers/tests
    find_compile_cache_entry,
    get_record_last_used_at,
    load_compile_cache_entries,
)
from .memory_config import MemoryConfig
from .memory_identity import canonical_identity
from .memory_corpus import CompilableRecord, iter_compilable_records as _iter_records
from .memory_events import get_current_user
from .memory_paths import PathManager, PathSecurityError
from .memory_result import error_result, ok_result
from .memory_task_context import get_task_ids_for_user


# ── Filter ─────────────────────────────────────────────────────────────


def _matches_filter(
    record: CompilableRecord,
    *,
    user: str | None,
    user_task_ids: set[str] | None = None,
    task_id: str | None,
    branch: str | None,
    include_scopes: list[str],
    include_statuses: list[str],
    preferred_tags: list[str],
) -> bool:
    metadata = record.metadata
    scope = str(metadata.get("scope", ""))
    status = str(metadata.get("status", ""))
    author = str(metadata.get("author", ""))
    record_task_id = metadata.get("task_id")
    record_branch = metadata.get("branch")
    tags = [str(tag) for tag in metadata.get("tags", []) if str(tag)]

    if scope not in include_scopes:
        return False
    if status not in include_statuses:
        return False
    # Author isolation: private records are visible when authored by the
    # current user or attached to a task context owned by that user.
    if scope in {"personal", "session", "user_private"} and user:
        known_task_ids = user_task_ids or set()
        record_task = str(record_task_id or "").strip()
        if canonical_identity(author) != canonical_identity(user) and not (record_task and record_task in known_task_ids):
            return False
    if task_id and record_task_id != task_id:
        return False
    if branch and record_branch not in (None, branch):
        return False
    if preferred_tags and not set(preferred_tags).intersection(tags):
        return False
    return True


# ── Public entry: memory_compile ──────────────────────────────────────


def memory_compile(
    config: MemoryConfig,
    *,
    target: str,
    user: str | None = None,
    task_id: str | None = None,
    branch: str | None = None,
    include_scopes: list[str] | None = None,
    include_statuses: list[str] | None = None,
    preferred_tags: list[str] | None = None,
    body_mode: str | None = None,
    as_of: str | None = None,
    narrative: bool = False,
) -> dict[str, Any]:
    if target not in SUPPORTED_TARGETS:
        return error_result("invalid_input", f"target must be one of: {', '.join(sorted(SUPPORTED_TARGETS))}")
    effective_body_mode = body_mode or DEFAULT_BODY_MODE
    if effective_body_mode not in SUPPORTED_BODY_MODES:
        return error_result("invalid_input", f"body_mode must be one of: {', '.join(sorted(SUPPORTED_BODY_MODES))}")
    for name, value in {
        "include_scopes": include_scopes,
        "include_statuses": include_statuses,
        "preferred_tags": preferred_tags,
    }.items():
        if value is not None and (
            not isinstance(value, list) or not all(isinstance(item, str) for item in value)
        ):
            return error_result("invalid_input", f"{name} must be a list of strings")

    effective_user = user or get_current_user(config.repo_root)
    if user is None and target in {"publish_queue", "system_digest"}:
        effective_user = None
    if effective_user == "unknown":
        effective_user = None
    scopes = [str(item) for item in (include_scopes or DEFAULT_INCLUDE_SCOPES)]
    if include_statuses is not None:
        statuses = [str(item) for item in include_statuses]
    elif target == "publish_queue":
        statuses = ["candidate"]
    else:
        statuses = [str(item) for item in DEFAULT_INCLUDE_STATUSES]
    tags = [str(item) for item in (preferred_tags or [])]
    user_task_ids = get_task_ids_for_user(config, effective_user)
    if include_scopes is None and target == "system_digest":
        scopes = ["shared"]
    elif include_scopes is None and target == "publish_queue":
        scopes = ["shared", "personal"]

    try:
        records, scan_stats = _iter_records(config)
    except PathSecurityError as exc:
        return error_result("path_not_allowed", str(exc))
    except FileNotFoundError as exc:
        return error_result("not_found", str(exc))

    if target in SNAPSHOT_TARGETS:
        result = _compile_snapshot_target(
            config,
            target=target,
            records=records,
            user=effective_user,
            task_id=task_id,
            branch=branch,
            body_mode=effective_body_mode,
            as_of=as_of,
            narrative=narrative,
        )
        if result.get("ok"):
            result["stats"] = {**scan_stats, "matched_records": len(result.get("included_record_ids", []))}
        return result

    if target in DIGEST_LEVELS:
        result = _compile_level_digest(
            config,
            target=target,
            records=records,
            user=effective_user,
            task_id=task_id,
            branch=branch,
            body_mode=effective_body_mode,
        )
        if result.get("ok"):
            result["stats"] = {**scan_stats, "matched_records": len(result.get("included_record_ids", []))}
        return result

    if target == "review_queue":
        result = _compile_review_queue(
            config,
            records=records,
            user=effective_user,
            task_id=task_id,
            branch=branch,
            body_mode=effective_body_mode,
        )
        if result.get("ok"):
            result["stats"] = {**scan_stats, "matched_records": len(result.get("included_record_ids", []))}
        return result

    if target == "rollback_context":
        result = _compile_rollback_context(
            config,
            records=records,
            user=effective_user,
            task_id=task_id,
            branch=branch,
            body_mode=effective_body_mode,
        )
        if result.get("ok"):
            result["stats"] = {**scan_stats, "matched_records": len(result.get("included_record_ids", []))}
        return result

    included = [
        record
        for record in records
        if _matches_filter(
            record,
            user=effective_user,
            user_task_ids=user_task_ids,
            task_id=task_id,
            branch=branch,
            include_scopes=scopes,
            include_statuses=statuses,
            preferred_tags=tags,
        )
    ]
    included.sort(key=_record_sort_key)

    content = _render_compile_markdown(
        config=config,
        target=target,
        records=included,
        user=effective_user,
        task_id=task_id,
        branch=branch,
        include_scopes=scopes,
        include_statuses=statuses,
        preferred_tags=tags,
        body_mode=effective_body_mode,
    )

    result = _write_compiled_view(
        config,
        target=target,
        rel_path=_compiled_path(target, user=effective_user, task_id=task_id, branch=branch),
        content=content,
        included=included,
        user=effective_user,
        task_id=task_id,
        branch=branch,
        body_mode=effective_body_mode,
        cache_extra={
            "include_scopes": scopes,
            "include_statuses": statuses,
            "preferred_tags": tags,
        },
    )
    if result.get("ok"):
        result["stats"] = {**scan_stats, "matched_records": len(included)}
    return result


# ── Public entry: memory_get_runtime_digest ───────────────────────────


def memory_get_runtime_digest(
    config: MemoryConfig,
    *,
    user: str | None = None,
    task_id: str | None = None,
    branch: str | None = None,
    max_chars: int | None = None,
) -> dict[str, Any]:
    if max_chars is not None and max_chars < 0:
        return error_result("invalid_input", "max_chars must be >= 0")

    effective_user = user or get_current_user(config.repo_root)
    if effective_user == "unknown":
        effective_user = None
    rel_path = _compiled_path("runtime_digest", user=effective_user, task_id=task_id, branch=branch)
    manager = PathManager(config)
    try:
        resolved = manager.resolve(rel_path, must_exist=True, must_be_file=True)
    except PathSecurityError as exc:
        return error_result("path_not_allowed", str(exc))
    except FileNotFoundError:
        return error_result(
            "not_found",
            f"runtime digest does not exist: {rel_path}. Run memory_compile first.",
            path=rel_path,
        )
    except IsADirectoryError as exc:
        return error_result("invalid_path", str(exc))

    try:
        content = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return error_result("read_failed", f"failed to read runtime digest: {exc}")

    truncated = False
    if max_chars is not None:
        if len(content) > max_chars:
            content = content[:max_chars]
            truncated = True

    return ok_result(
        "runtime digest read",
        path=rel_path,
        content=content,
        truncated=truncated,
    )


__all__ = [
    "memory_compile",
    "memory_get_runtime_digest",
    "memory_compare_snapshots",
    "find_compile_cache_entry",
    "get_record_last_used_at",
    "load_compile_cache_entries",
]
