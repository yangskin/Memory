"""``memory_task_sync``：Hub 任务权威的无状态客户端。

Hub 是任务状态唯一的计算者。本模块只转发命令与读取；本地唯一的持久数据是
``report``/``submit`` 在传输失败时的待发命令队列（不保存任何任务状态）。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .memory_config import MemoryConfig
from .memory_device import DeviceNameError, device_name as local_device_name
from .memory_result import error_result, ok_result
from .memory_sync_client import MemoryHubClient

TASK_COMMAND_ACTIONS = frozenset(
    {"create", "assign", "reassign", "claim", "decline", "report", "block", "resume", "submit", "review", "cancel"}
)
READ_ACTIONS = frozenset({"sync", "task", "history"})
OUTBOX_ACTIONS = frozenset({"outbox", "outbox_dismiss"})
OFFLINE_ACTIONS = frozenset({"report", "submit"})
RETRYABLE_STATUSES = frozenset({0, 408, 429, 500, 502, 503, 504})
IMMEDIATE_RETRY_STATUSES = frozenset({0, 408, 500, 502, 503, 504})
COMMAND_FIELDS = (
    "command_id", "task_id", "expected_version", "expected_assignment_epoch", "channel", "agent",
    "title", "objective", "acceptance", "priority", "depends_on", "parent_task_id", "produced_memory",
    "assignee", "summary", "reason", "evidence", "decision", "submission_id",
)
_COMMON_FIELDS = frozenset({"command_id", "channel", "agent"})
# 与 Hub 契约一致：每个动作只发送它接受的字段，规则里“每次都带 expected_version”的写法不会导致 422。
ACTION_FIELDS: dict[str, frozenset[str]] = {
    "create": frozenset({"task_id", "expected_version", "title", "objective", "acceptance", "priority",
                         "depends_on", "parent_task_id", "produced_memory"}),
    "assign": frozenset({"task_id", "expected_version", "expected_assignment_epoch", "assignee"}),
    "reassign": frozenset({"task_id", "expected_version", "expected_assignment_epoch", "assignee"}),
    "claim": frozenset({"task_id", "expected_version", "expected_assignment_epoch"}),
    "decline": frozenset({"task_id", "expected_version", "expected_assignment_epoch"}),
    "report": frozenset({"task_id", "expected_assignment_epoch", "summary"}),
    "block": frozenset({"task_id", "expected_version", "expected_assignment_epoch", "reason"}),
    "resume": frozenset({"task_id", "expected_version", "expected_assignment_epoch"}),
    "submit": frozenset({"task_id", "expected_version", "expected_assignment_epoch", "summary", "evidence"}),
    "review": frozenset({"task_id", "expected_version", "expected_assignment_epoch", "decision", "summary", "submission_id"}),
    "cancel": frozenset({"task_id", "expected_version", "expected_assignment_epoch", "reason"}),
}
SYNC_PAGE_SIZE = 100
SYNC_MAX_PAGES = 20
FLUSH_BUDGET_SECONDS = 2.0
MAX_RETRY_DELAY_SECONDS = 300.0
LEGACY_STATE_KEY = "legacy_task_sync_migrated"
CONFIRMED_STATE_KEY = "hub_task_commands_confirmed"
CAPABILITY_RECHECK_SECONDS = 300.0
_AGENT_INVALID = re.compile(r"[^A-Za-z0-9._-]")
_device_cache: dict[str, str] = {}
_capability_cache: dict[str, tuple[bool, float]] = {}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _timeout(shared: Any) -> float:
    return max(0.1, float(getattr(shared, "task_command_timeout_seconds", getattr(shared, "upload_timeout_seconds", 5.0))))


def _project_path(shared: Any, suffix: str) -> str:
    return f"/v1/projects/{quote(str(shared.project_id), safe='')}{suffix}"


def _agent_label(value: object) -> str | None:
    text = _AGENT_INVALID.sub("-", str(value or "").strip())[:64].strip("-")
    return text or None


def _remote_error(status: int, response: dict[str, Any], default_code: str) -> dict[str, Any]:
    details: dict[str, Any] = {"remote_status": status}
    for key in ("field", "current", "retry_after_seconds"):
        if response.get(key) is not None:
            details[key] = response[key]
    code = str(response.get("error") or default_code)
    message = str(response.get("message") or response.get("detail") or response.get("error") or f"Hub returned HTTP {status}")
    if status in RETRYABLE_STATUSES:
        details["retryable"] = True
        code = "task_authority_unavailable"
    return error_result(code, message, **details)


def _token_key(shared: Any) -> str:
    raw = f"{getattr(shared, 'server_url', '')}|{shared.project_id}|{getattr(shared, 'token', '')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ensure_device(shared: Any) -> tuple[str | None, dict[str, Any] | None]:
    """返回 Hub 绑定的本机设备名；未绑定的旧凭据以计算机名自动完成一次性绑定，并核对两者一致。"""
    key = _token_key(shared)
    if key in _device_cache:
        return _device_cache[key], None
    client = MemoryHubClient(shared)
    status, info = client.get(_project_path(shared, "/connection"), {}, _timeout(shared))
    if status != 200:
        return None, _remote_error(status, info, "hub_connection_failed")
    if (info.get("capabilities") or {}).get("task_commands") is not True:
        return None, error_result("hub_version_unsupported", "the connected Hub does not support task commands; upgrade the Hub")
    try:
        local = local_device_name()
    except DeviceNameError:
        return None, error_result("invalid_device_name", "local computer name cannot be used as a Hub device identity")
    device = info.get("device_name")
    if device is None:
        status, bound = client.post(_project_path(shared, "/device-binding"), {"device_name": local}, _timeout(shared))
        if status != 200 or bound.get("device_name") != local:
            return None, _remote_error(status, bound, "device_binding_failed")
        device = local
    if not isinstance(device, str) or not device:
        return None, error_result("invalid_hub_response", "Hub returned an invalid device identity")
    if device != local:
        return None, error_result("device_identity_mismatch",
                                  "this credential is bound to another device name; re-enroll this computer",
                                  bound_device=device, local_device=local)
    _device_cache[key] = device
    return device, None


def hub_supports_task_commands(config: Any) -> bool:
    """只读判断已连接的 Hub 是否声明任务命令能力；网络失败或未配置一律视为不支持。

    支持结果永久缓存；不支持的结果每 CAPABILITY_RECHECK_SECONDS 复查一次，Hub 升级后长驻进程无需重启即可生效。
    """
    shared = getattr(config, "shared_memory", None)
    if shared is None or not getattr(shared, "active", False):
        return False
    key = _token_key(shared)
    cached = _capability_cache.get(key)
    if cached is not None and (cached[0] or time.monotonic() - cached[1] < CAPABILITY_RECHECK_SECONDS):
        return cached[0]
    try:
        status, info = MemoryHubClient(shared).get(_project_path(shared, "/connection"), {}, _timeout(shared))
    except Exception:
        return False
    if status != 200:
        return False
    supported = (info.get("capabilities") or {}).get("task_commands") is True
    _capability_cache[key] = (supported, time.monotonic())
    return supported


def _command_body(action: str, args: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {"action": action}
    allowed = ACTION_FIELDS.get(action, frozenset()) | _COMMON_FIELDS
    for key in COMMAND_FIELDS:
        if key not in allowed:
            continue
        value = args.get(key)
        if value is None or (isinstance(value, str) and key != "command_id" and not value.strip() and key not in {"objective", "acceptance"}):
            continue
        body[key] = value
    if "agent" in body:
        label = _agent_label(body["agent"])
        if label is None:
            body.pop("agent")
        else:
            body["agent"] = label
    return body


def _post_command(shared: Any, body: dict[str, Any], *, attempts: int = 2) -> tuple[int, dict[str, Any]]:
    timeout = _timeout(shared)
    deadline = time.monotonic() + timeout
    status, response = 0, {"error": "remote_unavailable"}
    for attempt in range(attempts):
        remaining = deadline - time.monotonic()
        if attempt and remaining <= 0:
            break
        status, response = MemoryHubClient(shared).post(_project_path(shared, "/task-commands"), body, max(0.1, min(timeout, remaining)))
        if status not in IMMEDIATE_RETRY_STATUSES:
            break
    return status, response


class TaskCommandOutbox:
    """跨进程持久的待发命令队列；只保存命令，不保存任务状态。网络调用不在写锁内进行。"""

    def __init__(self, repo_root: Path) -> None:
        self.path = Path(repo_root) / ".ai-memory" / "task-command-outbox.db"

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS pending_commands (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                command_id TEXT NOT NULL UNIQUE,
                task_id TEXT NOT NULL,
                action TEXT NOT NULL,
                request TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_retry_at REAL NOT NULL DEFAULT 0,
                lease_until REAL NOT NULL DEFAULT 0,
                last_error TEXT,
                last_message TEXT,
                current_task TEXT,
                origin TEXT NOT NULL DEFAULT 'offline',
                created_at TEXT NOT NULL,
                failed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_pending_commands_task ON pending_commands(task_id, seq);
            CREATE TABLE IF NOT EXISTS outbox_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        return connection

    def _run(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = operation(connection)
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
            return result
        finally:
            connection.close()

    def enqueue(self, body: dict[str, Any], *, origin: str = "offline", state: str = "pending",
                error: str | None = None, message: str | None = None) -> bool:
        def insert(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO pending_commands(command_id, task_id, action, request, state, origin, last_error, "
                "last_message, created_at, failed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (str(body["command_id"]), str(body.get("task_id") or ""), str(body["action"]),
                 json.dumps(body, ensure_ascii=False, separators=(",", ":")), state, origin, error, message,
                 _now_iso(), _now_iso() if state == "failed" else None),
            )
            return cursor.rowcount == 1
        return bool(self._run(insert))

    def get_state(self, key: str) -> str | None:
        connection = self._connect()
        try:
            row = connection.execute("SELECT value FROM outbox_state WHERE key=?", (key,)).fetchone()
            return None if row is None else str(row["value"])
        finally:
            connection.close()

    def put_state(self, key: str, value: str) -> None:
        self._run(lambda c: c.execute("INSERT OR REPLACE INTO outbox_state(key, value) VALUES(?, ?)", (key, value)))

    def _lease_next(self, now: float) -> sqlite3.Row | None:
        """租用最早一条可发送的命令；同一任务内若更早的命令仍在等待重试，则不越过它。"""
        def lease(connection: sqlite3.Connection) -> sqlite3.Row | None:
            heads = connection.execute(
                "SELECT p.* FROM pending_commands p WHERE p.state='pending' AND p.seq = ("
                " SELECT MIN(q.seq) FROM pending_commands q WHERE q.state='pending' AND q.task_id=p.task_id)"
                " ORDER BY p.seq"
            ).fetchall()
            for row in heads:
                if row["next_retry_at"] <= now and row["lease_until"] <= now:
                    connection.execute("UPDATE pending_commands SET lease_until=? WHERE seq=?", (now + 30, row["seq"]))
                    return row
            return None
        return self._run(lease)

    def _settle(self, seq: int, status: int, response: dict[str, Any], attempts: int) -> str:
        if status == 200:
            self._run(lambda c: c.execute("DELETE FROM pending_commands WHERE seq=?", (seq,)))
            return "sent"
        if status in RETRYABLE_STATUSES:
            delay = min(MAX_RETRY_DELAY_SECONDS, 5.0 * (2 ** min(attempts, 6)))
            retry_after = response.get("retry_after_seconds")
            if isinstance(retry_after, (int, float)):
                delay = max(delay, float(retry_after))
            self._run(lambda c: c.execute(
                "UPDATE pending_commands SET attempts=?, next_retry_at=?, lease_until=0, last_error=?, last_message=? WHERE seq=?",
                (attempts + 1, time.time() + delay, "task_authority_unavailable", f"HTTP {status}", seq)))
            return "retry"
        current = response.get("current")
        self._run(lambda c: c.execute(
            "UPDATE pending_commands SET state='failed', attempts=?, lease_until=0, last_error=?, last_message=?, current_task=?, "
            "failed_at=? WHERE seq=?",
            (attempts + 1, str(response.get("error") or f"http_{status}"),
             str(response.get("message") or response.get("detail") or "")[:2000],
             json.dumps(current, ensure_ascii=False) if isinstance(current, dict) else None, _now_iso(), seq)))
        return "failed"

    def flush(self, shared: Any, *, budget_seconds: float = FLUSH_BUDGET_SECONDS) -> dict[str, int]:
        counts = {"sent": 0, "retry": 0, "failed": 0}
        if not self.path.exists() or not bool(getattr(shared, "active", False)):
            return counts
        deadline = time.monotonic() + max(0.0, budget_seconds)
        while time.monotonic() < deadline:
            row = self._lease_next(time.time())
            if row is None:
                break
            status, response = _post_command(shared, json.loads(row["request"]), attempts=1)
            counts[self._settle(int(row["seq"]), status, response, int(row["attempts"]))] += 1
        return counts

    def failures(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM pending_commands WHERE state='failed' ORDER BY seq LIMIT ?", (max(1, limit),)).fetchall()
        finally:
            connection.close()
        return [self._view(row) for row in rows]

    def failure_count(self) -> int:
        if not self.path.exists():
            return 0
        connection = self._connect()
        try:
            return int(connection.execute("SELECT COUNT(*) FROM pending_commands WHERE state='failed'").fetchone()[0])
        finally:
            connection.close()

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        connection = self._connect()
        try:
            rows = connection.execute("SELECT * FROM pending_commands ORDER BY seq").fetchall()
        finally:
            connection.close()
        return [self._view(row) for row in rows]

    def dismiss(self, command_ids: list[str]) -> dict[str, list[str]]:
        def remove(connection: sqlite3.Connection) -> dict[str, list[str]]:
            removed, rejected = [], []
            for command_id in command_ids:
                cursor = connection.execute("DELETE FROM pending_commands WHERE command_id=? AND state='failed'", (command_id,))
                (removed if cursor.rowcount else rejected).append(command_id)
            return {"removed": removed, "not_failed": rejected}
        return self._run(remove)

    @staticmethod
    def _view(row: sqlite3.Row) -> dict[str, Any]:
        current = json.loads(row["current_task"]) if row["current_task"] else None
        return {
            "command_id": row["command_id"], "task_id": row["task_id"], "action": row["action"], "state": row["state"],
            "origin": row["origin"], "attempts": row["attempts"], "error": row["last_error"],
            "message": row["last_message"], "current": current, "created_at": row["created_at"], "failed_at": row["failed_at"],
        }


def migrate_legacy_task_data(config: MemoryConfig) -> dict[str, int]:
    """一次性把未上传或已被新 Hub 拒收的旧 ``task_sync`` 事件转换为待发命令，然后删除旧本地任务库。

    调用方必须先确认 Hub 声明支持任务命令；旧 Hub 下旧事件按原协议照常上传。
    """
    outbox = TaskCommandOutbox(config.repo_root)
    if outbox.get_state(LEGACY_STATE_KEY):
        return {"converted": 0, "failed": 0}
    counts = {"converted": 0, "failed": 0}
    sync_db = Path(config.repo_root) / ".ai-memory" / "shared-sync.db"
    if sync_db.exists():
        from .memory_sync_store import SyncStore

        store = SyncStore(sync_db)
        connection = store._connect()
        try:
            rows = connection.execute(
                "SELECT event_id, payload_json FROM outbox_events WHERE state IN ('pending','uploading') "
                "OR (state='rejected' AND last_error='task_sync_retired') ORDER BY local_seq").fetchall()
        finally:
            connection.close()
        reports_before: dict[str, int] = {}
        for row in rows:
            try:
                envelope = json.loads(str(row["payload_json"]))
            except ValueError:
                continue
            if not isinstance(envelope, dict) or envelope.get("operation") != "task_sync":
                continue
            event = (envelope.get("metadata") or {}).get("task_event")
            body = _legacy_command(event, reports_before) if isinstance(event, dict) else None
            if body is None:
                task_event = event if isinstance(event, dict) else {}
                outbox.enqueue({"command_id": str(task_event.get("command_id") or row["event_id"]),
                                "task_id": str(task_event.get("task_id") or ""),
                                "action": str(task_event.get("event_type") or "unknown")},
                               origin="legacy", state="failed", error="legacy_task_event_not_replayable",
                               message="this pre-upgrade task event cannot be replayed automatically; redo it with a new command")
                counts["failed"] += 1
            else:
                outbox.enqueue(body, origin="legacy")
                counts["converted"] += 1
            store.reject(str(row["event_id"]), "migrated_to_task_commands")
    outbox.put_state(LEGACY_STATE_KEY, _now_iso())
    for suffix in ("", "-wal", "-shm"):
        (Path(config.repo_root) / ".ai-memory" / f"task-graph.db{suffix}").unlink(missing_ok=True)
    return counts


def _legacy_command(event: dict[str, Any], reports_before: dict[str, int]) -> dict[str, Any] | None:
    kind = event.get("event_type")
    task_id = event.get("task_id")
    command_id = event.get("command_id")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    if kind not in {"TaskReported", "TaskSubmitted"} or not isinstance(task_id, str) or not isinstance(command_id, str):
        return None
    body: dict[str, Any] = {"command_id": command_id, "task_id": task_id,
                            "expected_assignment_epoch": event.get("expected_assignment_epoch")}
    if str(event.get("actor_id") or "").endswith("-dispatch"):
        body["channel"] = "dispatch"
    if kind == "TaskReported":
        reports_before[task_id] = reports_before.get(task_id, 0) + 1
        body.update(action="report", summary=payload.get("summary"))
    else:
        expected = event.get("expected_version")
        if not isinstance(expected, int):
            return None
        # 旧版本里离线 report 也会递增本地版本；它们从未到达 Hub，补发前扣除。
        body.update(action="submit", summary=payload.get("summary"), evidence=payload.get("evidence") or [],
                    expected_version=max(0, expected - reports_before.pop(task_id, 0)))
    if not isinstance(body.get("expected_assignment_epoch"), int) or not body.get("summary"):
        return None
    return body


def _sync(shared: Any, device: str, args: dict[str, Any]) -> dict[str, Any]:
    assignee = _optional_text(args, "assignee") or device
    state = _optional_text(args, "state") or "working"
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(SYNC_MAX_PAGES):
        params: dict[str, Any] = {"state": state, "assignee": assignee, "order": "created", "limit": SYNC_PAGE_SIZE}
        if cursor:
            params["cursor"] = cursor
        status, page = MemoryHubClient(shared).get(_project_path(shared, "/tasks"), params, _timeout(shared))
        if status != 200:
            return _remote_error(status, page, "task_read_failed")
        if not isinstance(page.get("items"), list):
            return error_result("invalid_hub_response", "Hub task catalog response is invalid")
        items.extend(page["items"])
        next_cursor = page.get("next_cursor")
        if next_cursor is None:
            return ok_result("Hub tasks loaded", device_name=device, assignee=assignee, state=state, tasks=items, total=len(items))
        if not isinstance(next_cursor, str) or next_cursor == cursor:
            break
        cursor = next_cursor
    return error_result("hub_scan_incomplete", "Hub task catalog did not finish within the page budget",
                        loaded=len(items), max_pages=SYNC_MAX_PAGES)


def _bounded_int(args: dict[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
    value = args.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{key} must be an integer between {minimum} and {maximum}")
    return value


def _optional_text(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(f"{key} must be a non-empty string of at most 256 characters")
    return value.strip()


def _read(shared: Any, action: str, args: dict[str, Any]) -> dict[str, Any]:
    if action == "task":
        task_id = _optional_text(args, "task_id")
        if not task_id:
            return error_result("invalid_input", "task_id is required")
        status, body = MemoryHubClient(shared).get(_project_path(shared, "/tasks/" + quote(task_id, safe="")), {}, _timeout(shared))
        if status != 200:
            return _remote_error(status, body, "task_read_failed")
        if not isinstance(body.get("task"), dict):
            return error_result("invalid_hub_response", "Hub task detail response is invalid")
        return ok_result("Hub task loaded", task=body["task"])
    params: dict[str, Any] = {"max_items": _bounded_int(args, "max_items", 50, 1, 200), "cursor": _bounded_int(args, "cursor", 0, 0, 2**63 - 1)}
    task_id = _optional_text(args, "task_id")
    if task_id:
        params["task_id"] = task_id
    status, body = MemoryHubClient(shared).get(_project_path(shared, "/task-events"), params, _timeout(shared))
    if status != 200:
        return _remote_error(status, body, "task_read_failed")
    if not isinstance(body.get("events"), list) or not isinstance(body.get("cursor"), str) or not body["cursor"].isdigit():
        return error_result("invalid_hub_response", "Hub task history response is invalid")
    return ok_result("Hub task history loaded", events=body["events"], cursor=body["cursor"])


def _command(config: MemoryConfig, shared: Any, action: str, args: dict[str, Any]) -> dict[str, Any]:
    body = _command_body(action, args)
    if not isinstance(body.get("command_id"), str) or not body["command_id"].strip():
        return error_result("invalid_input", "command_id is required for task commands")
    status, response = _post_command(shared, body)
    if status == 200:
        return ok_result("Hub accepted task command", **{k: v for k, v in response.items() if k not in {"ok", "message"}},
                         status="accepted", authoritative=True)
    if status in RETRYABLE_STATUSES and action in OFFLINE_ACTIONS:
        if not body.get("task_id"):
            return _remote_error(status, response, "task_authority_unavailable")
        try:
            TaskCommandOutbox(config.repo_root).enqueue(body)
        except (OSError, sqlite3.Error) as exc:
            return error_result("task_outbox_unavailable", f"Hub unavailable and the local command queue is not writable: {exc}")
        return ok_result("Hub unavailable; command queued and will be resent with the same command_id",
                         command_id=body["command_id"], task_id=body["task_id"], action=action,
                         status="queued", authoritative=False, remote_status=status)
    return _remote_error(status, response, "task_command_rejected")


def task_sync(config: MemoryConfig, args: dict[str, Any]) -> dict[str, Any]:
    """转发任务操作到 Hub；Hub 未配置时返回 ``task_hub_required``。"""
    action = str(args.get("action") or "sync").strip().lower()
    if action not in TASK_COMMAND_ACTIONS | READ_ACTIONS | OUTBOX_ACTIONS:
        return error_result("invalid_input", f"unsupported action {action}")
    if "actor_id" in args:
        return error_result("actor_not_accepted", "actor identity is derived by the Hub from this device credential")
    shared = getattr(config, "shared_memory", None)
    outbox = TaskCommandOutbox(config.repo_root)
    try:
        if action == "outbox":
            return _attach_failures(ok_result("Task command queue loaded", entries=outbox.entries()), outbox)
        if action == "outbox_dismiss":
            ids = args.get("command_ids") or ([args["command_id"]] if args.get("command_id") else [])
            if not isinstance(ids, list) or not ids or not all(isinstance(item, str) and item for item in ids):
                return error_result("invalid_input", "command_ids must be a non-empty list of failed command ids")
            return _attach_failures(ok_result("Task command queue updated", **outbox.dismiss(ids)), outbox)
        scopes = set(getattr(shared, "sync_scopes", ()) or ())
        if not bool(getattr(shared, "enabled", False)) or "project_shared" not in scopes:
            return error_result("task_hub_required", "task commands require an enabled Hub with project_shared sync")
        if not bool(getattr(shared, "active", False)):
            return error_result("hub_not_ready", "Hub enrollment or connection configuration is incomplete; retry later")
        device, failure = _ensure_device(shared)
        if failure is not None:
            # 只有此前确认过支持任务命令的 Hub 才允许离线排队，避免把命令攒给尚未升级的 Hub。
            confirmed = outbox.get_state(CONFIRMED_STATE_KEY) == _token_key(shared)
            if action in OFFLINE_ACTIONS and failure.get("retryable") and confirmed:
                result = _command(config, shared, action, args)
            else:
                return _attach_failures(failure, outbox)
        else:
            outbox.put_state(CONFIRMED_STATE_KEY, _token_key(shared))
            migrate_legacy_task_data(config)
            outbox.flush(shared)
            result = _sync(shared, device, args) if action == "sync" else (
                _read(shared, action, args) if action in READ_ACTIONS else _command(config, shared, action, args))
    except (OSError, sqlite3.Error) as exc:
        return error_result("task_outbox_unavailable", str(exc))
    except (TypeError, ValueError) as exc:
        return error_result("invalid_input", str(exc))
    return _attach_failures(result, outbox)


def _attach_failures(result: dict[str, Any], outbox: TaskCommandOutbox) -> dict[str, Any]:
    try:
        failures = outbox.failures()
    except (OSError, sqlite3.Error):
        return result
    if failures:
        result["outbox_failures"] = failures
    return result


def flush_task_outbox(config: MemoryConfig, *, budget_seconds: float = 5.0) -> dict[str, int]:
    """后台同步周期调用：Hub 声明支持任务命令后，完成一次性旧数据转换并补发待发命令。"""
    shared = getattr(config, "shared_memory", None)
    if not bool(getattr(shared, "active", False)) or not hub_supports_task_commands(config):
        return {"sent": 0, "retry": 0, "failed": 0}
    migrate_legacy_task_data(config)
    return TaskCommandOutbox(config.repo_root).flush(shared, budget_seconds=budget_seconds)


def task_outbox_failure_count(repo_root: Path) -> int:
    try:
        return TaskCommandOutbox(repo_root).failure_count()
    except (OSError, sqlite3.Error):
        return 0


__all__ = [
    "TaskCommandOutbox",
    "flush_task_outbox",
    "migrate_legacy_task_data",
    "task_outbox_failure_count",
    "task_sync",
]
