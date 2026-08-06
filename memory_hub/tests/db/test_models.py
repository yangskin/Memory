from memory_hub.db.models import Base, BoardPost, GraphEdge, GraphNode, GraphProjectionState, MemoryEvent


def test_required_tables_and_event_idempotency_constraint_are_declared() -> None:
    assert set(Base.metadata.tables) == {
        "access_tokens",
        "memory_events",
        "board_posts",
        "brief_jobs",
        "brief_snapshots",
        "brief_heads",
        "context_usage_daily",
        "graph_nodes",
        "graph_edges",
        "graph_projection_states",
    }
    unique_constraints = {constraint.name for constraint in MemoryEvent.__table__.constraints}
    indexes = {index.name for index in MemoryEvent.__table__.indexes}
    assert "uq_memory_events_project_event" in unique_constraints
    assert indexes == {
        "idx_events_project_seq",
        "idx_events_project_time",
        "idx_events_project_user_time",
        "idx_events_project_task_time",
        "idx_events_project_agent_time",
    }


def test_board_post_indexes_are_declared() -> None:
    indexes = {index.name for index in BoardPost.__table__.indexes}
    assert indexes == {
        "idx_board_posts_project_created",
        "idx_board_posts_project_status",
        "idx_board_posts_project_thread",
    }


def test_graph_constraints_and_indexes_are_declared() -> None:
    assert "uq_graph_nodes_project_type_key" in {item.name for item in GraphNode.__table__.constraints}
    assert "uq_graph_edges_project_relation" in {item.name for item in GraphEdge.__table__.constraints}
    assert {item.name for item in GraphNode.__table__.indexes} == {"idx_graph_nodes_project_type"}
    assert {item.name for item in GraphEdge.__table__.indexes} == {"idx_graph_edges_project_source", "idx_graph_edges_project_target"}
    assert GraphProjectionState.__table__.primary_key.columns.keys() == ["project_id"]