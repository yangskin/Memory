from memory_hub.db.models import (
    Base,
    BoardPost,
    BriefTokenUsageDaily,
    DeviceStatus,
    MemoryEvent,
    Task,
    TaskAttempt,
    TaskCommand,
    TaskEvent,
    TaskLink,
    TaskReview,
    TaskSubmission,
    TokenDevice,
)


def test_required_tables_and_event_idempotency_constraint_are_declared() -> None:
    assert set(Base.metadata.tables) == {
        "access_tokens",
        "device_enrollments",
        "memory_events",
        "board_posts",
        "brief_jobs",
        "brief_snapshots",
        "brief_heads",
        "brief_token_usage_daily",
        "context_usage_daily",
        "device_status",
        "token_devices",
        "task_events",
        "tasks",
        "task_attempts",
        "task_submissions",
        "task_reviews",
        "task_commands",
        "task_links",
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


def test_device_status_is_one_row_per_project_channel_credential_and_device() -> None:
    assert DeviceStatus.__table__.primary_key.columns.keys() == [
        "project_id",
        "channel",
        "author_auth_id",
        "device_name",
    ]
    assert {item.name for item in DeviceStatus.__table__.indexes} == {
        "idx_device_status_project_channel_device",
        "idx_device_status_project_updated",
    }
    from pathlib import Path

    migration = (Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0015_device_status.py").read_text(encoding="utf-8")
    for name in ("idx_device_status_project_channel_device", "idx_device_status_project_updated"):
        assert name in migration


def test_task_authority_tables_have_identity_and_ordering_constraints() -> None:
    assert TokenDevice.__table__.primary_key.columns.keys() == ["token_id"]
    assert TaskCommand.__table__.primary_key.columns.keys() == ["project_id", "command_id"]
    assert TaskLink.__table__.primary_key.columns.keys() == ["project_id", "task_id", "relation", "target"]
    assert "fk_task_links_task" in {item.name for item in TaskLink.__table__.constraints}


def test_task_projection_tables_have_project_scoped_identity_and_history_constraints() -> None:
    assert Task.__table__.primary_key.columns.keys() == ["project_id", "task_id"]
    assert TaskAttempt.__table__.primary_key.columns.keys() == ["project_id", "attempt_id"]
    assert TaskSubmission.__table__.primary_key.columns.keys() == ["project_id", "submission_id"]
    assert TaskReview.__table__.primary_key.columns.keys() == ["project_id", "review_id"]
    assert {item.name for item in TaskEvent.__table__.constraints} >= {
        "uq_task_events_project_command",
        "uq_task_events_project_task_seq",
    }
    assert "source_event_id" not in TaskEvent.__table__.columns
    assert {column.name for column in TaskEvent.__table__.columns} >= {
        "task_seq",
        "agent",
        "expected_version",
        "expected_assignment_epoch",
        "task_version",
        "assignment_epoch",
    }
    assert {column.name for column in Task.__table__.columns} >= {"last_task_seq", "last_activity_at"}