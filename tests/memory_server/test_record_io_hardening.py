"""Regression tests for atomic write hardening (P1-F/G).

Covers:
- Successful writes still succeed and produce expected content.
- Tmp files do not linger after a successful write.
- A pre-existing file at a *different* target path causes `target_exists`
  rather than silent clobber.
- Re-validating in place (same target) works because `same_path` is honored.
- The atomic helper refuses to clobber a pre-existing tmp file with the same
  random suffix (extremely improbable, but exercises the O_EXCL flag).
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_record_io import (
    _atomic_write_text,
    write_record_to_target,
    write_same_record,
)
from servers.memory_server.memory_records import memory_write_record


def _make_config(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "memory-bank").mkdir()
    (workspace / ".ai-context").mkdir()
    (workspace / ".ai-memory").mkdir()
    cfg_path = workspace / ".ai-memory" / "config.json"
    cfg_path.write_text(json.dumps({}), encoding="utf-8")
    return load_config(str(workspace), str(cfg_path))


def _create_candidate_record(config) -> dict:
    return memory_write_record(
        config,
        content_markdown="# Hardening Test\n\nbody text.",
        record_kind="note",
        scope="personal",
        status="candidate",
        tags=["mcp"],
    )


def test_atomic_write_text_success(tmp_path):
    target = tmp_path / "out.txt"
    _atomic_write_text(target, "hello\n")
    assert target.read_text(encoding="utf-8") == "hello\n"
    # No leftover tmp files.
    leftovers = list(tmp_path.glob(".out.txt.*.tmp"))
    assert leftovers == []


def test_atomic_write_text_overwrites_existing(tmp_path):
    target = tmp_path / "out.txt"
    target.write_text("old", encoding="utf-8")
    _atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"


def test_atomic_write_text_creates_parent_dirs(tmp_path):
    target = tmp_path / "nested" / "deep" / "out.txt"
    _atomic_write_text(target, "x")
    assert target.read_text(encoding="utf-8") == "x"


def test_atomic_write_uses_oexcl_on_tmp(tmp_path, monkeypatch):
    """Force the random tmp name and pre-create it; the helper must raise."""
    target = tmp_path / "out.txt"
    fixed_hex = "deadbeef"

    class _FixedHexUUID:
        hex = fixed_hex + "0" * 24

    monkeypatch.setattr(uuid, "uuid4", lambda: _FixedHexUUID())
    # Pre-create the tmp file the helper would try to use.
    blocking = target.parent / f".{target.name}.{fixed_hex}.tmp"
    blocking.write_text("squatting", encoding="utf-8")

    with pytest.raises(OSError):
        _atomic_write_text(target, "should not land")
    # Original target was not created.
    assert not target.exists()
    # The squatter is left alone (we don't touch foreign files).
    assert blocking.read_text(encoding="utf-8") == "squatting"


def test_write_record_to_target_refuses_existing_target(tmp_path):
    config = _make_config(tmp_path)
    res = _create_candidate_record(config)
    assert res["ok"], res

    record_id = res["id"]
    candidate_path = config.repo_root / res["path"]
    assert candidate_path.is_file()

    # Manually craft a "validated" twin file at the target path so a future
    # validate-style transition sees a collision.
    from servers.memory_server.memory_records import target_path_for_record

    validated_rel = target_path_for_record(
        record_id, "note", "personal", "validated", res.get("author") or "unknown"
    )
    validated_abs = config.repo_root / validated_rel
    validated_abs.parent.mkdir(parents=True, exist_ok=True)
    validated_abs.write_text("# squatter\n", encoding="utf-8")

    # Now try the transition.
    metadata = dict(res)
    metadata["status"] = "validated"
    out = write_record_to_target(
        config,
        old_abs_path=candidate_path,
        old_rel_path=res["path"],
        metadata={
            "id": record_id,
            "record_kind": "note",
            "scope": "personal",
            "status": "validated",
            "author": res.get("author") or "unknown",
        },
        body="# Hardening Test\n\nbody text.\n",
    )
    assert out["ok"] is False
    assert out["error"] == "target_exists"
    # Old candidate file is still there (no destructive action taken).
    assert candidate_path.is_file()
    # Squatter untouched.
    assert validated_abs.read_text(encoding="utf-8") == "# squatter\n"


def test_write_same_record_round_trip(tmp_path):
    config = _make_config(tmp_path)
    res = _create_candidate_record(config)
    assert res["ok"]
    abs_path = config.repo_root / res["path"]
    out = write_same_record(
        config,
        abs_path=abs_path,
        rel_path=res["path"],
        metadata={
            "id": res["id"],
            "record_kind": "note",
            "scope": "personal",
            "status": "candidate",
            "author": res.get("author") or "unknown",
        },
        body="# Hardening Test\n\nupdated body.\n",
    )
    assert out["ok"] is True
    assert "updated body" in abs_path.read_text(encoding="utf-8")
    # No leftover tmp files in record dir.
    leftovers = list(abs_path.parent.glob(f".{abs_path.name}.*.tmp"))
    assert leftovers == []
