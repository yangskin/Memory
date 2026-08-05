from __future__ import annotations

from pathlib import Path

from servers.memory_server.memory_config import load_config
from servers.memory_server.server import _dispatch_tool
from servers.memory_server import server_dispatch as dispatch_module


def test_board_post_query_reply_resolve_flow(repo: Path) -> None:
    config = load_config(repo)

    post = _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "board",
            "action": "post",
            "post_type": "question",
            "content_markdown": "请确认网络接口修改影响",
            "task_id": "network",
        },
    )
    assert post["ok"] is True
    root = post["post"]
    assert root["post_type"] == "question"
    assert root["status"] == "open"
    assert root["thread_id"] == root["post_id"]

    reply = _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "board",
            "action": "reply",
            "thread_id": root["thread_id"],
            "reply_to": root["post_id"],
            "content_markdown": "已检查，影响在复制排序阶段。",
            "task_id": "network",
        },
    )
    assert reply["ok"] is True
    assert reply["post"]["post_type"] == "reply"
    assert reply["post"]["thread_id"] == root["thread_id"]

    unresolved = _dispatch_tool(
        config,
        "memory_read",
        {
            "operation": "board",
            "action": "query",
            "filter": "unresolved",
            "task_id": "network",
            "max_items": 20,
        },
    )
    assert unresolved["ok"] is True
    ids = {item["post_id"] for item in unresolved["items"]}
    assert root["post_id"] in ids
    assert reply["post"]["post_id"] in ids

    resolved = _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "board",
            "action": "resolve",
            "post_id": root["post_id"],
        },
    )
    assert resolved["ok"] is True
    assert resolved["post"]["status"] == "resolved"

    resolved_list = _dispatch_tool(
        config,
        "memory_read",
        {
            "operation": "board",
            "action": "query",
            "status": "resolved",
            "thread_id": root["thread_id"],
            "max_items": 20,
        },
    )
    assert resolved_list["ok"] is True
    assert any(item["post_id"] == root["post_id"] for item in resolved_list["items"])


def test_board_post_type_validation(repo: Path) -> None:
    config = load_config(repo)
    result = _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "board",
            "action": "post",
            "post_type": "decision",
            "content_markdown": "不允许的类型",
        },
    )
    assert result["ok"] is False
    assert result["error"] == "invalid_input"


def test_board_uses_remote_when_available(repo: Path, monkeypatch) -> None:
    config = load_config(repo)

    monkeypatch.setattr(
        dispatch_module,
        "remote_board_post",
        lambda _config, _payload: {
            "ok": True,
            "remote": {
                "post": {
                    "post_id": "remote-1",
                    "thread_id": "remote-1",
                    "post_type": "question",
                    "status": "open",
                }
            },
            "http_status": 200,
        },
    )

    result = _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "board",
            "action": "post",
            "post_type": "question",
            "content_markdown": "远端优先",
        },
    )
    assert result["ok"] is True
    assert result["post"]["post_id"] == "remote-1"
    assert result["board_sync"]["remote"] is True


def test_board_falls_back_to_local_when_remote_fails(repo: Path, monkeypatch) -> None:
    config = load_config(repo)

    monkeypatch.setattr(
        dispatch_module,
        "remote_board_post",
        lambda _config, _payload: {
            "ok": False,
            "error": "remote_unavailable",
            "message": "remote_unavailable",
            "http_status": 0,
        },
    )

    result = _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "board",
            "action": "post",
            "post_type": "note",
            "content_markdown": "远端失败本地兜底",
        },
    )
    assert result["ok"] is True
    assert result["post"]["post_type"] == "note"
    assert result["board_sync"]["fallback"] is True
    assert result["board_sync"]["error"] == "remote_unavailable"


def test_task_context_injects_open_board_items_by_default(repo: Path) -> None:
    config = load_config(repo)
    _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "board",
            "action": "post",
            "post_type": "question",
            "content_markdown": "当前任务有一个待确认问题",
            "task_id": "network",
        },
    )

    ctx = _dispatch_tool(
        config,
        "memory_read",
        {
            "operation": "task_context",
            "task_id": "network",
            "user": "alice",
            "agent_id": "pytest",
            "user_goal": "继续 network 任务",
            "board_max_items": 5,
            "board_max_tokens": 500,
        },
    )
    assert ctx["ok"] is True
    assert "open_board_items" in ctx
    assert any(item["task_id"] == "network" for item in ctx["open_board_items"])
    if isinstance(ctx.get("task_brief"), dict) and ctx["task_brief"].get("ok"):
        assert "open_board_items" in ctx["task_brief"]
