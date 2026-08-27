from memory_hub.db.models import (
    Base,
    BoardPost,
    GraphEdge,
    GraphNode,
    BriefTokenUsageDaily,
    MemoryEvent,
    Task,
    TaskAttempt,
    TaskEvent,
    TaskReview,
    TaskSubmission,
)


def test_required_tables_and_event_idempotency_constraint_are_declared() -> None:
    assert set(Base.metadata.tables) == {
        "access_tokens",
        "memory_events",
        "board_posts",
        "brief_jobs",
        "brief_snapshots",
        "brief_heads",
        "brief_token_usage_daily",
        "context_usage_daily",
        "graph_nodes",
        "graph_edges",
        "task_events",
        "tasks",
        "task_agents",
        "task_attempts",
        "task_submissions",
        "task_reviews",
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


def test_brief_token_usage_is_project_day_scoped() -> None:
    assert BriefTokenUsageDaily.__table__.primary_key.columns.keys() == [
        "project_id",
        "usage_date",
    ]


def test_graph_constraints_and_indexes_are_declared() -> None:
    assert "uq_graph_nodes_project_type_key" in {item.name for item in GraphNode.__table__.constraints}
    assert "uq_graph_edges_project_relation" in {item.name for item in GraphEdge.__table__.constraints}
    assert {item.name for item in GraphNode.__table__.indexes} == {"idx_graph_nodes_project_type"}
    assert {item.name for item in GraphEdge.__table__.indexes} == {"idx_graph_edges_project_source", "idx_graph_edges_project_target"}


def test_task_projection_tables_have_project_scoped_identity_and_history_constraints() -> None:
    assert Task.__table__.primary_key.columns.keys() == ["project_id", "task_id"]
    assert TaskAttempt.__table__.primary_key.columns.keys() == ["project_id", "attempt_id"]
    assert TaskSubmission.__table__.primary_key.columns.keys() == ["project_id", "submission_id"]
    assert TaskReview.__table__.primary_key.columns.keys() == ["project_id", "review_id"]
    assert {item.name for item in TaskEvent.__table__.constraints} >= {
        "uq_task_events_project_command",
        "uq_task_events_project_source_event",
    }
    assert {column.name for column in TaskEvent.__table__.columns} >= {
        "expected_version",
        "expected_assignment_epoch",
        "task_version",
        "assignment_epoch",
    }