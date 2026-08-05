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


def test_dedicated_board_tools_reuse_board_read_write_flow(repo: Path) -> None:
    config = load_config(repo)

    posted = _dispatch_tool(
        config,
        "memory_board_write",
        {
            "action": "post",
            "post_type": "request",
            "content": "验证专用留言板工具",
            "task_id": "board-discovery",
        },
    )
    assert posted["ok"] is True

    queried = _dispatch_tool(
        config,
        "memory_board_read",
        {"filter": "unresolved", "task_id": "board-discovery", "max_items": 20},
    )
    assert queried["ok"] is True
    assert any(item["post_id"] == posted["post"]["post_id"] for item in queried["items"])


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


def test_board_write_is_local_first_even_when_remote_available(repo: Path, monkeypatch) -> None:
    config = load_config(repo)
    monkeypatch.setattr(dispatch_module, "_schedule_board_sync", lambda _config: None)

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
    assert result["post"]["post_id"]
    assert result["board_sync"]["queued"] is True
    assert result["board_sync"]["non_blocking"] is True


def test_board_post_normalizes_blank_optional_values_for_remote(repo: Path, monkeypatch) -> None:
    config = load_config(repo)
    captured = {}
    monkeypatch.setattr(dispatch_module, "_schedule_board_sync", lambda _config: None)

    def fake_remote(_config, payload):
        captured.update(payload)
        return {
            "ok": True,
            "remote": {
                "post": {
                    "post_id": "remote-blank",
                    "thread_id": "remote-blank",
                    "post_type": "note",
                    "status": "open",
                }
            },
            "http_status": 200,
        }

    monkeypatch.setattr(dispatch_module, "remote_board_post", fake_remote)
    result = _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "board",
            "action": "post",
            "post_type": "note",
            "content_markdown": "空 UUID 应按未填写处理",
            "thread_id": "",
            "task_id": "",
        },
    )

    assert result["ok"] is True
    sync = dispatch_module._sync_pending_board_posts(config)
    assert sync["synced"] == 1
    assert captured["thread_id"] is None
    assert captured["task_id"] is None
    assert captured["references_json"] == []


def test_board_falls_back_to_local_when_remote_fails(repo: Path, monkeypatch) -> None:
    config = load_config(repo)
    monkeypatch.setattr(dispatch_module, "_schedule_board_sync", lambda _config: None)

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
    assert result["board_sync"]["queued"] is True
    assert result["board_sync"]["non_blocking"] is True
    assert result["post"]["remote_sync"] == "pending"


def test_board_query_retries_pending_local_post_to_remote(repo: Path, monkeypatch) -> None:
    config = load_config(repo)
    calls = {"post": 0}
    monkeypatch.setattr(dispatch_module, "_schedule_board_sync", lambda _config: None)

    def fake_remote_post(_config, payload):
        calls["post"] += 1
        return {
            "ok": True,
            "remote": {
                "post": {
                    "post_id": payload["post_id"],
                    "thread_id": payload["post_id"],
                    "post_type": payload["post_type"],
                    "content": payload["content"],
                    "status": "open",
                    "created_at": "2026-08-05T00:00:00+00:00",
                }
            },
            "http_status": 200,
        }

    monkeypatch.setattr(dispatch_module, "remote_board_post", fake_remote_post)
    local_post_id = {"value": ""}

    def fake_remote_query(_config, _payload):
        return {
            "ok": True,
            "remote": {
                "filter": "unresolved",
                "total": 1,
                "items": [{
                    "post_id": local_post_id["value"],
                    "thread_id": local_post_id["value"],
                    "post_type": "question",
                    "content": "需要远端补传",
                    "status": "open",
                    "created_at": "2026-08-05T00:00:00+00:00",
                }],
            },
            "http_status": 200,
        }

    monkeypatch.setattr(dispatch_module, "remote_board_query", fake_remote_query)

    fallback = _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "board",
            "action": "post",
            "post_type": "question",
            "content_markdown": "需要远端补传",
            "task_id": "sync-task",
        },
    )
    assert fallback["board_sync"]["queued"] is True
    local_post_id["value"] = fallback["post"]["post_id"]

    sync = dispatch_module._sync_pending_board_posts(config)
    assert sync["synced"] == 1

    queried = _dispatch_tool(
        config,
        "memory_read",
        {
            "operation": "board",
            "action": "query",
            "filter": "unresolved",
            "task_id": "sync-task",
        },
    )
    assert queried["ok"] is True
    assert [item["post_id"] for item in queried["items"]] == [local_post_id["value"]]


def test_board_query_merges_pending_local_when_retry_still_fails(repo: Path, monkeypatch) -> None:
    config = load_config(repo)
    monkeypatch.setattr(dispatch_module, "_schedule_board_sync", lambda _config: None)
    monkeypatch.setattr(
        dispatch_module,
        "remote_board_post",
        lambda _config, _payload: {"ok": False, "error": "remote_unavailable", "http_status": 0},
    )

    fallback = _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "board",
            "action": "post",
            "post_type": "warning",
            "content_markdown": "远端未恢复也必须可见",
            "task_id": "merge-task",
        },
    )
    local_id = fallback["post"]["post_id"]

    monkeypatch.setattr(
        dispatch_module,
        "remote_board_query",
        lambda _config, _payload: {
            "ok": True,
            "remote": {"filter": "unresolved", "total": 0, "items": []},
            "http_status": 200,
        },
    )
    queried = _dispatch_tool(
        config,
        "memory_read",
        {
            "operation": "board",
            "action": "query",
            "filter": "unresolved",
            "task_id": "merge-task",
        },
    )
    assert queried["ok"] is True
    assert any(item["post_id"] == local_id for item in queried["items"])


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
