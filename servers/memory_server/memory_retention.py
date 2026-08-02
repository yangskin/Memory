from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .memory_config import MemoryConfig
from .memory_events import append_event
from .memory_locks import LockTimeoutError, file_lock
from .memory_result import error_result, ok_result


@dataclass(frozen=True)
class RetentionSettings:
    enabled: bool = True
    keep_active_context_archives_per_user: int = 2
    keep_manual_edits: int = 2
    keep_compiled_runtime_per_dir: int = 5
    keep_snapshots: int = 20
    dest_dir: str = ".ai-memory/retention-archive"
    max_moves_per_run: int = 50
    compact_record_packs: bool = True
    record_pack_archive_after_days: int = 90
    record_pack_archive_max_chars: int = 1_048_576
    record_pack_max_files_per_run: int = 50


def _settings(config: MemoryConfig) -> RetentionSettings:
    raw_auto = getattr(config, "mcp_auto_maintenance", None) or {}
    raw = raw_auto.get("retention") if isinstance(raw_auto, dict) else None
    if not isinstance(raw, dict):
        raw = {}

    def _int(name: str, default: int) -> int:
        try:
            return max(0, int(raw.get(name, default)))
        except (TypeError, ValueError):
            return default

    dest = str(raw.get("dest_dir") or ".ai-memory/retention-archive").replace("\\", "/").strip("/")
    if not dest:
        dest = ".ai-memory/retention-archive"
    return RetentionSettings(
        enabled=bool(raw.get("enabled", True)),
        keep_active_context_archives_per_user=_int("keep_active_context_archives_per_user", 2),
        keep_manual_edits=_int("keep_manual_edits", 2),
        keep_compiled_runtime_per_dir=_int("keep_compiled_runtime_per_dir", 5),
        keep_snapshots=_int("keep_snapshots", 20),
        dest_dir=dest,
        max_moves_per_run=_int("max_moves_per_run", 50),
        compact_record_packs=bool(raw.get("compact_record_packs", True)),
        record_pack_archive_after_days=_int("record_pack_archive_after_days", config.record_packing_archive_after_days),
        record_pack_archive_max_chars=_int("record_pack_archive_max_chars", config.record_packing_archive_pack_max_chars),
        record_pack_max_files_per_run=_int("record_pack_max_files_per_run", 50),
    )


def _files_newest_first(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    files = [p for p in root.rglob("*") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def _overflow(files: Iterable[Path], keep: int) -> list[Path]:
    ordered = list(files)
    if keep <= 0:
        return ordered
    return ordered[keep:]


def _collect_candidates(config: MemoryConfig, settings: RetentionSettings) -> list[Path]:
    root = config.repo_root
    candidates: list[Path] = []

    active_root = root / "memory-bank" / "archive" / "activeContext"
    if active_root.is_dir():
        for user_dir in sorted(p for p in active_root.iterdir() if p.is_dir()):
            candidates.extend(
                _overflow(
                    _files_newest_first(user_dir),
                    settings.keep_active_context_archives_per_user,
                )
            )

    manual_edits = root / "memory-bank" / "archive" / "manual-edits"
    candidates.extend(_overflow(_files_newest_first(manual_edits), settings.keep_manual_edits))

    runtime_root = root / "memory-bank" / "compiled" / "runtime"
    if runtime_root.is_dir():
        dirs = [runtime_root] + [p for p in runtime_root.rglob("*") if p.is_dir()]
        for directory in dirs:
            direct_files = [p for p in directory.iterdir() if p.is_file()]
            direct_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            candidates.extend(
                _overflow(direct_files, settings.keep_compiled_runtime_per_dir)
            )

    snapshots_root = root / "memory-bank" / "compiled" / "snapshots"
    candidates.extend(_overflow(_files_newest_first(snapshots_root), settings.keep_snapshots))

    unique: dict[str, Path] = {}
    for path in candidates:
        try:
            rel = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
        unique[rel] = path
    return list(unique.values())[: settings.max_moves_per_run]


def _move_to_retention(config: MemoryConfig, source: Path, settings: RetentionSettings) -> dict[str, Any]:
    try:
        rel = source.resolve().relative_to(config.repo_root.resolve()).as_posix()
    except ValueError:
        return error_result("path_not_allowed", "source is outside repo", path=str(source))
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dest = config.repo_root / settings.dest_dir / stamp / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    counter = 0
    final_dest = dest
    while final_dest.exists():
        counter += 1
        final_dest = dest.with_name(f"{dest.stem}-{counter}{dest.suffix}")
    try:
        with file_lock(config.repo_root, source):
            os.replace(source, final_dest)
    except LockTimeoutError as exc:
        return error_result("lock_timeout", str(exc), path=rel)
    except OSError as exc:
        return error_result("move_failed", str(exc), path=rel)
    return {
        "ok": True,
        "path": rel,
        "moved_to": final_dest.relative_to(config.repo_root).as_posix(),
        "bytes": final_dest.stat().st_size,
    }


def apply_retention(config: MemoryConfig) -> dict[str, Any]:
    settings = _settings(config)
    if not settings.enabled:
        return ok_result("retention disabled", skipped=True, actions=[])

    candidates = _collect_candidates(config, settings)
    actions = [_move_to_retention(config, path, settings) for path in candidates]
    record_pack_compaction: dict[str, Any] | None = None
    if settings.compact_record_packs:
        try:
            from .memory_record_packing import compact_old_record_packs

            record_pack_compaction = compact_old_record_packs(
                config,
                older_than_days=settings.record_pack_archive_after_days,
                max_pack_chars=settings.record_pack_archive_max_chars,
                max_files=settings.record_pack_max_files_per_run,
                dry_run=False,
            )
        except Exception as exc:  # pragma: no cover - retention must remain best-effort
            record_pack_compaction = error_result("record_pack_compaction_failed", str(exc))
    try:
        append_event(
            config,
            event_type="retention_applied",
            payload={
                "moved": [
                    {
                        "path": action.get("path"),
                        "moved_to": action.get("moved_to"),
                        "bytes": action.get("bytes"),
                    }
                    for action in actions
                    if action.get("ok")
                ],
                "errors": [
                    {
                        "path": action.get("path"),
                        "error": action.get("error"),
                        "message": action.get("message"),
                    }
                    for action in actions
                    if not action.get("ok")
                ],
                "record_pack_compaction": record_pack_compaction,
            },
            status="ok" if all(a.get("ok", True) for a in actions) else "error",
        )
    except Exception:
        pass
    return ok_result(
        "retention applied",
        actions=actions,
        moved=sum(1 for action in actions if action.get("ok")),
        errors=[action for action in actions if not action.get("ok")],
        record_pack_compaction=record_pack_compaction,
    )
