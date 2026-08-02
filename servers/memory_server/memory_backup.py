from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .memory_config import MemoryConfig
from .memory_events import append_event
from .memory_paths import PathManager, PathSecurityError
from .memory_result import error_result, ok_result

logger = logging.getLogger(__name__)

# ── Single-file append backup format ────────────────────────────────────
# Each record is appended to a flat file (.ai-memory/backups/backup-NNN.md).
# When the file exceeds backup_max_file_bytes a new file is created.
#
# Record format:
#   <<<BACKUP|timestamp|path|reason>>>
#   (original file content)
#   <<<END_BACKUP>>>

_RECORD_START_PREFIX = "<<<BACKUP|"
_RECORD_END = "<<<END_BACKUP>>>"
_BACKUP_FILE_PREFIX = "backup-"
_BACKUP_FILE_EXT = ".md"


def _list_backup_files(backups_dir: Path) -> list[Path]:
    """Return backup files sorted by name (backup-001.md, backup-002.md, …)."""
    if not backups_dir.exists():
        return []
    files: list[Path] = []
    try:
        for item in sorted(backups_dir.iterdir()):
            if item.is_file() and item.name.startswith(_BACKUP_FILE_PREFIX) and item.name.endswith(_BACKUP_FILE_EXT):
                files.append(item)
    except OSError:
        pass
    return files


def _current_backup_file(config: MemoryConfig) -> Path:
    """Return the current backup file to append to, creating if needed.

    If the latest backup file is >= backup_max_file_bytes, a new file is created.
    """
    config.backups_dir.mkdir(parents=True, exist_ok=True)
    files = _list_backup_files(config.backups_dir)
    max_bytes = config.backup_max_file_bytes

    if files:
        latest = files[-1]
        try:
            size = latest.stat().st_size
        except OSError:
            size = 0
        if max_bytes is None or size < max_bytes:
            return latest

    # Create a new backup file with incremented number
    next_num = len(files) + 1
    new_name = f"{_BACKUP_FILE_PREFIX}{next_num:03d}{_BACKUP_FILE_EXT}"
    new_path = config.backups_dir / new_name
    new_path.touch()
    return new_path


def _rotate_backups(config: MemoryConfig) -> dict | None:
    """Remove oldest backup files to stay within configured limits.

    Returns a summary dict if any files were removed, else None.
    """
    max_bytes = config.backup_max_total_bytes
    max_files = config.backup_max_batches  # reuse config field as max backup files
    if max_bytes is None and max_files is None:
        return None

    files = _list_backup_files(config.backups_dir)
    removed: list[str] = []

    # Enforce max files (keep newest)
    if max_files is not None:
        while len(files) > max_files:
            oldest = files.pop(0)
            try:
                rel = oldest.relative_to(config.repo_root).as_posix()
            except ValueError:
                rel = str(oldest)
            try:
                oldest.unlink()
                removed.append(rel)
                logger.info("Backup rotation: removed %s (max_files=%d)", rel, max_files)
            except OSError as exc:
                logger.warning("Backup rotation: failed to remove %s: %s", rel, exc)

    # Enforce max total bytes
    if max_bytes is not None:
        total = sum(f.stat().st_size for f in files if f.exists())
        while total > max_bytes and files:
            oldest = files.pop(0)
            try:
                rel = oldest.relative_to(config.repo_root).as_posix()
            except ValueError:
                rel = str(oldest)
            try:
                fsize = oldest.stat().st_size
                oldest.unlink()
                removed.append(rel)
                total -= fsize
                logger.info("Backup rotation: removed %s (total %d > max %d)", rel, total + fsize, max_bytes)
            except OSError as exc:
                logger.warning("Backup rotation: failed to remove %s: %s", rel, exc)

    if removed:
        return {"removed_count": len(removed), "removed": removed}
    return None


def _append_record(backup_file: Path, source_rel: str, content: str, reason: str | None) -> None:
    """Append a single backup record to the backup file."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    header = f"{_RECORD_START_PREFIX}{ts}|{source_rel}|{reason or ''}>>>"
    with open(backup_file, "a", encoding="utf-8") as f:
        f.write(header + "\n")
        f.write(content)
        if content and not content.endswith("\n"):
            f.write("\n")
        f.write(_RECORD_END + "\n")


def backup_files(
    config: MemoryConfig,
    paths: list[str],
    *,
    reason: str | None = None,
    tag: str | None = None,
    event_type: str = "memory_backup",
    write_event: bool = True,
) -> dict:
    if not isinstance(paths, list) or not paths:
        return error_result("invalid_input", "paths must not be empty")

    manager = PathManager(config)
    batch_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]

    # Phase 1: validate all paths and read content before writing anything
    resolved_entries: list[tuple[str, str]] = []  # (source_rel, content)
    for raw_path in paths:
        try:
            source = manager.resolve(raw_path, must_exist=True, must_be_file=True)
            source_rel = manager.to_repo_relative(source)
            content = source.read_text(encoding="utf-8", errors="replace")
            resolved_entries.append((source_rel, content))
        except PathSecurityError as exc:
            return error_result("path_not_allowed", str(exc))
        except FileNotFoundError as exc:
            return error_result("not_found", str(exc))
        except IsADirectoryError as exc:
            return error_result("invalid_path", str(exc))

    # Phase 2: append records to the current backup file
    backup_file = _current_backup_file(config)
    copied_items: list[dict[str, str]] = []
    failed_items: list[dict[str, str]] = []

    for source_rel, content in resolved_entries:
        try:
            _append_record(backup_file, source_rel, content, reason)
            try:
                bk_rel = backup_file.relative_to(config.repo_root).as_posix()
            except ValueError:
                bk_rel = str(backup_file)
            copied_items.append(
                {
                    "source_path": source_rel,
                    "backup_file": bk_rel,
                }
            )
        except OSError as exc:
            failed_items.append(
                {
                    "source_path": source_rel,
                    "error": str(exc),
                }
            )

    if failed_items and not copied_items:
        return error_result("backup_failed", f"all {len(failed_items)} file(s) failed to backup")

    result_msg = "backup completed" if not failed_items else "backup partially completed"
    payload: dict = {
        "batch_id": batch_id,
        "reason": reason,
        "tag": tag,
        "count": len(copied_items),
        "items": copied_items,
    }
    if failed_items:
        payload["failed"] = failed_items

    if write_event:
        append_event(config, event_type=event_type, payload=payload)

    return ok_result(
        result_msg,
        batch_id=batch_id,
        backups=copied_items,
        failed=failed_items if failed_items else None,
        reason=reason,
        tag=tag,
        rotation=_rotate_backups(config),
    )
