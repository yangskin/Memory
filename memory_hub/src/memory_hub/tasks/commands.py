"""Hub 任务命令服务：唯一的任务状态机。

每条命令在一个事务内完成：幂等存档查找 → 锁定任务行 → 校验与状态转换 →
递增任务内序号写事件 → 写命令存档。客户端不提供任何派生值。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from memory_hub.db.models import Task, TaskAttempt, TaskCommand as TaskCommandRow, TaskEvent, TaskLink, TaskReview, TaskSubmission
from memory_hub.domain.tasks import TaskCommand, TaskCommandError
from memory_hub.tasks.queries import TERMINAL_STATES, current_attempt, task_detail, task_summary

EVENT_TYPES = {
    "create": "TaskCreated", "assign": "TaskAssigned", "reassign": "TaskReassigned",
    "claim": "TaskClaimed", "decline": "TaskDeclined", "report": "TaskReported",
    "block": "TaskBlocked", "resume": "TaskResumed", "submit": "TaskSubmitted",
    "review": "TaskReviewed", "cancel": "TaskCancelled",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return prefix + "_" + uuid4().hex[:24]


def request_hash(command: TaskCommand, actor: str) -> str:
    canonical = json.dumps({"actor": actor, **command.canonical()}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def actor_identity(device_name: str, channel: str | None) -> str:
    return device_name + ("-" + channel if channel else "")


def _conflict(session: Session, task: Task, code: str, message: str, status: int = 409) -> TaskCommandError:
    return TaskCommandError(status, code, message, current=task_summary(session, task))


def _check_version(session: Session, task: Task, command: TaskCommand) -> None:
    if command.expected_version is not None and command.expected_version != task.version:
        raise _conflict(session, task, "version_conflict", f"task version is {task.version}, expected {command.expected_version}")


def _check_epoch(session: Session, task: Task, command: TaskCommand) -> None:
    if command.expected_assignment_epoch is not None and command.expected_assignment_epoch != task.assignment_epoch:
        raise _conflict(session, task, "assignment_epoch_conflict",
                        f"task assignment epoch is {task.assignment_epoch}, expected {command.expected_assignment_epoch}")


def _require_state(session: Session, task: Task, allowed: set[str], action: str) -> None:
    if task.state not in allowed:
        raise _conflict(session, task, "invalid_task_state", f"cannot {action} a {task.state} task")


def _require_attempt(session: Session, task: Task, allowed: set[str], action: str) -> TaskAttempt:
    attempt = current_attempt(session, task)
    if attempt is None or attempt.status not in allowed:
        status = "none" if attempt is None else attempt.status
        raise _conflict(session, task, "invalid_attempt_state", f"cannot {action} while the current attempt is {status}")
    return attempt


def _require_executor(session: Session, task: Task, attempt: TaskAttempt, actor: str) -> None:
    if attempt.assignee != actor:
        raise _conflict(session, task, "assignment_forbidden", "only the current assignee may perform this action", status=403)


def _advance(task: Task, now: datetime, state: str | None = None) -> None:
    task.version += 1
    task.updated_at = now
    if state is not None:
        task.state = state


def _create(session: Session, project_id: str, command: TaskCommand, actor: str, now: datetime) -> tuple[Task, dict[str, Any]]:
    values = command.values
    task_id = command.task_id or _new_id("task")
    if session.get(Task, (project_id, task_id)) is not None:
        raise TaskCommandError(409, "task_exists", "task_id already exists", field="task_id")
    targets = list(values["depends_on"]) + ([values["parent_task_id"]] if values["parent_task_id"] else [])
    for target in targets:
        if target == task_id:
            raise TaskCommandError(422, "link_cycle", "a task cannot depend on or be the parent of itself",
                                   field="parent_task_id" if target == values["parent_task_id"] else "depends_on")
    for field_name, items in (("depends_on", values["depends_on"]), ("parent_task_id", [values["parent_task_id"]] if values["parent_task_id"] else [])):
        for target in items:
            if session.get(Task, (project_id, target)) is None:
                raise TaskCommandError(422, "link_target_not_found", f"linked task {target} does not exist", field=field_name)
    task = Task(project_id=project_id, task_id=task_id, title=values["title"], objective=values["objective"],
                acceptance=values["acceptance"], priority=values["priority"], state="open", version=1,
                assignment_epoch=0, current_attempt_id=None, last_task_seq=0, created_at=now, updated_at=now,
                last_activity_at=now)
    session.add(task)
    session.flush()
    for relation, items in (("depends_on", values["depends_on"]), ("produced_memory", values["produced_memory"]),
                            ("parent", [values["parent_task_id"]] if values["parent_task_id"] else [])):
        for target in items:
            session.add(TaskLink(project_id=project_id, task_id=task_id, relation=relation, target=target, created_at=now))
    payload = {key: values[key] for key in ("title", "objective", "acceptance", "priority", "depends_on", "parent_task_id", "produced_memory")}
    return task, payload


def _assign(session: Session, task: Task, command: TaskCommand, actor: str, now: datetime) -> dict[str, Any]:
    reassign = command.action == "reassign"
    _check_version(session, task, command)
    _check_epoch(session, task, command)
    attempt = current_attempt(session, task)
    if reassign:
        _require_state(session, task, {"open", "active", "blocked"}, "reassign")
        previous = _require_attempt(session, task, {"offered", "active"}, "reassign")
        previous.status = "superseded"
        previous.updated_at = now
    else:
        if task.state in TERMINAL_STATES or task.state == "review":
            raise _conflict(session, task, "invalid_task_state", f"cannot assign a {task.state} task")
        if attempt is not None and attempt.status in {"offered", "active"}:
            raise _conflict(session, task, "assignment_in_progress", "the task already has an offered or active attempt; use reassign")
        _require_state(session, task, {"open"}, "assign")
    epoch = task.assignment_epoch + 1
    attempt_id = _new_id("attempt")
    assignee = command.values["assignee"]
    session.add(TaskAttempt(project_id=task.project_id, attempt_id=attempt_id, task_id=task.task_id, epoch=epoch,
                            assignee=assignee, assigned_by=actor, status="offered", created_at=now, updated_at=now))
    task.current_attempt_id = attempt_id
    task.assignment_epoch = epoch
    _advance(task, now, "open")
    return {"attempt_id": attempt_id, "assignee": assignee, "assigned_by": actor, "epoch": epoch}


def _executor(session: Session, task: Task, command: TaskCommand, actor: str, *, states: set[str], attempts: set[str]) -> TaskAttempt:
    _check_version(session, task, command)
    _check_epoch(session, task, command)
    _require_state(session, task, states, command.action)
    attempt = _require_attempt(session, task, attempts, command.action)
    _require_executor(session, task, attempt, actor)
    return attempt


def _apply(session: Session, task: Task, command: TaskCommand, actor: str, now: datetime) -> dict[str, Any]:
    action = command.action
    values = command.values
    if action in {"assign", "reassign"}:
        return _assign(session, task, command, actor, now)
    if action == "claim":
        attempt = _executor(session, task, command, actor, states={"open"}, attempts={"offered"})
        attempt.status, attempt.updated_at = "active", now
        _advance(task, now, "active")
        return {"attempt_id": attempt.attempt_id}
    if action == "decline":
        attempt = _executor(session, task, command, actor, states={"open", "active", "blocked"}, attempts={"offered", "active"})
        attempt.status, attempt.updated_at = "superseded", now
        task.current_attempt_id = None
        _advance(task, now, "open")
        return {"attempt_id": attempt.attempt_id}
    if action == "report":
        attempt = _executor(session, task, command, actor, states={"active", "blocked"}, attempts={"active"})
        return {"attempt_id": attempt.attempt_id, "summary": values["summary"]}
    if action == "block":
        attempt = _executor(session, task, command, actor, states={"active"}, attempts={"active"})
        _advance(task, now, "blocked")
        return {"attempt_id": attempt.attempt_id, "reason": values["reason"]}
    if action == "resume":
        attempt = _executor(session, task, command, actor, states={"blocked"}, attempts={"active"})
        _advance(task, now, "active")
        return {"attempt_id": attempt.attempt_id}
    if action == "submit":
        attempt = _executor(session, task, command, actor, states={"active"}, attempts={"active"})
        _advance(task, now, "review")
        submission_id = _new_id("submission")
        session.add(TaskSubmission(project_id=task.project_id, submission_id=submission_id, task_id=task.task_id,
                                   attempt_id=attempt.attempt_id, summary=values["summary"], evidence_json=values["evidence"],
                                   task_version=task.version, created_at=now))
        attempt.status, attempt.updated_at = "submitted", now
        return {"attempt_id": attempt.attempt_id, "submission_id": submission_id, "summary": values["summary"], "evidence": values["evidence"]}
    if action == "review":
        _check_version(session, task, command)
        _check_epoch(session, task, command)
        _require_state(session, task, {"review"}, "review")
        attempt = _require_attempt(session, task, {"submitted"}, "review")
        latest = session.scalar(
            select(TaskSubmission)
            .where(TaskSubmission.project_id == task.project_id, TaskSubmission.task_id == task.task_id,
                   TaskSubmission.attempt_id == attempt.attempt_id)
            .order_by(TaskSubmission.created_at.desc(), TaskSubmission.submission_id.desc())
            .limit(1))
        requested = values["submission_id"]
        if latest is None or (requested is not None and requested != latest.submission_id) or latest.task_version != task.version:
            raise _conflict(session, task, "submission_conflict", "review must target the latest submission of the current attempt")
        if attempt.assignee == actor:
            raise _conflict(session, task, "reviewer_conflict", "reviewer must differ from the submission executor")
        review_id = _new_id("review")
        session.add(TaskReview(project_id=task.project_id, review_id=review_id, task_id=task.task_id,
                               submission_id=latest.submission_id, reviewer=actor, decision=values["decision"],
                               summary=values["summary"], created_at=now))
        if values["decision"] == "approved":
            _advance(task, now, "done")
        else:
            attempt.status, attempt.updated_at = "active", now
            _advance(task, now, "active")
        return {"review_id": review_id, "submission_id": latest.submission_id, "decision": values["decision"], "summary": values["summary"]}
    if action == "cancel":
        _check_version(session, task, command)
        _check_epoch(session, task, command)
        if task.state in TERMINAL_STATES:
            raise _conflict(session, task, "invalid_task_state", f"cannot cancel a {task.state} task")
        attempt = current_attempt(session, task)
        if attempt is not None and attempt.status in {"offered", "active"}:
            attempt.status, attempt.updated_at = "superseded", now
        _advance(task, now, "cancelled")
        return {"reason": values["reason"] or None}
    raise TaskCommandError(422, "invalid_input", f"unsupported action {action}", field="action")


def _result(command: TaskCommand, task: Task, event: TaskEvent, payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": True,
        "command_id": command.command_id,
        "action": command.action,
        "task_id": task.task_id,
        "version": task.version,
        "assignment_epoch": task.assignment_epoch,
        "state": task.state,
        "actor": event.actor_id,
        "event": {"seq": event.task_event_seq, "task_seq": event.task_seq, "event_type": event.event_type},
    }
    for key in ("attempt_id", "submission_id", "review_id"):
        if key in payload:
            result[key] = payload[key]
    return result


def _replay(session: Session, project_id: str, command: TaskCommand, actor: str, digest: str) -> dict[str, Any] | None:
    stored = session.get(TaskCommandRow, (project_id, command.command_id))
    if stored is None:
        # 迁移前被接受的命令只有事件、没有存档：同任务、同类型、同执行者视为已接受，否则冲突。
        legacy = session.scalar(select(TaskEvent).where(TaskEvent.project_id == project_id, TaskEvent.command_id == command.command_id))
        if legacy is None:
            return None
        if (legacy.task_id != (command.task_id or legacy.task_id) or legacy.event_type != EVENT_TYPES[command.action]
                or legacy.actor_id != actor):
            raise TaskCommandError(409, "command_id_conflict", "command_id was already accepted with different content", field="command_id")
        payload = legacy.payload_json or {}
        result = {"ok": True, "command_id": command.command_id, "action": command.action, "task_id": legacy.task_id,
                  "version": legacy.task_version, "assignment_epoch": legacy.assignment_epoch, "actor": legacy.actor_id,
                  "event": {"seq": legacy.task_event_seq, "task_seq": legacy.task_seq, "event_type": legacy.event_type},
                  **{key: payload[key] for key in ("attempt_id", "submission_id", "review_id") if key in payload}}
        return {**result, "idempotent": True, "task": task_detail(session, project_id, legacy.task_id)}
    if stored.request_hash != digest or stored.actor != actor:
        raise TaskCommandError(409, "command_id_conflict", "command_id was already accepted with different content", field="command_id")
    result = dict(stored.result_json or {})
    result["idempotent"] = True
    result["task"] = task_detail(session, project_id, stored.task_id)
    return result


def execute_command(session: Session, project_id: str, device_name: str, command: TaskCommand) -> dict[str, Any]:
    """执行一条命令并提交事务；失败时回滚且不占用 command_id。"""
    actor = actor_identity(device_name, command.channel)
    digest = request_hash(command, actor)
    try:
        replayed = _replay(session, project_id, command, actor, digest)
        if replayed is not None:
            return replayed
        now = _now()
        if command.action == "create":
            task, payload = _create(session, project_id, command, actor, now)
        else:
            task = session.scalar(select(Task).where(Task.project_id == project_id, Task.task_id == command.task_id).with_for_update())
            if task is None:
                raise TaskCommandError(404, "task_not_found", "task_id does not exist", field="task_id")
            payload = _apply(session, task, command, actor, now)
        task.last_task_seq += 1
        task.last_activity_at = now
        event = TaskEvent(project_id=project_id, task_seq=task.last_task_seq, command_id=command.command_id,
                          task_id=task.task_id, event_type=EVENT_TYPES[command.action], actor_id=actor, agent=command.agent,
                          expected_version=command.expected_version, expected_assignment_epoch=command.expected_assignment_epoch,
                          task_version=task.version, assignment_epoch=task.assignment_epoch, payload_json=payload, occurred_at=now)
        session.add(event)
        session.flush()
        result = _result(command, task, event, payload)
        session.add(TaskCommandRow(project_id=project_id, command_id=command.command_id, request_hash=digest, actor=actor,
                                   task_id=task.task_id, result_json=result, created_at=now))
        session.commit()
    except TaskCommandError:
        session.rollback()
        raise
    except IntegrityError:
        session.rollback()
        return _resolve_integrity_conflict(session, project_id, command, actor, digest)
    return {**result, "idempotent": False, "task": task_detail(session, project_id, task.task_id)}


def _resolve_integrity_conflict(session: Session, project_id: str, command: TaskCommand, actor: str, digest: str) -> dict[str, Any]:
    """并发写入撞上唯一约束：按语义映射，不报告无关错误。"""
    replayed = _replay(session, project_id, command, actor, digest)
    if replayed is not None:
        return replayed
    task_id = command.task_id
    task = session.get(Task, (project_id, task_id)) if task_id else None
    if command.action == "create":
        raise TaskCommandError(409, "task_exists", "task_id already exists", field="task_id")
    if task is None:
        raise TaskCommandError(404, "task_not_found", "task_id does not exist", field="task_id")
    raise TaskCommandError(409, "version_conflict", "the task changed concurrently; reload and retry", current=task_summary(session, task))


__all__ = ["EVENT_TYPES", "actor_identity", "execute_command", "request_hash"]
