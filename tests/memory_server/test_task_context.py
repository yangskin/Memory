from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from servers.memory_server.memory_config import load_config
from servers.memory_server.server import _dispatch_tool


def _begin(
    config,
    *,
    agent_id: str,
    session: str,
    goal: str,
    user: str = "alice",
    external_ref: str | None = None,
    active_files: list[str] | None = None,
) -> dict:
    payload = {
        "operation": "task_context",
        "agent_id": agent_id,
        "client_session_id": session,
        "user": user,
        "workspace_id": "ToolTest",
        "branch": "main",
        "active_files": active_files or ["MCP/Memory/README.md"],
        "user_goal": goal,
    }
    if external_ref:
        payload["external_ref"] = external_ref
    return _dispatch_tool(config, "memory_read", payload)


def _write_marker(config, token: str, marker: str) -> dict:
    return _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "record",
            "context_token": token,
            "content_markdown": f"# Context Marker\n\nshared lookup marker {marker}\n",
            "record_kind": "note",
            "scope": "personal",
            "status": "validated",
            "tags": ["mcp"],
        },
    )


def _retrieve_markers(config, token: str) -> dict:
    return _dispatch_tool(
        config,
        "memory_read",
        {
            "operation": "retrieve_context",
            "context_token": token,
            "query": "shared lookup marker",
            "top_k": 10,
        },
    )


def _context_ids(result: dict) -> set[str]:
    return {str(item.get("id")) for item in result.get("context_items", [])}


def test_begin_task_reuses_same_client_session_binding(repo: Path) -> None:
    config = load_config(repo)

    first = _begin(config, agent_id="codex", session="codex-session-1", goal="Investigate memory task context")
    second = _begin(config, agent_id="codex", session="codex-session-1", goal="A later wording should not fork")

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["status"] == "matched_session"
    assert second["context_token"] == first["context_token"]
    assert second["task_id"] == first["task_id"]
    assert second["task_run_id"] == first["task_run_id"]
    assert second["current_task"]["path"] == first["current_task"]["path"]
    assert second["current_task"]["path"].startswith(".ai-context/current-task/alice/")


def test_begin_task_external_ref_merges_task_but_not_runs(repo: Path) -> None:
    config = load_config(repo)

    codex = _begin(
        config,
        agent_id="codex",
        session="codex-session-issue-7",
        goal="Fix memory task id isolation",
        external_ref="issue://memory/7",
    )
    copilot = _begin(
        config,
        agent_id="copilot",
        session="copilot-session-issue-7",
        goal="Implement task context token isolation",
        external_ref="issue://memory/7",
    )

    assert codex["ok"] is True
    assert copilot["ok"] is True
    assert copilot["status"] == "matched_existing"
    assert copilot["task_id"] == codex["task_id"]
    assert copilot["task_run_id"] != codex["task_run_id"]
    assert copilot["context_token"] != codex["context_token"]


def test_same_user_same_file_different_goals_get_isolated_tasks(repo: Path) -> None:
    config = load_config(repo)

    task_a = _begin(
        config,
        agent_id="codex",
        session="codex-session-a",
        goal="Design task context token resolver",
        active_files=["MCP/Memory/README.md"],
    )
    task_b = _begin(
        config,
        agent_id="copilot",
        session="copilot-session-b",
        goal="Document embedding model download presets",
        active_files=["MCP/Memory/README.md"],
    )

    assert task_a["ok"] is True
    assert task_b["ok"] is True
    assert task_a["task_id"] != task_b["task_id"]


def test_context_token_injects_user_but_does_not_hide_same_user_tasks(repo: Path) -> None:
    config = load_config(repo)
    task_a = _begin(
        config,
        agent_id="codex",
        session="codex-session-a",
        goal="Design task context token resolver",
    )
    task_b = _begin(
        config,
        agent_id="copilot",
        session="copilot-session-b",
        goal="Document embedding model download presets",
    )

    write_a = _write_marker(config, task_a["context_token"], "alpha")
    write_b = _write_marker(config, task_b["context_token"], "bravo")
    got_a = _retrieve_markers(config, task_a["context_token"])
    got_b = _retrieve_markers(config, task_b["context_token"])
    got_a_explicit_task = _dispatch_tool(
        config,
        "memory_read",
        {
            "operation": "retrieve_context",
            "context_token": task_a["context_token"],
            "query": "shared lookup marker",
            "task_id": task_a["task_id"],
            "top_k": 10,
        },
    )

    assert write_a["ok"] is True
    assert write_b["ok"] is True
    assert write_a["task_id"] == task_a["task_id"]
    assert write_b["task_id"] == task_b["task_id"]
    assert write_a["path"] != write_b["path"]
    assert f"/packs/{task_a['task_id']}/" in write_a["path"]
    assert f"/packs/{task_b['task_id']}/" in write_b["path"]
    assert write_a["author"] == "alice"
    assert write_b["author"] == "alice"
    assert got_a["ok"] is True
    assert got_b["ok"] is True
    assert write_a["id"] in _context_ids(got_a)
    assert write_b["id"] in _context_ids(got_a)
    assert write_b["id"] in _context_ids(got_b)
    assert write_a["id"] in _context_ids(got_b)
    assert got_a_explicit_task["ok"] is True
    assert write_a["id"] in _context_ids(got_a_explicit_task)
    assert write_b["id"] not in _context_ids(got_a_explicit_task)


def test_interleaved_agents_do_not_overwrite_current_task(repo: Path) -> None:
    config = load_config(repo)
    task_a = _begin(config, agent_id="codex", session="a", goal="Alpha task")
    task_b = _begin(config, agent_id="copilot", session="b", goal="Bravo task")

    # B begins after A, then A writes again. If the server had a global
    # current_task_id, this write would leak into B.
    write_b = _write_marker(config, task_b["context_token"], "bravo")
    write_a = _write_marker(config, task_a["context_token"], "alpha")

    assert write_b["ok"] is True
    assert write_a["ok"] is True
    assert write_a["task_context"]["task_id"] == task_a["task_id"]
    assert write_b["task_context"]["task_id"] == task_b["task_id"]
    assert task_a["current_task"]["path"] != task_b["current_task"]["path"]
    assert task_a["context_token"] in task_a["current_task"]["content"]
    assert task_b["context_token"] in task_b["current_task"]["content"]


def test_invalid_context_token_with_body_is_recovered_as_orphan(repo: Path) -> None:
    config = load_config(repo)

    result = _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "record",
            "context_token": "ctx_missing",
            "content_markdown": "# Should Not Write\n\ninvalid\n",
            "record_kind": "note",
            "scope": "personal",
            "status": "validated",
            "tags": ["mcp", "sample_domain"],
        },
    )

    assert result["ok"] is True
    assert result["task_id"] == "recovered_invalid_context"
    assert result["status"] == "raw"
    assert result["context_recovery"]["mode"] == "orphan"
    assert result["context_recovery"]["invalid_context_token"] == "ctx_missing"
    assert result["warnings"][0]["code"] == "context_token_invalid_recovered"
    written = (repo / result["path"]).read_text(encoding="utf-8")
    assert "mcp" in written
    assert "needs_validation" in written
    assert "sample_domain" not in written


def test_invalid_context_token_observation_with_body_is_recovered(repo: Path) -> None:
    config = load_config(repo)

    result = _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "observation",
            "context_token": "ctx_missing",
            "content_markdown": "# Observation\n\nUseful body must survive stale token.",
            "tags": ["workflow"],
        },
    )

    assert result["ok"] is True
    assert result["record_kind"] == "observation"
    assert result["task_id"] == "recovered_invalid_context"
    assert result["context_recovery"]["mode"] == "orphan"
    assert result["warnings"][0]["code"] == "context_token_invalid_recovered"


def test_invalid_context_token_without_body_is_structured_error(repo: Path) -> None:
    config = load_config(repo)

    result = _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "record",
            "context_token": "ctx_missing",
            "content_markdown": "",
            "record_kind": "note",
            "scope": "personal",
            "tags": ["mcp"],
        },
    )

    assert result["ok"] is False
    assert result["error"] == "invalid_context_token"


def test_invalid_context_token_checkpoint_with_body_is_not_recovered(repo: Path) -> None:
    config = load_config(repo)

    result = _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "checkpoint",
            "context_token": "ctx_missing",
            "task_phase": "task_done",
            "content_markdown": "# Checkpoint\n\nThis operation is not recoverable without valid task identity.",
        },
    )

    assert result["ok"] is False
    assert result["error"] == "invalid_context_token"


def test_invalid_context_token_rebinds_high_confidence_task(repo: Path) -> None:
    config = load_config(repo)
    task = _begin(
        config,
        agent_id="copilot",
        session="sample_domain-session",
        goal="Fix ASampleDomainActor editor preview for SampleDomain",
        active_files=["Source/SampleGame/Systems/SampleDomain/SampleDomainActor.cpp"],
    )

    result = _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "record",
            "context_token": "ctx_stale_from_other_session",
            "user": "alice",
            "agent_id": "copilot",
            "workspace_id": "ToolTest",
            "content_markdown": "# ASampleDomainActor Editor Preview\n\nRebuild SampleDomain editor preview through SampleDomainActor.",
            "record_kind": "decision",
            "scope": "personal",
            "system_area": "Source/SampleGame/Systems/SampleDomain",
            "tags": ["handoff_ready", "needs_validation"],
        },
    )

    assert result["ok"] is True
    assert result["task_id"] == task["task_id"]
    assert result["task_context"]["context_token"] == task["context_token"]
    assert result["context_recovery"]["mode"] == "rebound"
    assert result["context_recovery"]["context_rebound"] is True
    assert result["warnings"][0]["code"] == "context_token_invalid_rebound"


def test_parallel_agent_calls_share_store_without_cross_contamination(repo: Path) -> None:
    config = load_config(repo)

    def worker(idx: int) -> tuple[str, str, str]:
        started = _begin(
            config,
            agent_id=f"agent-{idx}",
            session=f"session-{idx}",
            goal=f"Parallel isolated task {idx}",
            active_files=[f"Source/Task{idx}.cpp"],
        )
        written = _write_marker(config, started["context_token"], f"parallel-{idx}")
        return started["task_id"], written["task_id"], written["task_context"]["agent_id"]

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(worker, range(6)))

    task_ids = {task_id for task_id, written_task_id, _agent_id in results}
    assert len(task_ids) == 6
    for task_id, written_task_id, agent_id in results:
        assert task_id == written_task_id
        assert agent_id.startswith("agent-")
