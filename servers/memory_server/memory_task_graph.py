"""Build bounded task graph deltas from structured local record metadata."""

from __future__ import annotations

import json
from typing import Any

from .memory_config import MemoryConfig
from .memory_record_index import record_paths_for_exact_task
from .memory_record_io import iter_parsed_records
from .memory_request_id import content_sha
from .memory_result import error_result, ok_result

GRAPH_DELTA_VERSION = "1.0"
MAX_GRAPH_NODES = 30
MAX_GRAPH_EDGES = 50
MAX_NODE_KEY_LENGTH = 1024
MAX_EDGE_EVIDENCE_IDS = 16
MAX_EVIDENCE_ID_LENGTH = 256
PROJECT_VISIBLE_SCOPES = frozenset({"shared", "project_shared", "org_shared"})

_ENTITY_FIELDS = (
    ("active_files", "file"),
    ("class_names", "class"),
    ("module_names", "module"),
    ("asset_paths", "asset"),
    ("blueprint_paths", "blueprint"),
    ("map_names", "map"),
    ("plugin_names", "plugin"),
)


def _metadata_values(metadata: dict[str, Any], field: str) -> list[str]:
    raw = metadata.get(field)
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return sorted(
        {
            value
            for item in raw
            if (value := str(item).strip()) and len(value) <= MAX_NODE_KEY_LENGTH
        }
    )


def build_task_graph_delta(config: MemoryConfig, *, task_id: str) -> dict[str, Any]:
    """Return observed graph facts only; this path never invokes an LLM."""

    task = str(task_id or "").strip()
    if not task:
        return error_result("invalid_input", "task graph settlement requires task_id")

    indexed = record_paths_for_exact_task(
        config,
        task_id=task,
        include_scopes=sorted(PROJECT_VISIBLE_SCOPES),
    )
    indexed_paths = set(indexed.get("paths", [])) if indexed.get("ok") else None
    records, stats = iter_parsed_records(config, include_rel_paths=indexed_paths)
    matching = [
        record
        for record in records
        if str(record.metadata.get("task_id") or "") == task
        and str(record.metadata.get("scope") or "") in PROJECT_VISIBLE_SCOPES
    ]
    nodes: dict[tuple[str, str], dict[str, Any]] = {
        ("task", task): {"type": "task", "key": task, "name": task}
    }
    edges: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}

    for record in matching:
        metadata = record.metadata
        record_id = str(metadata.get("id") or "").strip()
        if not record_id or len(record_id) > MAX_EVIDENCE_ID_LENGTH:
            continue
        entities: list[tuple[str, str]] = []
        system_area = str(metadata.get("system_area") or "").strip()
        if system_area and len(system_area) <= MAX_NODE_KEY_LENGTH:
            entities.append(("system", system_area))
        for field, node_type in _ENTITY_FIELDS:
            entities.extend((node_type, value) for value in _metadata_values(metadata, field))

        for node_type, key in entities:
            nodes.setdefault((node_type, key), {"type": node_type, "key": key, "name": key})
            edge_key = ("task", task, node_type, key, "affects")
            edge = edges.setdefault(
                edge_key,
                {
                    "source": {"type": "task", "key": task},
                    "target": {"type": node_type, "key": key},
                    "relation": "affects",
                    "origin": "observed",
                    "confidence": 1.0,
                    "evidence_ids": [],
                },
            )
            if record_id not in edge["evidence_ids"] and len(edge["evidence_ids"]) < MAX_EDGE_EVIDENCE_IDS:
                edge["evidence_ids"].append(record_id)

    task_node = nodes[("task", task)]
    entity_nodes = [nodes[key] for key in sorted(nodes) if key != ("task", task)]
    ordered_nodes = [task_node, *entity_nodes[: MAX_GRAPH_NODES - 1]]
    allowed_nodes = {(node["type"], node["key"]) for node in ordered_nodes}
    ordered_edges = [
        edge
        for key, edge in sorted(edges.items())
        if (edge["source"]["type"], edge["source"]["key"]) in allowed_nodes
        and (edge["target"]["type"], edge["target"]["key"]) in allowed_nodes
    ][:MAX_GRAPH_EDGES]
    delta_body = {
        "version": GRAPH_DELTA_VERSION,
        "task_id": task,
        "nodes": ordered_nodes,
        "edges": ordered_edges,
    }
    canonical_body = json.dumps(delta_body, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    graph_delta = {
        **delta_body,
        "delta_id": f"sha256:{content_sha(canonical_body)}",
    }
    return ok_result(
        "task graph delta built",
        task_id=task,
        graph_delta=graph_delta,
        observed_only=True,
        source_records=len(matching),
        stats=stats,
    )


__all__ = ["build_task_graph_delta"]