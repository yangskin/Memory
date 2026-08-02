from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .memory_config import DEFAULT_ALLOWED_TAGS, MemoryConfig
from .memory_events import append_event, get_current_user
from .memory_identity import canonical_identity
from .memory_frontmatter import (
    PACK_HEADER,
    _format_scalar,
    _parse_scalar,
    dump_front_matter,
    parse_front_matter,
    parse_record_markdown,
    render_record_pack_entry,
    render_record_markdown,
)
from .memory_locks import file_lock
from .memory_paths import PathManager, PathSecurityError
from .memory_result import error_result, ok_result

SCHEMA_VERSION = "1.0"
SCHEMA_VERSION_V2 = "2.0"

V1_RECORD_KINDS = {
    "note",
    "event",
    "claim_candidate",
    "rule_candidate",
    "handoff",
    "skill_candidate",
    "validation_result",
    "system_rule",
    "archive_record",
}

P3_RECORD_KINDS = {
    "observation",
    "artifact_ref",
    "incident",
    "decision",
    "procedure",
    "distilled_summary",
    "snapshot_daily",
    "snapshot_weekly",
    "snapshot_monthly",
}

ALLOWED_RECORD_KINDS = V1_RECORD_KINDS | P3_RECORD_KINDS

V1_SCOPES = {"personal", "shared", "local", "archive"}
P3_SCOPES = {"session", "user_private", "task_or_branch", "project_shared", "org_shared"}
ALLOWED_SCOPES = V1_SCOPES | P3_SCOPES
ALLOWED_STATUSES = {"raw", "candidate", "validated", "published", "degraded", "archived", "distilled"}
ALLOWED_MEMORY_TIERS = {"hot", "warm", "cold", "fossil"}
ALLOWED_COGNITIVE_LEVELS = {"dao", "fa", "shu"}

V2_LIST_FIELDS = [
    "derived_from_record_ids",
    "derived_from_snapshot_ids",
    "derived_from_revision_ids",
    "supersedes",
    "conflicts_with",
    "related_artifact_ids",
    "asset_paths",
    "map_names",
    "plugin_names",
    "module_names",
    "class_names",
    "blueprint_paths",
]

V2_SCALAR_FIELDS = [
    "occurred_at",
    "valid_from",
    "valid_to",
    "memory_tier",
    "cognitive_level",
    "importance_score",
    "system_area",
    "provenance",
    "immutable",
    "authoritative",
    "replaceable",
    "model",
    "distilled_at",
]

V2_FIELDS = set(V2_LIST_FIELDS) | set(V2_SCALAR_FIELDS)

# Built-in default tag vocabulary. Sourced from memory_config so the runtime
# validator and the default-config writer cannot drift apart. Custom configs
# can still override via tag_schema.allowed_tags.
ALLOWED_TAGS = frozenset(DEFAULT_ALLOWED_TAGS)


def _default_status(record_kind: str) -> str:
    if record_kind.endswith("_candidate"):
        return "candidate"
    if record_kind == "system_rule":
        return "published"
    if record_kind == "archive_record":
        return "archived"
    if record_kind == "distilled_summary":
        return "distilled"
    return "raw"


def _record_id(now: datetime) -> str:
    return f"mem_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def _slug_user(author: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", author.strip()).strip("-._")
    return slug or "unknown"


def _slug_path_segment(value: str | None, fallback: str, *, max_len: int = 80) -> str:
    slug = _slug_user(str(value or ""))
    if slug == "unknown":
        slug = fallback
    slug = slug[:max_len].strip("-._")
    return slug or fallback


def _is_shared_record_target(record_kind: str, scope: str, status: str) -> bool:
    return scope in {"shared", "project_shared", "org_shared"} or status == "published" or record_kind == "system_rule"


def _target_path(record_id: str, record_kind: str, scope: str, status: str, author: str) -> str:
    if status == "candidate":
        return f"memory-bank/candidates/{record_id}.md"
    if status == "archived" or record_kind == "archive_record" or scope == "archive":
        return f"memory-bank/archive/{record_id}.md"
    if _is_shared_record_target(record_kind, scope, status):
        return f"memory-bank/shared/{record_id}.md"
    return f"memory-bank/people/{_slug_user(author)}/{record_id}.md"


def target_path_for_record(record_id: str, record_kind: str, scope: str, status: str, author: str) -> str:
    return _target_path(record_id, record_kind, scope, status, author)


def _target_pack_path(
    record_id: str,
    record_kind: str,
    scope: str,
    status: str,
    author: str,
    now: datetime,
    index: int,
    *,
    task_id: str | None = None,
    branch: str | None = None,
) -> str:
    single_path = Path(_target_path(record_id, record_kind, scope, status, author))
    date_label = now.strftime("%Y%m%d")
    pack_dir = single_path.parent / "packs"
    work_key = task_id or branch
    if _is_shared_record_target(record_kind, scope, status):
        author_slug = _slug_path_segment(author, "unknown")
        work_slug = _slug_path_segment(work_key, "general", max_len=96)
        pack_dir = pack_dir / author_slug / work_slug
    elif work_key:
        work_slug = _slug_path_segment(work_key, "general", max_len=96)
        pack_dir = pack_dir / work_slug
    return (pack_dir / f"{date_label}-{index:03d}.md").as_posix()


def _write_record_to_pack(
    config: MemoryConfig,
    *,
    record_id: str,
    record_kind: str,
    scope: str,
    status: str,
    author: str,
    now: datetime,
    final_content: str,
    task_id: str | None = None,
    branch: str | None = None,
) -> dict[str, Any] | None:
    entry = render_record_pack_entry(record_id, final_content)
    if len(entry) + len(PACK_HEADER) + 2 > config.record_packing_max_pack_chars:
        return error_result(
            "record_too_large",
            f"record {record_id} exceeds record_packing.max_pack_chars={config.record_packing_max_pack_chars}",
        )

    manager = PathManager(config)
    for pack_index in range(1, 1000):
        rel_path = _target_pack_path(
            record_id,
            record_kind,
            scope,
            status,
            author,
            now,
            pack_index,
            task_id=task_id,
            branch=branch,
        )
        try:
            resolved = manager.resolve(rel_path, must_exist=False, must_be_file=False)
        except PathSecurityError as exc:
            return error_result("path_not_allowed", str(exc))

        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            with file_lock(config.repo_root, resolved):
                if resolved.exists() and not resolved.is_file():
                    return error_result("invalid_path", f"target is not a file: {rel_path}")
                existing = resolved.read_text(encoding="utf-8") if resolved.exists() else ""
                if existing:
                    updated = existing + ("" if existing.endswith("\n") else "\n") + "\n" + entry
                else:
                    updated = f"{PACK_HEADER}\n\n{entry}"
                if len(updated) > config.record_packing_max_pack_chars:
                    continue
                # 记录包是原始事实源，不能用原地 append：进程被强杀时 append
                # 可能留下半条 Front Matter 并污染同包全部记录。原子替换保证
                # 磁盘上始终是旧完整包或新完整包。
                from .memory_record_io import _atomic_write_text

                _atomic_write_text(resolved, updated, fsync_strict=config.mcp_fsync_strict)
                return ok_result(
                    "record packed",
                    id=record_id,
                    path=rel_path,
                    record_kind=record_kind,
                    scope=scope,
                    status=status,
                    author=author,
                    packed=True,
                )
        except OSError as exc:
            return error_result("write_failed", f"failed to write record pack: {exc}")

    return error_result("pack_full", "no available record pack slot")


def _validate_record_input(
    *,
    content_markdown: str,
    schema_version: str,
    record_kind: str,
    scope: str,
    status: str,
    tags: list[str],
    confidence: float | None,
    memory_tier: str | None = None,
    cognitive_level: str | None = None,
    importance_score: float | None = None,
    allowed_tags: list[str] | None = None,
) -> str | None:
    if not content_markdown.strip():
        return "content_markdown must not be empty"
    if "\x00" in content_markdown:
        return "content_markdown must not contain NUL bytes"
    if "\ufffd" in content_markdown:
        return "content_markdown contains Unicode replacement characters; repair the source encoding first"
    try:
        content_markdown.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        return f"content_markdown is not valid UTF-8 text: {exc}"
    if schema_version not in {SCHEMA_VERSION, SCHEMA_VERSION_V2}:
        return f"schema_version must be one of: {SCHEMA_VERSION}, {SCHEMA_VERSION_V2}"
    if record_kind not in ALLOWED_RECORD_KINDS:
        return f"record_kind must be one of: {', '.join(sorted(ALLOWED_RECORD_KINDS))}"
    if scope not in ALLOWED_SCOPES:
        return f"scope must be one of: {', '.join(sorted(ALLOWED_SCOPES))}"
    if status not in ALLOWED_STATUSES:
        return f"status must be one of: {', '.join(sorted(ALLOWED_STATUSES))}"
    allowed_tag_set = set(allowed_tags or ALLOWED_TAGS)
    unknown_tags = sorted(set(tags) - allowed_tag_set)
    if unknown_tags:
        return f"tags contain unsupported value(s): {', '.join(unknown_tags)}"
    if confidence is not None and not 0 <= confidence <= 1:
        return "confidence must be between 0 and 1"
    if memory_tier is not None and memory_tier not in ALLOWED_MEMORY_TIERS:
        return f"memory_tier must be one of: {', '.join(sorted(ALLOWED_MEMORY_TIERS))}"
    if cognitive_level is not None and cognitive_level not in ALLOWED_COGNITIVE_LEVELS:
        return f"cognitive_level must be one of: {', '.join(sorted(ALLOWED_COGNITIVE_LEVELS))}"
    if importance_score is not None and not 0 <= importance_score <= 1:
        return "importance_score must be between 0 and 1"
    return None


def _tag_schema_error_details(
    *,
    tags: list[str],
    allowed_tags: list[str] | None,
    tag_schema_version: str | None,
) -> dict[str, Any]:
    allowed = sorted(set(allowed_tags or ALLOWED_TAGS))
    rejected = sorted(set(tags) - set(allowed))
    if not rejected:
        return {}
    return {
        "invalid_field": "tags",
        "rejected_tags": rejected,
        "allowed_tags": allowed,
        "tag_schema_version": tag_schema_version,
        "hint": "Use allowed_tags for tags; put domain words in system_area, typed metadata fields, or the record body.",
    }


def _normalize_string_list(value: list[str] | None) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _uses_v2_schema(record_kind: str, scope: str, metadata: dict[str, Any]) -> bool:
    if record_kind in P3_RECORD_KINDS or scope in P3_SCOPES:
        return True
    return any(metadata.get(field) not in (None, [], "") for field in V2_FIELDS)


def memory_write_record(
    config: MemoryConfig,
    *,
    content_markdown: str,
    schema_version: str | None = None,
    record_kind: str = "note",
    scope: str = "personal",
    status: str | None = None,
    author: str | None = None,
    tags: list[str] | None = None,
    confidence: float | None = None,
    source_refs: list[str] | None = None,
    task_id: str | None = None,
    branch: str | None = None,
    validated_by: str | None = None,
    classifier_model: str | None = None,
    classifier_prompt_version: str | None = None,
    tag_schema_version: str | None = None,
    occurred_at: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    memory_tier: str | None = None,
    cognitive_level: str | None = None,
    derived_from_record_ids: list[str] | None = None,
    derived_from_snapshot_ids: list[str] | None = None,
    derived_from_revision_ids: list[str] | None = None,
    supersedes: list[str] | None = None,
    conflicts_with: list[str] | None = None,
    related_artifact_ids: list[str] | None = None,
    importance_score: float | None = None,
    asset_paths: list[str] | None = None,
    map_names: list[str] | None = None,
    plugin_names: list[str] | None = None,
    module_names: list[str] | None = None,
    class_names: list[str] | None = None,
    blueprint_paths: list[str] | None = None,
    system_area: str | None = None,
    provenance: str | None = None,
    immutable: bool | None = None,
    authoritative: bool | None = None,
    replaceable: bool | None = None,
    model: str | None = None,
    distilled_at: str | None = None,
) -> dict[str, Any]:
    """Write a structured memory record as Markdown + Front Matter."""
    normalized_tags = _normalize_string_list(tags)
    normalized_source_refs = _normalize_string_list(source_refs)
    effective_status = status or _default_status(record_kind)
    effective_author = canonical_identity(author or get_current_user(config.repo_root))
    try:
        effective_confidence = float(confidence) if confidence is not None else None
        effective_importance_score = float(importance_score) if importance_score is not None else None
    except (TypeError, ValueError, OverflowError) as exc:
        return error_result("invalid_input", f"confidence/importance_score must be numeric: {exc}")
    v2_metadata: dict[str, Any] = {
        "occurred_at": occurred_at,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "memory_tier": memory_tier,
        "cognitive_level": cognitive_level,
        "derived_from_record_ids": _normalize_string_list(derived_from_record_ids),
        "derived_from_snapshot_ids": _normalize_string_list(derived_from_snapshot_ids),
        "derived_from_revision_ids": _normalize_string_list(derived_from_revision_ids),
        "supersedes": _normalize_string_list(supersedes),
        "conflicts_with": _normalize_string_list(conflicts_with),
        "related_artifact_ids": _normalize_string_list(related_artifact_ids),
        "importance_score": effective_importance_score,
        "asset_paths": _normalize_string_list(asset_paths),
        "map_names": _normalize_string_list(map_names),
        "plugin_names": _normalize_string_list(plugin_names),
        "module_names": _normalize_string_list(module_names),
        "class_names": _normalize_string_list(class_names),
        "blueprint_paths": _normalize_string_list(blueprint_paths),
        "system_area": system_area,
        "provenance": provenance,
        "immutable": immutable,
        "authoritative": authoritative,
        "replaceable": replaceable,
        "model": model,
        "distilled_at": distilled_at,
    }
    effective_schema_version = (
        schema_version
        or (SCHEMA_VERSION_V2 if _uses_v2_schema(record_kind, scope, v2_metadata) else SCHEMA_VERSION)
    )
    if effective_schema_version == SCHEMA_VERSION and _uses_v2_schema(record_kind, scope, v2_metadata):
        return error_result("invalid_input", "schema_version 2.0 is required for P3 record kinds, scopes, or v2 fields")

    validation_error = _validate_record_input(
        content_markdown=content_markdown,
        schema_version=effective_schema_version,
        record_kind=record_kind,
        scope=scope,
        status=effective_status,
        tags=normalized_tags,
        confidence=effective_confidence,
        memory_tier=memory_tier,
        cognitive_level=cognitive_level,
        importance_score=effective_importance_score,
        allowed_tags=config.tag_allowed_tags,
    )
    if validation_error:
        details: dict[str, Any] = {}
        if validation_error.startswith("tags contain unsupported value"):
            details = _tag_schema_error_details(
                tags=normalized_tags,
                allowed_tags=config.tag_allowed_tags,
                tag_schema_version=tag_schema_version or config.tag_schema_version,
            )
        return error_result("invalid_input", validation_error, **details)

    now = datetime.now(timezone.utc)
    now_text = now.isoformat()
    record_id = _record_id(now)
    rel_path = _target_path(record_id, record_kind, scope, effective_status, effective_author)

    metadata: dict[str, Any] = {
        "schema_version": effective_schema_version,
        "id": record_id,
        "record_kind": record_kind,
        "scope": scope,
        "status": effective_status,
        "author": effective_author,
        "created_at": now_text,
        "updated_at": now_text,
        "tags": normalized_tags,
        "confidence": effective_confidence,
        "source_refs": normalized_source_refs,
        "task_id": task_id,
        "branch": branch,
        "validated_by": validated_by,
        "last_used_at": None,
        "classifier_model": classifier_model,
        "classifier_prompt_version": classifier_prompt_version,
        "tag_schema_version": tag_schema_version or config.tag_schema_version,
    }
    if effective_schema_version == SCHEMA_VERSION_V2:
        metadata.update(v2_metadata)

    final_content = render_record_markdown(metadata, content_markdown)

    pack_result = _write_record_to_pack(
        config,
        record_id=record_id,
        record_kind=record_kind,
        scope=scope,
        status=effective_status,
        author=effective_author,
        now=now,
        final_content=final_content,
        task_id=task_id,
        branch=branch,
    )
    if pack_result is not None:
        if not pack_result.get("ok"):
            return pack_result
        rel_path = str(pack_result.get("path"))
        event_warning = _append_event_warning(
            config,
            {
                "id": record_id,
                "path": rel_path,
                "record_kind": record_kind,
                "scope": scope,
                "status": effective_status,
                "tags": normalized_tags,
                "task_id": task_id,
                "branch": branch,
                "packed": True,
            },
        )
        extra: dict[str, Any] = {}
        warnings = _build_ue_warnings(config, plugin_names, module_names)
        if event_warning:
            warnings = [*(warnings or []), event_warning]
        index_warning = _sync_record_index(config, rel_path)
        if index_warning:
            warnings = [*(warnings or []), index_warning]
        if warnings:
            extra["warnings"] = warnings
        return ok_result(
            "record written",
            id=record_id,
            path=rel_path,
            record_kind=record_kind,
            scope=scope,
            status=effective_status,
            author=effective_author,
            packed=True,
            **extra,
        )

    manager = PathManager(config)
    try:
        resolved = manager.resolve(rel_path, must_exist=False, must_be_file=False)
    except PathSecurityError as exc:
        return error_result("path_not_allowed", str(exc))

    if resolved.exists():
        return error_result("already_exists", f"record already exists: {rel_path}")
    if resolved.exists() and not resolved.is_file():
        return error_result("invalid_path", f"target is not a file: {rel_path}")

    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        # Atomic create: O_CREAT|O_EXCL guarantees we are the unique writer
        # for this record id (closes the TOCTOU window above) and prevents
        # concurrent MCP clients from clobbering each other's records.
        fd = os.open(str(resolved), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(final_content)
        except Exception:
            # If write fails after creation, clean up the empty file so retries can succeed.
            try:
                resolved.unlink()
            except OSError:
                pass
            raise
    except FileExistsError:
        return error_result("already_exists", f"record already exists: {rel_path}")
    except OSError as exc:
        return error_result("write_failed", f"failed to write record: {exc}")

    event_warning = _append_event_warning(
        config,
        {
            "id": record_id,
            "path": rel_path,
            "record_kind": record_kind,
            "scope": scope,
            "status": effective_status,
            "tags": normalized_tags,
            "task_id": task_id,
            "branch": branch,
        },
    )

    extra: dict[str, Any] = {}
    warnings = _build_ue_warnings(config, plugin_names, module_names)
    if event_warning:
        warnings = [*(warnings or []), event_warning]
    index_warning = _sync_record_index(config, rel_path)
    if index_warning:
        warnings = [*(warnings or []), index_warning]
    if warnings:
        extra["warnings"] = warnings
    return ok_result(
        "record written",
        id=record_id,
        path=rel_path,
        record_kind=record_kind,
        scope=scope,
        status=effective_status,
        author=effective_author,
        **extra,
    )


def _append_event_warning(config: MemoryConfig, payload: dict[str, Any]) -> dict[str, Any] | None:
    """审计日志故障不能把已经持久化的主记录变成调用失败。"""

    try:
        append_event(config, "memory_write_record", payload)
    except Exception as exc:  # noqa: BLE001 - primary record is already durable
        return {
            "code": "event_log_deferred",
            "message": "primary record was written but its audit event could not be appended",
            "detail": f"{type(exc).__name__}: {exc}"[:500],
        }
    return None


def _sync_record_index(config: MemoryConfig, rel_path: str) -> dict[str, Any] | None:
    """在索引已经存在时同步新记录；失败会留下可自愈的 dirty 标记。"""
    if not (config.repo_root / ".ai-memory/search.db").exists():
        return None
    try:
        from .memory_record_index import mark_index_dirty, memory_update_index

        result = memory_update_index(config, paths=[rel_path])
        if result.get("ok"):
            return None
        mark_index_dirty(config, reason=str(result.get("error") or "index update failed"), paths=[rel_path])
        return {
            "code": "index_sync_deferred",
            "message": "primary record was written; derived search index will self-heal before the next indexed read",
            "path": rel_path,
        }
    except Exception as exc:
        try:
            from .memory_record_index import mark_index_dirty

            mark_index_dirty(config, reason=str(exc), paths=[rel_path])
        except Exception:
            pass
        return {
            "code": "index_sync_deferred",
            "message": "primary record was written; derived search index will self-heal before the next indexed read",
            "path": rel_path,
        }


def _build_ue_warnings(
    config: MemoryConfig,
    plugin_names: list[str] | None,
    module_names: list[str] | None,
) -> list[dict[str, Any]] | None:
    """P1-2: non-blocking warning for unknown UE components.

    Returns ``None`` when no facets are cached or all referenced names
    are recognised — keeps the response payload identical to legacy.
    """
    try:
        from .memory_ue_facets import known_components, load_facets
    except Exception:  # pragma: no cover
        return None
    facets = load_facets(config)
    if facets is None or not facets.is_ue_project:
        return None
    known = known_components(facets)
    referenced: list[str] = []
    referenced.extend(plugin_names or [])
    referenced.extend(module_names or [])
    unknown = sorted(set(referenced) - known)
    if not unknown:
        return None
    return [
        {
            "code": "ue_unknown_components",
            "message": f"referenced names not found in detected UE facets: {unknown}",
            "unknown": unknown,
            "hint": "If this is a new module/plugin, run the bootstrap rescan to refresh .ai-memory/ue_facets.json.",
        }
    ]
