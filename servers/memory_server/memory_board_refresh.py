"""任务简报使用明确标注的 Board 快照，远端咨询信息不阻塞本地读取。"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from collections import OrderedDict
from pathlib import Path

from .memory_record_io import _atomic_write_text

_LOCK = threading.Lock()
_RUNNING: set[str] = set()
_ERRORS: OrderedDict[str, str] = OrderedDict()
logger = logging.getLogger(__name__)


def _cache_path(config, payload) -> Path:
    shared = config.shared_memory
    # 凭据仅参与单向隔离摘要，不写入缓存或诊断，换账号后不能复用旧权限快照。
    identity = {'server': shared.server_url, 'project': shared.project_id,
                'credential': hashlib.sha256(str(shared.token).encode()).hexdigest(),
                'query': payload}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return config.repo_root / '.ai-memory/board-query-cache' / (key + '.json')


def cached_board_query(config, payload, *, query):
    """返回 (已有快照或 None, 明确的新鲜度/刷新状态)，后台最多两个请求。"""
    shared = config.shared_memory
    if not shared.active or not shared.read_enabled:
        return None, {'source': 'local', 'freshness': 'local_only', 'refresh': 'disabled', 'advisory': True}
    path = _cache_path(config, payload)
    cached = None
    fetched_at = None
    cache_error = None
    try:
        if path.exists():
            if path.stat().st_size > 2_000_000:
                raise ValueError('cache exceeds size limit')
            data = json.loads(path.read_text(encoding='utf-8'))
            if data.get('version') != 1 or not isinstance(data.get('items'), list):
                raise ValueError('invalid Board cache')
            fetched_at = float(data['fetched_at'])
            if not math.isfinite(fetched_at):
                raise ValueError('invalid Board cache timestamp')
            cached = [dict(item) for item in data['items'][:50] if isinstance(item, dict)]
    except (OSError, ValueError, KeyError, TypeError):
        cache_error = 'board_cache_invalid'
        cached = None
        fetched_at = None
    fresh = fetched_at is not None and 0 <= time.time() - fetched_at < shared.fresh_cache_seconds
    state = {'source': 'cache' if cached is not None else 'local',
             'freshness': 'fresh' if fresh else ('stale' if cached is not None else 'not_verified'),
             'refresh': 'not_needed' if fresh else 'pending', 'advisory': True}
    if fetched_at is not None:
        state['fetched_at'] = fetched_at
    if cache_error:
        state['error'] = cache_error
    key = str(path.resolve())
    with _LOCK:
        if key in _ERRORS:
            state['last_refresh_error'] = _ERRORS[key]
        if fresh:
            return cached, state
        if key in _RUNNING:
            state['refresh'] = 'in_progress'
            return cached, state
        if len(_RUNNING) >= 2:
            state['refresh'] = 'busy'
            return cached, state
        _RUNNING.add(key)

    def refresh():
        error = None
        try:
            result = query(config, dict(payload))
            if not result.get('ok'):
                error = str(result.get('error') or 'remote_unavailable')[:80]
            else:
                body = result.get('remote')
                if not isinstance(body, dict) or not isinstance(body.get('items'), list):
                    raise ValueError('remote Board response has no item list')
                items = [dict(item) for item in body['items'][:50] if isinstance(item, dict)]
                # 成功的空列表也发布快照，下一次读取不继续显示已解决的远端旧项。
                data = {'version': 1, 'fetched_at': time.time(), 'items': items}
                encoded = json.dumps(data, ensure_ascii=False)
                if len(encoded.encode('utf-8')) > 2_000_000:
                    raise ValueError('remote Board snapshot exceeds size limit')
                path.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write_text(path, encoded, fsync_strict=config.mcp_fsync_strict)
        except Exception as exc:
            error = type(exc).__name__
        finally:
            with _LOCK:
                _RUNNING.discard(key)
                if error:
                    _ERRORS[key] = error
                    _ERRORS.move_to_end(key)
                    while len(_ERRORS) > 128:
                        _ERRORS.popitem(last=False)
                else:
                    _ERRORS.pop(key, None)
            if error:
                logger.warning('advisory Board refresh failed: %s', error)

    thread = threading.Thread(target=refresh, name='memory-board-refresh', daemon=True)
    try:
        thread.start()
        state['refresh'] = 'scheduled'
    except RuntimeError:
        with _LOCK:
            _RUNNING.discard(key)
        state['refresh'] = 'failed'
        state['error'] = 'board_refresh_start_failed'
    return cached, state
