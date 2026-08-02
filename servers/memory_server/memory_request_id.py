"""Request-id and content-hash helpers for v0.5.4 multi-agent safety.

- ``new_request_id()`` returns a UUID7 (time-ordered, unique per call) so
  audit events can be correlated across processes and clients can do
  idempotent retries.
- ``content_sha()`` returns a deterministic SHA-256 hex digest of file
  bytes; used by the optional ``if_match`` optimistic-locking parameter
  on ``memory_write``.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
import uuid


_uuid7_lock = threading.Lock()
_uuid7_last_ms = -1
_uuid7_counter = 0


def _uuid7_fallback() -> uuid.UUID:
    """Pure-Python UUID7 implementation for Python < 3.14.

    Layout per RFC 9562:
      - 48 bits: Unix timestamp in milliseconds (big-endian)
      -  4 bits: version (0b0111)
      - 12 bits: monotonic sub-ms counter (seeded with random bits)
      -  2 bits: variant (0b10)
      - 62 bits: random

    Within a single millisecond the 12-bit counter increments per call
    so ids stay strictly monotonic per process. When the counter would
    overflow we busy-wait one millisecond. Per-call randomness in the
    62-bit tail keeps values unique across processes.
    """
    global _uuid7_last_ms, _uuid7_counter
    with _uuid7_lock:
        ts_ms = time.time_ns() // 1_000_000
        if ts_ms == _uuid7_last_ms:
            _uuid7_counter += 1
            if _uuid7_counter > 0xFFF:
                # Overflow: wait for the next millisecond and reseed.
                while ts_ms == _uuid7_last_ms:
                    ts_ms = time.time_ns() // 1_000_000
                _uuid7_counter = secrets.randbits(8)
        else:
            # New millisecond: seed counter with 8 random bits so ids from
            # different processes within the same ms are unlikely to collide.
            _uuid7_counter = secrets.randbits(8)
        _uuid7_last_ms = ts_ms
        rand_a = _uuid7_counter & 0xFFF
        rand_b = secrets.randbits(62)
    value = (
        ((ts_ms & 0xFFFFFFFFFFFF) << 80)
        | (0x7 << 76)
        | (rand_a << 64)
        | (0b10 << 62)
        | rand_b
    )
    return uuid.UUID(int=value)


_uuid7 = getattr(uuid, "uuid7", _uuid7_fallback)


def new_request_id() -> str:
    """Return a fresh UUID7 string.

    UUID7 (RFC 9562) is a time-ordered UUID: the leading bits are a
    millisecond Unix timestamp, the trailing bits are random, so values
    sort lexicographically by creation time without colliding under
    high concurrency. Uses :func:`uuid.uuid7` on Python 3.14+ and a
    pure-Python equivalent on older interpreters.
    """
    return str(_uuid7())


def content_sha(content: str) -> str:
    """Return the SHA-256 hex digest of ``content`` (UTF-8 encoded).

    Used as the lightweight ``ETag`` for optimistic-locking writes:
    callers read the file, compute its sha, then pass it back as
    ``if_match`` on the next write. The server compares against the
    current on-disk sha and rejects mismatches.
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


__all__ = ["new_request_id", "content_sha"]
