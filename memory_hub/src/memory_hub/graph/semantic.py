"""Bounded, source-backed project graph snapshots from shared events."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Iterable

from .extractor import EdgeFact, GraphFacts, NodeFact

PROJECT_GRAPH_TYPE = "project_graph"
PROJECT_VISIBLE_SCOPES = frozenset({"shared", "project_shared", "org_shared"})
ENTITY_NODE_TYPES = frozenset({"file", "class", "module", "asset", "blueprint", "map", "plugin"})
SOURCE_NODE_TYPE = "source"
GRAPH_NODE_TYPES = ENTITY_NODE_TYPES | {SOURCE_NODE_TYPE}
GRAPH_RELATIONS = frozenset({"depends_on", "implements", "validates", "caused_by", "supersedes"})
PROJECT_GRAPH_RELATIONS = GRAPH_RELATIONS | {"documents"}
ENTITY_FIELDS = (
    ("active_files", "file"),
    ("class_names", "class"),
    ("module_names", "module"),
    ("asset_paths", "asset"),
    ("blueprint_paths", "blueprint"),
    ("map_names", "map"),
    ("plugin_names", "plugin"),
)
MAX_GRAPH_EVENTS = 80
MAX_GRAPH_INPUT_CHARS = 48_000
MAX_EVENT_CONTENT_CHARS = 2_000
MAX_GRAPH_NODES = 80
MAX_GRAPH_EDGES = 120
MAX_EDGE_EVIDENCE_IDS = 16
MIN_CONFIDENCE = 0.7


def _values(metadata: dict[str, Any], field: str) -> list[str]:
    raw = metadata.get(field)
    values = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    return sorted({value for item in values if (value := str(item).strip()) and len(value) <= 1024})


def _entities(metadata: dict[str, Any]) -> list[dict[str, str]]:
    entities = [
        {"type": node_type, "key": value, "name": value}
        for field, node_type in ENTITY_FIELDS
        for value in _values(metadata, field)
    ]
    return sorted({(item["type"], item["key"]): item for item in entities}.values(), key=lambda item: (item["type"], item["key"]))


def _source_name(body: str, metadata: dict[str, Any], record_kind: str) -> str:
    heading = next(
        (
            line.lstrip("#").strip()
            for line in body.splitlines()
            if line.lstrip().startswith("#") and line.lstrip("#").strip()
        ),
        "",
    )
    label = heading or str(metadata.get("system_area") or "").strip()
    if not label:
        label = next((line.strip() for line in body.splitlines() if line.strip()), "Shared memory")
    prefix = record_kind.strip() or "Shared memory"
    return f"{prefix}: {label}"[:240]


def has_project_graph_entities(metadata: object) -> bool:
    return bool(_entities(metadata if isinstance(metadata, dict) else {}))


def project_graph_semantic_inputs(event_payloads: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    """Keep expensive relation extraction to sources that name both endpoints."""
    return [
        payload
        for payload in event_payloads
        if isinstance(payload.get("entities"), list) and len(payload["entities"]) >= 2
    ]


def project_graph_inputs(events: Iterable[Any]) -> list[dict[str, object]]:
    """Select bounded shared sources that explicitly name stable project entities."""
    candidates: list[dict[str, object]] = []
    for event in events:
        if getattr(event, "scope", "") not in PROJECT_VISIBLE_SCOPES:
            continue
        body = str(getattr(event, "content_markdown", "") or "").strip()
        metadata = getattr(event, "metadata_json", {})
        metadata = metadata if isinstance(metadata, dict) else {}
        entities = _entities(metadata)
        if not body or not entities:
            continue
        event_id = str(event.event_id)
        record_kind = str(getattr(event, "record_kind", "") or "")
        candidates.append(
            {
                "event_id": event_id,
                "record_kind": record_kind,
                "source": {"type": SOURCE_NODE_TYPE, "key": f"event:{event_id}", "name": _source_name(body, metadata, record_kind)},
                "entities": entities,
                "content": body,
            }
        )

    selected: list[dict[str, object]] = []
    remaining = MAX_GRAPH_INPUT_CHARS
    for candidate in reversed(candidates):
        if len(selected) >= MAX_GRAPH_EVENTS or remaining <= 0:
            break
        content = str(candidate["content"])
        size = min(len(content), MAX_EVENT_CONTENT_CHARS, remaining)
        if size <= 0:
            break
        selected.append({**candidate, "content": content[:size]})
        remaining -= size
    return list(reversed(selected))


def _identity(raw: object, allowed_types: frozenset[str]) -> tuple[str, str] | None:
    if not isinstance(raw, dict):
        return None
    node_type = str(raw.get("type") or "").strip()
    node_key = str(raw.get("key") or "").strip()
    if node_type not in allowed_types or not node_key or len(node_key) > 1024:
        return None
    return node_type, node_key


def _entity_identity(raw: object) -> tuple[str, str] | None:
    return _identity(raw, ENTITY_NODE_TYPES)


def _graph_identity(raw: object) -> tuple[str, str] | None:
    return _identity(raw, GRAPH_NODE_TYPES)


def validate_project_graph(raw: object, event_payloads: list[dict[str, object]]) -> dict[str, object]:
    """Normalize model output and discard every relation lacking direct event evidence."""
    value = raw if isinstance(raw, dict) else {}
    candidates_by_event: dict[str, set[tuple[str, str]]] = {}
    candidate_nodes: dict[tuple[str, str], dict[str, str]] = {}
    for payload in event_payloads:
        event_id = str(payload.get("event_id") or "").strip()
        raw_entities = payload.get("entities")
        if not event_id or not isinstance(raw_entities, list):
            continue
        identities = {_entity_identity(entity) for entity in raw_entities}
        candidates_by_event[event_id] = {identity for identity in identities if identity is not None}
        for entity in raw_entities:
            identity = _entity_identity(entity)
            if identity is not None:
                candidate_nodes[identity] = {"type": identity[0], "key": identity[1], "name": identity[1]}

    raw_nodes = value.get("nodes")
    raw_edges = value.get("edges")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise ValueError("project graph response must contain nodes and edges arrays")

    declared_nodes = {
        identity
        for raw_node in raw_nodes[:MAX_GRAPH_NODES]
        if (identity := _entity_identity(raw_node)) in candidate_nodes
    }
    edges: dict[tuple[tuple[str, str], tuple[str, str], str], dict[str, object]] = {}
    for raw_edge in raw_edges[:MAX_GRAPH_EDGES]:
        if not isinstance(raw_edge, dict):
            continue
        source = _entity_identity(raw_edge.get("source"))
        target = _entity_identity(raw_edge.get("target"))
        relation = str(raw_edge.get("relation") or "").strip()
        evidence_ids = raw_edge.get("evidence_ids")
        try:
            confidence = float(raw_edge.get("confidence"))
        except (TypeError, ValueError):
            continue
        if (
            source is None
            or target is None
            or source == target
            or source not in declared_nodes
            or target not in declared_nodes
            or relation not in GRAPH_RELATIONS
            or not math.isfinite(confidence)
            or not MIN_CONFIDENCE <= confidence <= 1.0
            or not isinstance(evidence_ids, list)
            or not 1 <= len(evidence_ids) <= MAX_EDGE_EVIDENCE_IDS
        ):
            continue
        supporting_ids = [
            event_id
            for item in evidence_ids
            if (event_id := str(item).strip())
            and source in candidates_by_event.get(event_id, set())
            and target in candidates_by_event.get(event_id, set())
        ]
        if not supporting_ids:
            continue
        key = (source, target, relation)
        existing = edges.get(key)
        normalized_ids = list(dict.fromkeys(supporting_ids))
        if existing is None or confidence > float(existing["confidence"]):
            edges[key] = {
                "source": {"type": source[0], "key": source[1]},
                "target": {"type": target[0], "key": target[1]},
                "relation": relation,
                "confidence": confidence,
                "evidence_ids": normalized_ids,
            }
        else:
            existing["evidence_ids"] = list(dict.fromkeys([*existing["evidence_ids"], *normalized_ids]))[:MAX_EDGE_EVIDENCE_IDS]

    referenced = {identity for edge in edges.values() for endpoint in (edge["source"], edge["target"]) if (identity := _entity_identity(endpoint)) is not None}
    source_ids = [
        str(payload["event_id"])
        for payload in event_payloads
        if str(payload.get("event_id") or "") in {event_id for edge in edges.values() for event_id in edge["evidence_ids"]}
    ]
    return {
        "schema_version": "1.0",
        "as_of": str(value.get("as_of") or datetime.now(UTC).isoformat()),
        "nodes": [candidate_nodes[identity] for identity in sorted(referenced)],
        "edges": [edges[key] for key in sorted(edges)],
        "source_event_ids": source_ids,
    }


def build_project_graph(raw: object, event_payloads: list[dict[str, object]]) -> dict[str, object]:
    """Combine LLM-validated semantic facts with deterministic source provenance."""
    semantic = validate_project_graph(raw, event_payloads)
    nodes = {
        identity: node
        for node in semantic["nodes"]
        if (identity := _graph_identity(node)) is not None
    }
    edges = {
        (edge["source"]["type"], edge["source"]["key"], edge["target"]["type"], edge["target"]["key"], edge["relation"]): edge
        for edge in semantic["edges"]
    }
    used_event_ids = set(semantic["source_event_ids"])
    for payload in event_payloads:
        event_id = str(payload.get("event_id") or "").strip()
        source = _graph_identity(payload.get("source"))
        raw_entities = payload.get("entities")
        if not event_id or source is None or not isinstance(raw_entities, list):
            continue
        source_raw = payload.get("source") if isinstance(payload.get("source"), dict) else {}
        source_name = str(source_raw.get("name") or source[1]).strip()[:1024] or source[1]
        provenance_edges = 0
        for raw_entity in raw_entities:
            entity = _entity_identity(raw_entity)
            if entity is None:
                continue
            nodes[entity] = {"type": entity[0], "key": entity[1], "name": entity[1]}
            key = (source[0], source[1], entity[0], entity[1], "documents")
            edges[key] = {
                "source": {"type": source[0], "key": source[1]},
                "target": {"type": entity[0], "key": entity[1]},
                "relation": "documents",
                "confidence": 1.0,
                "evidence_ids": [event_id],
            }
            provenance_edges += 1
        if provenance_edges:
            nodes[source] = {"type": source[0], "key": source[1], "name": source_name}
            used_event_ids.add(event_id)
    return {
        "schema_version": "1.0",
        "as_of": semantic["as_of"],
        "nodes": [nodes[identity] for identity in sorted(nodes)],
        "edges": [edges[key] for key in sorted(edges)],
        "source_event_ids": [
            str(payload["event_id"])
            for payload in event_payloads
            if str(payload.get("event_id") or "") in used_event_ids
        ],
    }


def facts_from_project_graph(document: object) -> GraphFacts:
    """Turn a previously validated project graph snapshot into projection facts."""
    value = document if isinstance(document, dict) else {}
    raw_nodes = value.get("nodes")
    raw_edges = value.get("edges")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        return GraphFacts((), ())
    nodes: dict[tuple[str, str], NodeFact] = {}
    for raw_node in raw_nodes:
        identity = _graph_identity(raw_node)
        if identity is not None:
            name = str(raw_node.get("name") or identity[1]).strip()[:1024] or identity[1]
            nodes[identity] = NodeFact(identity[0], identity[1], name, {"server_provenance": identity[0] == SOURCE_NODE_TYPE, "server_semantic": identity[0] != SOURCE_NODE_TYPE})
    edges: set[EdgeFact] = set()
    for raw_edge in raw_edges:
        if not isinstance(raw_edge, dict):
            continue
        source = _graph_identity(raw_edge.get("source"))
        target = _graph_identity(raw_edge.get("target"))
        relation = str(raw_edge.get("relation") or "").strip()
        evidence_ids = raw_edge.get("evidence_ids")
        try:
            confidence = float(raw_edge.get("confidence"))
        except (TypeError, ValueError):
            continue
        if (
            source not in nodes
            or target not in nodes
            or relation not in PROJECT_GRAPH_RELATIONS
            or not math.isfinite(confidence)
            or not (confidence == 1.0 if relation == "documents" else MIN_CONFIDENCE <= confidence <= 1.0)
            or not isinstance(evidence_ids, list)
            or not evidence_ids
        ):
            continue
        normalized_ids = tuple(dict.fromkeys(str(item).strip() for item in evidence_ids if str(item).strip()))
        if normalized_ids:
            edges.add(EdgeFact(source, target, relation, confidence, normalized_ids))
    referenced = {identity for edge in edges for identity in (edge.source, edge.target)}
    return GraphFacts(tuple(nodes[key] for key in sorted(referenced)), tuple(sorted(edges, key=lambda edge: (edge.source, edge.target, edge.relation_type))))