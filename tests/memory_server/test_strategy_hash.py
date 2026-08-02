"""P2-3: scoring strategy hash drift detection (v0.6.0 OOTB hardening)."""

from __future__ import annotations

import json
from pathlib import Path

from servers.memory_server.memory_auto_maintenance import run_if_due
from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_events import append_event
from servers.memory_server.memory_maintenance import memory_health_check
from servers.memory_server.memory_strategy_hash import (
    current_strategy_hash,
    detect_strategy_drift,
    latest_recorded_hash,
)


def _bootstrap(tmp_path: Path) -> object:
    (tmp_path / "memory-bank").mkdir()
    (tmp_path / ".ai-memory").mkdir()
    (tmp_path / ".ai-memory" / "config.json").write_text(
        json.dumps({"allowed_roots": ["memory-bank"]}), encoding="utf-8"
    )
    (tmp_path / "memory-bank" / "seed.md").write_text("# seed\n", encoding="utf-8")
    return load_config(tmp_path)


def test_current_hash_is_stable_and_short() -> None:
    h1 = current_strategy_hash()
    h2 = current_strategy_hash()
    assert h1 == h2
    assert len(h1) == 16


def test_latest_recorded_hash_none_when_no_events(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path)
    assert latest_recorded_hash(config) is None


def test_auto_maintenance_writes_strategy_hash(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path)
    run_if_due(config)
    assert latest_recorded_hash(config) == current_strategy_hash()


def test_detect_drift_returns_false_when_unchanged(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path)
    run_if_due(config)
    report = detect_strategy_drift(config)
    assert report["drift"] is False


def test_detect_drift_flags_when_old_hash_differs(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path)
    # Manually inject a fake old hash event.
    append_event(
        config,
        event_type="auto_maintenance",
        payload={"scoring_strategy_hash": "deadbeefdeadbeef"},
    )
    report = detect_strategy_drift(config)
    assert report["drift"] is True
    assert report["previous"] == "deadbeefdeadbeef"
    assert report["current"] == current_strategy_hash()


def test_health_surfaces_strategy_drift(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path)
    append_event(
        config,
        event_type="auto_maintenance",
        payload={"scoring_strategy_hash": "deadbeefdeadbeef"},
    )
    result = memory_health_check(config)
    codes = [issue["code"] for issue in result["issues"]]
    assert "scoring_strategy_changed" in codes
    assert result.get("scoring_strategy_hash") == current_strategy_hash()
