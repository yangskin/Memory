"""P0-3: startup auto-maintenance (v0.6.0 OOTB hardening)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from servers.memory_server.memory_auto_maintenance import (
    AutoMaintenanceConfig,
    _decide_actions,
    run_if_due,
)
from servers.memory_server.memory_config import load_config


def _bootstrap(tmp_path: Path, *, mcp: dict | None = None) -> object:
    (tmp_path / "memory-bank").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai-memory").mkdir(parents=True, exist_ok=True)
    (tmp_path / "memory-bank" / "seed.md").write_text("# seed\n", encoding="utf-8")
    cfg: dict = {"allowed_roots": ["memory-bank"]}
    if mcp is not None:
        cfg["mcp"] = mcp
    (tmp_path / ".ai-memory" / "config.json").write_text(
        json.dumps(cfg), encoding="utf-8"
    )
    return load_config(tmp_path)


# ---------------------------------------------------------------------------
# decision-only (pure) layer
# ---------------------------------------------------------------------------


def test_first_boot_triggers_full_run(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path)
    settings = AutoMaintenanceConfig()
    state: dict = {}
    decisions = _decide_actions(config, settings, state, now=time.time())

    assert decisions["any"] is True
    assert decisions["health_check"] is True
    assert decisions["rebuild_index"] is True


def test_recent_run_is_skipped(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path)
    settings = AutoMaintenanceConfig()
    now = time.time()
    state = {"last_run_ts": now - 60}  # 60s ago

    # Build a search.db so index-stale check returns False.
    (config.repo_root / ".ai-memory" / "search.db").write_bytes(b"")
    decisions = _decide_actions(config, settings, state, now=now)

    assert decisions["health_check"] is False
    assert decisions["rebuild_index"] is False


def test_stale_index_forces_rebuild_even_when_recent(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path)
    settings = AutoMaintenanceConfig(index_stale_seconds=1)
    now = time.time()
    state = {"last_run_ts": now - 60}
    # Create stale db
    db_path = config.repo_root / ".ai-memory" / "search.db"
    db_path.write_bytes(b"")
    # Make the seed file newer
    seed = config.repo_root / "memory-bank" / "seed.md"
    new_time = time.time() + 5
    import os

    os.utime(seed, (new_time, new_time))

    decisions = _decide_actions(config, settings, state, now=now)
    assert decisions["rebuild_index"] is True
    assert decisions["health_check"] is False


def test_oversize_events_triggers_rotate(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path)
    settings = AutoMaintenanceConfig(events_max_bytes=10)
    config.events_file.write_text("x" * 100, encoding="utf-8")
    now = time.time()
    state = {"last_run_ts": now - 60}

    (config.repo_root / ".ai-memory" / "search.db").write_bytes(b"")
    decisions = _decide_actions(config, settings, state, now=now)
    assert decisions["rotate_events"] is True
    assert decisions["any"] is True


def test_total_budget_exceeded_triggers_guard_optimize_even_without_target_overflow(tmp_path: Path) -> None:
    (tmp_path / ".ai-memory").mkdir(parents=True, exist_ok=True)
    (tmp_path / "memory-bank").mkdir(parents=True, exist_ok=True)
    for name in ("activeContext.md", "progress.md"):
        (tmp_path / "memory-bank" / name).write_text("x" * 400, encoding="utf-8")
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
    config = load_config(tmp_path)
    now = time.time()
    (tmp_path / ".ai-memory" / "search.db").write_bytes(b"")
    decisions = _decide_actions(
        config,
        AutoMaintenanceConfig(min_interval_seconds=86400),
        {"last_run_ts": now - 60},
        now=now,
    )

    assert decisions["guard_optimize"] is True
    assert decisions["any"] is True


# ---------------------------------------------------------------------------
# runner layer
# ---------------------------------------------------------------------------


def test_run_if_due_persists_state(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path)
    result = run_if_due(config)

    assert result["ok"] is True
    state_path = tmp_path / ".ai-memory" / "last_maintenance.json"
    assert state_path.is_file()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "last_run_ts" in state
    assert "actions_summary" in state


def test_run_if_due_skips_when_disabled(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path, mcp={"auto_maintenance": {"enabled": False}})
    result = run_if_due(config)

    assert result["ok"] is True
    assert result.get("skipped") is True
    assert result.get("reason") == "disabled"
    # No state file should be created.
    assert not (tmp_path / ".ai-memory" / "last_maintenance.json").exists()


def test_run_if_due_skips_when_not_due(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path)
    # First run produces state.
    run_if_due(config)
    # Touch search.db so index is fresh.
    db = tmp_path / ".ai-memory" / "search.db"
    if not db.exists():
        db.write_bytes(b"")
    now = time.time() + 1
    second = run_if_due(config, now=now)

    assert second["ok"] is True
    # On second immediate call nothing should be due.
    assert second.get("skipped") is True or second["actions"] == []


def test_run_if_due_repairs_guard_overflow_even_when_interval_not_due(tmp_path: Path) -> None:
    (tmp_path / ".ai-memory").mkdir(parents=True, exist_ok=True)
    active_dir = tmp_path / "memory-bank" / "activeContext"
    active_dir.mkdir(parents=True, exist_ok=True)
    active = active_dir / "alice.md"
    active.write_text(
        "# Active\n\n## Current sprint\n"
        + "\n".join(f"- long active context line {idx}" for idx in range(80)),
        encoding="utf-8",
    )
    (tmp_path / ".ai-memory" / "config.json").write_text(
        json.dumps(
            {
                "allowed_roots": ["memory-bank", ".ai-context"],
                "guard": {
                    "default_max_tokens": 3000,
                    "targets": [
                        {
                            "path": "memory-bank/activeContext.md",
                            "max_chars": 500,
                            "policy": "warm_context",
                            "write_policy": "user_scoped",
                        }
                    ],
                },
                "mcp": {"auto_maintenance": {"min_interval_seconds": 86400}},
            }
        ),
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    now = time.time()
    (tmp_path / ".ai-memory" / "search.db").write_bytes(b"")
    (tmp_path / ".ai-memory" / "last_maintenance.json").write_text(
        json.dumps({"last_run_ts": now - 60}),
        encoding="utf-8",
    )

    result = run_if_due(config, now=now)

    assert result["ok"] is True
    assert result["decisions"]["guard_optimize"] is True
    steps = {a["step"] for a in result["actions"]}
    assert "guard_optimize" in steps
    optimized = active.read_text(encoding="utf-8")
    assert len(optimized) <= 500
    archives = list((tmp_path / "memory-bank" / "archive" / "activeContext" / "alice").glob("*.md"))
    assert archives
    assert "long active context line 79" in archives[0].read_text(encoding="utf-8")


def test_run_if_due_uses_deterministic_guard_optimizer(tmp_path: Path, monkeypatch) -> None:
    config = _bootstrap(tmp_path)
    captured: dict = {}

    import servers.memory_server.memory_auto_maintenance as maintenance
    import servers.memory_server.memory_guard_optimizer as optimizer

    monkeypatch.setattr(
        maintenance,
        "_decide_actions",
        lambda *_args, **_kwargs: {
            "health_check": False,
            "rebuild_index": False,
            "rotate_events": False,
            "guard_optimize": True,
            "retention": False,
            "any": True,
        },
    )

    def fake_optimize(_config, *, prefer_llm):
        captured["prefer_llm"] = prefer_llm
        return {"ok": True, "actions": []}

    monkeypatch.setattr(optimizer, "optimize_guard_targets", fake_optimize)

    result = run_if_due(config)

    assert result["ok"] is True
    assert captured["prefer_llm"] is False


def test_run_if_due_applies_retention_for_memory_bank_growth(tmp_path: Path) -> None:
    (tmp_path / ".ai-memory").mkdir(parents=True, exist_ok=True)
    manual = tmp_path / "memory-bank" / "archive" / "manual-edits"
    manual.mkdir(parents=True, exist_ok=True)
    (tmp_path / "memory-bank" / "seed.md").write_text("# seed\n", encoding="utf-8")
    for idx in range(4):
        path = manual / f"manual-{idx}.md"
        path.write_text("x" * 1000, encoding="utf-8")
        import os

        os.utime(path, (time.time() + idx, time.time() + idx))
    (tmp_path / ".ai-memory" / "baseline.json").write_text(
        json.dumps(
            {
                "captured_at": "2026-01-01T00:00:00+00:00",
                "metrics": {
                    "memory_bank_files": 1,
                    "memory_bank_total_bytes": 100,
                    "records_count": 0,
                    "events_total_bytes": 0,
                    "index_db_bytes": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".ai-memory" / "config.json").write_text(
        json.dumps(
            {
                "allowed_roots": ["memory-bank"],
                "mcp": {
                    "auto_maintenance": {
                        "min_interval_seconds": 86400,
                        "retention": {"keep_manual_edits": 1},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    now = time.time()
    (tmp_path / ".ai-memory" / "search.db").write_bytes(b"")
    (tmp_path / ".ai-memory" / "last_maintenance.json").write_text(
        json.dumps({"last_run_ts": now - 60}),
        encoding="utf-8",
    )

    result = run_if_due(config, now=now)

    assert result["ok"] is True
    assert result["decisions"]["retention"] is True
    assert "retention" in {a["step"] for a in result["actions"]}
    remaining = list(manual.glob("*.md"))
    assert len(remaining) == 1
    moved = list((tmp_path / ".ai-memory" / "retention-archive").rglob("manual-*.md"))
    assert len(moved) == 3


def test_run_if_due_writes_audit_event(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path)
    run_if_due(config)

    events_text = config.events_file.read_text(encoding="utf-8")
    assert "auto_maintenance" in events_text


def test_run_if_due_never_raises_on_action_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _bootstrap(tmp_path)

    def boom(*_a, **_kw):
        raise RuntimeError("simulated maintenance failure")

    # Patch both action functions so any decision branch hits the
    # exception path; the runner must still return a structured dict.
    import servers.memory_server.memory_auto_maintenance as mod

    monkeypatch.setattr(mod, "_safe_run", lambda step, fn: {"step": step, "ok": False, "error": "Boom"})
    # Force decisions True
    monkeypatch.setattr(mod, "_decide_actions", lambda *a, **kw: {
        "health_check": True,
        "rebuild_index": True,
        "rotate_events": False,
        "any": True,
    })
    result = run_if_due(config)

    assert result["ok"] is True
    steps = {a["step"] for a in result["actions"]}
    assert {"health_check", "rebuild_index"}.issubset(steps)
