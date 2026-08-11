"""Best-effort enqueue for locally persisted records destined for Memory Hub."""

from __future__ import annotations

import logging
from typing import Any

from .memory_config import MemoryConfig

logger = logging.getLogger(__name__)


def enqueue_shared_record(
    config: MemoryConfig,
    args: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    if not result.get("ok") or not config.shared_memory.enabled:
        return result
    try:
        from .memory_frontmatter import parse_record_pack_entries
        from .memory_events import get_current_user
        from .memory_reader import memory_get
        from .memory_sync_protocol import build_memory_event
        from .memory_sync_store import SyncStore

        canonical: dict[str, Any] = {}
        record_id = str(result.get("id") or "")
        record_path = str(result.get("path") or "")
        if record_id and record_path:
            persisted = memory_get(config, record_path)
            if persisted.get("ok"):
                for metadata, content in parse_record_pack_entries(str(persisted.get("content") or "")):
                    if str(metadata.get("id") or "") == record_id:
                        canonical = {**metadata, "content_markdown": content}
                        break
        event = build_memory_event(args, result, canonical, repo_root=config.repo_root)
        if event["scope"] not in config.shared_memory.sync_scopes:
            return result
        queued = SyncStore(config.repo_root / ".ai-memory" / "shared-sync.db").enqueue(
            event["event_id"],
            event,
            event["content_hash"],
            get_current_user(config.repo_root),
        )
        if queued:
            from .memory_sync_worker import wake_sync_worker

            wake_sync_worker(config.repo_root)
        result["shared_sync"] = {"enabled": True, "queued": queued}
    except Exception as exc:  # synchronization must never change local write success
        logger.warning("shared event enqueue failed: %s", type(exc).__name__)
        result["shared_sync"] = {"enabled": True, "queued": False}
    return result