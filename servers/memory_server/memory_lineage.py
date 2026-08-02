from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .memory_config import MemoryConfig
from .memory_events import append_event, get_current_user
from .memory_paths import PathSecurityError
from .memory_record_io import (
    find_record_by_id as _find_record,
    iter_parsed_records,
    refresh_index_if_exists as _refresh_index_if_exists,
    write_same_record as _write_same_record,
)
from .memory_records import (
    SCHEMA_VERSION_V2,
    V2_LIST_FIELDS,
    memory_write_record,
)
from .memory_artifact_paths import (
    attach_git_sha,
    normalize_asset_paths as _normalize_asset_paths,
)
from .memory_result import error_result, ok_result

ARTIFACT_FIELDS = [
    "related_artifact_ids",
    "asset_paths",
    "map_names",
    "plugin_names",
    "module_names",
    "class_names",
    "blueprint_paths",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_string_list(value: list[str] | None) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _merge_unique(existing: Any, additions: list[str]) -> list[str]:
    merged: list[str] = []
    for item in (existing if isinstance(existing, list) else []):
        text = str(item).strip()
        if text and text not in merged:
            merged.append(text)
    for item in additions:
        if item not in merged:
            merged.append(item)
    return merged


def _iter_records(config: MemoryConfig) -> list[tuple[Path, str, dict[str, Any], str]]:
    """Adapter that keeps the legacy 4-tuple shape used by lineage helpers."""
    parsed, _stats = iter_parsed_records(config)
    return [(r.abs_path, r.rel_path, r.metadata, r.body) for r in parsed]


def memory_record_observation(
    config: MemoryConfig,
    *,
    content_markdown: str,
    author: str | None = None,
    tags: list[str] | None = None,
    confidence: float | None = None,
    source_refs: list[str] | None = None,
    task_id: str | None = None,
    branch: str | None = None,
    occurred_at: str | None = None,
    memory_tier: str | None = "hot",
    cognitive_level: str | None = "shu",
    related_artifact_ids: list[str] | None = None,
    asset_paths: list[str] | None = None,
    map_names: list[str] | None = None,
    plugin_names: list[str] | None = None,
    module_names: list[str] | None = None,
    class_names: list[str] | None = None,
    blueprint_paths: list[str] | None = None,
    system_area: str | None = None,
) -> dict[str, Any]:
    """Create a schema v2 observation record with evidence/facet metadata."""
    result = memory_write_record(
        config,
        content_markdown=content_markdown,
        schema_version=SCHEMA_VERSION_V2,
        record_kind="observation",
        scope="session",
        status="raw",
        author=author or get_current_user(config.repo_root),
        tags=tags,
        confidence=confidence,
        source_refs=source_refs,
        task_id=task_id,
        branch=branch,
        occurred_at=occurred_at or _now(),
        memory_tier=memory_tier,
        cognitive_level=cognitive_level,
        related_artifact_ids=related_artifact_ids,
        asset_paths=asset_paths,
        map_names=map_names,
        plugin_names=plugin_names,
        module_names=module_names,
        class_names=class_names,
        blueprint_paths=blueprint_paths,
        system_area=system_area,
    )
    if result.get("ok"):
        append_event(
            config,
            "memory_record_observation",
            {
                "id": result.get("id"),
                "path": result.get("path"),
                "task_id": task_id,
                "branch": branch,
                "system_area": system_area,
            },
        )
    return result


def memory_link_artifact(
    config: MemoryConfig,
    record_id: str,
    *,
    related_artifact_ids: list[str] | None = None,
    asset_paths: list[str] | None = None,
    map_names: list[str] | None = None,
    plugin_names: list[str] | None = None,
    module_names: list[str] | None = None,
    class_names: list[str] | None = None,
    blueprint_paths: list[str] | None = None,
    system_area: str | None = None,
) -> dict[str, Any]:
    found = _find_record(config, record_id)
    if isinstance(found, dict):
        return found
    abs_path, rel_path, metadata, body = found

    updates = {
        "related_artifact_ids": _normalize_string_list(related_artifact_ids),
        "asset_paths": _normalize_asset_paths(asset_paths),
        "map_names": _normalize_string_list(map_names),
        "plugin_names": _normalize_string_list(plugin_names),
        "module_names": _normalize_string_list(module_names),
        "class_names": _normalize_string_list(class_names),
        "blueprint_paths": _normalize_asset_paths(blueprint_paths),
    }
    if not any(updates.values()) and not system_area:
        return error_result("invalid_input", "at least one artifact facet or system_area must be provided")

    metadata["schema_version"] = SCHEMA_VERSION_V2
    for key in V2_LIST_FIELDS:
        if key not in metadata:
            metadata[key] = []
    for key, values in updates.items():
        metadata[key] = _merge_unique(metadata.get(key), values)
    if system_area:
        metadata["system_area"] = system_area
    metadata["updated_at"] = _now()

    result = _write_same_record(config, abs_path=abs_path, rel_path=rel_path, metadata=metadata, body=body)
    if result.get("ok"):
        event_payload: dict[str, Any] = {
            "id": record_id,
            "path": rel_path,
            "artifact_fields": {key: values for key, values in updates.items() if values},
            "system_area": system_area,
        }
        attach_git_sha(config.repo_root, event_payload)
        append_event(
            config,
            "memory_link_artifact",
            event_payload,
        )
        result["linked_fields"] = {key: metadata.get(key, []) for key in ARTIFACT_FIELDS}
        result["system_area"] = metadata.get("system_area")
        if "git_sha" in event_payload:
            result["git_sha"] = event_payload["git_sha"]
    return result


def _first_heading(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return "Untitled Record"


def memory_trace_lineage(config: MemoryConfig, record_id: str, *, max_depth: int | None = None) -> dict[str, Any]:
    if max_depth is not None and max_depth < 0:
        return error_result("invalid_input", "max_depth must be >= 0")
    depth_limit = max_depth if max_depth is not None else 20
    try:
        records = _iter_records(config)
    except (PathSecurityError, FileNotFoundError) as exc:
        return error_result("path_error", str(exc))

    by_id = {str(metadata.get("id")): (rel_path, metadata, body) for _abs, rel_path, metadata, body in records}
    if record_id not in by_id:
        return error_result("not_found", f"record not found: {record_id}", record_id=record_id)

    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, str]] = []
    missing: list[dict[str, str]] = []
    visited: set[str] = set()

    def add_node(current_id: str, depth: int) -> None:
        if current_id in visited or depth > depth_limit:
            return
        visited.add(current_id)
        rel_path, metadata, body = by_id[current_id]
        nodes[current_id] = {
            "id": current_id,
            "path": rel_path,
            "title": _first_heading(body),
            "record_kind": metadata.get("record_kind"),
            "scope": metadata.get("scope"),
            "status": metadata.get("status"),
            "memory_tier": metadata.get("memory_tier"),
            "cognitive_level": metadata.get("cognitive_level"),
        }
        for field in ["derived_from_record_ids", "supersedes", "conflicts_with"]:
            targets = metadata.get(field)
            if not isinstance(targets, list):
                continue
            for target_id in [str(item) for item in targets if str(item)]:
                edges.append({"from": current_id, "to": target_id, "type": field})
                if target_id in by_id:
                    add_node(target_id, depth + 1)
                else:
                    missing.append({"from": current_id, "to": target_id, "type": field})

    add_node(record_id, 0)
    return ok_result(
        "lineage traced",
        record_id=record_id,
        nodes=list(nodes.values()),
        edges=edges,
        missing=missing,
        stats={
            "nodes": len(nodes),
            "edges": len(edges),
            "missing": len(missing),
            "max_depth": depth_limit,
        },
    )


def _record_summary(rel_path: str, metadata: dict[str, Any], body: str) -> dict[str, Any]:
    return {
        "id": str(metadata.get("id", "")),
        "path": rel_path,
        "title": _first_heading(body),
        "record_kind": metadata.get("record_kind"),
        "scope": metadata.get("scope"),
        "status": metadata.get("status"),
        "author": metadata.get("author"),
        "memory_tier": metadata.get("memory_tier"),
        "cognitive_level": metadata.get("cognitive_level"),
        "system_area": metadata.get("system_area"),
    }


def memory_list_conflicts(
    config: MemoryConfig,
    *,
    include_resolved: bool = False,
) -> dict[str, Any]:
    """List deterministic conflict edges declared through conflicts_with."""
    try:
        records = _iter_records(config)
    except (PathSecurityError, FileNotFoundError) as exc:
        return error_result("path_error", str(exc))

    by_id = {str(metadata.get("id")): (rel_path, metadata, body) for _abs, rel_path, metadata, body in records}
    conflicts: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str]] = set()

    for _abs_path, rel_path, metadata, body in records:
        source_id = str(metadata.get("id", ""))
        targets = metadata.get("conflicts_with")
        if not source_id or not isinstance(targets, list):
            continue
        for target_id in [str(item).strip() for item in targets if str(item).strip()]:
            edge_key = tuple(sorted([source_id, target_id]))
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)

            source_summary = _record_summary(rel_path, metadata, body)
            if target_id not in by_id:
                missing.append({"source": source_summary, "target_id": target_id, "type": "conflicts_with"})
                continue

            target_path, target_metadata, target_body = by_id[target_id]
            statuses = {str(metadata.get("status", "")), str(target_metadata.get("status", ""))}
            resolved = "archived" in statuses or "degraded" in statuses
            if resolved and not include_resolved:
                continue
            conflicts.append(
                {
                    "source": source_summary,
                    "target": _record_summary(target_path, target_metadata, target_body),
                    "type": "conflicts_with",
                    "resolved": resolved,
                }
            )

    conflicts.sort(key=lambda item: (item["source"]["title"].lower(), item["target"]["title"].lower()))
    missing.sort(key=lambda item: (item["source"]["title"].lower(), item["target_id"]))
    return ok_result(
        "conflicts listed",
        conflicts=conflicts,
        missing=missing,
        stats={
            "conflicts": len(conflicts),
            "missing": len(missing),
            "include_resolved": include_resolved,
        },
    )
