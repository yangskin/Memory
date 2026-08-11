"""Local event-sourced Task Graph storage for the ``memory_task_sync`` MCP tool."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .memory_config import MemoryConfig
from .memory_result import error_result, ok_result
from .memory_sync_client import MemoryHubClient

TASK_SYNC_VERSION = "1.0"
MAX_GRAPH_NODES = 200
MAX_GRAPH_EDGES = 400
MAX_HISTORY_EVENTS = 200

TASK_STATES = frozenset({"open", "active", "blocked", "review", "done", "cancelled"})
TASK_MUTATION_ACTIONS = frozenset(
    {"create", "assign", "claim", "decline", "report", "block", "resume", "submit", "review", "reassign", "cancel"}
)
OFFLINE_TASK_ACTIONS = frozenset({"report", "submit"})
RETRYABLE_TASK_AUTHORITY_STATUSES = frozenset({0, 408, 429, 500, 502, 503, 504})
IMMEDIATE_TASK_AUTHORITY_RETRY_STATUSES = frozenset({0, 408, 500, 502, 503, 504})
MAX_TASK_AUTHORITY_ATTEMPTS = 2


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _load_json(value: object, default: object) -> object:
    try:
        loaded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return loaded


def _text(value: object, *, field: str, required: bool = False, maximum: int = 4096) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    result = value.strip()
    if required and not result:
        raise ValueError(f"{field} is required")
    if len(result) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return result


def _string_list(value: object, *, field: str, maximum: int = 64) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _text(item, field=field, maximum=1024)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
        if len(result) >= maximum:
            break
    return result


def _integer(value: object, *, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer greater than or equal to {minimum}")
    return value


def _node_id(node_type: str, node_key: str) -> str:
    return f"{node_type}:{node_key}"


def _command_hash(args: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_json(args).encode("utf-8")).hexdigest()


def _store_path(config: MemoryConfig) -> Path:
    return config.repo_root / ".ai-memory" / "task-graph.db"


class TaskSyncStore:
    """Small stdlib-only local Task Graph event store and projection."""

    def __init__(
        self,
        path: Path,
        authorize: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.path = path
        self._authorize = authorize
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _authorize_task_event(
        self,
        task_event: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if self._authorize is None:
            return None, None
        result = self._authorize(task_event)
        if not isinstance(result, dict):
            return error_result("task_authority_unavailable", "Hub task authority returned an invalid response"), None
        if not result.get("ok"):
            return result, None
        shared_sync = result.get("shared_sync")
        return None, dict(shared_sync) if isinstance(shared_sync, dict) else None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS task_events (
                    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    command_id TEXT NOT NULL UNIQUE,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    expected_version INTEGER NOT NULL,
                    expected_assignment_epoch INTEGER,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_task_events_task_seq
                    ON task_events(task_id, event_seq);
                CREATE TABLE IF NOT EXISTS task_commands (
                    command_id TEXT PRIMARY KEY,
                    command_hash TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    objective TEXT NOT NULL DEFAULT '',
                    acceptance TEXT NOT NULL DEFAULT '',
                    priority TEXT NOT NULL DEFAULT 'normal',
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    assignment_epoch INTEGER NOT NULL DEFAULT 0,
                    current_attempt_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_agents (
                    agent_id TEXT PRIMARY KEY,
                    role TEXT NOT NULL DEFAULT '',
                    capabilities_json TEXT NOT NULL DEFAULT '[]',
                    owner TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'available',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    epoch INTEGER NOT NULL,
                    assignee TEXT NOT NULL REFERENCES task_agents(agent_id),
                    assigned_by TEXT NOT NULL REFERENCES task_agents(agent_id),
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(task_id, epoch)
                );
                CREATE TABLE IF NOT EXISTS task_submissions (
                    submission_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    attempt_id TEXT NOT NULL REFERENCES task_attempts(attempt_id),
                    summary TEXT NOT NULL,
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    task_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_reviews (
                    review_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    submission_id TEXT NOT NULL REFERENCES task_submissions(submission_id),
                    reviewer TEXT NOT NULL REFERENCES task_agents(agent_id),
                    decision TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_graph_nodes (
                    node_id TEXT PRIMARY KEY,
                    node_type TEXT NOT NULL,
                    node_key TEXT NOT NULL,
                    name TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(node_type, node_key)
                );
                CREATE TABLE IF NOT EXISTS task_graph_edges (
                    source_node_id TEXT NOT NULL REFERENCES task_graph_nodes(node_id) ON DELETE CASCADE,
                    target_node_id TEXT NOT NULL REFERENCES task_graph_nodes(node_id) ON DELETE CASCADE,
                    relation_type TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(source_node_id, target_node_id, relation_type)
                );
                """
            )

    def execute(self, args: dict[str, Any]) -> dict[str, Any]:
        action = _text(args.get("action") or "sync", field="action", maximum=64).lower()
        if action == "sync":
            return self.bundle(task_id=args.get("task_id"), agent_id=args.get("agent_id"), cursor=args.get("cursor"))
        if action == "history":
            return self.history(task_id=args.get("task_id"), cursor=args.get("cursor"), max_items=args.get("max_items"))
        if action == "create":
            return self._create(args)
        if action == "assign":
            return self._assign(args, reassign=False)
        if action == "claim":
            return self._claim(args)
        if action == "decline":
            return self._decline(args)
        if action == "report":
            return self._report(args)
        if action == "block":
            return self._block(args)
        if action == "resume":
            return self._resume(args)
        if action == "submit":
            return self._submit(args)
        if action == "review":
            return self._review(args)
        if action == "reassign":
            return self._assign(args, reassign=True)
        if action == "cancel":
            return self._cancel(args)
        return error_result("invalid_input", f"unknown task action: {action}")

    def _create(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            command_id = _text(args.get("command_id"), field="command_id", required=True, maximum=256)
            expected_version = _integer(args.get("expected_version"), field="expected_version")
            if expected_version != 0:
                return error_result("version_conflict", "create requires expected_version=0", expected_version=expected_version)
            actor_id = _text(args.get("actor_id") or args.get("agent_id"), field="actor_id", required=True, maximum=256)
            task_id = _text(args.get("task_id"), field="task_id", maximum=256)
            if not task_id:
                digest = hashlib.sha256(command_id.encode("utf-8")).hexdigest()[:20]
                task_id = f"task_{digest}"
            title = _text(args.get("title"), field="title", required=True, maximum=1024)
            objective = _text(args.get("objective"), field="objective", maximum=8192)
            acceptance = _text(args.get("acceptance"), field="acceptance", maximum=8192)
            priority = _text(args.get("priority") or "normal", field="priority", maximum=64)
            depends_on = _string_list(args.get("depends_on"), field="depends_on")
            produced_memory = _string_list(args.get("produced_memory"), field="produced_memory")
            parent_task_id = _text(args.get("parent_task_id"), field="parent_task_id", maximum=256)
        except ValueError as exc:
            return error_result("invalid_input", str(exc))

        command_hash = _command_hash(args)
        occurred_at = _now()
        payload = {
            "title": title,
            "objective": objective,
            "acceptance": acceptance,
            "priority": priority,
            "depends_on": depends_on,
            "produced_memory": produced_memory,
            "parent_task_id": parent_task_id or None,
        }
        task_event = {
            "version": TASK_SYNC_VERSION,
            "command_id": command_id,
            "event_type": "TaskCreated",
            "task_id": task_id,
            "actor_id": actor_id,
            "expected_version": expected_version,
            "expected_assignment_epoch": None,
            "task_version": 1,
            "assignment_epoch": 0,
            "payload": payload,
            "occurred_at": occurred_at,
        }
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                duplicate = connection.execute(
                    "SELECT command_hash, result_json FROM task_commands WHERE command_id=?", (command_id,)
                ).fetchone()
                if duplicate is not None:
                    if str(duplicate["command_hash"]) != command_hash:
                        connection.execute("ROLLBACK")
                        return error_result("command_id_conflict", "command_id was already used with different input")
                    connection.execute("ROLLBACK")
                    result = _load_json(duplicate["result_json"], {})
                    if isinstance(result, dict):
                        result["idempotent"] = True
                        return result
                    return error_result("corrupt_command_result", "stored task command result is invalid")

                if connection.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone() is not None:
                    connection.execute("ROLLBACK")
                    return error_result("task_exists", "task_id already exists", task_id=task_id)
                self._upsert_agent(connection, actor_id, occurred_at)
                connection.execute(
                    """
                    INSERT INTO tasks(task_id,title,objective,acceptance,priority,state,version,assignment_epoch,current_attempt_id,created_at,updated_at)
                    VALUES(?,?,?,?,?,'open',1,0,NULL,?,?)
                    """,
                    (task_id, title, objective, acceptance, priority, occurred_at, occurred_at),
                )
                authority_error, shared_sync = self._authorize_task_event(task_event)
                if authority_error is not None:
                    connection.execute("ROLLBACK")
                    return authority_error
                self._refresh_task_graph(connection, task_id, payload, occurred_at)
                connection.execute(
                    """
                    INSERT INTO task_events(command_id,task_id,event_type,actor_id,expected_version,expected_assignment_epoch,payload_json,occurred_at)
                    VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (command_id, task_id, "TaskCreated", actor_id, expected_version, None, _json(payload), occurred_at),
                )
                bundle = self._bundle(connection, task_id=task_id, agent_id=None, cursor=None)
                result = ok_result(
                    "task created",
                    action="create",
                    task_id=task_id,
                    version=1,
                    assignment_epoch=0,
                    task_event=task_event,
                    bundle=bundle,
                    idempotent=False,
                )
                if shared_sync is not None:
                    result["shared_sync"] = shared_sync
                connection.execute(
                    "INSERT INTO task_commands(command_id,command_hash,result_json,created_at) VALUES(?,?,?,?)",
                    (command_id, command_hash, _json(result), occurred_at),
                )
                connection.execute("COMMIT")
                return result
        except sqlite3.Error as exc:
            return error_result("task_store_unavailable", str(exc))

    def _mutate_task(
        self,
        args: dict[str, Any],
        *,
        action: str,
        requires_epoch: bool,
        mutation,
    ) -> dict[str, Any]:
        try:
            command_id = _text(args.get("command_id"), field="command_id", required=True, maximum=256)
            expected_version = _integer(args.get("expected_version"), field="expected_version")
            actor_id = _text(args.get("actor_id") or args.get("agent_id"), field="actor_id", required=True, maximum=256)
            task_id = _text(args.get("task_id"), field="task_id", required=True, maximum=256)
            raw_epoch = args.get("expected_assignment_epoch")
            if requires_epoch and raw_epoch is None:
                return error_result("missing_assignment_epoch", f"{action} requires expected_assignment_epoch")
            expected_epoch = _integer(raw_epoch, field="expected_assignment_epoch") if raw_epoch is not None else None
        except ValueError as exc:
            return error_result("invalid_input", str(exc))

        command_hash = _command_hash(args)
        timestamp = _now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                duplicate = connection.execute(
                    "SELECT command_hash, result_json FROM task_commands WHERE command_id=?", (command_id,)
                ).fetchone()
                if duplicate is not None:
                    if str(duplicate["command_hash"]) != command_hash:
                        connection.execute("ROLLBACK")
                        return error_result("command_id_conflict", "command_id was already used with different input")
                    connection.execute("ROLLBACK")
                    result = _load_json(duplicate["result_json"], {})
                    if isinstance(result, dict):
                        result["idempotent"] = True
                        return result
                    return error_result("corrupt_command_result", "stored task command result is invalid")

                task = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                if task is None:
                    connection.execute("ROLLBACK")
                    return error_result("task_not_found", "task_id does not exist", task_id=task_id)
                if int(task["version"]) != expected_version:
                    connection.execute("ROLLBACK")
                    return error_result(
                        "version_conflict",
                        "task version does not match expected_version",
                        task_id=task_id,
                        expected_version=expected_version,
                        current_version=int(task["version"]),
                    )
                if requires_epoch and int(task["assignment_epoch"]) != expected_epoch:
                    connection.execute("ROLLBACK")
                    return error_result(
                        "assignment_epoch_conflict",
                        "task assignment epoch does not match expected_assignment_epoch",
                        task_id=task_id,
                        expected_assignment_epoch=expected_epoch,
                        current_assignment_epoch=int(task["assignment_epoch"]),
                    )
                outcome = mutation(connection, task, actor_id, timestamp)
                if isinstance(outcome, dict):
                    connection.execute("ROLLBACK")
                    return outcome
                event_type, payload, message = outcome
                updated = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                if updated is None:
                    connection.execute("ROLLBACK")
                    return error_result("task_not_found", "task disappeared during mutation", task_id=task_id)
                task_event = {
                    "version": TASK_SYNC_VERSION,
                    "command_id": command_id,
                    "event_type": event_type,
                    "task_id": task_id,
                    "actor_id": actor_id,
                    "expected_version": expected_version,
                    "expected_assignment_epoch": expected_epoch,
                    "task_version": int(updated["version"]),
                    "assignment_epoch": int(updated["assignment_epoch"]),
                    "payload": payload,
                    "occurred_at": timestamp,
                }
                authority_error, shared_sync = self._authorize_task_event(task_event)
                if authority_error is not None:
                    connection.execute("ROLLBACK")
                    return authority_error
                self._refresh_task_graph(connection, task_id, {}, timestamp)
                connection.execute(
                    """
                    INSERT INTO task_events(command_id,task_id,event_type,actor_id,expected_version,expected_assignment_epoch,payload_json,occurred_at)
                    VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (command_id, task_id, event_type, actor_id, expected_version, expected_epoch, _json(payload), timestamp),
                )
                bundle = self._bundle(connection, task_id=task_id, agent_id=None, cursor=None)
                result = ok_result(
                    message,
                    action=action,
                    task_id=task_id,
                    version=int(updated["version"]),
                    assignment_epoch=int(updated["assignment_epoch"]),
                    task_event=task_event,
                    bundle=bundle,
                    idempotent=False,
                )
                if shared_sync is not None:
                    result["shared_sync"] = shared_sync
                connection.execute(
                    "INSERT INTO task_commands(command_id,command_hash,result_json,created_at) VALUES(?,?,?,?)",
                    (command_id, command_hash, _json(result), timestamp),
                )
                connection.execute("COMMIT")
                return result
        except sqlite3.Error as exc:
            return error_result("task_store_unavailable", str(exc))

    def _current_attempt(self, connection: sqlite3.Connection, task: sqlite3.Row) -> sqlite3.Row | dict[str, Any]:
        attempt_id = str(task["current_attempt_id"] or "")
        if not attempt_id:
            return error_result("no_current_attempt", "task has no current attempt", task_id=str(task["task_id"]))
        attempt = connection.execute("SELECT * FROM task_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if attempt is None:
            return error_result("no_current_attempt", "current attempt is unavailable", task_id=str(task["task_id"]))
        return attempt

    def _executor_attempt(
        self,
        connection: sqlite3.Connection,
        task: sqlite3.Row,
        actor_id: str,
        *,
        allowed_statuses: set[str],
    ) -> sqlite3.Row | dict[str, Any]:
        attempt = self._current_attempt(connection, task)
        if isinstance(attempt, dict):
            return attempt
        if str(attempt["assignee"]) != actor_id:
            return error_result("assignment_forbidden", "only the current assignee may perform this task action")
        if str(attempt["status"]) not in allowed_statuses:
            return error_result(
                "invalid_attempt_state",
                "current attempt does not permit this task action",
                current_attempt_status=str(attempt["status"]),
            )
        return attempt

    def _advance_task(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        timestamp: str,
        *,
        state: str | None = None,
        current_attempt_id: str | None | object = ..., 
        assignment_epoch: int | None = None,
    ) -> None:
        assignments = ["version=version+1", "updated_at=?"]
        values: list[object] = [timestamp]
        if state is not None:
            assignments.append("state=?")
            values.append(state)
        if current_attempt_id is not ...:
            assignments.append("current_attempt_id=?")
            values.append(current_attempt_id)
        if assignment_epoch is not None:
            assignments.append("assignment_epoch=?")
            values.append(assignment_epoch)
        values.append(task_id)
        connection.execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE task_id=?", values)

    def _assign(self, args: dict[str, Any], *, reassign: bool) -> dict[str, Any]:
        action = "reassign" if reassign else "assign"
        try:
            assignee = _text(args.get("assignee"), field="assignee", required=True, maximum=256)
            requested_attempt = _text(args.get("attempt_id"), field="attempt_id", maximum=256)
        except ValueError as exc:
            return error_result("invalid_input", str(exc))

        def mutate(connection: sqlite3.Connection, task: sqlite3.Row, actor_id: str, timestamp: str):
            state = str(task["state"])
            if state in {"done", "cancelled", "review"}:
                return error_result("invalid_task_state", f"cannot {action} a {state} task")
            if reassign and not task["current_attempt_id"]:
                return error_result("no_current_attempt", "reassign requires a current attempt")
            previous = self._current_attempt(connection, task) if task["current_attempt_id"] else None
            if isinstance(previous, sqlite3.Row) and str(previous["status"]) in {"offered", "active"}:
                connection.execute(
                    "UPDATE task_attempts SET status='superseded',updated_at=? WHERE attempt_id=?",
                    (timestamp, str(previous["attempt_id"])),
                )
            epoch = int(task["assignment_epoch"]) + 1
            attempt_seed = f"{task['task_id']}:{epoch}:{args.get('command_id')}"
            attempt_id = requested_attempt or f"attempt_{hashlib.sha256(attempt_seed.encode('utf-8')).hexdigest()[:20]}"
            if connection.execute("SELECT 1 FROM task_attempts WHERE attempt_id=?", (attempt_id,)).fetchone() is not None:
                return error_result("attempt_exists", "attempt_id already exists", attempt_id=attempt_id)
            self._upsert_agent(connection, actor_id, timestamp)
            self._upsert_agent(connection, assignee, timestamp)
            connection.execute(
                """
                INSERT INTO task_attempts(attempt_id,task_id,epoch,assignee,assigned_by,status,created_at,updated_at)
                VALUES(?,?,?,?,?,'offered',?,?)
                """,
                (attempt_id, str(task["task_id"]), epoch, assignee, actor_id, timestamp, timestamp),
            )
            self._advance_task(
                connection,
                str(task["task_id"]),
                timestamp,
                state="open",
                current_attempt_id=attempt_id,
                assignment_epoch=epoch,
            )
            return (
                "TaskReassigned" if reassign else "TaskAssigned",
                {"attempt_id": attempt_id, "assignee": assignee, "assigned_by": actor_id, "epoch": epoch},
                "task reassigned" if reassign else "task assigned",
            )

        return self._mutate_task(args, action=action, requires_epoch=reassign, mutation=mutate)

    def _claim(self, args: dict[str, Any]) -> dict[str, Any]:
        def mutate(connection: sqlite3.Connection, task: sqlite3.Row, actor_id: str, timestamp: str):
            if str(task["state"]) != "open":
                return error_result("invalid_task_state", "only an open task may be claimed")
            attempt = self._executor_attempt(connection, task, actor_id, allowed_statuses={"offered"})
            if isinstance(attempt, dict):
                return attempt
            connection.execute("UPDATE task_attempts SET status='active',updated_at=? WHERE attempt_id=?", (timestamp, str(attempt["attempt_id"])))
            self._advance_task(connection, str(task["task_id"]), timestamp, state="active")
            return "TaskClaimed", {"attempt_id": str(attempt["attempt_id"])}, "task claimed"

        return self._mutate_task(args, action="claim", requires_epoch=True, mutation=mutate)

    def _decline(self, args: dict[str, Any]) -> dict[str, Any]:
        def mutate(connection: sqlite3.Connection, task: sqlite3.Row, actor_id: str, timestamp: str):
            attempt = self._executor_attempt(connection, task, actor_id, allowed_statuses={"offered", "active"})
            if isinstance(attempt, dict):
                return attempt
            connection.execute("UPDATE task_attempts SET status='superseded',updated_at=? WHERE attempt_id=?", (timestamp, str(attempt["attempt_id"])))
            self._advance_task(connection, str(task["task_id"]), timestamp, state="open", current_attempt_id=None)
            return "TaskDeclined", {"attempt_id": str(attempt["attempt_id"])}, "task assignment declined"

        return self._mutate_task(args, action="decline", requires_epoch=True, mutation=mutate)

    def _report(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            summary = _text(args.get("summary"), field="summary", required=True, maximum=8192)
        except ValueError as exc:
            return error_result("invalid_input", str(exc))

        def mutate(connection: sqlite3.Connection, task: sqlite3.Row, actor_id: str, timestamp: str):
            attempt = self._executor_attempt(connection, task, actor_id, allowed_statuses={"active"})
            if isinstance(attempt, dict):
                return attempt
            self._advance_task(connection, str(task["task_id"]), timestamp)
            return "TaskReported", {"attempt_id": str(attempt["attempt_id"]), "summary": summary}, "task report recorded"

        return self._mutate_task(args, action="report", requires_epoch=True, mutation=mutate)

    def _block(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            reason = _text(args.get("reason"), field="reason", required=True, maximum=8192)
        except ValueError as exc:
            return error_result("invalid_input", str(exc))

        def mutate(connection: sqlite3.Connection, task: sqlite3.Row, actor_id: str, timestamp: str):
            if str(task["state"]) != "active":
                return error_result("invalid_task_state", "only an active task may be blocked")
            attempt = self._executor_attempt(connection, task, actor_id, allowed_statuses={"active"})
            if isinstance(attempt, dict):
                return attempt
            self._advance_task(connection, str(task["task_id"]), timestamp, state="blocked")
            return "TaskBlocked", {"attempt_id": str(attempt["attempt_id"]), "reason": reason}, "task blocked"

        return self._mutate_task(args, action="block", requires_epoch=True, mutation=mutate)

    def _resume(self, args: dict[str, Any]) -> dict[str, Any]:
        def mutate(connection: sqlite3.Connection, task: sqlite3.Row, actor_id: str, timestamp: str):
            if str(task["state"]) != "blocked":
                return error_result("invalid_task_state", "only a blocked task may be resumed")
            attempt = self._executor_attempt(connection, task, actor_id, allowed_statuses={"active"})
            if isinstance(attempt, dict):
                return attempt
            self._advance_task(connection, str(task["task_id"]), timestamp, state="active")
            return "TaskResumed", {"attempt_id": str(attempt["attempt_id"])}, "task resumed"

        return self._mutate_task(args, action="resume", requires_epoch=True, mutation=mutate)

    def _submit(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            summary = _text(args.get("summary"), field="summary", required=True, maximum=8192)
            evidence = _string_list(args.get("evidence"), field="evidence")
            requested_submission = _text(args.get("submission_id"), field="submission_id", maximum=256)
        except ValueError as exc:
            return error_result("invalid_input", str(exc))

        def mutate(connection: sqlite3.Connection, task: sqlite3.Row, actor_id: str, timestamp: str):
            if str(task["state"]) != "active":
                return error_result("invalid_task_state", "only an active task may be submitted")
            attempt = self._executor_attempt(connection, task, actor_id, allowed_statuses={"active"})
            if isinstance(attempt, dict):
                return attempt
            submission_seed = f"{task['task_id']}:{args.get('command_id')}"
            submission_id = requested_submission or f"submission_{hashlib.sha256(submission_seed.encode('utf-8')).hexdigest()[:20]}"
            if connection.execute("SELECT 1 FROM task_submissions WHERE submission_id=?", (submission_id,)).fetchone() is not None:
                return error_result("submission_exists", "submission_id already exists", submission_id=submission_id)
            next_version = int(task["version"]) + 1
            connection.execute(
                """
                INSERT INTO task_submissions(submission_id,task_id,attempt_id,summary,evidence_json,task_version,created_at)
                VALUES(?,?,?,?,?,?,?)
                """,
                (submission_id, str(task["task_id"]), str(attempt["attempt_id"]), summary, _json(evidence), next_version, timestamp),
            )
            connection.execute("UPDATE task_attempts SET status='submitted',updated_at=? WHERE attempt_id=?", (timestamp, str(attempt["attempt_id"])))
            self._advance_task(connection, str(task["task_id"]), timestamp, state="review")
            return "TaskSubmitted", {"attempt_id": str(attempt["attempt_id"]), "submission_id": submission_id, "summary": summary, "evidence": evidence}, "task submitted for review"

        return self._mutate_task(args, action="submit", requires_epoch=True, mutation=mutate)

    def _review(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            decision = _text(args.get("decision"), field="decision", required=True, maximum=64)
            if decision not in {"approved", "changes_requested"}:
                return error_result("invalid_input", "decision must be approved or changes_requested")
            summary = _text(args.get("summary"), field="summary", maximum=8192)
            requested_submission = _text(args.get("submission_id"), field="submission_id", maximum=256)
            requested_review = _text(args.get("review_id"), field="review_id", maximum=256)
        except ValueError as exc:
            return error_result("invalid_input", str(exc))

        def mutate(connection: sqlite3.Connection, task: sqlite3.Row, actor_id: str, timestamp: str):
            if str(task["state"]) != "review":
                return error_result("invalid_task_state", "only a task in review may be reviewed")
            submission = None
            if requested_submission:
                submission = connection.execute(
                    "SELECT * FROM task_submissions WHERE submission_id=? AND task_id=?",
                    (requested_submission, str(task["task_id"])),
                ).fetchone()
            else:
                submission = connection.execute(
                    "SELECT * FROM task_submissions WHERE task_id=? ORDER BY created_at DESC,submission_id DESC LIMIT 1",
                    (str(task["task_id"]),),
                ).fetchone()
            if submission is None:
                return error_result("submission_not_found", "no matching submission is available for review")
            submission_attempt = connection.execute(
                "SELECT assignee FROM task_attempts WHERE attempt_id=? AND task_id=?",
                (str(submission["attempt_id"]), str(task["task_id"])),
            ).fetchone()
            if submission_attempt is None:
                return error_result("submission_not_found", "submission attempt is unavailable for review")
            if str(submission_attempt["assignee"]) == actor_id:
                return error_result(
                    "reviewer_conflict",
                    "reviewer must differ from the submission executor",
                    task_id=str(task["task_id"]),
                    submission_id=str(submission["submission_id"]),
                )
            review_seed = f"{task['task_id']}:{args.get('command_id')}"
            review_id = requested_review or f"review_{hashlib.sha256(review_seed.encode('utf-8')).hexdigest()[:20]}"
            if connection.execute("SELECT 1 FROM task_reviews WHERE review_id=?", (review_id,)).fetchone() is not None:
                return error_result("review_exists", "review_id already exists", review_id=review_id)
            self._upsert_agent(connection, actor_id, timestamp)
            connection.execute(
                "INSERT INTO task_reviews(review_id,task_id,submission_id,reviewer,decision,summary,created_at) VALUES(?,?,?,?,?,?,?)",
                (review_id, str(task["task_id"]), str(submission["submission_id"]), actor_id, decision, summary, timestamp),
            )
            if decision == "approved":
                self._advance_task(connection, str(task["task_id"]), timestamp, state="done")
            else:
                connection.execute(
                    "UPDATE task_attempts SET status='active',updated_at=? WHERE attempt_id=?",
                    (timestamp, str(submission["attempt_id"])),
                )
                self._advance_task(connection, str(task["task_id"]), timestamp, state="active")
            return "TaskReviewed", {"review_id": review_id, "submission_id": str(submission["submission_id"]), "decision": decision, "summary": summary}, "task review recorded"

        return self._mutate_task(args, action="review", requires_epoch=False, mutation=mutate)

    def _cancel(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            reason = _text(args.get("reason"), field="reason", maximum=8192)
        except ValueError as exc:
            return error_result("invalid_input", str(exc))

        def mutate(connection: sqlite3.Connection, task: sqlite3.Row, _actor_id: str, timestamp: str):
            state = str(task["state"])
            if state in {"done", "cancelled"}:
                return error_result("invalid_task_state", f"cannot cancel a {state} task")
            attempt = self._current_attempt(connection, task) if task["current_attempt_id"] else None
            if isinstance(attempt, sqlite3.Row) and str(attempt["status"]) in {"offered", "active"}:
                connection.execute("UPDATE task_attempts SET status='superseded',updated_at=? WHERE attempt_id=?", (timestamp, str(attempt["attempt_id"])))
            self._advance_task(connection, str(task["task_id"]), timestamp, state="cancelled")
            return "TaskCancelled", {"reason": reason or None}, "task cancelled"

        return self._mutate_task(args, action="cancel", requires_epoch=False, mutation=mutate)

    def _upsert_agent(self, connection: sqlite3.Connection, agent_id: str, timestamp: str) -> None:
        connection.execute(
            """
            INSERT INTO task_agents(agent_id,created_at,updated_at) VALUES(?,?,?)
            ON CONFLICT(agent_id) DO UPDATE SET updated_at=excluded.updated_at
            """,
            (agent_id, timestamp, timestamp),
        )

    def _upsert_node(
        self,
        connection: sqlite3.Connection,
        *,
        node_type: str,
        node_key: str,
        name: str,
        metadata: dict[str, object],
        timestamp: str,
    ) -> str:
        node_id = _node_id(node_type, node_key)
        connection.execute(
            """
            INSERT INTO task_graph_nodes(node_id,node_type,node_key,name,metadata_json,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(node_id) DO UPDATE SET name=excluded.name,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at
            """,
            (node_id, node_type, node_key, name, _json(metadata), timestamp, timestamp),
        )
        return node_id

    def _upsert_edge(
        self,
        connection: sqlite3.Connection,
        *,
        source_node_id: str,
        target_node_id: str,
        relation_type: str,
        timestamp: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO task_graph_edges(source_node_id,target_node_id,relation_type,metadata_json,created_at,updated_at)
            VALUES(?,?,?,'{}',?,?)
            ON CONFLICT(source_node_id,target_node_id,relation_type) DO UPDATE SET updated_at=excluded.updated_at
            """,
            (source_node_id, target_node_id, relation_type, timestamp, timestamp),
        )

    def _project_agent_node(self, connection: sqlite3.Connection, agent_id: str, timestamp: str) -> str:
        agent = connection.execute("SELECT * FROM task_agents WHERE agent_id=?", (agent_id,)).fetchone()
        if agent is None:
            self._upsert_agent(connection, agent_id, timestamp)
            agent = connection.execute("SELECT * FROM task_agents WHERE agent_id=?", (agent_id,)).fetchone()
        capabilities = _load_json(agent["capabilities_json"], []) if agent is not None else []
        return self._upsert_node(
            connection,
            node_type="agent",
            node_key=agent_id,
            name=agent_id,
            metadata={
                "role": str(agent["role"] or "") if agent is not None else "",
                "capabilities": capabilities if isinstance(capabilities, list) else [],
                "owner": str(agent["owner"] or "") if agent is not None else "",
                "status": str(agent["status"] or "available") if agent is not None else "available",
            },
            timestamp=timestamp,
        )

    def _refresh_task_graph(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        links: dict[str, object],
        timestamp: str,
    ) -> None:
        task = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if task is None:
            return
        task_node = self._upsert_node(
            connection,
            node_type="task",
            node_key=task_id,
            name=str(task["title"]),
            metadata={
                "title": str(task["title"]),
                "objective": str(task["objective"]),
                "acceptance": str(task["acceptance"]),
                "priority": str(task["priority"]),
                "state": str(task["state"]),
                "version": int(task["version"]),
                "assignment_epoch": int(task["assignment_epoch"]),
                "current_attempt": task["current_attempt_id"],
            },
            timestamp=timestamp,
        )
        for dependency_id in links.get("depends_on") or []:
            dependency = str(dependency_id)
            dependency_node = self._upsert_node(
                connection,
                node_type="task",
                node_key=dependency,
                name=dependency,
                metadata={"external": True},
                timestamp=timestamp,
            )
            self._upsert_edge(
                connection,
                source_node_id=task_node,
                target_node_id=dependency_node,
                relation_type="depends_on",
                timestamp=timestamp,
            )
        parent_task_id = str(links.get("parent_task_id") or "")
        if parent_task_id:
            parent_node = self._upsert_node(
                connection,
                node_type="task",
                node_key=parent_task_id,
                name=parent_task_id,
                metadata={"external": True},
                timestamp=timestamp,
            )
            self._upsert_edge(
                connection,
                source_node_id=parent_node,
                target_node_id=task_node,
                relation_type="parent_of",
                timestamp=timestamp,
            )
        for asset_id in links.get("produced_memory") or []:
            asset = str(asset_id)
            asset_node = self._upsert_node(
                connection,
                node_type="asset",
                node_key=asset,
                name=asset,
                metadata={},
                timestamp=timestamp,
            )
            self._upsert_edge(
                connection,
                source_node_id=task_node,
                target_node_id=asset_node,
                relation_type="produced_memory",
                timestamp=timestamp,
            )
        connection.execute(
            "DELETE FROM task_graph_edges WHERE source_node_id=? AND relation_type='current_attempt'",
            (task_node,),
        )
        attempts = connection.execute(
            "SELECT * FROM task_attempts WHERE task_id=? ORDER BY epoch,created_at,attempt_id",
            (task_id,),
        ).fetchall()
        for attempt in attempts:
            attempt_id = str(attempt["attempt_id"])
            assignee = str(attempt["assignee"])
            assigned_by = str(attempt["assigned_by"])
            assignee_node = self._project_agent_node(connection, assignee, timestamp)
            assigned_by_node = self._project_agent_node(connection, assigned_by, timestamp)
            attempt_node = self._upsert_node(
                connection,
                node_type="attempt",
                node_key=attempt_id,
                name=attempt_id,
                metadata={
                    "task_id": task_id,
                    "epoch": int(attempt["epoch"]),
                    "assignee": assignee,
                    "assigned_by": assigned_by,
                    "status": str(attempt["status"]),
                    "created_at": str(attempt["created_at"]),
                },
                timestamp=timestamp,
            )
            if str(task["current_attempt_id"] or "") == attempt_id:
                self._upsert_edge(
                    connection,
                    source_node_id=task_node,
                    target_node_id=attempt_node,
                    relation_type="current_attempt",
                    timestamp=timestamp,
                )
            self._upsert_edge(
                connection,
                source_node_id=attempt_node,
                target_node_id=assignee_node,
                relation_type="assigned_to",
                timestamp=timestamp,
            )
            self._upsert_edge(
                connection,
                source_node_id=attempt_node,
                target_node_id=assigned_by_node,
                relation_type="assigned_by",
                timestamp=timestamp,
            )
        submissions = connection.execute(
            "SELECT * FROM task_submissions WHERE task_id=? ORDER BY created_at,submission_id",
            (task_id,),
        ).fetchall()
        for submission in submissions:
            submission_id = str(submission["submission_id"])
            attempt_node = _node_id("attempt", str(submission["attempt_id"]))
            submission_node = self._upsert_node(
                connection,
                node_type="submission",
                node_key=submission_id,
                name=submission_id,
                metadata={
                    "task_id": task_id,
                    "attempt_id": str(submission["attempt_id"]),
                    "summary": str(submission["summary"]),
                    "evidence": _load_json(submission["evidence_json"], []),
                    "version": int(submission["task_version"]),
                    "created_at": str(submission["created_at"]),
                },
                timestamp=timestamp,
            )
            self._upsert_edge(
                connection,
                source_node_id=attempt_node,
                target_node_id=submission_node,
                relation_type="has_submission",
                timestamp=timestamp,
            )
        reviews = connection.execute(
            "SELECT * FROM task_reviews WHERE task_id=? ORDER BY created_at,review_id",
            (task_id,),
        ).fetchall()
        for review in reviews:
            reviewer = str(review["reviewer"])
            self._project_agent_node(connection, reviewer, timestamp)
            review_id = str(review["review_id"])
            submission_node = _node_id("submission", str(review["submission_id"]))
            review_node = self._upsert_node(
                connection,
                node_type="review",
                node_key=review_id,
                name=review_id,
                metadata={
                    "task_id": task_id,
                    "submission_id": str(review["submission_id"]),
                    "reviewer": reviewer,
                    "decision": str(review["decision"]),
                    "summary": str(review["summary"]),
                    "created_at": str(review["created_at"]),
                },
                timestamp=timestamp,
            )
            self._upsert_edge(
                connection,
                source_node_id=submission_node,
                target_node_id=review_node,
                relation_type="has_review",
                timestamp=timestamp,
            )

    def _bundle(
        self,
        connection: sqlite3.Connection,
        *,
        task_id: object,
        agent_id: object,
        cursor: object,
    ) -> dict[str, object]:
        task_filter = _text(task_id, field="task_id", maximum=256) if task_id is not None else ""
        agent_filter = _text(agent_id, field="agent_id", maximum=256) if agent_id is not None else ""
        cursor_value = _integer(cursor, field="cursor", minimum=0) if cursor not in (None, "") else 0
        clauses = ["1=1"]
        values: list[object] = []
        if task_filter:
            clauses.append("t.task_id=?")
            values.append(task_filter)
        if agent_filter:
            clauses.append("a.assignee=?")
            values.append(agent_filter)
        tasks = connection.execute(
            f"""
            SELECT t.*, a.assignee, a.status AS attempt_status
            FROM tasks AS t
            LEFT JOIN task_attempts AS a ON a.attempt_id=t.current_attempt_id
            WHERE {' AND '.join(clauses)}
            ORDER BY t.updated_at DESC, t.task_id
            LIMIT ?
            """,
            (*values, MAX_GRAPH_NODES),
        ).fetchall()
        task_node_ids = [_node_id("task", str(task["task_id"])) for task in tasks]
        selected_node_ids = set(task_node_ids)
        if task_node_ids:
            placeholders = ",".join("?" for _ in task_node_ids)
            for edge in connection.execute(
                f"SELECT * FROM task_graph_edges WHERE source_node_id IN ({placeholders}) OR target_node_id IN ({placeholders})",
                (*task_node_ids, *task_node_ids),
            ):
                selected_node_ids.add(str(edge["source_node_id"]))
                selected_node_ids.add(str(edge["target_node_id"]))
            task_ids = [str(task["task_id"]) for task in tasks]
            task_placeholders = ",".join("?" for _ in task_ids)
            attempts = connection.execute(
                f"SELECT attempt_id,assignee,assigned_by FROM task_attempts WHERE task_id IN ({task_placeholders})",
                task_ids,
            ).fetchall()
            attempt_ids = [str(attempt["attempt_id"]) for attempt in attempts]
            selected_node_ids.update(_node_id("attempt", attempt_id) for attempt_id in attempt_ids)
            selected_node_ids.update(_node_id("agent", str(attempt["assignee"])) for attempt in attempts)
            selected_node_ids.update(_node_id("agent", str(attempt["assigned_by"])) for attempt in attempts)
            if attempt_ids:
                attempt_placeholders = ",".join("?" for _ in attempt_ids)
                submissions = connection.execute(
                    f"SELECT submission_id FROM task_submissions WHERE attempt_id IN ({attempt_placeholders})",
                    attempt_ids,
                ).fetchall()
                submission_ids = [str(submission["submission_id"]) for submission in submissions]
                selected_node_ids.update(_node_id("submission", submission_id) for submission_id in submission_ids)
                if submission_ids:
                    submission_placeholders = ",".join("?" for _ in submission_ids)
                    reviews = connection.execute(
                        f"SELECT review_id,reviewer FROM task_reviews WHERE submission_id IN ({submission_placeholders})",
                        submission_ids,
                    ).fetchall()
                    selected_node_ids.update(_node_id("review", str(review["review_id"])) for review in reviews)
                    selected_node_ids.update(_node_id("agent", str(review["reviewer"])) for review in reviews)
        selected = sorted(selected_node_ids)[:MAX_GRAPH_NODES]
        nodes: list[dict[str, object]] = []
        if selected:
            placeholders = ",".join("?" for _ in selected)
            for node in connection.execute(
                f"SELECT * FROM task_graph_nodes WHERE node_id IN ({placeholders}) ORDER BY node_type,node_key", selected
            ):
                metadata = _load_json(node["metadata_json"], {})
                nodes.append(
                    {
                        "id": str(node["node_id"]),
                        "type": str(node["node_type"]),
                        "key": str(node["node_key"]),
                        "name": str(node["name"]),
                        "metadata": metadata if isinstance(metadata, dict) else {},
                    }
                )
        edges: list[dict[str, object]] = []
        if selected:
            placeholders = ",".join("?" for _ in selected)
            for edge in connection.execute(
                f"""
                SELECT * FROM task_graph_edges
                WHERE source_node_id IN ({placeholders}) AND target_node_id IN ({placeholders})
                ORDER BY relation_type,source_node_id,target_node_id
                LIMIT ?
                """,
                (*selected, *selected, MAX_GRAPH_EDGES),
            ):
                metadata = _load_json(edge["metadata_json"], {})
                edges.append(
                    {
                        "source": str(edge["source_node_id"]),
                        "target": str(edge["target_node_id"]),
                        "relation_type": str(edge["relation_type"]),
                        "metadata": metadata if isinstance(metadata, dict) else {},
                    }
                )
        roots = {"current": [], "assigned": [], "review": [], "attention": []}
        for task in tasks:
            node_id = _node_id("task", str(task["task_id"]))
            state = str(task["state"])
            assignee = str(task["assignee"] or "")
            attempt_status = str(task["attempt_status"] or "")
            if state == "active" and (not agent_filter or assignee == agent_filter):
                roots["current"].append(node_id)
            if assignee and attempt_status in {"offered", "active"} and (not agent_filter or assignee == agent_filter):
                roots["assigned"].append(node_id)
            if state == "review":
                roots["review"].append(node_id)
            if state in {"open", "blocked"}:
                roots["attention"].append(node_id)
        latest = connection.execute("SELECT COALESCE(MAX(event_seq), 0) AS latest FROM task_events").fetchone()
        return {
            "version": TASK_SYNC_VERSION,
            "roots": roots,
            "nodes": nodes,
            "edges": edges,
            "cursor": str(max(cursor_value, int(latest["latest"] if latest else 0))),
        }

    def bundle(self, *, task_id: object = None, agent_id: object = None, cursor: object = None) -> dict[str, Any]:
        try:
            with self._connect() as connection:
                return ok_result("task graph bundle read", bundle=self._bundle(connection, task_id=task_id, agent_id=agent_id, cursor=cursor))
        except (sqlite3.Error, ValueError) as exc:
            return error_result("task_store_unavailable", str(exc))

    def history(self, *, task_id: object = None, cursor: object = None, max_items: object = None) -> dict[str, Any]:
        try:
            task_filter = _text(task_id, field="task_id", maximum=256) if task_id is not None else ""
            after = _integer(cursor, field="cursor", minimum=0) if cursor not in (None, "") else 0
            limit = _integer(max_items, field="max_items", minimum=1) if max_items is not None else 50
            limit = min(limit, MAX_HISTORY_EVENTS)
            clauses = ["event_seq>?"]
            values: list[object] = [after]
            if task_filter:
                clauses.append("task_id=?")
                values.append(task_filter)
            with self._connect() as connection:
                rows = connection.execute(
                    f"SELECT * FROM task_events WHERE {' AND '.join(clauses)} ORDER BY event_seq LIMIT ?",
                    (*values, limit),
                ).fetchall()
            events = [
                {
                    "seq": int(row["event_seq"]),
                    "command_id": str(row["command_id"]),
                    "task_id": str(row["task_id"]),
                    "event_type": str(row["event_type"]),
                    "actor_id": str(row["actor_id"]),
                    "expected_version": int(row["expected_version"]),
                    "expected_assignment_epoch": row["expected_assignment_epoch"],
                    "payload": _load_json(row["payload_json"], {}),
                    "occurred_at": str(row["occurred_at"]),
                }
                for row in rows
            ]
            next_cursor = str(events[-1]["seq"] if events else after)
            return ok_result("task history read", events=events, cursor=next_cursor)
        except (sqlite3.Error, ValueError) as exc:
            return error_result("task_store_unavailable", str(exc))


def _task_sync_memory_event(config: MemoryConfig, task_event: dict[str, Any]) -> dict[str, Any]:
    """Build one deterministic outer event for a task command."""

    from .memory_sync_protocol import build_memory_event

    task_id = str(task_event.get("task_id") or "")
    command_id = str(task_event.get("command_id") or "")
    actor_id = str(task_event.get("actor_id") or "")
    event = build_memory_event(
        {
            "operation": "task_sync",
            "scope": "project_shared",
            "task_id": task_id,
            "agent_id": actor_id,
            "content_markdown": "",
            "task_event": task_event,
        },
        {"ok": True, "task_id": task_id},
        repo_root=config.repo_root,
    )
    event["event_id"] = str(uuid5(NAMESPACE_URL, f"memory-task-sync:{config.repo_root.resolve()}:{command_id}"))
    return event


def _authorize_hub_task_event(config: MemoryConfig, task_event: dict[str, Any]) -> dict[str, Any]:
    """Send a coordinated task command to the Hub before local commit."""

    shared = getattr(config, "shared_memory", None)
    scopes = set(getattr(shared, "sync_scopes", ()) or ())
    if "project_shared" not in scopes:
        return error_result(
            "task_authority_scope_disabled",
            "Hub task authority requires project_shared in shared_memory.sync_scopes",
        )
    event = _task_sync_memory_event(config, task_event)
    timeout_seconds = float(
        getattr(shared, "task_command_timeout_seconds", getattr(shared, "upload_timeout_seconds", 5.0))
    )
    timeout_seconds = max(0.1, timeout_seconds)
    deadline = time.monotonic() + timeout_seconds
    status = 0
    response: dict[str, Any] = {"error": "remote_unavailable"}
    attempts = 0
    while attempts < MAX_TASK_AUTHORITY_ATTEMPTS:
        remaining = deadline - time.monotonic()
        if attempts and remaining <= 0:
            break
        attempts += 1
        status, response = MemoryHubClient(shared).post(
            f"/v1/projects/{shared.project_id}/events/batch",
            {"events": [event]},
            max(0.1, min(timeout_seconds, remaining)),
        )
        if status not in IMMEDIATE_TASK_AUTHORITY_RETRY_STATUSES:
            break
    event_id = str(event["event_id"])
    if status == 200:
        acknowledged = {str(value) for value in list(response.get("accepted", [])) + list(response.get("duplicates", []))}
        if event_id in acknowledged:
            shared_sync: dict[str, object] = {
                "enabled": True,
                "mode": "hub_authoritative",
                "queued": False,
                "remote_event_id": event_id,
            }
            if attempts > 1:
                shared_sync["authority_attempts"] = attempts
                shared_sync["recovered_after_retry"] = True
            return ok_result(
                "Hub accepted task command",
                shared_sync=shared_sync,
            )
        for rejected in response.get("rejected", []):
            if isinstance(rejected, dict) and str(rejected.get("event_id") or "") == event_id:
                return error_result(
                    str(rejected.get("code") or "task_authority_rejected"),
                    str(rejected.get("message") or "Hub rejected task command"),
                    remote_authority=True,
                )
        return error_result(
            "task_authority_unavailable",
            "Hub did not acknowledge the task command",
            remote_status=status,
            authority_attempts=attempts,
        )
    message = str(response.get("error") or f"Hub task command failed with HTTP {status}")
    if status in RETRYABLE_TASK_AUTHORITY_STATUSES:
        details: dict[str, object] = {
            "remote_status": status,
            "retryable": True,
            "authority_attempts": attempts,
        }
        if response.get("retry_after_seconds") is not None:
            details["retry_after_seconds"] = response["retry_after_seconds"]
        return error_result(
            "task_authority_unavailable",
            message,
            **details,
        )
    return error_result(
        "task_authority_rejected",
        message,
        remote_status=status,
        remote_authority=True,
        authority_attempts=attempts,
    )


def task_sync(config: MemoryConfig, args: dict[str, Any]) -> dict[str, Any]:
    """Execute a task command locally, with Hub authority in shared mode."""

    try:
        action = str(args.get("action") or "sync").strip().lower()
        shared = getattr(config, "shared_memory", None)
        authorize = None
        if action in TASK_MUTATION_ACTIONS and bool(getattr(shared, "enabled", False)):
            if not bool(getattr(shared, "active", False)):
                if action not in OFFLINE_TASK_ACTIONS:
                    return error_result(
                        "task_authority_unavailable",
                        f"{action} requires an active Hub when shared task authority is enabled",
                        action=action,
                        offline_allowed_actions=sorted(OFFLINE_TASK_ACTIONS),
                    )
            else:
                def authorize(event: dict[str, Any]) -> dict[str, Any]:
                    authority = _authorize_hub_task_event(config, event)
                    if authority.get("ok") or action not in OFFLINE_TASK_ACTIONS or not authority.get("retryable"):
                        return authority
                    return ok_result(
                        "Hub unavailable; task command recorded for later sync",
                        shared_sync={
                            "enabled": True,
                            "mode": "offline_pending",
                            "queued": False,
                            "remote_status": authority.get("remote_status", 0),
                        },
                    )
        return TaskSyncStore(_store_path(config), authorize=authorize).execute(args)
    except (OSError, sqlite3.Error) as exc:
        return error_result("task_store_unavailable", str(exc))


def enqueue_task_sync_event(config: MemoryConfig, result: dict[str, Any]) -> dict[str, Any]:
    """Queue a completed local task event for Hub upload without blocking the caller."""

    if not result.get("ok") or not isinstance(result.get("task_event"), dict):
        return result
    existing_sync = result.get("shared_sync")
    if isinstance(existing_sync, dict) and existing_sync.get("mode") == "hub_authoritative":
        return result
    shared = getattr(config, "shared_memory", None)
    if not getattr(shared, "enabled", False):
        return result
    task_event = dict(result["task_event"])
    try:
        from .memory_events import get_current_user
        from .memory_sync_store import SyncStore

        task_id = str(task_event.get("task_id") or "")
        event = _task_sync_memory_event(config, task_event)
        if event["scope"] not in getattr(shared, "sync_scopes", frozenset()):
            return result
        queued = SyncStore(config.repo_root / ".ai-memory" / "shared-sync.db").enqueue(
            event["event_id"], event, event["content_hash"], get_current_user(config.repo_root)
        )
        if queued:
            from .memory_sync_worker import wake_sync_worker

            wake_sync_worker(config.repo_root)
        sync_status = dict(existing_sync) if isinstance(existing_sync, dict) else {}
        sync_status.update({"enabled": True, "queued": queued})
        result["shared_sync"] = sync_status
    except Exception:
        sync_status = dict(existing_sync) if isinstance(existing_sync, dict) else {}
        sync_status.update({"enabled": True, "queued": False})
        result["shared_sync"] = sync_status
    return result


__all__ = ["TASK_SYNC_VERSION", "TaskSyncStore", "enqueue_task_sync_event", "task_sync"]