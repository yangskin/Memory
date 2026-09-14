from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from servers.memory_server import memory_auto_maintenance as maintenance
from servers.memory_server import memory_record_index as index
from servers.memory_server import memory_task_brief as brief
from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_paths import PathManager, PathSecurityError


def test_stdio_handshake_and_tools_respond_while_maintenance_is_blocked(tmp_path: Path) -> None:
    """真实 stdio 握手不能等待维护完成，后台队列关闭时维护仍执行。"""
    runtime = tmp_path / ".ai-memory"
    runtime.mkdir()
    (runtime / "config.json").write_text(json.dumps({
        "worker": {"enabled": False, "startup_grace_seconds": 0},
        "shared_memory": {"enabled": False},
    }), encoding="utf-8")
    script = """
import time
from pathlib import Path
from servers.memory_server import memory_auto_maintenance as maintenance
from servers.memory_server import memory_worker as worker
from servers.memory_server.server import main
def slow(config, **kwargs):
    (config.repo_root / 'maintenance-started').write_text('started')
    time.sleep(12)
    (config.repo_root / 'maintenance-finished').write_text('finished')
    return {'ok': True}
maintenance.run_if_due = slow
worker.run_if_due = slow
raise SystemExit(main())
"""

    async def probe() -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-B", "-c", script, "--root", str(tmp_path)],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await asyncio.wait_for(session.initialize(), timeout=6)
                tools = await asyncio.wait_for(session.list_tools(), timeout=2)
                assert "memory_read" in {tool.name for tool in tools.tools}
                deadline = time.monotonic() + 2
                while not (tmp_path / "maintenance-started").exists() and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                assert (tmp_path / "maintenance-started").exists()
                assert not (tmp_path / "maintenance-finished").exists()

    asyncio.run(probe())


def test_due_maintenance_does_not_rebuild_an_unchanged_index(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "memory-bank").mkdir()
    config = load_config(tmp_path)
    config = replace(config, mcp_auto_maintenance={"retention": {"enabled": False}})
    built = index.memory_rebuild_index(config)
    assert built["ok"], built
    monkeypatch.setattr(index, "memory_rebuild_index", lambda *_a, **_k: pytest.fail("unchanged index rebuilt"))
    monkeypatch.setattr(maintenance, "_guard_needs_optimization", lambda *_: False)
    monkeypatch.setattr("servers.memory_server.memory_maintenance.memory_health_check", lambda *_: {"ok": True})

    result = maintenance.run_if_due(config)

    assert result["ok"], result
    action = next(a for a in result["actions"] if a["step"] == "rebuild_index")
    assert action["result"]["message"] == "record index is fresh"
    assert result["elapsed_ms"] >= 0


def test_maintenance_competitor_does_not_wait_or_run_twice(tmp_path: Path, monkeypatch) -> None:
    config = load_config(tmp_path)
    entered, release = threading.Event(), threading.Event()
    results = []

    def blocked(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)
        return {"ok": True}

    monkeypatch.setattr(maintenance, "_run_if_due_locked", blocked)
    thread = threading.Thread(target=lambda: results.append(maintenance.run_if_due(config)))
    thread.start()
    try:
        assert entered.wait(3)
        started = time.monotonic()
        other = maintenance.run_if_due(config)
        assert time.monotonic() - started < 1
        assert other["reason"] == "maintenance_busy"
    finally:
        release.set()
        thread.join(3)
    assert results == [{"ok": True}]


def test_failed_maintenance_preserves_success_timestamp_for_retry(tmp_path: Path, monkeypatch) -> None:
    config = load_config(tmp_path)
    state_path = tmp_path / ".ai-memory" / "last_maintenance.json"
    state_path.write_text(json.dumps({"last_run_ts": 1}), encoding="utf-8")
    monkeypatch.setattr(maintenance, "_safe_run", lambda *_: {
        "step": "test", "ok": False, "result": {"error": "test_failure", "message": "bounded reason"},
    })

    result = maintenance.run_if_due(config)

    assert not result["ok"]
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["last_run_ts"] == 1
    assert state["actions_summary"][0]["error"] == "test_failure"
    assert state["actions_summary"][0]["message"] == "bounded reason"


def test_brief_skips_unused_latest_scan_even_when_relevant_result_is_empty(tmp_path: Path, monkeypatch) -> None:
    config = load_config(tmp_path)
    monkeypatch.setattr(brief, "memory_retrieve_context", lambda *_a, **_k: {"ok": True, "context_items": []})
    monkeypatch.setattr(brief, "memory_get_latest_memories", lambda *_a, **_k: pytest.fail("unused latest scan"))
    records, stats = brief._visible_evidence(config, user=None, branch=None, limit=10,
        query="startup", query_tokens={"startup"}, preferred_tags=set(), max_chars=2000, max_tokens=500)
    assert not records
    assert stats["retrieval_failed"] == 0
    assert stats["latest_fallback_used"] == 0


@pytest.mark.parametrize("latest_ok", [True, False])
def test_brief_still_attempts_latest_after_relevant_failure(tmp_path: Path, monkeypatch, latest_ok: bool) -> None:
    config = load_config(tmp_path)
    calls = []
    monkeypatch.setattr(brief, "memory_retrieve_context", lambda *_a, **_k: {"ok": False, "error": "test"})
    monkeypatch.setattr(brief, "memory_get_latest_memories", lambda *_a, **_k: calls.append(1) or {"ok": latest_ok})
    _, stats = brief._visible_evidence(config, user=None, branch=None, limit=10,
        query="startup", query_tokens={"startup"}, preferred_tags=set(), max_chars=2000, max_tokens=500)
    assert calls == [1]
    assert stats["retrieval_failed"] == 1
    assert stats["latest_fallback_used"] == int(latest_ok)


def test_file_scan_normalizes_each_candidate_once(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "memory-bank").mkdir()
    config = load_config(tmp_path)
    files = [tmp_path / "memory-bank" / f"record-{i}.md" for i in range(10)]
    for path in files:
        path.write_text("# record", encoding="utf-8")
    from servers.memory_server import memory_paths
    original = memory_paths._normalize_path
    counts = {}

    def counted(path):
        counts[path] = counts.get(path, 0) + 1
        return original(path)

    monkeypatch.setattr(memory_paths, "_normalize_path", counted)
    found = list(PathManager(config).iter_files(scopes=["memory-bank"], include_paths=["**/*.md"]))
    assert {path for path, _ in found} == set(files)
    assert all(counts[path] == 1 for path in files)


def test_file_scan_and_resolve_reject_links_outside_workspace(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    config = load_config(repo)
    (repo / "memory-bank").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "private.md"
    secret.write_text("outside", encoding="utf-8")
    link = repo / "memory-bank" / "escape.md"
    directory_link = repo / "memory-bank" / "escape-dir"
    try:
        link.symlink_to(secret)
        directory_link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    manager = PathManager(config)
    assert list(manager.iter_files(scopes=["memory-bank"])) == []
    with pytest.raises(PathSecurityError):
        manager.resolve("memory-bank/escape.md")
    with pytest.raises(PathSecurityError):
        manager.resolve("memory-bank/escape-dir/private.md")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction boundary")
def test_file_scan_rejects_windows_junction_and_rechecks_retargeted_directory(tmp_path: Path) -> None:
    import _winapi

    config = load_config(tmp_path / "repo")
    bank = config.repo_root / "memory-bank"
    bank.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.md").write_text("outside", encoding="utf-8")
    link = bank / "escape"
    _winapi.CreateJunction(str(outside), str(link))
    manager = PathManager(config)
    assert list(manager.iter_files(scopes=["memory-bank"])) == []
    with pytest.raises(PathSecurityError):
        manager.resolve("memory-bank/escape/private.md")
    link.rmdir()  # 只移除 junction 本身，外部目录仍存在。
    link.mkdir()
    (link / "safe.md").write_text("inside", encoding="utf-8")
    assert [p for _, p in manager.iter_files(scopes=["memory-bank"])] == ["memory-bank/escape/safe.md"]
    assert (outside / "private.md").is_file()
