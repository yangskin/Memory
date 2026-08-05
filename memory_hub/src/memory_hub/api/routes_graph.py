from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import and_, func, or_, select

from memory_hub.auth.permissions import Principal
from memory_hub.api.dependencies import require_principal
from memory_hub.db.models import GraphEdge, GraphNode, GraphProjectionState, MemoryEvent
from memory_hub.domain.graph import GraphQueryRequest

router = APIRouter()

GRAPH_NODE_TYPES = frozenset({"task", "file", "class", "module", "asset", "blueprint", "map", "plugin", "system"})


def _assert_project(principal: Principal, project_id: str) -> None:
    if principal.project_id != project_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "project access denied")


def _node(item: GraphNode, *, include_metadata: bool) -> dict[str, object]:
    result: dict[str, object] = {"id": str(item.id), "type": item.node_type, "key": item.node_key, "name": item.name}
    if include_metadata:
        result.update({"metadata": item.metadata_json or {}, "updated_at": item.updated_at.isoformat()})
    return result


def _edge(item: GraphEdge, *, include_source_event_ids: bool) -> dict[str, object]:
    source_event_ids = item.source_event_ids or []
    result: dict[str, object] = {"source": str(item.source_node_id), "target": str(item.target_node_id), "relation_type": item.relation_type, "confidence": item.confidence, "source_event_count": len(source_event_ids)}
    if include_source_event_ids:
        result.update({"id": str(item.id), "source_event_ids": source_event_ids})
    return result


def _graph(session, project_id: str, *, node_limit: int, edge_limit: int, node_ids: set[object] | None = None, include_metadata: bool = False, include_source_event_ids: bool = False) -> dict[str, object]:
    node_query = select(GraphNode).where(GraphNode.project_id == project_id, GraphNode.node_type.in_(GRAPH_NODE_TYPES)).order_by(GraphNode.node_type, GraphNode.node_key).limit(node_limit)
    if node_ids is not None:
        node_query = select(GraphNode).where(GraphNode.project_id == project_id, GraphNode.id.in_(node_ids), GraphNode.node_type.in_(GRAPH_NODE_TYPES)).order_by(GraphNode.node_type, GraphNode.node_key).limit(node_limit)
    nodes = list(session.scalars(node_query))
    selected_ids = {node.id for node in nodes}
    edges = list(session.scalars(select(GraphEdge).where(GraphEdge.project_id == project_id, GraphEdge.source_node_id.in_(selected_ids), GraphEdge.target_node_id.in_(selected_ids)).order_by(GraphEdge.relation_type).limit(edge_limit))) if selected_ids else []
    state = session.get(GraphProjectionState, project_id)
    latest = int(session.scalar(select(func.coalesce(func.max(MemoryEvent.server_seq), 0)).where(MemoryEvent.project_id == project_id)) or 0)
    covered = int(state.covers_through_seq if state else 0)
    return {"project_id": project_id, "nodes": [_node(item, include_metadata=include_metadata) for item in nodes], "edges": [_edge(item, include_source_event_ids=include_source_event_ids) for item in edges], "freshness": {"covers_through_seq": covered, "latest_event_seq": latest, "stale": covered < latest}}


@router.get("/v1/projects/{project_id}/graph")
def graph_snapshot(project_id: str, request: Request, include_metadata: bool = Query(False), include_source_event_ids: bool = Query(False), principal: Principal = Depends(require_principal("context:read"))) -> dict[str, object]:
    _assert_project(principal, project_id)
    if not request.app.state.rate_limiter.allow(principal.token_id, "graph_read", 120):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded")
    with request.app.state.session_factory() as session:
        return _graph(session, project_id, node_limit=200, edge_limit=400, include_metadata=include_metadata, include_source_event_ids=include_source_event_ids)


@router.post("/v1/projects/{project_id}/graph/query")
def graph_query(project_id: str, payload: GraphQueryRequest, request: Request, principal: Principal = Depends(require_principal("context:read"))) -> dict[str, object]:
    _assert_project(principal, project_id)
    if not request.app.state.rate_limiter.allow(principal.token_id, "graph_read", 120):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded")
    with request.app.state.session_factory() as session:
        filters = []
        for node_type, values in (("task", [payload.task_id] if payload.task_id else []), ("file", payload.files), ("class", payload.classes), ("module", payload.modules), ("asset", payload.assets), ("blueprint", payload.blueprints), ("map", payload.maps), ("plugin", payload.plugins), ("system", payload.system_areas)):
            if values:
                filters.append((node_type, {value for value in values if value}))
        if not filters:
            return _graph(session, project_id, node_limit=payload.max_nodes, edge_limit=payload.max_edges, include_metadata=payload.include_metadata, include_source_event_ids=payload.include_source_event_ids)
        match_predicates = [and_(GraphNode.node_type == node_type, GraphNode.node_key.in_(values)) for node_type, values in filters if values]
        matches = session.scalars(select(GraphNode).where(GraphNode.project_id == project_id, or_(*match_predicates))).all()
        selected = {node.id for node in matches}
        for _ in range(payload.depth):
            if len(selected) >= payload.max_nodes:
                break
            remaining = payload.max_nodes - len(selected)
            related = list(session.scalars(select(GraphEdge).where(GraphEdge.project_id == project_id, GraphEdge.source_node_id.in_(selected) | GraphEdge.target_node_id.in_(selected)).order_by(GraphEdge.relation_type).limit(min(payload.max_edges, remaining * 2)))) if selected else []
            selected.update(edge.source_node_id for edge in related)
            selected.update(edge.target_node_id for edge in related)
            if len(selected) > payload.max_nodes:
                selected = set(sorted(selected, key=str)[:payload.max_nodes])
        return _graph(session, project_id, node_limit=payload.max_nodes, edge_limit=payload.max_edges, node_ids=selected, include_metadata=payload.include_metadata, include_source_event_ids=payload.include_source_event_ids)