"""Extract graph facts only from structured event fields."""

from __future__ import annotations

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


@dataclass(frozen=True)
class GraphFacts:
    nodes: tuple[NodeFact, ...]
    edges: tuple[EdgeFact, ...]


_ENTITY_FIELDS = (
    ("active_files", "file"),
    ("class_names", "class"),
    ("module_names", "module"),
    ("asset_paths", "asset"),
    ("blueprint_paths", "blueprint"),
    ("map_names", "map"),
    ("plugin_names", "plugin"),
)


def _values(metadata: dict[str, Any], field: str) -> list[str]:
    value = metadata.get(field)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return sorted({text for item in value if (text := str(item).strip()) and len(text) <= MAX_NODE_KEY_LENGTH})


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

    agent_name = str(event.agent_instance_id or event.agent_id or "").strip()
    task_name = str(event.task_id or "").strip()
    agent = add_node("agent", agent_name, agent_id=event.agent_id) if agent_name else None
    task = add_node("task", task_name, task_run_id=event.task_run_id) if task_name else None
    if agent and task:
        edges.add(EdgeFact(agent, task, "performed"))

    entities: list[tuple[str, str]] = []
    metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
    system_area = str(metadata.get("system_area") or "").strip()
    if system_area and len(system_area) <= MAX_NODE_KEY_LENGTH:
        entities.append(add_node("system", system_area))
    for field, node_type in _ENTITY_FIELDS:
        entities.extend(add_node(node_type, value) for value in _values(metadata, field))

    owner = task or agent
    if owner:
        edges.update(EdgeFact(owner, entity, "affects") for entity in entities)
    return GraphFacts(tuple(nodes[key] for key in sorted(nodes)), tuple(sorted(edges, key=lambda item: (item.source, item.target, item.relation_type))))