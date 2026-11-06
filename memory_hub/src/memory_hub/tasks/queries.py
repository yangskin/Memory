"""任务读取：目录、详情与时间线。所有身份字段都来自服务端推导。"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from memory_hub.db.models import Task, TaskAttempt, TaskEvent, TaskLink, TaskReview, TaskSubmission

MAX_HISTORY_ITEMS = 200
DETAIL_ITEMS = 50
DETAIL_MAX_BYTES = 1024 * 1024
TERMINAL_STATES = frozenset({"done", "cancelled"})
TASK_STATES = frozenset({"open", "active", "blocked", "review", "done", "cancelled"})
CATALOG_ORDERS = frozenset({"updated", "created"})


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def attempt_view(attempt: TaskAttempt | None) -> dict[str, Any] | None:
    if attempt is None:
        return None
    return {
        "attempt_id": attempt.attempt_id,
        "epoch": attempt.epoch,
        "assignee": attempt.assignee,
        "assigned_by": attempt.assigned_by,
        "status": attempt.status,
        "created_at": _iso(attempt.created_at),
        "updated_at": _iso(attempt.updated_at),
    }


def current_attempt(session: Session, task: Task) -> TaskAttempt | None:
    if not task.current_attempt_id:
        return None
    attempt = session.get(TaskAttempt, (task.project_id, task.current_attempt_id))
    return attempt if attempt is not None and attempt.task_id == task.task_id else None


def task_summary(session: Session, task: Task) -> dict[str, Any]:
    """冲突响应里的当前任务摘要，足以让调用方判断是否仍是执行者。"""
    return {
        "task_id": task.task_id,
        "state": task.state,
        "version": task.version,
        "assignment_epoch": task.assignment_epoch,
        "current_attempt": attempt_view(current_attempt(session, task)),
    }


def event_view(event: TaskEvent) -> dict[str, Any]:
    return {
        "seq": event.task_event_seq,
        "task_seq": event.task_seq,
        "command_id": event.command_id,
        "task_id": event.task_id,
        "event_type": event.event_type,
        "actor_id": event.actor_id,
        "agent": event.agent,
        "expected_version": event.expected_version,
        "expected_assignment_epoch": event.expected_assignment_epoch,
        "task_version": event.task_version,
        "assignment_epoch": event.assignment_epoch,
        "payload": event.payload_json or {},
        "occurred_at": _iso(event.occurred_at),
    }


def _links(session: Session, project_id: str, task_id: str) -> dict[str, Any]:
    rows = list(session.scalars(
        select(TaskLink).where(TaskLink.project_id == project_id, TaskLink.task_id == task_id).order_by(TaskLink.relation, TaskLink.created_at, TaskLink.target)
    ))
    return {
        "depends_on": [row.target for row in rows if row.relation == "depends_on"],
        "parent_task_id": next((row.target for row in rows if row.relation == "parent"), None),
        "produced_memory": [row.target for row in rows if row.relation == "produced_memory"],
    }


def _newest(session: Session, query, order_columns, total_query) -> tuple[list[Any], int]:
    total = int(session.scalar(total_query) or 0)
    rows = list(session.scalars(query.order_by(*[column.desc() for column in order_columns]).limit(DETAIL_ITEMS)))
    rows.reverse()
    return rows, total


def task_detail(session: Session, project_id: str, task_id: str) -> dict[str, Any] | None:
    task = session.get(Task, (project_id, task_id))
    if task is None:
        return None
    scope = {"project_id": project_id, "task_id": task_id}

    def count(model):
        return select(func.count()).select_from(model).where(model.project_id == project_id, model.task_id == task_id)

    attempts, attempts_total = _newest(
        session, select(TaskAttempt).filter_by(**scope), (TaskAttempt.epoch, TaskAttempt.created_at), count(TaskAttempt))
    submissions, submissions_total = _newest(
        session, select(TaskSubmission).filter_by(**scope), (TaskSubmission.created_at, TaskSubmission.submission_id), count(TaskSubmission))
    reviews, reviews_total = _newest(
        session, select(TaskReview).filter_by(**scope), (TaskReview.created_at, TaskReview.review_id), count(TaskReview))
    events, events_total = _newest(
        session, select(TaskEvent).filter_by(**scope), (TaskEvent.task_seq,), count(TaskEvent))
    # 提交/审核记录本身不存 agent 标签；从对应事件取回，标签只作展示。
    agents: dict[str, str | None] = {}
    for event in session.scalars(select(TaskEvent).filter_by(**scope).where(TaskEvent.event_type.in_(("TaskSubmitted", "TaskReviewed")))):
        payload = event.payload_json or {}
        key = payload.get("submission_id") if event.event_type == "TaskSubmitted" else payload.get("review_id")
        if isinstance(key, str):
            agents[key] = event.agent
    attempt_assignee = {attempt.attempt_id: attempt.assignee for attempt in session.scalars(select(TaskAttempt).filter_by(**scope))}
    detail: dict[str, Any] = {
        "task_id": task.task_id,
        "title": task.title,
        "objective": task.objective,
        "acceptance": task.acceptance,
        "priority": task.priority,
        "state": task.state,
        "version": task.version,
        "assignment_epoch": task.assignment_epoch,
        "created_at": _iso(task.created_at),
        "updated_at": _iso(task.updated_at),
        "last_activity_at": _iso(task.last_activity_at or task.updated_at),
        "current_attempt": attempt_view(current_attempt(session, task)),
        "links": _links(session, project_id, task_id),
        "attempts": [attempt_view(item) for item in attempts],
        "attempts_total": attempts_total,
        "submissions": [
            {
                "submission_id": item.submission_id,
                "attempt_id": item.attempt_id,
                "executor": attempt_assignee.get(item.attempt_id),
                "agent": agents.get(item.submission_id),
                "summary": item.summary,
                "evidence": list(item.evidence_json or []),
                "task_version": item.task_version,
                "created_at": _iso(item.created_at),
            }
            for item in submissions
        ],
        "submissions_total": submissions_total,
        "reviews": [
            {
                "review_id": item.review_id,
                "submission_id": item.submission_id,
                "reviewer": item.reviewer,
                "agent": agents.get(item.review_id),
                "decision": item.decision,
                "summary": item.summary,
                "created_at": _iso(item.created_at),
            }
            for item in reviews
        ],
        "reviews_total": reviews_total,
        "recent_events": [event_view(item) for item in events],
        "events_total": events_total,
    }
    _bound_detail(detail)
    return detail


def _bound_detail(detail: dict[str, Any]) -> None:
    """超过 1 MiB 时从最旧条目开始裁剪；最新提交与当前尝试优先保留。"""
    lists = ("recent_events", "submissions", "reviews", "attempts")
    while len(json.dumps(detail, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > DETAIL_MAX_BYTES:
        candidates = [name for name in lists if len(detail[name]) > 1]
        if not candidates:
            break
        largest = max(candidates, key=lambda name: len(json.dumps(detail[name], ensure_ascii=False)))
        detail[largest].pop(0)
    detail["truncated"] = any(len(detail[name]) < detail[total] for name, total in (
        ("attempts", "attempts_total"), ("submissions", "submissions_total"),
        ("reviews", "reviews_total"), ("recent_events", "events_total")))


def _encode_cursor(order: str, task: Task) -> str:
    key = task.updated_at if order == "updated" else task.created_at
    payload = json.dumps({"o": order, "k": key.isoformat(), "t": task.task_id}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str, order: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        key = datetime.fromisoformat(str(value["k"]))
        task_id = str(value["t"])
        cursor_order = value["o"]
    except (binascii.Error, KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ValueError("invalid task cursor") from exc
    if not task_id or cursor_order != order:
        raise ValueError("invalid task cursor")
    return key, task_id


def _states(state: str) -> tuple[str, frozenset[str] | None]:
    """返回 (模式, 状态集合)；all 返回 None 表示不过滤。"""
    names = [item.strip().lower() for item in state.split(",") if item.strip()]
    if not names:
        raise ValueError("invalid task state")
    if names == ["all"]:
        return "all", None
    if names == ["working"]:
        return "working", TASK_STATES - TERMINAL_STATES
    if any(name not in TASK_STATES for name in names):
        raise ValueError("invalid task state")
    return "set", frozenset(names)


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def task_catalog(
    session: Session,
    project_id: str,
    *,
    state: str = "working",
    search: str = "",
    assignee: str = "",
    order: str = "updated",
    cursor: str | None = None,
    limit: int = 40,
) -> dict[str, object]:
    if order not in CATALOG_ORDERS:
        raise ValueError("invalid task order")
    _, states = _states(state)
    query = select(Task).where(Task.project_id == project_id)
    if states is not None:
        query = query.where(Task.state.in_(sorted(states)))
    normalized_search = search.strip()
    if normalized_search:
        pattern = "%" + _escape_like(normalized_search) + "%"
        query = query.where(or_(
            Task.task_id.ilike(pattern, escape="\\"),
            Task.title.ilike(pattern, escape="\\"),
            Task.objective.ilike(pattern, escape="\\"),
            Task.acceptance.ilike(pattern, escape="\\"),
        ))
    normalized_assignee = assignee.strip()
    if normalized_assignee:
        query = query.join(
            TaskAttempt,
            (TaskAttempt.project_id == Task.project_id)
            & (TaskAttempt.task_id == Task.task_id)
            & (TaskAttempt.attempt_id == Task.current_attempt_id),
        ).where(TaskAttempt.assignee == normalized_assignee)

    total = int(session.scalar(select(func.count()).select_from(query.subquery())) or 0)
    if order == "updated":
        if cursor:
            key, cursor_task = _decode_cursor(cursor, order)
            query = query.where(or_(Task.updated_at < key, and_(Task.updated_at == key, Task.task_id > cursor_task)))
        query = query.order_by(Task.updated_at.desc(), Task.task_id)
    else:
        if cursor:
            key, cursor_task = _decode_cursor(cursor, order)
            query = query.where(or_(Task.created_at > key, and_(Task.created_at == key, Task.task_id > cursor_task)))
        query = query.order_by(Task.created_at, Task.task_id)

    page_size = max(1, min(100, limit))
    rows = list(session.scalars(query.limit(page_size + 1)))
    page = rows[:page_size]
    task_ids = [task.task_id for task in page]
    attempt_ids = [task.current_attempt_id for task in page if task.current_attempt_id]
    attempts = {
        attempt.attempt_id: attempt
        for attempt in session.scalars(select(TaskAttempt).where(TaskAttempt.project_id == project_id, TaskAttempt.attempt_id.in_(attempt_ids)))
    } if attempt_ids else {}
    latest_submissions: dict[str, TaskSubmission] = {}
    latest_reviews: dict[str, TaskReview] = {}
    if task_ids:
        for submission in session.scalars(select(TaskSubmission).where(TaskSubmission.project_id == project_id, TaskSubmission.task_id.in_(task_ids))):
            previous = latest_submissions.get(submission.task_id)
            if previous is None or (submission.created_at, submission.submission_id) > (previous.created_at, previous.submission_id):
                latest_submissions[submission.task_id] = submission
        for review in session.scalars(select(TaskReview).where(TaskReview.project_id == project_id, TaskReview.task_id.in_(task_ids))):
            previous = latest_reviews.get(review.task_id)
            if previous is None or (review.created_at, review.review_id) > (previous.created_at, previous.review_id):
                latest_reviews[review.task_id] = review
    state_counts = {
        task_state: int(count)
        for task_state, count in session.execute(
            select(Task.state, func.count()).where(Task.project_id == project_id).group_by(Task.state))
    }
    agent_loads = {
        name: int(count)
        for name, count in session.execute(
            select(TaskAttempt.assignee, func.count())
            .join(Task, (Task.project_id == TaskAttempt.project_id) & (Task.task_id == TaskAttempt.task_id) & (Task.current_attempt_id == TaskAttempt.attempt_id))
            .where(TaskAttempt.project_id == project_id, ~Task.state.in_(TERMINAL_STATES))
            .group_by(TaskAttempt.assignee))
    }
    items: list[dict[str, object]] = []
    for task in page:
        attempt = attempts.get(task.current_attempt_id or "")
        submission = latest_submissions.get(task.task_id)
        review = latest_reviews.get(task.task_id)
        items.append({
            "task_id": task.task_id,
            "title": task.title,
            "objective": task.objective,
            "acceptance": task.acceptance,
            "priority": task.priority,
            "state": task.state,
            "version": task.version,
            "assignment_epoch": task.assignment_epoch,
            "created_at": _iso(task.created_at),
            "updated_at": _iso(task.updated_at),
            "last_activity_at": _iso(task.last_activity_at or task.updated_at),
            "current_attempt": attempt_view(attempt) if attempt is not None and attempt.task_id == task.task_id else None,
            "latest_submission": None if submission is None else {
                "submission_id": submission.submission_id,
                "summary": submission.summary,
                "created_at": _iso(submission.created_at),
            },
            "latest_review": None if review is None else {
                "review_id": review.review_id,
                "reviewer": review.reviewer,
                "decision": review.decision,
                "summary": review.summary,
                "created_at": _iso(review.created_at),
            },
        })
    return {
        "items": items,
        "total": total,
        "state_counts": state_counts,
        "agent_loads": agent_loads,
        "order": order,
        "next_cursor": _encode_cursor(order, page[-1]) if len(rows) > page_size else None,
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
        "events": [event_view(event) for event in events],
        "cursor": str(events[-1].task_event_seq if events else max(0, after_seq)),
    }


__all__ = [
    "MAX_HISTORY_ITEMS",
    "TERMINAL_STATES",
    "attempt_view",
    "current_attempt",
    "event_view",
    "task_catalog",
    "task_detail",
    "task_history",
    "task_summary",
]
