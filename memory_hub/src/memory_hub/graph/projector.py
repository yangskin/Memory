"""Best-effort, idempotent projection of visible Memory events into Graph."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from memory_hub.db.models import BriefHead, BriefSnapshot, GraphEdge, GraphNode, GraphProjectionState, MemoryEvent

from .extractor import EdgeFact, GraphFacts, extract_event_facts
from .semantic import PROJECT_GRAPH_TYPE, facts_from_project_graph

MAX_SOURCE_EVENT_IDS = 256
MAX_EVIDENCE_IDS = 256


def _merge_ids(existing: list[str] | None, incoming: tuple[str, ...], limit: int) -> list[str]:
    return list(dict.fromkeys([*(existing or []), *incoming]))[-limit:]

def _node_id(project_id: str, node_type: str, node_key: str):
    return uuid5(NAMESPACE_URL, f"memory-hub:graph-node:{project_id}:{node_type}:{node_key}")


def _edge_id(project_id: str, source_id, target_id, relation_type: str):
    return uuid5(NAMESPACE_URL, f"memory-hub:graph-edge:{project_id}:{source_id}:{target_id}:{relation_type}")


def _project_facts(session: Session, project_id: str, facts: GraphFacts, *, source_ids_for_edge, pending_nodes: dict[object, GraphNode], pending_edges: dict[object, GraphEdge]) -> None:
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
        source_event_ids = tuple(source_ids_for_edge(edge))
        if existing is None:
            existing = GraphEdge(id=edge_id, project_id=project_id, source_node_id=source_id, target_node_id=target_id, relation_type=edge.relation_type, confidence=edge.confidence, source_event_ids=_merge_ids([], source_event_ids, MAX_SOURCE_EVENT_IDS), evidence_ids=list(edge.evidence_ids), updated_at=datetime.now(UTC))
            pending_edges[edge_id] = existing
            session.add(existing)
        else:
            existing.source_event_ids = _merge_ids(existing.source_event_ids, source_event_ids, MAX_SOURCE_EVENT_IDS)
            existing.confidence = max(float(existing.confidence or 0.0), edge.confidence)
            existing.updated_at = datetime.now(UTC)
        existing.evidence_ids = _merge_ids(existing.evidence_ids, edge.evidence_ids, MAX_EVIDENCE_IDS)


def current_project_graph_facts(session: Session, project_id: str) -> GraphFacts:
    head = session.get(BriefHead, (project_id, PROJECT_GRAPH_TYPE, ""))
    snapshot = session.get(BriefSnapshot, head.current_brief_id) if head is not None else None
    return facts_from_project_graph(snapshot.structured_brief if snapshot is not None else None)


def current_project_graph_edge_origins(session: Session, project_id: str) -> dict[object, str]:
    facts = current_project_graph_facts(session, project_id)
    return {
        _edge_id(project_id, _node_id(project_id, *edge.source), _node_id(project_id, *edge.target), edge.relation_type): "server_provenance" if edge.relation_type == "documents" else "server_semantic"
        for edge in facts.edges
    }


def project_current_project_graph(session: Session, project_id: str) -> int:
    facts = current_project_graph_facts(session, project_id)
    if not facts.edges:
        return 0
    _project_facts(session, project_id, facts, source_ids_for_edge=lambda edge: edge.evidence_ids, pending_nodes={}, pending_edges={})
    session.commit()
    return len(facts.edges)


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
        _project_facts(session, project_id, facts, source_ids_for_edge=lambda _edge: (str(event.event_id),), pending_nodes=pending_nodes, pending_edges=pending_edges)

    latest = events[-1].server_seq
    state.covers_through_seq = max(state.covers_through_seq, latest)
    state.updated_at = datetime.now(UTC)
    session.commit()
    return latest


def rebuild_project(session: Session, project_id: str, *, batch_size: int = 500) -> int:
    """Replace a project's derived graph from its immutable event history."""
    session.execute(insert(GraphProjectionState).values(project_id=project_id).on_conflict_do_nothing(index_elements=[GraphProjectionState.project_id]))
    state = session.get(GraphProjectionState, project_id, with_for_update=True)
    session.execute(delete(GraphEdge).where(GraphEdge.project_id == project_id))
    session.execute(delete(GraphNode).where(GraphNode.project_id == project_id))
    if state is not None:
        state.covers_through_seq = 0
        state.updated_at = datetime.now(UTC)
    session.commit()

    while True:
        before = session.get(GraphProjectionState, project_id)
        before_seq = int(before.covers_through_seq if before else 0)
        after_seq = project_events(session, project_id, batch_size=batch_size)
        if after_seq <= before_seq:
            project_current_project_graph(session, project_id)
            return after_seq


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