"""v0.5.6 P0/P2 follow-up tests.

- ``KeyboardInterrupt`` during ``file_lock`` polling does not leak fds.
- ``_atomic_write_text`` raises ``DiskFullError`` on ENOSPC; ``memory_write``
  surfaces it as a structured ``disk_full`` error code, the original
  file is left untouched, and no tmp file leaks behind.
"""

from __future__ import annotations

import errno
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

MEMORY_ROOT = Path(__file__).resolve().parents[2]
if str(MEMORY_ROOT) not in sys.path:
    sys.path.insert(0, str(MEMORY_ROOT))

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_locks import file_lock
from servers.memory_server.memory_record_io import (
    DiskFullError,
    _atomic_write_text,
)
from servers.memory_server.memory_writer import memory_write


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "memory-bank").mkdir()
    (tmp_path / "memory-bank" / "notes.md").write_text("# original\n", encoding="utf-8")
    (tmp_path / ".ai-memory").mkdir()
    return tmp_path


# ── P0/P1-A: lock fd leak under async exception ──────────────────────


def test_file_lock_keyboard_interrupt_during_polling_does_not_leak_fd(
    workspace: Path,
) -> None:
    """If a ``KeyboardInterrupt`` lands while ``_acquire_os_lock`` is
    polling, the fd opened for the sidecar lock file must still be
    closed. Prior to v0.5.6 P0 the open fd was orphaned: only
    ``LockTimeoutError`` triggered the cleanup branch.

    Repro: holder thread keeps the lock for 2s; waiter thread starts
    acquiring with a 5s timeout, then we asynchronously raise
    ``KeyboardInterrupt`` *into* the waiter thread (via ``ctypes``
    PyThreadState_SetAsyncExc) while it's mid-poll. After the dust
    settles, the holder releases and we re-acquire the lock — if the
    waiter had leaked its fd while still holding the OS lock, the
    re-acquire would block (or on some platforms succeed but with two
    open fds, which we can also count).
    """
    import ctypes

    config = load_config(str(workspace))
    target = workspace / "memory-bank" / "notes.md"

    holder_started = threading.Event()
    holder_done = threading.Event()

    def holder() -> None:
        with file_lock(config.repo_root, target):
            holder_started.set()
            time.sleep(0.8)
        holder_done.set()

    waiter_exc: list[BaseException] = []

    def waiter() -> None:
        try:
            with file_lock(config.repo_root, target, timeout=5.0):
                pass  # would normally acquire after holder releases
        except BaseException as exc:  # noqa: BLE001
            waiter_exc.append(exc)

    h = threading.Thread(target=holder)
    w = threading.Thread(target=waiter)
    h.start()
    assert holder_started.wait(2.0), "holder failed to acquire"
    w.start()
    # Let waiter sit in _acquire_os_lock polling for a few ticks.
    time.sleep(0.15)

    # Inject KeyboardInterrupt into the waiter thread.
    tid = ctypes.c_ulong(w.ident)  # type: ignore[arg-type]
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        tid, ctypes.py_object(KeyboardInterrupt)
    )
    assert res == 1, f"async exc injection failed: {res}"
    w.join(timeout=3.0)
    assert not w.is_alive(), "waiter did not unwind after KeyboardInterrupt"
    h.join(timeout=3.0)
    assert holder_done.is_set()

    # Waiter should have surfaced KeyboardInterrupt.
    assert waiter_exc and isinstance(waiter_exc[0], KeyboardInterrupt), waiter_exc

    # The lock must be fully releasable now: a fresh acquire must
    # succeed quickly. If the waiter had leaked an fd that still held
    # the OS lock, this would block and time out.
    started = time.monotonic()
    with file_lock(config.repo_root, target, timeout=1.0):
        elapsed = time.monotonic() - started
    assert elapsed < 0.5, f"re-acquire took {elapsed}s — fd likely leaked"


# ── P2-4: disk_full structured error ─────────────────────────────────


class _FakeENOSPC(OSError):
    pass


def test_atomic_write_text_raises_disk_full_on_enospc(workspace: Path) -> None:
    """When the underlying rename hits ``ENOSPC`` we must raise
    ``DiskFullError`` (subclass of ``OSError``) so callers can branch
    on the specific signal. We inject at ``os.replace`` because the
    Python C-level FileIO.write does not go through the ``os.write``
    Python attribute and is therefore not mock-patchable; ``ENOSPC``
    on rename is itself a real-world scenario (target volume fills
    up between the tmp create and the rename commit)."""
    target = workspace / "memory-bank" / "ondisk.md"

    def fake_replace(src, dst):  # noqa: ARG001
        raise OSError(errno.ENOSPC, "No space left on device")

    with patch(
        "servers.memory_server.memory_record_io.os.replace",
        side_effect=fake_replace,
    ):
        with pytest.raises(DiskFullError) as excinfo:
            _atomic_write_text(target, "payload\n")

    assert excinfo.value.errno == errno.ENOSPC
    # Original target must NOT have been created (we faulted on
    # rename) and no tmp file may be left behind.
    assert not target.exists()
    leftovers = [p for p in target.parent.iterdir() if p.name.startswith(f".{target.name}.")]
    assert leftovers == [], f"tmp file leaked: {leftovers}"


def test_memory_write_returns_disk_full_code_on_enospc(workspace: Path) -> None:
    """The MCP-facing ``memory_write`` must surface the structured
    ``disk_full`` error code AND keep the original file intact, so
    callers can react (e.g. trigger compaction) without losing data."""
    config = load_config(str(workspace))
    target = workspace / "memory-bank" / "notes.md"
    original = target.read_text(encoding="utf-8")

    def fake_replace(src, dst):  # noqa: ARG001
        raise OSError(errno.ENOSPC, "No space left on device")

    with patch(
        "servers.memory_server.memory_record_io.os.replace",
        side_effect=fake_replace,
    ):
        result = memory_write(
            config,
            path="memory-bank/notes.md",
            content="should never land\n",
            mode="overwrite",
            backup=False,
            inject_user_tag=False,
        )

    assert result.get("ok") is False, result
    assert result.get("error") == "disk_full", result
    assert result.get("errno") == errno.ENOSPC, result
    # Original file must be byte-for-byte unchanged.
    assert target.read_text(encoding="utf-8") == original
    # No tmp file leaks in the target directory.
    leftovers = [
        p for p in target.parent.iterdir()
        if p.name.startswith(f".{target.name}.") and p.name.endswith(".tmp")
    ]
    assert leftovers == [], f"tmp file leaked: {leftovers}"
