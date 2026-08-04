"""Small in-process fixed-window rate limiter for the single-instance V1 API."""

from __future__ import annotations

import threading
import time
from collections import deque


class TokenRateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()
        self._last_cleanup = 0.0

    def allow(self, token_id: str, category: str, limit: int, *, window_seconds: float = 60) -> bool:
        now = time.monotonic()
        key = (token_id, category)
        with self._lock:
            if now - self._last_cleanup >= window_seconds:
                for stale_key, stale_bucket in list(self._buckets.items()):
                    while stale_bucket and stale_bucket[0] <= now - window_seconds:
                        stale_bucket.popleft()
                    if not stale_bucket:
                        del self._buckets[stale_key]
                self._last_cleanup = now
            bucket = self._buckets.setdefault(key, deque())
            cutoff = now - window_seconds
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                return False
            bucket.append(now)
            return True