"""Versioned JSON payload construction for Memory Hub V1."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any


def build_memory_event(args: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    content = str(args.get("content_markdown") or args.get("content") or "")
    content_hash = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
    metadata = {key: args.get(key) for key in ("branch", "system_area", "module_names", "class_names", "asset_paths", "blueprint_paths", "active_files", "confidence", "validated_by") if args.get(key) is not None}
    return {
        "schema_version": "1.0", "event_id": str(uuid.uuid4()),
        "source_node_id": hashlib.sha256(str(result.get("path") or "local").encode()).hexdigest()[:16],
        "agent_id": str(args.get("agent_id") or "memory-mcp"),
        "agent_instance_id": str(args.get("agent_instance_id") or args.get("agent_id") or "memory-mcp"),
        "task_id": result.get("task_id") or args.get("task_id"), "task_run_id": result.get("task_run_id") or args.get("task_run_id"),
        "operation": str(args.get("operation") or "record"), "record_kind": result.get("record_kind") or args.get("record_kind"),
        "scope": result.get("scope") or args.get("scope") or "personal", "task_phase": args.get("task_phase"),
        "content_markdown": content, "metadata": metadata, "source_record_id": result.get("id"),
        "occurred_at": args.get("occurred_at") or datetime.now(UTC).isoformat(), "content_hash": content_hash,
    }