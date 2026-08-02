"""Shared low-level record I/O helpers.

This module exists so that governance, lineage, maintenance, compiler and
retrieval no longer each carry their own copies of ``_iter_records``,
``_find_record``, ``_refresh_index_if_exists`` and ``_write_record_to_target``.

Only deterministic file-system primitives live here. Callers continue to wrap
these helpers with their own domain-specific logic (e.g. ``CompilableRecord``
projections in ``memory_compiler``).
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .memory_config import MemoryConfig
from .memory_locks import file_lock
from .memory_paths import PathManager, PathSecurityError
from .memory_frontmatter import parse_record_pack_entries, replace_record_pack_entry
from .memory_records import parse_record_markdown, render_record_markdown, target_path_for_record
from .memory_result import error_result, ok_result


# errno values that mean "the underlying volume can no longer accept writes".
# We surface these as a structured ``disk_full`` error so callers can
# distinguish operational disk pressure from generic I/O failure (which
# might be a permission issue, a corrupted FS, antivirus interference,
# etc.). ``EDQUOT`` (122 on Linux) is missing from ``errno`` on some
# platforms — fall back to ``None`` and skip the comparison.
_DISK_FULL_ERRNOS = {
    getattr(__import__("errno"), name, None)
    for name in ("ENOSPC", "EDQUOT", "EFBIG")
} - {None}


class DiskFullError(OSError):
    """Raised by ``_atomic_write_text`` when the volume is out of space.

    Subclass of ``OSError`` so existing ``except OSError`` catch sites
    keep working; new code can branch on this specific type to surface
    a ``disk_full`` error code.
    """


def _is_disk_full(exc: OSError) -> bool:
    return exc.errno is not None and exc.errno in _DISK_FULL_ERRNOS


def safe_read_text(
    path: Path,
    *,
    encoding: str = "utf-8",
    errors: str = "strict",
    max_attempts: int = 20,
    delay_seconds: float = 0.005,
) -> str:
    """Read ``path`` tolerating brief writer-replace contention.

    On Windows, when a writer is in the middle of ``os.replace`` of an
    atomic-write tmp file onto ``path``, a concurrent reader can observe:

    * ``PermissionError`` -- the writer holds the destination handle without
      ``FILE_SHARE_DELETE`` for the duration of the replace syscall.
    * ``FileNotFoundError`` -- the source tmp was renamed but, in rare
      cases (junctions, antivirus filters), the destination momentarily
      lacks a directory entry.

    Both windows are tiny (sub-millisecond on a healthy filesystem). This
    helper bounded-retries the read so callers never spuriously see
    "file vanished" / "access denied" in normal multi-agent operation.
    Other ``OSError`` subclasses propagate unchanged.
    """
    last_exc: OSError | None = None
    for _ in range(max(1, max_attempts)):
        try:
            return path.read_text(encoding=encoding, errors=errors)
        except (PermissionError, FileNotFoundError) as exc:
            last_exc = exc
            time.sleep(delay_seconds)
    assert last_exc is not None
    raise last_exc


def _fsync_parent_dir(target: Path) -> None:
    """Fsync the directory containing ``target`` so the rename is durable.

    Best-effort: silently swallow errors. POSIX-only — ``os.open`` on a
    directory is not portable on Windows, where directory entries are
    flushed implicitly by ``os.replace``.
    """
    if os.name == "nt":
        return
    try:
        dir_fd = os.open(str(target.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
    finally:
        try:
            os.close(dir_fd)
        except OSError:
            pass


def _atomic_write_text(target: Path, content: str, *, fsync_strict: bool = False) -> None:
    """Atomically write ``content`` to ``target`` (UTF-8).

    Hardening (P1-F/G + v0.5.5):
    - Tmp file is created in the *same directory* as ``target`` so
      ``os.replace`` stays on one volume.
    - Tmp file is created with ``O_CREAT | O_EXCL`` to refuse to clobber a
      stale tmp from another writer in the same tick.
    - The tmp file's contents are flushed and ``fsync``-ed before rename so a
      crash between rename and writeback cannot leave a half-empty file.
    - After the rename, the parent directory is fsync-ed (POSIX) so the
      rename itself is durable across crash.
    - On any failure the tmp file is removed.

    Args:
        target: final destination path.
        content: UTF-8 text to write.
        fsync_strict: when True, ``fsync`` failures (file or parent dir on
            POSIX) propagate as ``OSError`` so the caller can surface the
            error. When False (default), ``fsync`` is best-effort and only
            the rename / write itself can fail.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.parent / f".{target.name}.{uuid.uuid4().hex[:8]}.tmp"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY  # Windows: avoid CR/LF translation
    fd = os.open(str(tmp_path), flags, 0o644)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content.encode("utf-8"))
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                if fsync_strict:
                    raise
                # Otherwise: some filesystems / mounts do not support fsync;
                # durability is best-effort, but we must still complete the
                # rename.
        # On Windows, ``os.replace`` may briefly fail with PermissionError
        # if another process has the target file open for read with the
        # default share mode (no FILE_SHARE_DELETE). This is common when
        # a reader (e.g. ``memory_search``) scans files concurrently with
        # writers protected only by the per-target ``file_lock`` (readers
        # are intentionally not behind that lock). The window is tiny —
        # the reader closes its handle as soon as ``read_text`` finishes
        # — so a small bounded retry recovers transparently. Without this
        # retry, real multi-agent workloads on Windows would surface
        # spurious ``write_failed`` errors under load.
        replace_attempts = 0
        max_replace_attempts = 20
        while True:
            try:
                os.replace(tmp_path, target)
                break
            except PermissionError:
                replace_attempts += 1
                if replace_attempts >= max_replace_attempts:
                    raise
                time.sleep(0.01)
        if fsync_strict:
            # Strict mode: parent-dir fsync errors propagate so the caller
            # learns the rename may not be durable across crash.
            if os.name != "nt":
                dir_fd = os.open(str(target.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        else:
            _fsync_parent_dir(target)
    except OSError as exc:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        # Promote disk-full / quota-exhausted / file-too-large to a
        # dedicated exception type so callers can return a structured
        # ``disk_full`` error code instead of a generic write_failed.
        # Operators can then alert / failover on the specific signal.
        if isinstance(exc, OSError) and _is_disk_full(exc):
            raise DiskFullError(exc.errno, str(exc)) from exc
        raise


@dataclass(frozen=True)
class ParsedRecord:
    """A successfully parsed Markdown record on disk."""

    abs_path: Path
    rel_path: str
    metadata: dict[str, Any]
    body: str
    packed: bool = False


def iter_record_files(config: MemoryConfig) -> list[tuple[Path, str]]:
    """Return all ``memory-bank/`` markdown files except compiled views."""
    manager = PathManager(config)
    return [
        (abs_path, rel_path)
        for abs_path, rel_path in manager.iter_files(
            scopes=["memory-bank"], include_paths=["memory-bank/**/*.md"]
        )
        if not rel_path.startswith("memory-bank/compiled/")
    ]


def iter_parsed_records(
    config: MemoryConfig,
    *,
    include_rel_paths: set[str] | None = None,
) -> tuple[list[ParsedRecord], dict[str, int]]:
    """Iterate every record file and parse Markdown + Front Matter.

    Returns a list of ``ParsedRecord`` and a stats dict with keys
    ``scanned_files``, ``skipped_non_records`` and ``skipped_read_errors``.
    Files without ``id`` or ``record_kind`` metadata are skipped.
    """
    records: list[ParsedRecord] = []
    stats = {
        "scanned_files": 0,
        "skipped_non_records": 0,
        "skipped_read_errors": 0,
    }
    if include_rel_paths is not None:
        manager = PathManager(config)
        candidate_files: list[tuple[Path, str]] = []
        for rel_path in sorted(include_rel_paths):
            if not rel_path.startswith("memory-bank/") or rel_path.startswith("memory-bank/compiled/"):
                continue
            try:
                abs_path = manager.resolve(rel_path, must_exist=True, must_be_file=True)
                candidate_files.append((abs_path, manager.to_repo_relative(abs_path)))
            except (OSError, PathSecurityError):
                stats["skipped_read_errors"] += 1
        files = candidate_files
    else:
        files = iter_record_files(config)

    for abs_path, rel_path in files:
        stats["scanned_files"] += 1
        try:
            text = safe_read_text(abs_path, errors="strict")
            packed_file = "<!-- memory-record-pack-entry " in text
            parsed_entries = parse_record_pack_entries(text)
        except ValueError:
            stats["skipped_non_records"] += 1
            continue
        except (OSError, UnicodeError):
            stats["skipped_read_errors"] += 1
            continue
        added = 0
        for metadata, body in parsed_entries:
            if not metadata.get("id") or not metadata.get("record_kind"):
                continue
            records.append(
                ParsedRecord(
                    abs_path=abs_path,
                    rel_path=rel_path,
                    metadata=metadata,
                    body=body,
                    packed=packed_file,
                )
            )
            added += 1
        if added == 0:
            stats["skipped_non_records"] += 1
    return records, stats


def find_record_by_id(
    config: MemoryConfig, record_id: str
) -> tuple[Path, str, dict[str, Any], str] | dict[str, Any]:
    """Find a record by id. Returns the 4-tuple, or an ``error_result`` dict."""
    try:
        records, _stats = iter_parsed_records(config)
    except PathSecurityError as exc:
        return error_result("path_not_allowed", str(exc))
    except FileNotFoundError as exc:
        return error_result("not_found", str(exc))
    for record in records:
        if str(record.metadata.get("id")) == record_id:
            return record.abs_path, record.rel_path, record.metadata, record.body
    return error_result("not_found", f"record not found: {record_id}", record_id=record_id)


def refresh_index_if_exists(config: MemoryConfig, path: str) -> None:
    """Update the SQLite FTS index for ``path`` if the index already exists.

    Best-effort: any failure is swallowed because the index is a derived view
    and callers must not fail their primary write because of a stale index.
    """
    if not (config.repo_root / ".ai-memory/search.db").exists():
        return
    try:
        from .memory_record_index import memory_update_index

        from .memory_record_index import mark_index_dirty

        result = memory_update_index(config, paths=[path])
        if not result.get("ok"):
            mark_index_dirty(config, reason=str(result.get("error") or "index update failed"), paths=[path])
    except Exception as exc:
        try:
            from .memory_record_index import mark_index_dirty

            mark_index_dirty(config, reason=str(exc), paths=[path])
        except Exception:
            pass


def write_same_record(
    config: MemoryConfig,
    *,
    abs_path: Path,
    rel_path: str,
    metadata: dict[str, Any],
    body: str,
) -> dict[str, Any]:
    """Re-render and atomically replace an existing record at the same path."""
    record_id = str(metadata.get("id", ""))
    content = render_record_markdown(metadata, body)
    try:
        with file_lock(config.repo_root, abs_path):
            try:
                current = safe_read_text(abs_path, errors="replace")
                if "<!-- memory-record-pack-entry " in current:
                    content_to_write = replace_record_pack_entry(current, record_id, content)
                else:
                    content_to_write = content
            except ValueError as exc:
                return error_result("record_pack_update_failed", str(exc))
            _atomic_write_text(abs_path, content_to_write, fsync_strict=config.mcp_fsync_strict)
    except OSError as exc:
        return error_result("write_failed", f"failed to update record: {exc}")

    refresh_index_if_exists(config, rel_path)
    return ok_result(
        "record updated",
        id=metadata.get("id"),
        path=rel_path,
        record_kind=metadata.get("record_kind"),
        scope=metadata.get("scope"),
        status=metadata.get("status"),
    )


def write_record_to_target(
    config: MemoryConfig,
    *,
    old_abs_path: Path,
    old_rel_path: str,
    metadata: dict[str, Any],
    body: str,
) -> dict[str, Any]:
    """Atomically write the record to its canonical target path.

    Used by status transitions (validate/publish/archive). The new file is
    staged in a sibling temp file then atomically renamed; only after that
    succeeds is the previous file removed. This avoids "two copies" or
    "zero copies" outcomes on partial failure.

    Hardening (P1-F): if the resolved target path differs from the source
    path AND a file already exists at the target, we refuse to overwrite —
    that situation indicates either a record-id collision or a stale
    governance file from a partial earlier write, both of which deserve a
    surfaced error rather than silent clobber.
    """
    record_id = str(metadata.get("id", ""))
    rel_path = target_path_for_record(
        record_id,
        str(metadata.get("record_kind", "")),
        str(metadata.get("scope", "")),
        str(metadata.get("status", "")),
        str(metadata.get("author", "")),
    )
    manager = PathManager(config)
    try:
        new_abs_path = manager.resolve(rel_path, must_exist=False, must_be_file=False)
    except PathSecurityError as exc:
        return error_result("path_not_allowed", str(exc))

    same_path = old_abs_path.resolve() == new_abs_path.resolve()
    if not same_path and new_abs_path.exists():
        return error_result(
            "target_exists",
            f"refusing to overwrite existing record at target: {rel_path}",
            path=rel_path,
            previous_path=old_rel_path,
        )

    content = render_record_markdown(metadata, body)
    try:
        # Lock both old and new target paths so concurrent transitions
        # (e.g. two agents both publishing the same candidate, or one
        # archiving while another publishes) cannot interleave the
        # rename + unlink pair below.
        with file_lock(config.repo_root, new_abs_path):
            if same_path:
                _atomic_write_text(new_abs_path, content, fsync_strict=config.mcp_fsync_strict)
            else:
                with file_lock(config.repo_root, old_abs_path):
                    # Re-check target existence under the lock to close
                    # the TOCTOU window between the check above and the
                    # write below.
                    if new_abs_path.exists():
                        return error_result(
                            "target_exists",
                            f"refusing to overwrite existing record at target: {rel_path}",
                            path=rel_path,
                            previous_path=old_rel_path,
                        )
                    _atomic_write_text(new_abs_path, content, fsync_strict=config.mcp_fsync_strict)
                    try:
                        current = safe_read_text(old_abs_path, errors="replace")
                        if "<!-- memory-record-pack-entry " in current:
                            updated_pack = replace_record_pack_entry(current, record_id, None)
                            try:
                                parse_record_pack_entries(updated_pack)
                            except ValueError:
                                old_abs_path.unlink()
                            else:
                                _atomic_write_text(old_abs_path, updated_pack, fsync_strict=config.mcp_fsync_strict)
                        else:
                            old_abs_path.unlink()
                    except FileNotFoundError:
                        pass
                    except ValueError as exc:
                        return error_result("record_pack_update_failed", str(exc))
    except OSError as exc:
        return error_result("write_failed", f"failed to update record: {exc}")

    return ok_result(
        "record updated",
        id=record_id,
        path=rel_path,
        previous_path=old_rel_path,
        status=metadata.get("status"),
        scope=metadata.get("scope"),
        record_kind=metadata.get("record_kind"),
    )
