"""交互式摘要的有界等待；晚到结果不写缓存，不累积无限请求线程。"""
from __future__ import annotations

from queue import Queue, Empty
from threading import BoundedSemaphore, Thread
from typing import Callable, Any

from .memory_llm import LLMRequestError

_SLOTS = BoundedSemaphore(2)


def within_budget(operation: Callable[[], Any], seconds: float) -> Any:
    if seconds <= 0:
        raise LLMRequestError('task brief enhancement timeout: local evidence used the interactive budget')
    if not _SLOTS.acquire(blocking=False):
        raise LLMRequestError('task brief enhancement busy: previous requests have not finished')
    result = Queue(maxsize=1)
    def run():
        try:
            result.put((True, operation()))
        except Exception as exc:
            result.put((False, exc))
        finally:
            _SLOTS.release()
    thread = Thread(target=run, name='memory-brief-enhancement', daemon=True)
    try:
        thread.start()
    except Exception:
        _SLOTS.release()
        raise
    try:
        ok, value = result.get(timeout=seconds)
    except Empty as exc:
        raise LLMRequestError('task brief enhancement timeout: returning deterministic evidence') from exc
    if not ok:
        raise value
    return value
