"""Best-effort, idempotent projection of visible Memory events into Graph."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from memory_hub.db.models import GraphEdge, GraphNode, GraphProjectionState, MemoryEvent

from .extractor import extract_event_facts

MAX_SOURCE_EVENT_IDS = 256

def _node_id(project_id: str, node_type: str, node_key: str):
    return uuid5(NAMESPACE_URL, f"memory-hub:graph-node:{project_id}:{node_type}:{node_key}")


def _edge_id(project_id: str, source_id, target_id, relation_type: str):
    return uuid5(NAMESPACE_URL, f"memory-hub:graph-edge:{project_id}:{source_id}:{target_id}:{relation_type}")


def project_events(session: Session, project_id: str, *, batch_size: int = 500) -> int:
    session.execute(insert(GraphProjectionState).values(project_id=project_id).on_conflict_do_nothing(index_elements=[GraphProjectionState.project_id]))
    state = session.get(GraphProjectionState, project_id, with_for_update=True)
    through = state.covers_through_seq if state else 0
    events = list(session.scalars(select(MemoryEvent).where(MemoryEvent.project_id == project_id, MemoryEvent.server_seq > through).order_by(MemoryEvent.server_seq).limit(batch_size)))
    if not events:
        return through

    pending_nodes: dict[object, GraphNode] = {}
    pending_edges: dict[object, GraphEdge] = {}
    for event in events:
        facts = extract_event_facts(event)
        for fact in facts.nodes:
            key = (fact.node_type, fact.node_key)
            node_id = _node_id(project_id, *key)
            node = pending_nodes.get(node_id) or session.get(GraphNode, node_id)
            if node is None:
                node = GraphNode(id=node_id, project_id=project_id, node_type=fact.node_type, node_key=fact.node_key, name=fact.name, metadata_json=fact.metadata, updated_at=datetime.now(UTC))
                pending_nodes[node_id] = node
                session.add(node)
            else:
                node.updated_at = datetime.now(UTC)
                node.metadata_json = {**(node.metadata_json or {}), **fact.metadata}
        session.flush()
        for edge in facts.edges:
            source_id = _node_id(project_id, *edge.source)
            target_id = _node_id(project_id, *edge.target)
            edge_id = _edge_id(project_id, source_id, target_id, edge.relation_type)
            existing = pending_edges.get(edge_id) or session.get(GraphEdge, edge_id)
            if existing is None:
                existing = GraphEdge(id=edge_id, project_id=project_id, source_node_id=source_id, target_node_id=target_id, relation_type=edge.relation_type, source_event_ids=[str(event.event_id)], updated_at=datetime.now(UTC))
                pending_edges[edge_id] = existing
                session.add(existing)
            elif str(event.event_id) not in (existing.source_event_ids or []):
                existing.source_event_ids = [*(existing.source_event_ids or []), str(event.event_id)][-MAX_SOURCE_EVENT_IDS:]
                existing.updated_at = datetime.now(UTC)

    latest = events[-1].server_seq
    state.covers_through_seq = max(state.covers_through_seq, latest)
    state.updated_at = datetime.now(UTC)
    session.commit()
    return latest


def project_pending(session: Session, *, batch_size: int = 500) -> int:
    projects = list(session.scalars(select(MemoryEvent.project_id).distinct()))
    processed = 0
    for project_id in projects:
        before = session.get(GraphProjectionState, project_id)
        before_seq = before.covers_through_seq if before else 0
        after_seq = project_events(session, project_id, batch_size=batch_size)
        if after_seq > before_seq:
            processed += 1
    return processed