from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_guard import memory_guard_check
from servers.memory_server.memory_guard_optimizer import optimize_guard_targets, optimize_text_for_guard


def _config(tmp_path: Path):
    (tmp_path / ".ai-memory").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai-memory" / "config.json").write_text(
        json.dumps(
            {
                "allowed_roots": ["memory-bank", ".ai-context"],
                "guard": {
                    "default_max_tokens": 3000,
                    "targets": [
                        {
                            "path": "memory-bank/progress.md",
                            "max_chars": 300,
                            "policy": "warm_context",
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    return load_config(tmp_path)


def test_optimize_text_prefers_llm_when_available(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)

    class _Envelope:
        ok = True
        value = "# Progress\n\n- compact from llm\n"
        status = "ok"

        def to_dict(self) -> dict[str, Any]:
            return {"ok": True, "status": "ok", "capability": "guard_compaction"}

    import servers.memory_server.memory_llm_runner as runner

    monkeypatch.setattr(runner, "run_llm_capability", lambda *a, **kw: _Envelope())

    source = "# Progress\n\n" + ("verbose detail\n" * 200)
    text, meta = optimize_text_for_guard(
        config,
        rel_path="memory-bank/progress.md",
        text=source,
        prefer_llm=True,
    )

    assert meta["optimized"] is True
    assert meta["method"] == "llm"
    assert "compact from llm" in text
    assert len(text) <= 300


def test_optimize_text_falls_back_deterministically_without_llm(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)

    class _Envelope:
        ok = False
        status = "unavailable"
        error = "no llm"

        def to_dict(self) -> dict[str, Any]:
            return {"ok": False, "status": "unavailable", "error": "no llm"}

    import servers.memory_server.memory_llm_runner as runner

    monkeypatch.setattr(runner, "run_llm_capability", lambda *a, **kw: _Envelope())

    source = "# Progress\n\n## Current sprint\n" + ("- verbose detail\n" * 200)
    text, meta = optimize_text_for_guard(
        config,
        rel_path="memory-bank/progress.md",
        text=source,
        prefer_llm=True,
    )

    assert meta["optimized"] is True
    assert meta["method"] == "deterministic"
    assert len(text) <= 300


def test_optimize_text_does_not_touch_legacy_generated_header_within_budget(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = (
        "<!-- generated_by=memory-mcp renderer=deterministic "
        "source_record_ids=[a,b] generated_at=2026-08-05T00:00:00+00:00 "
        "config_hash=abc123 guard_optimized=deterministic -->\n\n"
        "# Progress\n\n- Stable content\n"
    )

    text, meta = optimize_text_for_guard(
        config,
        rel_path="memory-bank/progress.md",
        text=source,
        prefer_llm=False,
    )

    assert meta["optimized"] is False
    assert meta["reason"] == "within_budget"
    assert text == source


def test_deterministic_guard_compaction_does_not_restore_legacy_header_fields(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)

    class _Envelope:
        ok = False
        status = "unavailable"
        error = "no llm"

        def to_dict(self) -> dict[str, Any]:
            return {"ok": False, "status": "unavailable", "error": "no llm"}

    import servers.memory_server.memory_llm_runner as runner

    monkeypatch.setattr(runner, "run_llm_capability", lambda *a, **kw: _Envelope())
    source = (
        "<!-- generated_by=memory-mcp renderer=deterministic "
        "source_record_ids=[a,b] generated_at=2026-08-05T00:00:00+00:00 "
        "config_hash=abc123 guard_optimized=deterministic -->\n\n"
        "# Progress\n\n## Current sprint\n" + ("- verbose detail\n" * 200)
    )

    text, meta = optimize_text_for_guard(
        config,
        rel_path="memory-bank/progress.md",
        text=source,
        prefer_llm=True,
    )

    assert meta["method"] == "deterministic"
    assert text.startswith("<!-- generated_by=memory-mcp renderer=deterministic -->")
    assert "source_record_ids" not in text
    assert "generated_at=" not in text
    assert "config_hash=" not in text
    assert "guard_optimized=" not in text


def test_optimize_guard_targets_reduces_total_budget_overflow(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".ai-memory").mkdir(parents=True, exist_ok=True)
    bank = tmp_path / "memory-bank"
    bank.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai-memory" / "config.json").write_text(
        json.dumps(
            {
                "allowed_roots": ["memory-bank"],
                "guard": {
                    "default_max_tokens": 3000,
                    "total_max_chars": 700,
                    "targets": [
                        {"path": "memory-bank/activeContext.md", "max_chars": 1000, "policy": "warm_context"},
                        {"path": "memory-bank/progress.md", "max_chars": 1000, "policy": "warm_context"},
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    (bank / "activeContext.md").write_text("# Active\n\n## Current sprint\n" + ("- alpha detail\n" * 50), encoding="utf-8")
    (bank / "progress.md").write_text("# Progress\n\n## Current sprint\n" + ("- beta detail\n" * 50), encoding="utf-8")

    class _Envelope:
        ok = False
        status = "unavailable"
        error = "no llm"

        def to_dict(self) -> dict[str, Any]:
            return {"ok": False, "status": "unavailable", "error": "no llm"}

    import servers.memory_server.memory_llm_runner as runner

    monkeypatch.setattr(runner, "run_llm_capability", lambda *a, **kw: _Envelope())
    config = load_config(tmp_path)

    before = memory_guard_check(config)
    assert before["total_budget"]["status"] == "exceeded"

    result = optimize_guard_targets(config, prefer_llm=True)

    assert result["ok"] is True
    assert any(action.get("reason") == "total_budget_exceeded" for action in result["actions"])
    after = memory_guard_check(config)
    assert after["total_budget"]["status"] == "ok"


def _user_guard_config(tmp_path: Path, *, total_chars: int = 10000):
    config_dir = tmp_path / ".ai-memory"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(json.dumps({
        "allowed_roots": ["memory-bank"],
        "guard": {"default_max_tokens": 10000, "total_max_chars": total_chars,
                  "targets": [{"path": "memory-bank/activeContext.md", "max_chars": 600,
                               "write_policy": "user_scoped", "policy": "warm_context"}]},
    }), encoding="utf-8")
    folder = tmp_path / "memory-bank" / "activeContext"
    folder.mkdir(parents=True)
    return load_config(tmp_path), folder


def test_guard_maintenance_only_compacts_current_user(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MEMORY_MCP_USER", "alice")
    config, folder = _user_guard_config(tmp_path)
    body = "# Active\n\n## Work\n" + "- detail of active work\n" * 100
    for name in ("alice", "bob"):
        (folder / f"{name}.md").write_text(body, encoding="utf-8")
    bob = (folder / "bob.md").read_bytes()
    result = optimize_guard_targets(config, prefer_llm=False)
    assert result["ok"]
    assert len((folder / "alice.md").read_text(encoding="utf-8")) <= 600
    assert (folder / "bob.md").read_bytes() == bob
    assert result["skipped_other_user_paths"] == ["memory-bank/activeContext/bob.md"]
    archive = tmp_path / "memory-bank" / "archive" / "activeContext"
    assert list((archive / "alice").glob("*.md"))
    assert not (archive / "bob").exists()


def test_other_user_overflow_does_not_trigger_or_consume_total_budget(tmp_path: Path, monkeypatch) -> None:
    from servers.memory_server.memory_auto_maintenance import _guard_needs_optimization
    from servers.memory_server.memory_guard_optimizer import maintenance_guard_check

    monkeypatch.setenv("MEMORY_MCP_USER", "alice")
    config, folder = _user_guard_config(tmp_path, total_chars=500)
    (folder / "alice.md").write_text("a" * 300, encoding="utf-8")
    (folder / "bob.md").write_text("b" * 5000, encoding="utf-8")
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    assert memory_guard_check(config)["total_budget"]["status"] == "exceeded"
    assert not _guard_needs_optimization(config)
    assert maintenance_guard_check(config)["total_budget"]["total_chars"] == 300
    assert optimize_guard_targets(config, prefer_llm=False)["count"] == 0
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before
    assert not (tmp_path / "memory-bank" / "archive" / "activeContext").exists()


def test_total_budget_compaction_never_selects_other_user(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MEMORY_MCP_USER", "alice")
    config, folder = _user_guard_config(tmp_path, total_chars=500)
    (folder / "alice.md").write_text("a" * 590, encoding="utf-8")
    (folder / "bob.md").write_text("b" * 5000, encoding="utf-8")
    bob = (folder / "bob.md").read_bytes()
    result = optimize_guard_targets(config, prefer_llm=False)
    assert any(a.get("reason") == "total_budget_exceeded" for a in result["actions"])
    assert all(a["path"] == "memory-bank/activeContext/alice.md" for a in result["actions"])
    assert (folder / "bob.md").read_bytes() == bob


def test_unknown_user_skips_personal_files_but_admin_can_opt_in(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MEMORY_MCP_USER", "unknown")
    config, folder = _user_guard_config(tmp_path)
    body = "# Active\n\n## Work\n" + "- detail of active work\n" * 100
    for name in ("alice", "bob", "unknown"):
        (folder / f"{name}.md").write_text(body, encoding="utf-8")
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    assert optimize_guard_targets(config, prefer_llm=False)["count"] == 0
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before
    result = optimize_guard_targets(config, prefer_llm=False, all_users=True)
    assert result["ok"]
    assert {a["path"] for a in result["actions"]} == {
        f"memory-bank/activeContext/{name}.md" for name in ("alice", "bob", "unknown")}
    assert all(len(p.read_text(encoding="utf-8")) <= 600 for p in folder.iterdir())
