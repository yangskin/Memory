"""Extract graph facts only from structured event fields."""

from __future__ import annotations

import math
import hashlib
import json
from dataclasses import dataclass
from typing import Any

PROJECT_VISIBLE_SCOPES = frozenset({"shared", "project_shared", "org_shared"})
MAX_NODE_KEY_LENGTH = 1024


@dataclass(frozen=True)
class NodeFact:
    node_type: str
    node_key: str
    name: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class EdgeFact:
    source: tuple[str, str]
    target: tuple[str, str]
    relation_type: str
    confidence: float = 1.0
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class GraphFacts:
    nodes: tuple[NodeFact, ...]
    edges: tuple[EdgeFact, ...]


class InvalidGraphDelta(ValueError):
    """Raised when an explicitly submitted graph delta fails validation."""


_ENTITY_FIELDS = (
    ("active_files", "file"),
    ("class_names", "class"),
    ("module_names", "module"),
    ("asset_paths", "asset"),
    ("blueprint_paths", "blueprint"),
    ("map_names", "map"),
    ("plugin_names", "plugin"),
)

_ALLOWED_NODE_TYPES = frozenset({"task", "system", "file", "class", "module", "asset", "blueprint", "map", "plugin"})
_ALLOWED_RELATIONS = frozenset({"affects", "depends_on", "implements", "validates", "caused_by", "supersedes"})
_MAX_DELTA_NODES = 30
_MAX_DELTA_EDGES = 50
_MAX_EDGE_EVIDENCE_IDS = 16
_MAX_EVIDENCE_ID_LENGTH = 256


def _values(metadata: dict[str, Any], field: str) -> list[str]:
    value = metadata.get(field)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return sorted({text for item in value if (text := str(item).strip()) and len(text) <= MAX_NODE_KEY_LENGTH})


def _delta_facts(metadata: dict[str, Any], task_id: str) -> GraphFacts | None:
    delta = metadata.get("graph_delta")
    if not isinstance(delta, dict) or delta.get("version") != "1.0":
        return None
    if str(delta.get("task_id") or "").strip() != task_id:
        return None
    delta_body = {key: value for key, value in delta.items() if key != "delta_id"}
    canonical_body = json.dumps(delta_body, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    expected_delta_id = f"sha256:{hashlib.sha256(canonical_body.encode('utf-8')).hexdigest()}"
    if delta.get("delta_id") != expected_delta_id:
        return None
    raw_nodes = delta.get("nodes")
    raw_edges = delta.get("edges")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        return None
    if len(raw_nodes) > _MAX_DELTA_NODES or len(raw_edges) > _MAX_DELTA_EDGES:
        return None

    nodes: dict[tuple[str, str], NodeFact] = {}
    for raw in raw_nodes:
        if not isinstance(raw, dict):
            return None
        node_type = str(raw.get("type") or "").strip()
        node_key = str(raw.get("key") or "").strip()
        name = str(raw.get("name") or node_key).strip()
        if node_type not in _ALLOWED_NODE_TYPES or not node_key or len(node_key) > MAX_NODE_KEY_LENGTH:
            return None
        identity = (node_type, node_key)
        if identity in nodes:
            return None
        nodes[identity] = NodeFact(node_type, node_key, name[:MAX_NODE_KEY_LENGTH], {"graph_delta": True})

    edges: set[EdgeFact] = set()
    for raw in raw_edges:
        if not isinstance(raw, dict) or raw.get("origin") not in {"observed", "inferred"}:
            return None
        source = raw.get("source")
        target = raw.get("target")
        relation = str(raw.get("relation") or "").strip()
        if not isinstance(source, dict) or not isinstance(target, dict) or relation not in _ALLOWED_RELATIONS:
            return None
        source_key = (str(source.get("type") or "").strip(), str(source.get("key") or "").strip())
        target_key = (str(target.get("type") or "").strip(), str(target.get("key") or "").strip())
        if source_key not in nodes or target_key not in nodes:
            return None
        try:
            confidence = float(raw.get("confidence"))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0 or (raw.get("origin") == "observed" and confidence != 1.0):
            return None
        evidence_ids = raw.get("evidence_ids")
        if (
            not isinstance(evidence_ids, list)
            or not 1 <= len(evidence_ids) <= _MAX_EDGE_EVIDENCE_IDS
            or any(
                not isinstance(item, str)
                or not item.strip()
                or len(item.strip()) > _MAX_EVIDENCE_ID_LENGTH
                for item in evidence_ids
            )
        ):
            return None
        normalized_evidence = tuple(dict.fromkeys(item.strip() for item in evidence_ids))
        edges.add(EdgeFact(source_key, target_key, relation, confidence, normalized_evidence))
    return GraphFacts(tuple(nodes[key] for key in sorted(nodes)), tuple(sorted(edges, key=lambda item: (item.source, item.target, item.relation_type))))


def validate_graph_delta(metadata: dict[str, Any], task_id: str) -> GraphFacts:
    """Strictly validate an explicitly submitted graph delta."""

    if "graph_delta" not in metadata:
        raise InvalidGraphDelta("graph_delta is missing")
    facts = _delta_facts(metadata, str(task_id or "").strip())
    if facts is None:
        raise InvalidGraphDelta("graph_delta failed schema, identity, bounds, evidence, or integrity validation")
    return facts


def extract_event_facts(event: Any) -> GraphFacts:
    if event.scope not in PROJECT_VISIBLE_SCOPES:
        return GraphFacts((), ())

    nodes: dict[tuple[str, str], NodeFact] = {}
    edges: set[EdgeFact] = set()

    def add_node(node_type: str, name: str, **metadata: Any) -> tuple[str, str]:
        key = name.strip()
        identity = (node_type, key)
        nodes[identity] = NodeFact(node_type, key, key, metadata)
        return identity

    task_name = str(event.task_id or "").strip()
    task = add_node("task", task_name, task_run_id=event.task_run_id) if task_name else None

    entities: list[tuple[str, str]] = []
    metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
    delta = _delta_facts(metadata, task_name)
    if delta is not None:
        return delta
    system_area = str(metadata.get("system_area") or "").strip()
    if system_area and len(system_area) <= MAX_NODE_KEY_LENGTH:
        entities.append(add_node("system", system_area))
    for field, node_type in _ENTITY_FIELDS:
        entities.extend(add_node(node_type, value) for value in _values(metadata, field))

    if task:
        edges.update(EdgeFact(task, entity, "affects") for entity in entities)
    return GraphFacts(tuple(nodes[key] for key in sorted(nodes)), tuple(sorted(edges, key=lambda item: (item.source, item.target, item.relation_type))))
