"""Transactional Task Graph event projection for the public Memory Hub."""

from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from memory_hub.db.models import (
    GraphEdge,
    GraphNode,
    MemoryEvent,
    Task,
    TaskAgent,
    TaskAttempt,
    TaskEvent,
    TaskReview,
    TaskSubmission,
)
from memory_hub.domain.tasks import TaskEventPayload


MAX_GRAPH_IDS = 256
MAX_HISTORY_ITEMS = 200
TERMINAL_STATES = frozenset({"done", "cancelled"})
TASK_LIST_STATES = frozenset({"all", "working", "open", "active", "blocked", "review", "done", "cancelled"})


class TaskProjectionError(ValueError):
    """An expected Task Graph command rejection suitable for the event API."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _encode_task_cursor(task: Task) -> str:
    payload = json.dumps(
        {"updated_at": task.updated_at.isoformat(), "task_id": task.task_id},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_task_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        updated_at = datetime.fromisoformat(str(value["updated_at"]))
        task_id = str(value["task_id"])
    except (binascii.Error, KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ValueError("invalid task cursor") from exc
    if not task_id:
        raise ValueError("invalid task cursor")
    return updated_at, task_id


def _node_id(project_id: str, node_type: str, node_key: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"memory-hub:graph-node:{project_id}:{node_type}:{node_key}")


def _edge_id(project_id: str, source_id: UUID, target_id: UUID, relation_type: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"memory-hub:graph-edge:{project_id}:{source_id}:{target_id}:{relation_type}")


def _now() -> datetime:
    return datetime.now(UTC)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _payload_text(
    payload: dict[str, object],
    key: str,
    *,
    required: bool = False,
    maximum: int = 8192,
) -> str:
    value = payload.get(key)
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise TaskProjectionError("invalid_task_event", f"task event payload.{key} must be a string")
    text = value.strip()
    if required and not text:
        raise TaskProjectionError("invalid_task_event", f"task event payload.{key} is required")
    if len(text) > maximum:
        raise TaskProjectionError("invalid_task_event", f"task event payload.{key} exceeds {maximum} characters")
    return text


def _payload_list(payload: dict[str, object], key: str, *, maximum: int = 64) -> list[str]:
    value = payload.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise TaskProjectionError("invalid_task_event", f"task event payload.{key} must be an array")
    values: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise TaskProjectionError("invalid_task_event", f"task event payload.{key} must contain strings")
        text = item.strip()
        if not text or len(text) > 1024:
            raise TaskProjectionError("invalid_task_event", f"task event payload.{key} has an invalid item")
        if text not in seen:
            values.append(text)
            seen.add(text)
        if len(values) >= maximum:
            break
    return values


def _payload_int(payload: dict[str, object], key: str, *, minimum: int = 0) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise TaskProjectionError("invalid_task_event", f"task event payload.{key} must be an integer")
    return value


def _upsert_agent(session: Session, project_id: str, agent_id: str, timestamp: datetime) -> TaskAgent:
    agent = session.get(TaskAgent, (project_id, agent_id))
    if agent is None:
        agent = TaskAgent(
            project_id=project_id,
            agent_id=agent_id,
            role="",
            capabilities_json=[],
            owner="",
            status="available",
            created_at=timestamp,
            updated_at=timestamp,
        )
        session.add(agent)
    else:
        agent.updated_at = timestamp
    return agent


def _require_task(session: Session, project_id: str, task_id: str) -> Task:
    task = session.get(Task, (project_id, task_id))
    if task is None:
        raise TaskProjectionError("task_not_found", "task_id does not exist")
    return task


def _check_version(task: Task, event: TaskEventPayload) -> None:
    if task.version != event.expected_version:
        raise TaskProjectionError(
            "version_conflict",
            f"task version {task.version} does not match expected_version {event.expected_version}",
        )
    if event.task_version != event.expected_version + 1:
        raise TaskProjectionError("invalid_task_event", "task_version must advance expected_version by one")


def _check_epoch(task: Task, event: TaskEventPayload, *, required: bool) -> None:
    if required and event.expected_assignment_epoch is None:
        raise TaskProjectionError("missing_assignment_epoch", "task action requires expected_assignment_epoch")
    if event.expected_assignment_epoch is not None and task.assignment_epoch != event.expected_assignment_epoch:
        raise TaskProjectionError(
            "assignment_epoch_conflict",
            f"task assignment epoch {task.assignment_epoch} does not match expected_assignment_epoch {event.expected_assignment_epoch}",
        )


def _advance(task: Task, event: TaskEventPayload, timestamp: datetime, *, state: str | None = None) -> None:
    task.version = event.task_version
    task.updated_at = timestamp
    if state is not None:
        task.state = state


def _current_attempt(session: Session, project_id: str, task: Task) -> TaskAttempt:
    if not task.current_attempt_id:
        raise TaskProjectionError("no_current_attempt", "task has no current attempt")
    attempt = session.get(TaskAttempt, (project_id, task.current_attempt_id))
    if attempt is None or attempt.task_id != task.task_id:
        raise TaskProjectionError("no_current_attempt", "current task attempt is unavailable")
    return attempt


def _executor_attempt(
    session: Session,
    project_id: str,
    task: Task,
    event: TaskEventPayload,
    *,
    allowed_statuses: frozenset[str],
) -> TaskAttempt:
    attempt = _current_attempt(session, project_id, task)
    if attempt.assignee != event.actor_id:
        raise TaskProjectionError("assignment_forbidden", "only the current assignee may perform this task action")
    if attempt.status not in allowed_statuses:
        raise TaskProjectionError("invalid_attempt_state", "current attempt does not permit this task action")
    return attempt


def _require_current_attempt_payload(event: TaskEventPayload, attempt: TaskAttempt) -> None:
    attempt_id = _payload_text(event.payload, "attempt_id", required=True, maximum=256)
    if attempt_id != attempt.attempt_id:
        raise TaskProjectionError("attempt_conflict", "task event attempt_id does not match the current attempt")


def _apply_created(session: Session, project_id: str, event: TaskEventPayload) -> Task:
    if event.expected_version != 0 or event.task_version != 1 or event.assignment_epoch != 0:
        raise TaskProjectionError("invalid_task_event", "TaskCreated must start at version 1 and assignment epoch 0")
    if session.get(Task, (project_id, event.task_id)) is not None:
        raise TaskProjectionError("task_exists", "task_id already exists")
    payload = event.payload
    title = _payload_text(payload, "title", required=True, maximum=1024)
    objective = _payload_text(payload, "objective")
    acceptance = _payload_text(payload, "acceptance")
    priority = _payload_text(payload, "priority", maximum=64) or "normal"
    _payload_list(payload, "depends_on")
    _payload_list(payload, "produced_memory")
    _payload_text(payload, "parent_task_id", maximum=256)
    _upsert_agent(session, project_id, event.actor_id, event.occurred_at)
    task = Task(
        project_id=project_id,
        task_id=event.task_id,
        title=title,
        objective=objective,
        acceptance=acceptance,
        priority=priority,
        state="open",
        version=event.task_version,
        assignment_epoch=event.assignment_epoch,
        current_attempt_id=None,
        created_at=event.occurred_at,
        updated_at=event.occurred_at,
    )
    session.add(task)
    return task


def _apply_assignment(
    session: Session,
    project_id: str,
    task: Task,
    event: TaskEventPayload,
    *,
    reassign: bool,
) -> None:
    _check_version(task, event)
    _check_epoch(task, event, required=reassign)
    if task.state in TERMINAL_STATES or task.state == "review":
        raise TaskProjectionError("invalid_task_state", f"cannot assign a {task.state} task")
    if reassign and not task.current_attempt_id:
        raise TaskProjectionError("no_current_attempt", "TaskReassigned requires a current attempt")
    payload = event.payload
    attempt_id = _payload_text(payload, "attempt_id", required=True, maximum=256)
    assignee = _payload_text(payload, "assignee", required=True, maximum=256)
    assigned_by = _payload_text(payload, "assigned_by", required=True, maximum=256)
    epoch = _payload_int(payload, "epoch")
    if assigned_by != event.actor_id:
        raise TaskProjectionError("invalid_task_event", "assigned_by must match actor_id")
    if epoch != task.assignment_epoch + 1 or event.assignment_epoch != epoch:
        raise TaskProjectionError("invalid_task_event", "assignment epoch is not the next task assignment epoch")
    if session.get(TaskAttempt, (project_id, attempt_id)) is not None:
        raise TaskProjectionError("attempt_exists", "attempt_id already exists")
    if task.current_attempt_id:
        previous = _current_attempt(session, project_id, task)
        if previous.status in {"offered", "active"}:
            previous.status = "superseded"
            previous.updated_at = event.occurred_at
    _upsert_agent(session, project_id, event.actor_id, event.occurred_at)
    _upsert_agent(session, project_id, assignee, event.occurred_at)
    session.add(
        TaskAttempt(
            project_id=project_id,
            attempt_id=attempt_id,
            task_id=task.task_id,
            epoch=epoch,
            assignee=assignee,
            assigned_by=assigned_by,
            status="offered",
            created_at=event.occurred_at,
            updated_at=event.occurred_at,
        )
    )
    task.current_attempt_id = attempt_id
    task.assignment_epoch = epoch
    _advance(task, event, event.occurred_at, state="open")


def _apply_claim(session: Session, project_id: str, task: Task, event: TaskEventPayload) -> None:
    _check_version(task, event)
    _check_epoch(task, event, required=True)
    if task.state != "open":
        raise TaskProjectionError("invalid_task_state", "only an open task may be claimed")
    attempt = _executor_attempt(session, project_id, task, event, allowed_statuses=frozenset({"offered"}))
    _require_current_attempt_payload(event, attempt)
    attempt.status = "active"
    attempt.updated_at = event.occurred_at
    _advance(task, event, event.occurred_at, state="active")


def _apply_decline(session: Session, project_id: str, task: Task, event: TaskEventPayload) -> None:
    _check_version(task, event)
    _check_epoch(task, event, required=True)
    attempt = _executor_attempt(session, project_id, task, event, allowed_statuses=frozenset({"offered", "active"}))
    _require_current_attempt_payload(event, attempt)
    attempt.status = "superseded"
    attempt.updated_at = event.occurred_at
    task.current_attempt_id = None
    _advance(task, event, event.occurred_at, state="open")


def _apply_report(session: Session, project_id: str, task: Task, event: TaskEventPayload) -> None:
    _check_version(task, event)
    _check_epoch(task, event, required=True)
    attempt = _executor_attempt(session, project_id, task, event, allowed_statuses=frozenset({"active"}))
    _require_current_attempt_payload(event, attempt)
    _payload_text(event.payload, "summary", required=True)
    _advance(task, event, event.occurred_at)


def _apply_block(session: Session, project_id: str, task: Task, event: TaskEventPayload) -> None:
    _check_version(task, event)
    _check_epoch(task, event, required=True)
    if task.state != "active":
        raise TaskProjectionError("invalid_task_state", "only an active task may be blocked")
    attempt = _executor_attempt(session, project_id, task, event, allowed_statuses=frozenset({"active"}))
    _require_current_attempt_payload(event, attempt)
    _payload_text(event.payload, "reason", required=True)
    _advance(task, event, event.occurred_at, state="blocked")


def _apply_resume(session: Session, project_id: str, task: Task, event: TaskEventPayload) -> None:
    _check_version(task, event)
    _check_epoch(task, event, required=True)
    if task.state != "blocked":
        raise TaskProjectionError("invalid_task_state", "only a blocked task may be resumed")
    attempt = _executor_attempt(session, project_id, task, event, allowed_statuses=frozenset({"active"}))
    _require_current_attempt_payload(event, attempt)
    _advance(task, event, event.occurred_at, state="active")


def _apply_submit(session: Session, project_id: str, task: Task, event: TaskEventPayload) -> None:
    _check_version(task, event)
    _check_epoch(task, event, required=True)
    if task.state != "active":
        raise TaskProjectionError("invalid_task_state", "only an active task may be submitted")
    attempt = _executor_attempt(session, project_id, task, event, allowed_statuses=frozenset({"active"}))
    _require_current_attempt_payload(event, attempt)
    payload = event.payload
    submission_id = _payload_text(payload, "submission_id", required=True, maximum=256)
    summary = _payload_text(payload, "summary", required=True)
    evidence = _payload_list(payload, "evidence")
    if session.get(TaskSubmission, (project_id, submission_id)) is not None:
        raise TaskProjectionError("submission_exists", "submission_id already exists")
    session.add(
        TaskSubmission(
            project_id=project_id,
            submission_id=submission_id,
            task_id=task.task_id,
            attempt_id=attempt.attempt_id,
            summary=summary,
            evidence_json=evidence,
            task_version=event.task_version,
            created_at=event.occurred_at,
        )
    )
    attempt.status = "submitted"
    attempt.updated_at = event.occurred_at
    _advance(task, event, event.occurred_at, state="review")


def _apply_review(session: Session, project_id: str, task: Task, event: TaskEventPayload) -> None:
    _check_version(task, event)
    if task.state != "review":
        raise TaskProjectionError("invalid_task_state", "only a task in review may be reviewed")
    payload = event.payload
    review_id = _payload_text(payload, "review_id", required=True, maximum=256)
    submission_id = _payload_text(payload, "submission_id", required=True, maximum=256)
    decision = _payload_text(payload, "decision", required=True, maximum=64)
    summary = _payload_text(payload, "summary")
    if decision not in {"approved", "changes_requested"}:
        raise TaskProjectionError("invalid_task_event", "review decision must be approved or changes_requested")
    submission = session.get(TaskSubmission, (project_id, submission_id))
    if submission is None or submission.task_id != task.task_id:
        raise TaskProjectionError("submission_not_found", "review submission does not belong to this task")
    if submission.task_version != event.expected_version:
        raise TaskProjectionError("submission_conflict", "review submission is not the current task version")
    attempt = session.get(TaskAttempt, (project_id, submission.attempt_id))
    if attempt is None:
        raise TaskProjectionError("no_current_attempt", "submission attempt is unavailable")
    if attempt.assignee == event.actor_id:
        raise TaskProjectionError("reviewer_conflict", "reviewer must differ from the submission executor")
    if session.get(TaskReview, (project_id, review_id)) is not None:
        raise TaskProjectionError("review_exists", "review_id already exists")
    _upsert_agent(session, project_id, event.actor_id, event.occurred_at)
    session.add(
        TaskReview(
            project_id=project_id,
            review_id=review_id,
            task_id=task.task_id,
            submission_id=submission_id,
            reviewer=event.actor_id,
            decision=decision,
            summary=summary,
            created_at=event.occurred_at,
        )
    )
    if decision == "approved":
        _advance(task, event, event.occurred_at, state="done")
        return
    attempt.status = "active"
    attempt.updated_at = event.occurred_at
    _advance(task, event, event.occurred_at, state="active")


def _apply_cancel(session: Session, project_id: str, task: Task, event: TaskEventPayload) -> None:
    _check_version(task, event)
    if task.state in TERMINAL_STATES:
        raise TaskProjectionError("invalid_task_state", f"cannot cancel a {task.state} task")
    _payload_text(event.payload, "reason")
    if task.current_attempt_id:
        attempt = _current_attempt(session, project_id, task)
        if attempt.status in {"offered", "active"}:
            attempt.status = "superseded"
            attempt.updated_at = event.occurred_at
    _advance(task, event, event.occurred_at, state="cancelled")


def _apply_event(session: Session, project_id: str, event: TaskEventPayload) -> Task:
    if event.event_type == "TaskCreated":
        return _apply_created(session, project_id, event)
    task = _require_task(session, project_id, event.task_id)
    if event.event_type == "TaskAssigned":
        _apply_assignment(session, project_id, task, event, reassign=False)
    elif event.event_type == "TaskReassigned":
        _apply_assignment(session, project_id, task, event, reassign=True)
    elif event.event_type == "TaskClaimed":
        _apply_claim(session, project_id, task, event)
    elif event.event_type == "TaskDeclined":
        _apply_decline(session, project_id, task, event)
    elif event.event_type == "TaskReported":
        _apply_report(session, project_id, task, event)
    elif event.event_type == "TaskBlocked":
        _apply_block(session, project_id, task, event)
    elif event.event_type == "TaskResumed":
        _apply_resume(session, project_id, task, event)
    elif event.event_type == "TaskSubmitted":
        _apply_submit(session, project_id, task, event)
    elif event.event_type == "TaskReviewed":
        _apply_review(session, project_id, task, event)
    elif event.event_type == "TaskCancelled":
        _apply_cancel(session, project_id, task, event)
    else:
        raise TaskProjectionError("invalid_task_event", f"unsupported event type: {event.event_type}")
    if task.version != event.task_version or task.assignment_epoch != event.assignment_epoch:
        raise TaskProjectionError("task_projection_mismatch", "task event result does not match its projected version or assignment epoch")
    return task


def _upsert_graph_node(
    session: Session,
    project_id: str,
    node_type: str,
    node_key: str,
    name: str,
    metadata: dict[str, object],
    timestamp: datetime,
) -> UUID:
    identifier = _node_id(project_id, node_type, node_key)
    node = session.get(GraphNode, identifier)
    if node is None:
        node = GraphNode(
            id=identifier,
            project_id=project_id,
            node_type=node_type,
            node_key=node_key,
            name=name,
            metadata_json=metadata,
            updated_at=timestamp,
        )
        session.add(node)
    else:
        node.name = name
        node.metadata_json = metadata
        node.updated_at = timestamp
    return identifier


def _upsert_graph_edge(
    session: Session,
    project_id: str,
    source_id: UUID,
    target_id: UUID,
    relation_type: str,
    timestamp: datetime,
    source_event_id: UUID | None,
) -> None:
    identifier = _edge_id(project_id, source_id, target_id, relation_type)
    edge = session.get(GraphEdge, identifier)
    source_ids = [str(item) for item in (edge.source_event_ids or [])] if edge is not None else []
    if source_event_id is not None:
        source_ids = list(dict.fromkeys([*source_ids, str(source_event_id)]))[-MAX_GRAPH_IDS:]
    if edge is None:
        edge = GraphEdge(
            id=identifier,
            project_id=project_id,
            source_node_id=source_id,
            target_node_id=target_id,
            relation_type=relation_type,
            confidence=1.0,
            source_event_ids=source_ids,
            evidence_ids=[],
            updated_at=timestamp,
        )
        session.add(edge)
    else:
        edge.source_event_ids = source_ids
        edge.updated_at = timestamp


def project_task_graph(
    session: Session,
    project_id: str,
    task_id: str,
    *,
    source_event_id: UUID | None = None,
) -> None:
    task = _require_task(session, project_id, task_id)
    timestamp = _now()
    task_node = _upsert_graph_node(
        session,
        project_id,
        "task",
        task.task_id,
        task.title,
        {
            "title": task.title,
            "objective": task.objective,
            "acceptance": task.acceptance,
            "priority": task.priority,
            "state": task.state,
            "version": task.version,
            "assignment_epoch": task.assignment_epoch,
            "current_attempt": task.current_attempt_id,
        },
        timestamp,
    )
    created = session.scalar(
        select(TaskEvent)
        .where(TaskEvent.project_id == project_id, TaskEvent.task_id == task_id, TaskEvent.event_type == "TaskCreated")
        .order_by(TaskEvent.task_event_seq)
        .limit(1)
    )
    created_payload = created.payload_json if created is not None and isinstance(created.payload_json, dict) else {}
    dependencies = _payload_list(created_payload, "depends_on") if created_payload else []
    parent_task_id = _payload_text(created_payload, "parent_task_id", maximum=256) if created_payload else ""
    produced_memory = _payload_list(created_payload, "produced_memory") if created_payload else []
    dependency_nodes = [
        _upsert_graph_node(session, project_id, "task", dependency, dependency, {"external": True}, timestamp)
        for dependency in dependencies
    ]
    parent_node = (
        _upsert_graph_node(session, project_id, "task", parent_task_id, parent_task_id, {"external": True}, timestamp)
        if parent_task_id
        else None
    )
    asset_nodes = [
        _upsert_graph_node(session, project_id, "asset", asset, asset, {}, timestamp)
        for asset in produced_memory
    ]
    attempts = list(
        session.scalars(
            select(TaskAttempt)
            .where(TaskAttempt.project_id == project_id, TaskAttempt.task_id == task_id)
            .order_by(TaskAttempt.epoch, TaskAttempt.created_at, TaskAttempt.attempt_id)
        )
    )
    submissions = list(
        session.scalars(
            select(TaskSubmission)
            .where(TaskSubmission.project_id == project_id, TaskSubmission.task_id == task_id)
            .order_by(TaskSubmission.created_at, TaskSubmission.submission_id)
        )
    )
    reviews = list(
        session.scalars(
            select(TaskReview)
            .where(TaskReview.project_id == project_id, TaskReview.task_id == task_id)
            .order_by(TaskReview.created_at, TaskReview.review_id)
        )
    )
    agent_ids = {attempt.assignee for attempt in attempts} | {attempt.assigned_by for attempt in attempts} | {review.reviewer for review in reviews}
    agent_nodes: dict[str, UUID] = {}
    for agent_id in sorted(agent_ids):
        agent = session.get(TaskAgent, (project_id, agent_id))
        if agent is None:
            agent = _upsert_agent(session, project_id, agent_id, timestamp)
        agent_nodes[agent_id] = _upsert_graph_node(
            session,
            project_id,
            "agent",
            agent_id,
            agent_id,
            {
                "role": agent.role,
                "capabilities": agent.capabilities_json or [],
                "owner": agent.owner,
                "status": agent.status,
            },
            timestamp,
        )
    attempt_nodes: dict[str, UUID] = {}
    for attempt in attempts:
        attempt_nodes[attempt.attempt_id] = _upsert_graph_node(
            session,
            project_id,
            "attempt",
            attempt.attempt_id,
            attempt.attempt_id,
            {
                "task_id": attempt.task_id,
                "epoch": attempt.epoch,
                "assignee": attempt.assignee,
                "assigned_by": attempt.assigned_by,
                "status": attempt.status,
                "created_at": attempt.created_at.isoformat(),
            },
            timestamp,
        )
    submission_nodes: dict[str, UUID] = {}
    for submission in submissions:
        submission_nodes[submission.submission_id] = _upsert_graph_node(
            session,
            project_id,
            "submission",
            submission.submission_id,
            submission.submission_id,
            {
                "task_id": submission.task_id,
                "attempt_id": submission.attempt_id,
                "summary": submission.summary,
                "evidence": submission.evidence_json or [],
                "version": submission.task_version,
                "created_at": submission.created_at.isoformat(),
            },
            timestamp,
        )
    review_nodes: dict[str, UUID] = {}
    for review in reviews:
        review_nodes[review.review_id] = _upsert_graph_node(
            session,
            project_id,
            "review",
            review.review_id,
            review.review_id,
            {
                "task_id": review.task_id,
                "submission_id": review.submission_id,
                "reviewer": review.reviewer,
                "decision": review.decision,
                "summary": review.summary,
                "created_at": review.created_at.isoformat(),
            },
            timestamp,
        )
    session.flush()
    session.execute(
        delete(GraphEdge).where(
            GraphEdge.project_id == project_id,
            GraphEdge.source_node_id == task_node,
            GraphEdge.relation_type == "current_attempt",
        )
    )
    for dependency_node in dependency_nodes:
        _upsert_graph_edge(session, project_id, task_node, dependency_node, "depends_on", timestamp, source_event_id)
    if parent_node is not None:
        _upsert_graph_edge(session, project_id, parent_node, task_node, "parent_of", timestamp, source_event_id)
    for asset_node in asset_nodes:
        _upsert_graph_edge(session, project_id, task_node, asset_node, "produced_memory", timestamp, source_event_id)
    for attempt in attempts:
        attempt_node = attempt_nodes[attempt.attempt_id]
        if task.current_attempt_id == attempt.attempt_id:
            _upsert_graph_edge(session, project_id, task_node, attempt_node, "current_attempt", timestamp, source_event_id)
        _upsert_graph_edge(session, project_id, attempt_node, agent_nodes[attempt.assignee], "assigned_to", timestamp, source_event_id)
        _upsert_graph_edge(session, project_id, attempt_node, agent_nodes[attempt.assigned_by], "assigned_by", timestamp, source_event_id)
    for submission in submissions:
        attempt_node = attempt_nodes.get(submission.attempt_id)
        if attempt_node is not None:
            _upsert_graph_edge(session, project_id, attempt_node, submission_nodes[submission.submission_id], "has_submission", timestamp, source_event_id)
    for review in reviews:
        submission_node = submission_nodes.get(review.submission_id)
        if submission_node is not None:
            _upsert_graph_edge(session, project_id, submission_node, review_nodes[review.review_id], "has_review", timestamp, source_event_id)
            _upsert_graph_edge(session, project_id, review_nodes[review.review_id], agent_nodes[review.reviewer], "reviewed_by", timestamp, source_event_id)


def project_task_event(
    session: Session,
    project_id: str,
    source_event: MemoryEvent,
    event: TaskEventPayload,
) -> str:
    """Apply a single validated task event or raise a structured projection error."""

    existing = session.scalar(
        select(TaskEvent).where(TaskEvent.project_id == project_id, TaskEvent.command_id == event.command_id)
    )
    if existing is not None:
        if (
            existing.task_id == event.task_id
            and existing.event_type == event.event_type
            and existing.actor_id == event.actor_id
            and existing.expected_version == event.expected_version
            and existing.expected_assignment_epoch == event.expected_assignment_epoch
            and existing.task_version == event.task_version
            and existing.assignment_epoch == event.assignment_epoch
            and _json(existing.payload_json or {}) == _json(event.payload)
        ):
            return "duplicate"
        raise TaskProjectionError("task_command_conflict", "command_id already exists with different task event content")
    task = _apply_event(session, project_id, event)
    session.add(
        TaskEvent(
            project_id=project_id,
            source_event_id=source_event.event_id,
            command_id=event.command_id,
            task_id=event.task_id,
            event_type=event.event_type,
            actor_id=event.actor_id,
            expected_version=event.expected_version,
            expected_assignment_epoch=event.expected_assignment_epoch,
            task_version=event.task_version,
            assignment_epoch=event.assignment_epoch,
            payload_json=dict(event.payload),
            occurred_at=event.occurred_at,
        )
    )
    session.flush()
    project_task_graph(session, project_id, task.task_id, source_event_id=source_event.event_id)
    return "applied"


def rebuild_task_graph(session: Session, project_id: str) -> None:
    """Recreate task Graph nodes and edges from the authoritative projections."""

    task_ids = list(session.scalars(select(Task.task_id).where(Task.project_id == project_id).order_by(Task.task_id)))
    for task_id in task_ids:
        project_task_graph(session, project_id, task_id)
    session.flush()


def task_graph_bundle(
    session: Session,
    project_id: str,
    *,
    task_id: str | None = None,
    agent_id: str | None = None,
    max_nodes: int = 200,
    max_edges: int = 400,
) -> dict[str, object]:
    task_query = select(Task).where(Task.project_id == project_id)
    if agent_id:
        task_query = task_query.join(
            TaskAttempt,
            (TaskAttempt.project_id == Task.project_id)
            & (TaskAttempt.task_id == Task.task_id)
            & (TaskAttempt.attempt_id == Task.current_attempt_id),
        ).where(TaskAttempt.assignee == agent_id)
    if task_id:
        task_query = task_query.where(Task.task_id == task_id).limit(1)
    else:
        task_query = task_query.limit(max(1, min(200, max_nodes)))
    task_query = task_query.order_by(Task.updated_at.desc(), Task.task_id)
    tasks = list(session.scalars(task_query))
    task_ids = [task.task_id for task in tasks]
    attempts = list(session.scalars(select(TaskAttempt).where(TaskAttempt.project_id == project_id, TaskAttempt.task_id.in_(task_ids)))) if task_ids else []
    submissions = list(session.scalars(select(TaskSubmission).where(TaskSubmission.project_id == project_id, TaskSubmission.task_id.in_(task_ids)))) if task_ids else []
    reviews = list(session.scalars(select(TaskReview).where(TaskReview.project_id == project_id, TaskReview.task_id.in_(task_ids)))) if task_ids else []
    node_ids: set[UUID] = {_node_id(project_id, "task", task.task_id) for task in tasks}
    node_ids.update(_node_id(project_id, "attempt", attempt.attempt_id) for attempt in attempts)
    node_ids.update(_node_id(project_id, "submission", submission.submission_id) for submission in submissions)
    node_ids.update(_node_id(project_id, "review", review.review_id) for review in reviews)
    node_ids.update(_node_id(project_id, "agent", attempt.assignee) for attempt in attempts)
    node_ids.update(_node_id(project_id, "agent", attempt.assigned_by) for attempt in attempts)
    node_ids.update(_node_id(project_id, "agent", review.reviewer) for review in reviews)
    task_node_ids = [_node_id(project_id, "task", task.task_id) for task in tasks]
    if task_node_ids:
        related_edges = list(
            session.scalars(
                select(GraphEdge).where(
                    GraphEdge.project_id == project_id,
                    (GraphEdge.source_node_id.in_(task_node_ids) | GraphEdge.target_node_id.in_(task_node_ids)),
                )
            )
        )
        node_ids.update(edge.source_node_id for edge in related_edges)
        node_ids.update(edge.target_node_id for edge in related_edges)
    selected = sorted(node_ids, key=str)[: max(1, min(200, max_nodes))]
    nodes = list(session.scalars(select(GraphNode).where(GraphNode.project_id == project_id, GraphNode.id.in_(selected)).order_by(GraphNode.node_type, GraphNode.node_key))) if selected else []
    selected_ids = {node.id for node in nodes}
    edges = list(
        session.scalars(
            select(GraphEdge)
            .where(GraphEdge.project_id == project_id, GraphEdge.source_node_id.in_(selected_ids), GraphEdge.target_node_id.in_(selected_ids))
            .order_by(GraphEdge.relation_type, GraphEdge.source_node_id, GraphEdge.target_node_id)
            .limit(max(1, min(400, max_edges)))
        )
    ) if selected_ids else []
    current_attempts = {attempt.attempt_id: attempt for attempt in attempts}
    roots: dict[str, list[str]] = {"current": [], "assigned": [], "review": [], "attention": []}
    for task in tasks:
        attempt = current_attempts.get(task.current_attempt_id or "")
        identifier = str(_node_id(project_id, "task", task.task_id))
        if task.state == "active" and (not agent_id or (attempt is not None and attempt.assignee == agent_id)):
            roots["current"].append(identifier)
        if attempt is not None and attempt.status in {"offered", "active"} and (not agent_id or attempt.assignee == agent_id):
            roots["assigned"].append(identifier)
        if task.state == "review":
            roots["review"].append(identifier)
        if task.state in {"open", "blocked"}:
            roots["attention"].append(identifier)
    latest = int(session.scalar(select(func.coalesce(func.max(TaskEvent.task_event_seq), 0)).where(TaskEvent.project_id == project_id)) or 0)
    return {
        "version": "1.0",
        "roots": roots,
        "nodes": [
            {"id": str(node.id), "type": node.node_type, "key": node.node_key, "name": node.name, "metadata": node.metadata_json or {}}
            for node in nodes
        ],
        "edges": [
            {"source": str(edge.source_node_id), "target": str(edge.target_node_id), "relation_type": edge.relation_type, "metadata": {}}
            for edge in edges
        ],
        "cursor": str(latest),
    }


def task_catalog(
    session: Session,
    project_id: str,
    *,
    state: str = "working",
    search: str = "",
    agent: str = "",
    cursor: str | None = None,
    limit: int = 40,
) -> dict[str, object]:
    """Return a compact, cursor-paginated task index independent of GraphNode limits."""

    normalized_state = state.strip().lower()
    if normalized_state not in TASK_LIST_STATES:
        raise ValueError("invalid task state")
    normalized_search = search.strip()
    normalized_agent = agent.strip()

    query = select(Task).where(Task.project_id == project_id)
    if normalized_state == "working":
        query = query.where(~Task.state.in_(TERMINAL_STATES))
    elif normalized_state != "all":
        query = query.where(Task.state == normalized_state)
    if normalized_search:
        pattern = f"%{normalized_search}%"
        query = query.where(
            or_(
                Task.task_id.ilike(pattern),
                Task.title.ilike(pattern),
                Task.objective.ilike(pattern),
                Task.acceptance.ilike(pattern),
            )
        )
    if normalized_agent:
        query = query.join(
            TaskAttempt,
            (TaskAttempt.project_id == Task.project_id)
            & (TaskAttempt.task_id == Task.task_id)
            & (TaskAttempt.attempt_id == Task.current_attempt_id),
        ).where(TaskAttempt.assignee.ilike(f"%{normalized_agent}%"))

    total = int(session.scalar(select(func.count()).select_from(query.subquery())) or 0)
    if cursor:
        cursor_updated_at, cursor_task_id = _decode_task_cursor(cursor)
        query = query.where(
            or_(
                Task.updated_at < cursor_updated_at,
                and_(Task.updated_at == cursor_updated_at, Task.task_id > cursor_task_id),
            )
        )

    page_size = max(1, min(100, limit))
    rows = list(session.scalars(query.order_by(Task.updated_at.desc(), Task.task_id).limit(page_size + 1)))
    page = rows[:page_size]
    task_ids = [task.task_id for task in page]
    current_attempt_ids = [task.current_attempt_id for task in page if task.current_attempt_id]
    current_attempts = {
        attempt.attempt_id: attempt
        for attempt in session.scalars(
            select(TaskAttempt).where(
                TaskAttempt.project_id == project_id,
                TaskAttempt.attempt_id.in_(current_attempt_ids),
            )
        )
    } if current_attempt_ids else {}
    submissions = list(
        session.scalars(
            select(TaskSubmission).where(
                TaskSubmission.project_id == project_id,
                TaskSubmission.task_id.in_(task_ids),
            )
        )
    ) if task_ids else []
    reviews = list(
        session.scalars(
            select(TaskReview).where(
                TaskReview.project_id == project_id,
                TaskReview.task_id.in_(task_ids),
            )
        )
    ) if task_ids else []
    latest_submissions: dict[str, TaskSubmission] = {}
    for submission in submissions:
        previous = latest_submissions.get(submission.task_id)
        if previous is None or submission.created_at > previous.created_at:
            latest_submissions[submission.task_id] = submission
    latest_reviews: dict[str, TaskReview] = {}
    for review in reviews:
        previous = latest_reviews.get(review.task_id)
        if previous is None or review.created_at > previous.created_at:
            latest_reviews[review.task_id] = review
    state_counts = {
        task_state: int(count)
        for task_state, count in session.execute(
            select(Task.state, func.count()).where(Task.project_id == project_id).group_by(Task.state)
        )
    }
    agent_loads = {
        assignee: int(count)
        for assignee, count in session.execute(
            select(TaskAttempt.assignee, func.count())
            .join(
                Task,
                (Task.project_id == TaskAttempt.project_id)
                & (Task.task_id == TaskAttempt.task_id)
                & (Task.current_attempt_id == TaskAttempt.attempt_id),
            )
            .where(TaskAttempt.project_id == project_id, ~Task.state.in_(TERMINAL_STATES))
            .group_by(TaskAttempt.assignee)
        )
    }

    items: list[dict[str, object]] = []
    for task in page:
        attempt = current_attempts.get(task.current_attempt_id or "")
        submission = latest_submissions.get(task.task_id)
        review = latest_reviews.get(task.task_id)
        items.append(
            {
                "task_id": task.task_id,
                "title": task.title,
                "objective": task.objective,
                "acceptance": task.acceptance,
                "priority": task.priority,
                "state": task.state,
                "version": task.version,
                "assignment_epoch": task.assignment_epoch,
                "created_at": task.created_at.isoformat(),
                "updated_at": task.updated_at.isoformat(),
                "current_attempt": None if attempt is None else {
                    "attempt_id": attempt.attempt_id,
                    "assignee": attempt.assignee,
                    "assigned_by": attempt.assigned_by,
                    "status": attempt.status,
                    "epoch": attempt.epoch,
                    "updated_at": attempt.updated_at.isoformat(),
                },
                "latest_submission": None if submission is None else {
                    "submission_id": submission.submission_id,
                    "summary": submission.summary,
                    "created_at": submission.created_at.isoformat(),
                },
                "latest_review": None if review is None else {
                    "review_id": review.review_id,
                    "reviewer": review.reviewer,
                    "decision": review.decision,
                    "summary": review.summary,
                    "created_at": review.created_at.isoformat(),
                },
            }
        )
    return {
        "items": items,
        "total": total,
        "state_counts": state_counts,
        "agent_loads": agent_loads,
        "next_cursor": _encode_task_cursor(page[-1]) if len(rows) > page_size else None,
        "page_size": page_size,
    }


def task_history(
    session: Session,
    project_id: str,
    *,
    task_id: str | None = None,
    after_seq: int = 0,
    max_items: int = 50,
) -> dict[str, object]:
    query = select(TaskEvent).where(TaskEvent.project_id == project_id, TaskEvent.task_event_seq > max(0, after_seq))
    if task_id:
        query = query.where(TaskEvent.task_id == task_id)
    events = list(session.scalars(query.order_by(TaskEvent.task_event_seq).limit(max(1, min(MAX_HISTORY_ITEMS, max_items)))))
    return {
        "events": [
            {
                "seq": event.task_event_seq,
                "command_id": event.command_id,
                "task_id": event.task_id,
                "event_type": event.event_type,
                "actor_id": event.actor_id,
                "expected_version": event.expected_version,
                "expected_assignment_epoch": event.expected_assignment_epoch,
                "task_version": event.task_version,
                "assignment_epoch": event.assignment_epoch,
                "payload": event.payload_json or {},
                "occurred_at": event.occurred_at.isoformat(),
            }
            for event in events
        ],
        "cursor": str(events[-1].task_event_seq if events else max(0, after_seq)),
    }


__all__ = [
    "TaskProjectionError",
    "project_task_event",
    "project_task_graph",
    "rebuild_task_graph",
    "task_catalog",
    "task_graph_bundle",
    "task_history",
]