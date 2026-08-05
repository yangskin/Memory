"""Memory MCP — administrative CLI.

A first-class command-line entry point for maintenance / governance
operations that are intentionally kept outside the public agent MCP surface.

Examples (all run from the repository root)::

    python -m servers.memory_server.cli health
    python -m servers.memory_server.cli backup --path memory-bank/activeContext.md
    python -m servers.memory_server.cli compact --path memory-bank/activeContext.md \
        --policy hot_task_to_minimal --apply
    python -m servers.memory_server.cli compile --target runtime_digest

Output: every subcommand prints a JSON-encoded ``ok_result`` /
``error_result`` payload to stdout. Exit code is ``0`` on success and
``1`` on any non-ok result so the CLI is usable from CI / shell scripts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from .memory_backup import backup_files
from .memory_compactor import compact_memory, recover_compaction_transactions
from .memory_compiler import memory_compare_snapshots, memory_compile, memory_get_runtime_digest
from .memory_config import MemoryConfig, load_config
from .memory_diagnose import config_diagnose
from .memory_encoding import audit_memory_encoding, repair_memory_encoding
from .memory_governance import memory_archive_record, memory_publish_candidate, memory_validate_candidate
from .memory_guard import memory_guard_check
from .memory_key_documents import KEY_DOCUMENT_KEYS, rebuild_key_documents
from .memory_key_document_jobs import drain_key_document_rebuild_jobs, read_key_document_rebuild_jobs
from .memory_lineage import memory_link_artifact, memory_list_conflicts, memory_trace_lineage
from .memory_maintenance import memory_delete_record, memory_health_check, memory_migrate_records
from .memory_record_index import memory_rebuild_index
from .memory_record_packing import compact_old_record_packs, pack_existing_records
from .memory_reflection_jobs import (
    backfill_project_reflections,
    curate_project_reflections,
    drain_project_reflection_jobs,
    read_project_reflection_jobs,
)
from .memory_retrieval import memory_get_important_memories, memory_retrieve_context
from .memory_writer import memory_write as memory_write_file
from .memory_baseline import write_baseline as _write_baseline
from .memory_auto_maintenance import run_if_due as _run_if_due
from .memory_worker import MemoryBackgroundWorker


# ── Helpers ────────────────────────────────────────────────────────────


def _emit(payload: dict[str, Any], pretty: bool) -> int:
    indent = 2 if pretty else None
    text = json.dumps(payload, ensure_ascii=False, indent=indent, sort_keys=True) + "\n"
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except UnicodeEncodeError:
        # Windows consoles may still run under a legacy code page (for
        # example GBK). CLI commands must not fail after the maintenance
        # action already succeeded just because JSON contains Unicode.
        buffer = getattr(sys.stdout, "buffer", None)
        if buffer is not None:
            buffer.write(text.encode("utf-8"))
            buffer.flush()
        else:  # pragma: no cover - highly unusual stdout object
            sys.stdout.write(text.encode("ascii", errors="backslashreplace").decode("ascii"))
            sys.stdout.flush()
    return 0 if payload.get("ok") else 1


def _resolve_root(arg_root: str | None) -> Path:
    if arg_root:
        return Path(arg_root).expanduser().resolve()
    return Path.cwd().resolve()


def _load(args: argparse.Namespace) -> MemoryConfig:
    return load_config(_resolve_root(args.root), config_path=args.config)


# ── Subcommand handlers ───────────────────────────────────────────────


def _cmd_guard(args: argparse.Namespace) -> dict[str, Any]:
    return memory_guard_check(_load(args))


def _cmd_health(args: argparse.Namespace) -> dict[str, Any]:
    return memory_health_check(_load(args))


def _cmd_config_diagnose(args: argparse.Namespace) -> dict[str, Any]:
    return config_diagnose(_load(args))


def _cmd_rebuild_index(args: argparse.Namespace) -> dict[str, Any]:
    return memory_rebuild_index(_load(args))


def _cmd_scale_baseline(args: argparse.Namespace) -> dict[str, Any]:
    return _write_baseline(_load(args))


def _cmd_auto_maintenance(args: argparse.Namespace) -> dict[str, Any]:
    return _run_if_due(_load(args))


def _cmd_migrate(args: argparse.Namespace) -> dict[str, Any]:
    return memory_migrate_records(_load(args), target_schema_version=args.target_schema)


def _cmd_pack_existing_records(args: argparse.Namespace) -> dict[str, Any]:
    return pack_existing_records(
        _load(args),
        max_files=args.max_files,
        dry_run=not args.apply,
    )


def _cmd_compact_record_packs(args: argparse.Namespace) -> dict[str, Any]:
    return compact_old_record_packs(
        _load(args),
        older_than_days=args.older_than_days,
        max_pack_chars=args.max_pack_chars,
        max_files=args.max_files,
        dry_run=not args.apply,
    )


def _cmd_backup(args: argparse.Namespace) -> dict[str, Any]:
    if not args.path:
        return {"ok": False, "error": "invalid_input", "message": "at least one --path is required"}
    return backup_files(
        _load(args),
        paths=list(args.path),
        reason=args.reason,
        tag=args.tag,
    )


def _cmd_compact(args: argparse.Namespace) -> dict[str, Any]:
    return compact_memory(
        _load(args),
        path=args.path,
        policy=args.policy,
        dry_run=not args.apply,
        backup=not args.no_backup,
        archive_original=not args.no_archive_original,
        compress_to_tokens=args.compress_to_tokens,
    )


def _cmd_write_file(args: argparse.Namespace) -> dict[str, Any]:
    if args.content is not None and args.content_file is not None:
        return {"ok": False, "error": "invalid_input", "message": "use either --content or --content-file, not both"}
    if args.content_file is not None:
        content = Path(args.content_file).read_text(encoding="utf-8")
    else:
        content = args.content if args.content is not None else sys.stdin.read()
    return memory_write_file(
        _load(args),
        path=args.path,
        content=content,
        mode=args.mode,
        backup=not args.no_backup,
        create_if_missing=not args.no_create,
        reason=args.reason,
    )


def _cmd_validate(args: argparse.Namespace) -> dict[str, Any]:
    return memory_validate_candidate(_load(args), args.record_id, validated_by=args.by)


def _cmd_publish(args: argparse.Namespace) -> dict[str, Any]:
    return memory_publish_candidate(_load(args), args.record_id, published_by=args.by)


def _cmd_archive(args: argparse.Namespace) -> dict[str, Any]:
    return memory_archive_record(_load(args), args.record_id, reason=args.reason)


def _cmd_delete(args: argparse.Namespace) -> dict[str, Any]:
    return memory_delete_record(_load(args), args.record_id, reason=args.reason)


def _cmd_compile(args: argparse.Namespace) -> dict[str, Any]:
    return memory_compile(
        _load(args),
        target=args.target,
        user=args.user,
        task_id=args.task_id,
        branch=args.branch,
        include_scopes=args.include_scopes,
        include_statuses=args.include_statuses,
        preferred_tags=args.preferred_tags,
        body_mode=args.body_mode,
        as_of=args.as_of,
    )


def _cmd_snapshot_rebuild(args: argparse.Namespace) -> dict[str, Any]:
    return memory_compile(
        _load(args),
        target="daily_snapshot",
        user=args.user,
        task_id=args.task_id,
        branch=args.branch,
        as_of=args.as_of,
    )


def _cmd_weekly_snapshot_rebuild(args: argparse.Namespace) -> dict[str, Any]:
    """Rebuild the weekly snapshot.  ``--narrative`` opts into the v0.10.0
    LLM executive-summary section; without it the body is identical to v0.9.x."""
    return memory_compile(
        _load(args),
        target="weekly_snapshot",
        user=args.user,
        task_id=args.task_id,
        branch=args.branch,
        as_of=args.as_of,
        narrative=bool(getattr(args, "narrative", False)),
    )


def _cmd_monthly_snapshot_rebuild(args: argparse.Namespace) -> dict[str, Any]:
    """Rebuild the monthly snapshot.  ``--narrative`` opts into the v0.10.0
    LLM executive-summary section; without it the body is identical to v0.9.x."""
    return memory_compile(
        _load(args),
        target="monthly_snapshot",
        user=args.user,
        task_id=args.task_id,
        branch=args.branch,
        as_of=args.as_of,
        narrative=bool(getattr(args, "narrative", False)),
    )


def _cmd_runtime_digest(args: argparse.Namespace) -> dict[str, Any]:
    return memory_get_runtime_digest(
        _load(args),
        user=args.user,
        task_id=args.task_id,
        branch=args.branch,
        max_chars=args.max_chars,
    )


def _cmd_retrieve_context(args: argparse.Namespace) -> dict[str, Any]:
    return memory_retrieve_context(
        _load(args),
        query=args.query,
        user=args.user,
        task_id=args.task_id,
        branch=args.branch,
        include_scopes=args.include_scopes,
        include_statuses=args.include_statuses,
        preferred_tags=args.preferred_tags,
        window_start=args.window_start,
        window_end=args.window_end,
        system_area=args.system_area,
        asset_paths=args.asset_paths,
        map_names=args.map_names,
        plugin_names=args.plugin_names,
        module_names=args.module_names,
        class_names=args.class_names,
        blueprint_paths=args.blueprint_paths,
        top_k=args.top_k,
        max_chars=args.max_chars,
        max_tokens=args.max_tokens,
        max_items=args.max_items,
        facet_mode=args.facet_mode,
        ranking_version=args.ranking_version,
    )


def _cmd_important_memories(args: argparse.Namespace) -> dict[str, Any]:
    return memory_get_important_memories(
        _load(args),
        query=args.query,
        user=args.user,
        task_id=args.task_id,
        branch=args.branch,
        include_scopes=args.include_scopes,
        include_statuses=args.include_statuses,
        preferred_tags=args.preferred_tags,
        window_start=args.window_start,
        window_end=args.window_end,
        system_area=args.system_area,
        asset_paths=args.asset_paths,
        map_names=args.map_names,
        plugin_names=args.plugin_names,
        module_names=args.module_names,
        class_names=args.class_names,
        blueprint_paths=args.blueprint_paths,
        top_k=args.top_k,
        max_chars=args.max_chars,
        max_tokens=args.max_tokens,
        max_items=args.max_items,
        facet_mode=args.facet_mode,
        ranking_version=args.ranking_version,
    )


def _cmd_trace_lineage(args: argparse.Namespace) -> dict[str, Any]:
    return memory_trace_lineage(_load(args), args.record_id, max_depth=args.max_depth)


def _cmd_list_conflicts(args: argparse.Namespace) -> dict[str, Any]:
    return memory_list_conflicts(_load(args), include_resolved=args.include_resolved)


def _cmd_compare_snapshots(args: argparse.Namespace) -> dict[str, Any]:
    return memory_compare_snapshots(_load(args), path=args.path, other_path=args.other_path)


def _cmd_link_artifact(args: argparse.Namespace) -> dict[str, Any]:
    return memory_link_artifact(
        _load(args),
        args.record_id,
        related_artifact_ids=args.related_artifact_ids,
        asset_paths=args.asset_paths,
        map_names=args.map_names,
        plugin_names=args.plugin_names,
        module_names=args.module_names,
        class_names=args.class_names,
        blueprint_paths=args.blueprint_paths,
        system_area=args.system_area,
    )


def _cmd_enhance(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {"operation": args.operation}
    if args.content is not None:
        payload["content"] = args.content
    if args.content_file is not None:
        payload["content"] = Path(args.content_file).read_text(encoding="utf-8")
    if args.content_markdown is not None:
        payload["content_markdown"] = args.content_markdown
    if args.content_markdown_file is not None:
        payload["content_markdown"] = Path(args.content_markdown_file).read_text(encoding="utf-8")
    for name in ("source_record_id", "task_id", "branch", "max_tokens", "max_chars_per_record", "thinking", "reasoning_effort"):
        value = getattr(args, name, None)
        if value is not None:
            payload[name] = value
    for name in ("allowed_kinds", "allowed_scopes", "allowed_tags"):
        value = getattr(args, name, None)
        if value is not None:
            payload[name] = value
    for name in ("candidates", "records", "record_a", "record_b"):
        raw = getattr(args, name, None)
        if raw is not None:
            payload[name] = json.loads(raw)
    from .server_dispatch import _dispatch_memory_enhance

    return _dispatch_memory_enhance(_load(args), payload)


def _cmd_rebuild_key_docs(args: argparse.Namespace) -> dict[str, Any]:
    targets = list(args.target) if args.target else None
    if targets:
        unknown = [t for t in targets if t not in KEY_DOCUMENT_KEYS]
        if unknown:
            return {
                "ok": False,
                "error": "invalid_input",
                "message": f"unknown target(s): {unknown}; valid: {sorted(KEY_DOCUMENT_KEYS)}",
            }
    return rebuild_key_documents(
        _load(args),
        targets=targets,
        user=args.user,
        renderer=args.renderer,
        guard_prefer_llm=args.renderer in {"auto", "llm"},
    )


def _cmd_key_doc_jobs(args: argparse.Namespace) -> dict[str, Any]:
    config = _load(args)
    if args.drain:
        return drain_key_document_rebuild_jobs(config, max_jobs=args.max_jobs)
    return read_key_document_rebuild_jobs(config)


def _cmd_encoding_audit(args: argparse.Namespace) -> dict[str, Any]:
    return audit_memory_encoding(_load(args), paths=list(args.path) if args.path else None)


def _cmd_encoding_repair(args: argparse.Namespace) -> dict[str, Any]:
    return repair_memory_encoding(
        _load(args),
        path=args.path,
        mode=args.mode,
        apply=args.apply,
        if_match=args.if_match,
    )


def _cmd_recover_transactions(args: argparse.Namespace) -> dict[str, Any]:
    return recover_compaction_transactions(_load(args))


def _cmd_reflection_jobs(args: argparse.Namespace) -> dict[str, Any]:
    config = _load(args)
    if args.drain:
        return drain_project_reflection_jobs(config, max_jobs=args.max_jobs)
    return read_project_reflection_jobs(config)


def _cmd_reflection_backfill(args: argparse.Namespace) -> dict[str, Any]:
    return backfill_project_reflections(_load(args), limit=args.limit, force=args.force)


def _cmd_reflection_curate(args: argparse.Namespace) -> dict[str, Any]:
    return curate_project_reflections(_load(args))


def _cmd_worker_once(args: argparse.Namespace) -> dict[str, Any]:
    config = _load(args)
    return MemoryBackgroundWorker(lambda: config).run_once(config)


# ── Parser ─────────────────────────────────────────────────────────────


def _split_csv(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _csv_arg(value: str) -> list[str]:
    return _split_csv(value) or []


def _add_retrieval_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--query")
    parser.add_argument("--user")
    parser.add_argument("--task-id")
    parser.add_argument("--branch")
    parser.add_argument("--include-scopes", type=_csv_arg)
    parser.add_argument("--include-statuses", type=_csv_arg)
    parser.add_argument("--preferred-tags", type=_csv_arg)
    parser.add_argument("--facet-mode", choices=("hard", "boost"), default="hard")
    parser.add_argument("--ranking-version", choices=("v1", "v2"), default="v2")
    parser.add_argument("--window-start")
    parser.add_argument("--window-end")
    parser.add_argument("--system-area")
    parser.add_argument("--asset-paths", type=_csv_arg)
    parser.add_argument("--map-names", type=_csv_arg)
    parser.add_argument("--plugin-names", type=_csv_arg)
    parser.add_argument("--module-names", type=_csv_arg)
    parser.add_argument("--class-names", type=_csv_arg)
    parser.add_argument("--blueprint-paths", type=_csv_arg)
    parser.add_argument("--top-k", type=int)
    parser.add_argument("--max-chars", type=int)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--max-items", type=int)


def _add_artifact_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--related-artifact-ids", type=_csv_arg)
    parser.add_argument("--asset-paths", type=_csv_arg)
    parser.add_argument("--map-names", type=_csv_arg)
    parser.add_argument("--plugin-names", type=_csv_arg)
    parser.add_argument("--module-names", type=_csv_arg)
    parser.add_argument("--class-names", type=_csv_arg)
    parser.add_argument("--blueprint-paths", type=_csv_arg)
    parser.add_argument("--system-area")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memory-cli",
        description="Memory MCP CLI for admin, sync, rebuild, diagnose, lineage, retrieval, and LLM-enhance flows.",
    )
    parser.add_argument("--root", help="Repository root (defaults to current working directory).")
    parser.add_argument("--config", help="Path to .ai-memory/config.json (defaults to <root>/.ai-memory/config.json).")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output (indent=2).")

    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    sub.add_parser("guard", help="Run memory_guard_check.").set_defaults(func=_cmd_guard)
    sub.add_parser("health", help="Run memory_health_check.").set_defaults(func=_cmd_health)
    sub.add_parser("config-diagnose", help="Report effective Memory MCP configuration sources.").set_defaults(func=_cmd_config_diagnose)
    sub.add_parser("rebuild-index", help="Rebuild SQLite FTS index.").set_defaults(func=_cmd_rebuild_index)
    sub.add_parser("scale-baseline", help="Capture .ai-memory/baseline.json snapshot.").set_defaults(func=_cmd_scale_baseline)
    sub.add_parser("auto-maintenance", help="Run startup auto-maintenance if due.").set_defaults(func=_cmd_auto_maintenance)

    p_migrate = sub.add_parser("migrate", help="Migrate records to a target schema version.")
    p_migrate.add_argument("--target-schema", default="1.0", help="Target schema version (default: 1.0).")
    p_migrate.set_defaults(func=_cmd_migrate)

    p_pack_existing = sub.add_parser(
        "pack-existing-records",
        help="Coalesce existing small single-record Markdown files into date-based record packs (dry-run by default).",
    )
    p_pack_existing.add_argument("--apply", action="store_true", help="Write packs and remove migrated single-record files.")
    p_pack_existing.add_argument("--max-files", type=int, help="Maximum source files to process in this run.")
    p_pack_existing.set_defaults(func=_cmd_pack_existing_records)

    p_compact_packs = sub.add_parser(
        "compact-record-packs",
        help="Merge old date record packs into larger archive packs under memory-bank/archive/record-packs (dry-run by default).",
    )
    p_compact_packs.add_argument("--apply", action="store_true", help="Write archive packs and remove compacted source packs.")
    p_compact_packs.add_argument("--older-than-days", type=int, help="Age threshold. Defaults to record_packing.archive_after_days.")
    p_compact_packs.add_argument("--max-pack-chars", type=int, help="Archive pack size cap. Defaults to record_packing.archive_pack_max_chars.")
    p_compact_packs.add_argument("--max-files", type=int, help="Maximum source pack files to process in this run.")
    p_compact_packs.set_defaults(func=_cmd_compact_record_packs)

    p_backup = sub.add_parser("backup", help="Backup one or more files.")
    p_backup.add_argument("--path", action="append", required=True, help="Repository-relative path (can repeat).")
    p_backup.add_argument("--reason", help="Optional reason captured in the backup record.")
    p_backup.add_argument("--tag", help="Optional tag captured in the backup payload.")
    p_backup.set_defaults(func=_cmd_backup)

    p_compact = sub.add_parser("compact", help="Compact a hot file (dry-run by default).")
    p_compact.add_argument("--path", required=True)
    p_compact.add_argument("--policy", required=True)
    p_compact.add_argument("--apply", action="store_true", help="Disable dry-run and write the compacted file.")
    p_compact.add_argument("--no-backup", action="store_true", help="Skip backing up the original.")
    p_compact.add_argument("--no-archive-original", action="store_true")
    p_compact.add_argument("--compress-to-tokens", type=int)
    p_compact.set_defaults(func=_cmd_compact)

    p_write_file = sub.add_parser("write-file", help="Admin-only file write. Content defaults to stdin.")
    p_write_file.add_argument("--path", required=True)
    p_write_file.add_argument("--content")
    p_write_file.add_argument("--content-file")
    p_write_file.add_argument("--mode", choices=["overwrite", "append"], default="overwrite")
    p_write_file.add_argument("--no-backup", action="store_true")
    p_write_file.add_argument("--no-create", action="store_true")
    p_write_file.add_argument("--reason")
    p_write_file.set_defaults(func=_cmd_write_file)

    p_validate = sub.add_parser("validate", help="Validate a candidate record.")
    p_validate.add_argument("record_id")
    p_validate.add_argument("--by", help="Reviewer username (must differ from author).")
    p_validate.set_defaults(func=_cmd_validate)

    p_publish = sub.add_parser("publish", help="Publish a validated candidate.")
    p_publish.add_argument("record_id")
    p_publish.add_argument("--by", help="Publisher username.")
    p_publish.set_defaults(func=_cmd_publish)

    p_archive = sub.add_parser("archive", help="Archive a record.")
    p_archive.add_argument("record_id")
    p_archive.add_argument("--reason")
    p_archive.set_defaults(func=_cmd_archive)

    p_delete = sub.add_parser("delete", help="Delete an archived record (writes a tombstone).")
    p_delete.add_argument("record_id")
    p_delete.add_argument("--reason")
    p_delete.set_defaults(func=_cmd_delete)

    p_compile = sub.add_parser("compile", help="Run memory_compile against a target view.")
    p_compile.add_argument("--target", required=True)
    p_compile.add_argument("--user")
    p_compile.add_argument("--task-id")
    p_compile.add_argument("--branch")
    p_compile.add_argument("--include-scopes", type=_csv_arg, help="Comma-separated scopes.")
    p_compile.add_argument("--include-statuses", type=_csv_arg, help="Comma-separated statuses.")
    p_compile.add_argument("--preferred-tags", type=_csv_arg, help="Comma-separated tags.")
    p_compile.add_argument("--body-mode")
    p_compile.add_argument("--as-of")
    p_compile.set_defaults(func=_cmd_compile)

    p_snap = sub.add_parser("snapshot-rebuild", help="Rebuild the daily snapshot for an optional as-of date.")
    p_snap.add_argument("--user")
    p_snap.add_argument("--task-id")
    p_snap.add_argument("--branch")
    p_snap.add_argument("--as-of")
    p_snap.set_defaults(func=_cmd_snapshot_rebuild)

    p_weekly = sub.add_parser(
        "weekly-snapshot-rebuild",
        help="Rebuild the weekly snapshot. Pass --narrative to opt into the v0.10.0 LLM executive summary.",
    )
    p_weekly.add_argument("--user")
    p_weekly.add_argument("--task-id")
    p_weekly.add_argument("--branch")
    p_weekly.add_argument("--as-of")
    p_weekly.add_argument(
        "--narrative",
        action="store_true",
        help="Opt into the LLM-generated executive summary (additive only; honours unified runner timeout/budget).",
    )
    p_weekly.set_defaults(func=_cmd_weekly_snapshot_rebuild)

    p_monthly = sub.add_parser(
        "monthly-snapshot-rebuild",
        help="Rebuild the monthly snapshot. Pass --narrative to opt into the v0.10.0 LLM executive summary.",
    )
    p_monthly.add_argument("--user")
    p_monthly.add_argument("--task-id")
    p_monthly.add_argument("--branch")
    p_monthly.add_argument("--as-of")
    p_monthly.add_argument(
        "--narrative",
        action="store_true",
        help="Opt into the LLM-generated executive summary (additive only; honours unified runner timeout/budget).",
    )
    p_monthly.set_defaults(func=_cmd_monthly_snapshot_rebuild)

    p_digest = sub.add_parser("runtime-digest", help="Read the cached runtime_digest view.")
    p_digest.add_argument("--user")
    p_digest.add_argument("--task-id")
    p_digest.add_argument("--branch")
    p_digest.add_argument("--max-chars", type=int)
    p_digest.set_defaults(func=_cmd_runtime_digest)

    p_retrieve = sub.add_parser("retrieve-context", help="Run deterministic context retrieval from CLI.")
    _add_retrieval_filters(p_retrieve)
    p_retrieve.set_defaults(func=_cmd_retrieve_context)

    p_important = sub.add_parser("important-memories", help="Run budget-first important-memory retrieval from CLI.")
    _add_retrieval_filters(p_important)
    p_important.set_defaults(func=_cmd_important_memories)

    p_trace = sub.add_parser("trace-lineage", help="Trace lineage for a record.")
    p_trace.add_argument("record_id")
    p_trace.add_argument("--max-depth", type=int)
    p_trace.set_defaults(func=_cmd_trace_lineage)

    p_conflicts = sub.add_parser("list-conflicts", help="List open record conflicts.")
    p_conflicts.add_argument("--include-resolved", action="store_true")
    p_conflicts.set_defaults(func=_cmd_list_conflicts)

    p_compare = sub.add_parser("compare-snapshots", help="Compare two compiled snapshot files.")
    p_compare.add_argument("--path", required=True)
    p_compare.add_argument("--other-path", required=True)
    p_compare.set_defaults(func=_cmd_compare_snapshots)

    p_link = sub.add_parser("link-artifact", help="Attach artifact/facet metadata to an existing record.")
    p_link.add_argument("record_id")
    _add_artifact_args(p_link)
    p_link.set_defaults(func=_cmd_link_artifact)

    p_enhance = sub.add_parser("enhance", help="Run an opt-in LLM enhancement operation from CLI.")
    p_enhance.add_argument("--operation", required=True)
    p_enhance.add_argument("--content")
    p_enhance.add_argument("--content-file")
    p_enhance.add_argument("--content-markdown")
    p_enhance.add_argument("--content-markdown-file")
    p_enhance.add_argument("--source-record-id")
    p_enhance.add_argument("--allowed-kinds", type=_csv_arg)
    p_enhance.add_argument("--allowed-scopes", type=_csv_arg)
    p_enhance.add_argument("--allowed-tags", type=_csv_arg)
    p_enhance.add_argument("--candidates", help="JSON array")
    p_enhance.add_argument("--records", help="JSON array")
    p_enhance.add_argument("--record-a", help="JSON object")
    p_enhance.add_argument("--record-b", help="JSON object")
    p_enhance.add_argument("--task-id")
    p_enhance.add_argument("--branch")
    p_enhance.add_argument("--max-tokens", type=int)
    p_enhance.add_argument("--max-chars-per-record", type=int)
    p_enhance.add_argument("--thinking", action="store_true")
    p_enhance.add_argument("--reasoning-effort", choices=["low", "medium", "high"])
    p_enhance.set_defaults(func=_cmd_enhance)

    p_rebuild_kd = sub.add_parser(
        "rebuild-key-docs",
        help="Rebuild per-user activeContext and shared teamContext/progress/techContext/systemPatterns from raw records.",
    )
    p_rebuild_kd.add_argument(
        "--target",
        action="append",
        choices=sorted(KEY_DOCUMENT_KEYS),
        help="Subset of key documents to rebuild (repeatable). Omit to rebuild all four.",
    )
    p_rebuild_kd.add_argument(
        "--renderer",
        default="auto",
        choices=["auto", "deterministic", "llm", "embedding"],
        help="Renderer tier. 'auto' walks key_documents.prefer_order; explicit tiers fail-fast on their tier.",
    )
    p_rebuild_kd.add_argument("--user", help="User for per-user activeContext rebuild.")
    p_rebuild_kd.set_defaults(func=_cmd_rebuild_key_docs)

    p_kd_jobs = sub.add_parser(
        "key-doc-jobs",
        help="Inspect or drain queued asynchronous key-document rebuild jobs.",
    )
    p_kd_jobs.add_argument("--drain", action="store_true", help="Drain queued jobs now.")
    p_kd_jobs.add_argument("--max-jobs", type=int, default=1, help="Maximum jobs to drain in this run.")
    p_kd_jobs.set_defaults(func=_cmd_key_doc_jobs)

    p_encoding_audit = sub.add_parser("encoding-audit", help="Audit Memory MCP text files for invalid UTF-8 and mojibake signals.")
    p_encoding_audit.add_argument("--path", action="append", help="Repository-relative file path; repeatable. Omit for all memory text files.")
    p_encoding_audit.set_defaults(func=_cmd_encoding_audit)

    p_encoding_repair = sub.add_parser("encoding-repair", help="Repair one text file using an explicit codec strategy (dry-run by default).")
    p_encoding_repair.add_argument("--path", required=True)
    p_encoding_repair.add_argument("--mode", required=True, choices=["utf8_bom", "latin1_mojibake", "cp1252_mojibake", "gb18030"])
    p_encoding_repair.add_argument("--if-match", help="Optional source SHA returned by a prior dry run.")
    p_encoding_repair.add_argument("--apply", action="store_true")
    p_encoding_repair.set_defaults(func=_cmd_encoding_repair)

    sub.add_parser("recover-transactions", help="Resume prepared compaction transactions after an unclean shutdown.").set_defaults(func=_cmd_recover_transactions)

    p_reflection_jobs = sub.add_parser("reflection-jobs", help="Inspect or drain durable project-reflection jobs.")
    p_reflection_jobs.add_argument("--drain", action="store_true")
    p_reflection_jobs.add_argument("--max-jobs", type=int, default=1)
    p_reflection_jobs.set_defaults(func=_cmd_reflection_jobs)

    p_reflection_backfill = sub.add_parser("reflection-backfill", help="Queue historical task evidence for background reflection.")
    p_reflection_backfill.add_argument("--limit", type=int, default=100)
    p_reflection_backfill.add_argument("--force", action="store_true")
    p_reflection_backfill.set_defaults(func=_cmd_reflection_backfill)

    sub.add_parser("reflection-curate", help="Promote repeated high-confidence reflection proposals.").set_defaults(func=_cmd_reflection_curate)
    sub.add_parser("worker-once", help="Run one isolated background-worker cycle for CI or diagnostics.").set_defaults(func=_cmd_worker_once)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    handler: Callable[[argparse.Namespace], dict[str, Any]] = args.func
    try:
        result = handler(args)
    except Exception as exc:  # noqa: BLE001 - CLI surface needs structured error
        result = {"ok": False, "error": "cli_exception", "message": f"{type(exc).__name__}: {exc}"}
    if not isinstance(result, dict):
        result = {"ok": False, "error": "invalid_result", "message": "handler did not return a dict"}
    return _emit(result, pretty=args.pretty)


if __name__ == "__main__":  # pragma: no cover - manual entry point
    raise SystemExit(main())
