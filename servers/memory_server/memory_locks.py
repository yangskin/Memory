"""Cross-platform, cross-process file locking primitives.

This module exists so every writer in the memory server can serialize
concurrent updates to the *same* logical target without holding global
locks. It is used by:

- ``memory_writer.memory_write``    (per-target file lock)
- ``memory_record_io._atomic_write_text``    (per-record file lock)
- ``memory_compiler_cache.record_usage_stats``    (read-modify-write of
  ``.ai-memory/usage-stats.json``)
- ``memory_maintenance.memory_delete_record``    (append to
  ``.ai-memory/tombstones.jsonl``)

Design constraints
------------------
- **No third-party dependencies.** Uses only ``fcntl`` (POSIX) and
  ``msvcrt`` (Windows), both stdlib.
- **Cross-process safe.** The MCP server is launched as ``python -m
  servers.memory_server`` once per VS Code window / Codex session, so the
  same workspace can have N independent Python processes writing
  ``memory-bank/**``. A purely in-process ``threading.Lock`` is *not*
  sufficient. We acquire an OS-level exclusive lock on a sidecar
  ``*.lock`` file kept under ``.ai-memory/locks/`` so the original target
  file is never touched (which avoids confusing tools that watch the
  target).
- **Per-target granularity.** The lock path is derived from a stable
  hash of the repo-relative target so two different files never wait on
  each other.
- **Best-effort timeout.** Default 30 s; raises ``LockTimeoutError`` on
  expiry so the dispatch layer can return a structured error rather than
  hang an MCP call indefinitely.
- **Reentrant within a thread.** A counter inside the per-process map
  permits nested calls (``memory_write`` → ``backup_files`` → another
  ``memory_write``) to share the same OS lock without deadlocking
  themselves.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class LockTimeoutError(RuntimeError):
    """Raised when the cross-process lock cannot be acquired in time."""


_DEFAULT_TIMEOUT_SECONDS = 30.0
_POLL_INTERVAL_SECONDS = 0.05
DEFAULT_STALE_LOCK_SECONDS = 24 * 60 * 60

# Per-process, per-lock-path reentrance counters. Keyed by absolute
# string path of the sidecar lock file. Each entry holds:
#   {"thread_id": int, "count": int, "fd": int}
# Only the thread that originally acquired the OS lock may increment the
# counter; other threads in the same process must wait on the OS lock.
_reentrance_lock = threading.Lock()
_reentrance_state: dict[str, dict] = {}


def _lock_path_for(repo_root: Path, target: Path) -> Path:
    """Return the sidecar lock-file path for ``target``.

    Uses a SHA-1 hash of the repo-relative path so the file name is
    bounded in length (Windows MAX_PATH) and never contains separators.
    """
    try:
        rel = target.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        rel = str(target.resolve())
    digest = hashlib.sha1(rel.encode("utf-8")).hexdigest()
    locks_dir = repo_root / ".ai-memory" / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    return locks_dir / f"{digest}.lock"


def _target_rel(repo_root: Path, target: Path) -> str:
    try:
        return target.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(target.resolve())


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:
        return False
    return True


def _write_lock_metadata(fd: int, repo_root: Path, target: Path) -> None:
    payload = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "target": _target_rel(repo_root, target),
        "acquired_at": time.time(),
        "heartbeat_at": time.time(),
    }
    data = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, data)
        os.fsync(fd)
    except OSError:
        # Metadata is only for diagnostics / cleanup. The OS lock itself is
        # the authority for mutual exclusion.
        pass


def read_lock_metadata(lock_path: Path) -> dict | None:
    try:
        text = lock_path.read_text(encoding="utf-8", errors="replace").strip()
    except PermissionError:
        return {"_unreadable": True}
    except OSError:
        return None
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def is_lock_sidecar_stale(
    lock_path: Path,
    *,
    now: float | None = None,
    stale_seconds: float = DEFAULT_STALE_LOCK_SECONDS,
) -> bool:
    """Return True only when a lock sidecar is safe to remove.

    Sidecar files are not the lock; OS locks are released with the fd. Cleanup
    is therefore cosmetic, but deleting the sidecar while another process is
    using it can make diagnostics misleading. Treat live same-host PIDs as
    active, and keep legacy metadata-less sidecars for a much longer grace.
    """

    current = time.time() if now is None else now
    try:
        age = current - lock_path.stat().st_mtime
    except OSError:
        return False
    if age < stale_seconds:
        return False

    meta = read_lock_metadata(lock_path)
    if not meta:
        return True
    if meta.get("_unreadable"):
        return False
    host = str(meta.get("host") or "")
    try:
        pid = int(meta.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if host == socket.gethostname() and _process_alive(pid):
        return False
    return True


def _acquire_os_lock(fd: int, timeout: float) -> None:
    """Block until an exclusive OS-level lock is held on ``fd``.

    Raises ``LockTimeoutError`` after ``timeout`` seconds.
    """
    deadline = time.monotonic() + timeout
    if sys.platform == "win32":
        import msvcrt

        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(
                        f"could not acquire file lock within {timeout:.1f}s"
                    )
                time.sleep(_POLL_INTERVAL_SECONDS)
    else:
        import fcntl

        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(
                        f"could not acquire file lock within {timeout:.1f}s"
                    )
                time.sleep(_POLL_INTERVAL_SECONDS)


def _release_os_lock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


@contextmanager
def file_lock(
    repo_root: Path,
    target: Path,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Acquire a cross-process exclusive lock for the given ``target``.

    Usage::

        with file_lock(config.repo_root, target_path):
            ...  # safe critical section

    The lock is keyed on the *target's* repo-relative path, NOT on
    ``target`` itself, so two different absolute paths that resolve to
    the same logical file (rare on Windows with case-insensitive FS,
    common with symlinks on POSIX) still serialize correctly.
    """
    lock_path = _lock_path_for(repo_root, target)
    lock_key = str(lock_path)
    thread_id = threading.get_ident()

    # Reentrance: same thread already owns this lock?
    with _reentrance_lock:
        state = _reentrance_state.get(lock_key)
        if state is not None and state["thread_id"] == thread_id:
            state["count"] += 1
            owns_os_lock = False
        else:
            owns_os_lock = True

    if not owns_os_lock:
        try:
            yield
        finally:
            with _reentrance_lock:
                state = _reentrance_state.get(lock_key)
                if state is not None and state["thread_id"] == thread_id:
                    state["count"] -= 1
                    if state["count"] <= 0:
                        # Should not normally happen here (release is by
                        # the original holder), but be defensive.
                        pass
        return

    # Open / create the sidecar lock file.
    fd = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT,
        0o644,
    )
    try:
        _acquire_os_lock(fd, timeout)
    except BaseException:
        # Catch BaseException (not just OSError/LockTimeoutError) so a
        # KeyboardInterrupt / SystemExit / asynchronous exception raised
        # while we're inside the polling sleep cannot leak ``fd``. The
        # acquire helper itself sleeps in 50 ms ticks; Ctrl+C tends to
        # land inside one of those sleeps, where we have an open fd but
        # have not yet recorded it in ``_reentrance_state``.
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    _write_lock_metadata(fd, repo_root, target)

    with _reentrance_lock:
        _reentrance_state[lock_key] = {
            "thread_id": thread_id,
            "count": 1,
            "fd": fd,
        }
    try:
        yield
    finally:
        # Release reentrance + OS lock + fd unconditionally so an
        # exception inside the critical section cannot leak the file
        # descriptor (POSIX `flock` is bound to the fd, not the inode,
        # so leaking fds across many failed writes would eventually
        # exhaust ``ulimit -n``).
        try:
            with _reentrance_lock:
                state = _reentrance_state.get(lock_key)
                if state is not None:
                    state["count"] -= 1
                    if state["count"] <= 0:
                        _reentrance_state.pop(lock_key, None)
                        _release_os_lock(fd)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass


__all__ = [
    "DEFAULT_STALE_LOCK_SECONDS",
    "file_lock",
    "is_lock_sidecar_stale",
    "LockTimeoutError",
    "read_lock_metadata",
]
