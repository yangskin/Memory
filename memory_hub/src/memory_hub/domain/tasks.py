"""任务命令请求契约：按操作限定字段与上限，超限明确拒绝，不截断。

客户端只表达意图；执行者身份、版本、epoch、派生 ID 与时间全部由 Hub 计算。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MAX_INT = 2_147_483_647
MAX_BODY_BYTES = 64 * 1024
CHANNELS = frozenset({"dispatch"})
DECISIONS = frozenset({"approved", "changes_requested"})
_IDENTIFIER = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")
_AGENT = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_DERIVED = frozenset({"actor_id", "occurred_at", "task_version", "assignment_epoch", "attempt_id", "review_id"})

ACTIONS: dict[str, frozenset[str]] = {
    "create": frozenset({"task_id", "expected_version", "title", "objective", "acceptance", "priority", "depends_on", "parent_task_id", "produced_memory"}),
    "assign": frozenset({"task_id", "expected_version", "expected_assignment_epoch", "assignee"}),
    "reassign": frozenset({"task_id", "expected_version", "expected_assignment_epoch", "assignee"}),
    "claim": frozenset({"task_id", "expected_version", "expected_assignment_epoch"}),
    "decline": frozenset({"task_id", "expected_version", "expected_assignment_epoch"}),
    "report": frozenset({"task_id", "expected_version", "expected_assignment_epoch", "summary"}),
    "block": frozenset({"task_id", "expected_version", "expected_assignment_epoch", "reason"}),
    "resume": frozenset({"task_id", "expected_version", "expected_assignment_epoch"}),
    "submit": frozenset({"task_id", "expected_version", "expected_assignment_epoch", "summary", "evidence"}),
    "review": frozenset({"task_id", "expected_version", "expected_assignment_epoch", "decision", "summary", "submission_id"}),
    "cancel": frozenset({"task_id", "expected_version", "expected_assignment_epoch", "reason"}),
}
_COMMON = frozenset({"command_id", "action", "channel", "agent"})
_REQUIRES_VERSION = frozenset(ACTIONS) - {"create", "report"}
_REQUIRES_EPOCH = frozenset({"reassign", "claim", "decline", "report", "block", "resume", "submit"})
_TEXT_LIMITS = {"title": 1024, "objective": 8192, "acceptance": 8192, "summary": 8192, "reason": 8192, "priority": 64}
_REQUIRED_TEXT = {"create": ("title",), "report": ("summary",), "block": ("reason",), "submit": ("summary",)}
_LIST_LIMITS = {"depends_on": (64, True), "produced_memory": (64, True), "evidence": (32, False)}


class TaskCommandError(Exception):
    """可预期的命令拒绝；status/code 直接映射为结构化 HTTP 错误。"""

    def __init__(self, status: int, code: str, message: str, *, field: str | None = None, current: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.field = field
        self.current = current

    def body(self) -> dict[str, Any]:
        result: dict[str, Any] = {"ok": False, "error": self.code, "message": self.message}
        if self.field:
            result["field"] = self.field
        if self.current is not None:
            result["current"] = self.current
        return result


def invalid(field_name: str, message: str, code: str = "invalid_input") -> TaskCommandError:
    return TaskCommandError(422, code, message, field=field_name)


@dataclass(frozen=True)
class TaskCommand:
    command_id: str
    action: str
    channel: str | None
    agent: str | None
    task_id: str | None
    expected_version: int | None
    expected_assignment_epoch: int | None
    values: dict[str, Any] = field(default_factory=dict)

    def canonical(self) -> dict[str, Any]:
        """参与幂等摘要的规范化意图（不含执行者，执行者单独比较）。"""
        return {
            "action": self.action,
            "channel": self.channel,
            "task_id": self.task_id,
            "expected_version": self.expected_version,
            "expected_assignment_epoch": self.expected_assignment_epoch,
            "values": self.values,
        }


def _identifier(raw: dict[str, Any], key: str, *, required: bool) -> str | None:
    value = raw.get(key)
    if value is None:
        if required:
            raise invalid(key, f"{key} is required")
        return None
    if not isinstance(value, str) or not _IDENTIFIER.match(value) or value != value.strip():
        raise invalid(key, f"{key} must be 1-256 characters without control characters or surrounding whitespace")
    return value


def _integer(raw: dict[str, Any], key: str, *, required: bool) -> int | None:
    value = raw.get(key)
    if value is None:
        if required:
            raise invalid(key, f"{key} is required", "missing_assignment_epoch" if key == "expected_assignment_epoch" else "invalid_input")
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_INT:
        raise invalid(key, f"{key} must be an integer between 0 and {MAX_INT}")
    return value


def _text(raw: dict[str, Any], key: str, *, required: bool) -> str:
    value = raw.get(key)
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise invalid(key, f"{key} must be a string")
    text = value.strip()
    if required and not text:
        raise invalid(key, f"{key} is required")
    if len(text) > _TEXT_LIMITS[key]:
        raise invalid(key, f"{key} exceeds {_TEXT_LIMITS[key]} characters")
    return text


def _list(raw: dict[str, Any], key: str) -> list[str]:
    value = raw.get(key)
    if value is None:
        return []
    maximum, dedupe = _LIST_LIMITS[key]
    if not isinstance(value, list):
        raise invalid(key, f"{key} must be an array")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > 1024:
            raise invalid(key, f"{key} items must be 1-1024 character strings")
        text = item.strip()
        if dedupe and text in result:
            continue
        result.append(text)
    if len(result) > maximum:
        raise invalid(key, f"{key} accepts at most {maximum} items")
    return result


def parse_command(raw: object) -> TaskCommand:
    if not isinstance(raw, dict):
        raise invalid("body", "request body must be a JSON object")
    if "actor_id" in raw:
        raise TaskCommandError(422, "actor_not_accepted", "actor identity is derived from the device credential", field="actor_id")
    action = raw.get("action")
    if not isinstance(action, str) or action not in ACTIONS:
        raise invalid("action", "action must be one of " + ", ".join(sorted(ACTIONS)))
    allowed = ACTIONS[action] | _COMMON
    for key in raw:
        if key in _DERIVED:
            raise invalid(key, f"{key} is computed by the Hub and must not be sent")
        if key not in allowed:
            raise invalid(key, f"{key} is not accepted for {action}")
    command_id = _identifier(raw, "command_id", required=True)
    channel = raw.get("channel")
    if channel is not None and (not isinstance(channel, str) or channel not in CHANNELS):
        raise TaskCommandError(422, "invalid_channel", "channel must be one of " + ", ".join(sorted(CHANNELS)), field="channel")
    agent = raw.get("agent")
    if agent is not None and (not isinstance(agent, str) or not _AGENT.match(agent)):
        raise invalid("agent", "agent must be 1-64 characters of letters, digits, dot, underscore or dash")
    task_id = _identifier(raw, "task_id", required=action != "create")
    expected_version = _integer(raw, "expected_version", required=action in _REQUIRES_VERSION)
    if action == "create":
        # 0 表示任务尚不存在；兼容“每次修改都带 expected_version”的调用写法，并归一化以免影响幂等摘要。
        if expected_version not in (None, 0):
            raise invalid("expected_version", "create accepts only expected_version=0 (the task must not exist yet)")
        expected_version = None
    expected_epoch = _integer(raw, "expected_assignment_epoch", required=action in _REQUIRES_EPOCH)
    values: dict[str, Any] = {}
    for key in ("title", "objective", "acceptance", "priority", "summary", "reason"):
        if key in ACTIONS[action]:
            values[key] = _text(raw, key, required=key in _REQUIRED_TEXT.get(action, ()))
    if action == "create":
        values["priority"] = values["priority"] or "normal"
        values["parent_task_id"] = _identifier(raw, "parent_task_id", required=False)
    for key in ("depends_on", "produced_memory", "evidence"):
        if key in ACTIONS[action]:
            values[key] = _list(raw, key)
    if action in {"assign", "reassign"}:
        values["assignee"] = _identifier(raw, "assignee", required=True)
    if action == "review":
        decision = raw.get("decision")
        if decision not in DECISIONS:
            raise invalid("decision", "decision must be approved or changes_requested")
        values["decision"] = decision
        values["submission_id"] = _identifier(raw, "submission_id", required=False)
    return TaskCommand(
        command_id=command_id,
        action=action,
        channel=channel,
        agent=agent,
        task_id=task_id,
        expected_version=expected_version,
        expected_assignment_epoch=expected_epoch,
        values=values,
    )


__all__ = ["ACTIONS", "MAX_BODY_BYTES", "MAX_INT", "TaskCommand", "TaskCommandError", "invalid", "parse_command"]
