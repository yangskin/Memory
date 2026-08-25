"""依赖损坏时顶上来的最小 Memory MCP server（纯标准库）。

为什么不能复用 `mcp` 包：这个 server 存在的唯一场景就是 `mcp` 本身缺失、过旧或装成
了未迁移的大版本。真正的 server 在 import 阶段就崩，客户端只会显示 Memory MCP 不可用，
LLM 完全拿不到原因，也无从触发修复。

MCP 的 stdio 传输就是按行分隔的 JSON-RPC 2.0，用标准库实现握手 + 两个工具完全够用。
于是坏环境下客户端仍然连得上一个 server，LLM 能看到：

* `memory_environment_status` —— 到底缺什么 / 版本越界到哪里 / 自动修复试过什么；
* `memory_repair_environment` —— 主动再修一次（绕过冷却，因为这是显式请求）。

协议纪律：stdout 只允许出现 JSON-RPC 报文，一切人类可读信息都走 stderr。

`serverInfo.name` 固定带 `-degraded` 后缀：这是给启动探测器（以及任何健康检查）用的
机器可读信号，表示"握手成功但真实工具不在"，别把降级模式误判成部署成功。
"""

from __future__ import annotations

import json
import sys
from typing import Any, Optional

from .dependency_guard import REPAIR_HINT, check_dependencies, format_report

PROTOCOL_VERSION = "2024-11-05"

# 单条请求行的上限（字符数 —— stdio 是文本流，`readline(n)` 数的也是字符）。降级模式只收
# initialize / tools/list / tools/call 这类很小的报文，1 Mi 个字符已经远超需要；设上限是为了
# 不让一条畸形超长行把这个最后的诊断通道拖死。
MAX_REQUEST_CHARS = 1024 * 1024

_DEGRADED_BANNER = (
    "This Memory MCP server started in DEPENDENCY REPAIR MODE: its Python "
    "environment does not satisfy the declared requirements, so the real memory "
    "tools (memory_read / memory_write / memory_board_read / memory_board_write / "
    "memory_task_sync) are unavailable. An automatic repair was attempted. Call "
    "memory_environment_status for the diagnosis, memory_repair_environment to retry "
    "the repair, then restart this MCP server to get the real tools back. Do not "
    "treat the missing tools as 'the feature does not exist', and do not conclude "
    "that memory is empty."
)


def _stderr(message: str) -> None:
    try:
        sys.stderr.write(f"[fallback] {message}\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


def _summary_text(server_name: str, outcome: dict[str, Any]) -> str:
    """把启动时的体检/修复结论压成给 LLM 读的文本。"""
    report = outcome.get("after") or outcome.get("before") or {}
    lines = [
        f"server: {server_name}",
        "mode: dependency repair mode (real memory tools unavailable)",
        f"interpreter: {report.get('interpreter') or sys.executable}",
        f"python: {report.get('python_version') or '?'}",
        f"requirements: {report.get('requirements') or '?'}",
        f"status: {report.get('status')}",
        f"diagnosis: {format_report(report) if report else 'no report available'}",
    ]

    # 元数据齐全但 import 失败时，上面的 status 会显示 ok，单看它会得出错误结论。
    import_error = outcome.get("import_error")
    if import_error:
        lines.append(f"import check: FAILED - {import_error}")
        lines.append(
            "the declared dependencies are present but not usable in this interpreter "
            "(a half-installed distribution, an ABI mismatch, or a .pth path entry that "
            "only takes effect on a fresh interpreter start)"
        )

    blocked = outcome.get("blocked_reason")
    if blocked:
        lines.append(f"auto-repair skipped: {blocked}")

    repair = outcome.get("repair")
    if isinstance(repair, dict):
        lines.append(
            f"auto-repair: attempted={repair.get('attempted')} "
            f"repaired={repair.get('repaired')} method={repair.get('method')}"
        )
        if repair.get("error"):
            lines.append(f"auto-repair error: {repair['error']}")
        for step in repair.get("steps") or []:
            lines.append(f"  step {step.get('step')}: ok={step.get('ok')} - {step.get('detail')}")

    lines.append(f"manual fix: {REPAIR_HINT}")
    lines.append(
        "after a successful repair the server must be restarted (reload the MCP "
        "server in your client) - this process cannot hot-swap the real tools in."
    )
    lines.append(
        "while degraded, memory reads and writes are NOT persisted; continue the task "
        "with local reasoning and tell the user memory is unavailable."
    )
    return "\n".join(lines)


def _tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "memory_environment_status",
            "description": (
                "REQUIRED FIRST CALL. This Memory MCP server is running in dependency "
                "repair mode: the real memory tools are unavailable because its Python "
                "environment is broken. Returns exactly what is missing, out of range, "
                "or failed to install, plus how to fix it."
            ),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "memory_repair_environment",
            "description": (
                "Retry the automatic dependency repair for this server's virtual "
                "environment (offline wheels first, then PyPI). Use after "
                "memory_environment_status. On success, restart this MCP server to get "
                "the real memory tools back."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "allow_network": {
                        "type": "boolean",
                        "description": (
                            "Allow the PyPI fallback when offline wheels cannot fix it "
                            "(default true)."
                        ),
                    }
                },
                "additionalProperties": False,
            },
        },
    ]


def _call_tool(
    name: str,
    arguments: dict[str, Any],
    server_name: str,
    outcome: dict[str, Any],
) -> tuple[str, bool]:
    """执行兜底工具，返回 (文本, 是否错误)。"""
    if name == "memory_environment_status":
        return _summary_text(server_name, outcome), False

    if name == "memory_repair_environment":
        allow_network = arguments.get("allow_network", True)
        if not isinstance(allow_network, bool):
            return "allow_network must be a boolean", True

        # 显式请求：绕过冷却和"本进程已试过一次"的闸门 —— 那些闸门是为了防止客户端
        # 自动重启导致的装包循环，不该阻挡 LLM 或用户的主动重试。但跨进程锁仍要走：
        # 手动重试完全可能撞上另一个进程正在跑的启动期修复。
        from .dependency_guard import (
            PROBE_MODULES,
            _reset_metadata_caches,
            locked_repair as run_repair,
            probe_imports,
        )

        # 我们来到兜底 server，偏偏依赖元数据是齐的 —— 那就是"包目录没了、dist-info
        # 还在"这一类环境。此时普通 `install -r` 是空操作，必须 `--force-reinstall`。
        metadata_looked_fine = (outcome.get("before") or {}).get("status") in {"ok", "unknown"}
        # `verify_modules` 让"装完 import 一遍、必要时再强制重装一次"都发生在锁内，
        # 同时也补做 `.pth` 处理 —— 少了它，刚装好的包在本进程里可能仍然 import 不到，
        # 环境明明修好了却报告失败。
        result = run_repair(
            allow_network=allow_network,
            force_reinstall=metadata_looked_fine,
            verify_modules=PROBE_MODULES,
        )
        _reset_metadata_caches()
        after = check_dependencies()
        import_ok, import_error = probe_imports(PROBE_MODULES)
        ok = after.get("status") in {"ok", "unknown"} and import_ok

        payload = {
            "repaired": bool(result.get("repaired")) and ok,
            "method": result.get("method"),
            "environment_status": after.get("status"),
            "diagnosis": format_report(after),
            "import_error": import_error,
            "steps": result.get("steps"),
            "error": result.get("error"),
            "next_step": (
                "Repair succeeded. Restart this MCP server in your client to load the "
                "real memory tools."
                if ok
                else "Repair failed. Report the steps above to the user; do not retry in a loop."
            ),
        }
        # 修好之后把内存里的结论也更新掉，后续 status 调用不再报旧问题。
        if ok:
            outcome["ok"] = True
            outcome["after"] = after
            outcome["import_error"] = None
        return json.dumps(payload, indent=2), not ok

    return f"unknown tool: {name}", True


def _handle(
    message: dict[str, Any],
    server_name: str,
    outcome: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """处理一条 JSON-RPC 报文。返回 None 表示这是通知，不该回响应。"""
    method = message.get("method")
    msg_id = message.get("id")
    is_request = msg_id is not None

    if method == "initialize" and is_request:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": f"{server_name}-degraded", "version": "repair-mode"},
                "instructions": _DEGRADED_BANNER,
            },
        }

    if method == "tools/list" and is_request:
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": _tool_definitions()}}

    if method == "tools/call" and is_request:
        params = message.get("params") or {}
        name = params.get("name")
        # 只有"没给"才补默认值。用 `or {}` 会把 `[]`/`""` 这类畸形取值悄悄当成空参数，
        # 反而绕过下面的类型校验。
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(name, str):
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32602, "message": "params.name must be a string"},
            }
        if not isinstance(arguments, dict):
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32602, "message": "params.arguments must be an object"},
            }
        try:
            text, is_error = _call_tool(name, arguments, server_name, outcome)
        except Exception as exc:  # noqa: BLE001 — 兜底 server 不允许自己崩
            text, is_error = f"{type(exc).__name__}: {exc}", True
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"content": [{"type": "text", "text": text}], "isError": is_error},
        }

    if method == "ping" and is_request:
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    # resources/prompts 没有实现：明确回 method not found，比假装支持要好。
    if is_request:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"method not found in repair mode: {method}"},
        }
    return None


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _decode_and_handle(
    text: str, server_name: str, outcome: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """解析一行并交给 `_handle`，把协议层错误变成可见的响应。

    以前坏 JSON 和 JSON-RPC batch（数组）都被静默 `continue` 掉。兜底 server 的全部
    价值就是"说清出了什么问题"，静默丢弃恰好把它退化成一个连得上但永不回话的
    server —— 客户端只能一路超时，比明确报错更难排查。
    """
    try:
        message = json.loads(text)
    except json.JSONDecodeError as exc:
        return _error(None, -32700, f"parse error: {exc}")

    if isinstance(message, list):
        # MCP 现行协议已经去掉了 batch。明确拒绝，而不是让客户端等一个不会来的响应。
        return _error(None, -32600, "batch requests are not supported in repair mode")
    if not isinstance(message, dict):
        return _error(None, -32600, "request must be a JSON object")

    return _handle(message, server_name, outcome)


def _drain_line(src: Any) -> bool:
    """丢弃当前这一行剩下的部分，返回是否还能继续读。

    截断读之后必须把尾巴吃掉：否则超长行的后半截会被当成下一条请求，之后每一条都错位。
    """
    while True:
        try:
            chunk = src.readline(MAX_REQUEST_CHARS + 1)
        except (OSError, ValueError):
            return False
        if not chunk:
            return False
        if chunk.endswith("\n"):
            return True


def _force_utf8(stream: Any) -> None:
    """把 stdio 强制成 UTF-8。

    MCP 要求 UTF-8，但 Windows 上 Python 默认按系统 locale（中文机器是 GBK）编码
    stdout。`mcp` 库自己会重设编码，纯标准库实现必须做同样的事，否则报文里的非 ASCII
    字符会被替换成 U+FFFD 或直接抛 UnicodeEncodeError —— 恰好发生在诊断信息里。
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):  # 被重定向成不可重设的流时忽略
        pass


def serve_diagnostics(
    server_name: str,
    outcome: dict[str, Any],
    stdin: Any = None,
    stdout: Any = None,
) -> None:
    """在 stdio 上跑最小 MCP server，直到客户端断开。"""
    src = stdin if stdin is not None else sys.stdin
    dst = stdout if stdout is not None else sys.stdout
    _force_utf8(src)
    _force_utf8(dst)

    _stderr(f"{server_name} is serving dependency diagnostics only")

    while True:
        try:
            # 上限必须传给 readline 本身。事后再判长度是没用的：那时整行已经读进内存，
            # 而这正是要防的事 —— 一条畸形的超长行把"最后还能连上的通道"自己拖死。
            line = src.readline(MAX_REQUEST_CHARS + 1)
        except (OSError, ValueError):
            break
        if not line:
            break
        if len(line) > MAX_REQUEST_CHARS:
            response = _error(None, -32600, "request line is too large for repair mode")
            # 把这一行剩下的部分丢掉，否则它的尾巴会被当成下一条请求。
            if not line.endswith("\n") and not _drain_line(src):
                break
        else:
            text = line.strip()
            if not text:
                continue
            response = _decode_and_handle(text, server_name, outcome)

        if response is None:
            continue
        try:
            # ensure_ascii=True：即使流的重设失败，转义后的报文也一定能安全写出。
            dst.write(json.dumps(response) + "\n")
            dst.flush()
        except (OSError, ValueError):
            break

    _stderr("client disconnected")
