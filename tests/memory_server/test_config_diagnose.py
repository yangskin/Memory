"""P1-4: config diagnostics (v0.6.0 OOTB hardening)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_diagnose import config_diagnose
from servers.memory_server.server_dispatch import _dispatch_memory_context


def _bootstrap(tmp_path: Path, *, raw: dict | None = None) -> object:
    (tmp_path / "memory-bank").mkdir()
    (tmp_path / ".ai-memory").mkdir()
    payload = raw if raw is not None else {"allowed_roots": ["memory-bank"]}
    (tmp_path / ".ai-memory" / "config.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    return load_config(tmp_path)


def test_diagnose_default_sources(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path)
    result = config_diagnose(config)
    assert result["ok"] is True
    fields = result["fields"]
    assert fields["mcp.allow_unknown_user"]["source"] == "default"
    assert fields["mcp.shared_overwrite_policy"]["source"] == "default"
    assert fields["mcp.shared_overwrite_policy"]["value"] == "reject"
    assert fields["mcp.auto_maintenance.enabled"]["source"] == "default"
    assert fields["multi_user.mode"]["source"] == "default"
    assert fields["multi_user.mode"]["value"] == "always_on"


def test_diagnose_file_source(tmp_path: Path) -> None:
    config = _bootstrap(
        tmp_path,
        raw={
            "allowed_roots": ["memory-bank"],
            "multi_user": {"enabled": False},
            "mcp": {
                "allow_unknown_user": True,
                "shared_overwrite_policy": "downgrade",
                "auto_maintenance": {"enabled": False},
            },
        },
    )
    fields = config_diagnose(config)["fields"]
    assert fields["mcp.allow_unknown_user"]["source"] == "file"
    assert fields["mcp.allow_unknown_user"]["value"] is True
    assert fields["mcp.shared_overwrite_policy"]["source"] == "file"
    assert fields["mcp.shared_overwrite_policy"]["value"] == "downgrade"
    assert fields["mcp.auto_maintenance.enabled"]["source"] == "file"
    assert fields["mcp.auto_maintenance.enabled"]["value"] is False
    assert fields["multi_user.mode"]["value"] == "always_on"
    assert fields["multi_user.enabled_ignored"]["source"] == "file"
    assert fields["multi_user.enabled_ignored"]["value"] is False


def test_diagnose_user_vscode_source(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path)
    (tmp_path / ".vscode").mkdir()
    (tmp_path / ".vscode" / "settings.json").write_text(
        json.dumps({"memory-mcp.userName": "alice"}), encoding="utf-8"
    )
    fields = config_diagnose(config)["fields"]
    assert fields["user.effective"]["source"] == "vscode"
    assert fields["user.effective"]["value"] == "alice"


def test_diagnose_user_local_source_overrides_vscode_and_os(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _bootstrap(tmp_path)
    memory_root = tmp_path / "MCP" / "Memory"
    memory_root.mkdir(parents=True)
    (memory_root / "user_config.local.json").write_text(
        json.dumps({"user_name": "from-local"}), encoding="utf-8"
    )
    (tmp_path / ".vscode").mkdir()
    (tmp_path / ".vscode" / "settings.json").write_text(
        json.dumps({"memory-mcp.userName": "from-vscode"}), encoding="utf-8"
    )
    monkeypatch.setenv("USERNAME", "from-os")

    fields = config_diagnose(config)["fields"]

    assert fields["user.effective"]["source"] == "local"
    assert fields["user.effective"]["value"] == "from-local"
    assert fields["user.effective"]["source_detail"].endswith("user_config.local.json")


def test_diagnose_user_env_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _bootstrap(tmp_path)
    monkeypatch.setenv("USERNAME", "bob")
    fields = config_diagnose(config)["fields"]
    assert fields["user.effective"]["source"] == "env"
    assert fields["user.effective"]["value"] == "bob"


def test_dispatch_routes_config_diagnose(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path)
    result = _dispatch_memory_context(config, {"operation": "config_diagnose"})
    assert result["ok"] is True
    assert "fields" in result


# ---------------------------------------------------------------------------
# §15.2-D: llm_capabilities section in config_diagnose
# ---------------------------------------------------------------------------


def test_diagnose_llm_capabilities_defaults(tmp_path: Path) -> None:
    config = _bootstrap(tmp_path)
    caps = config_diagnose(config).get("llm_capabilities")
    assert caps is not None
    expected = {
        "distill_summary",
        "summarize_recall",
        "rebuild_key_document",
        "query_rewrite",
        "snapshot_narrative",
    }
    assert expected.issubset(set(caps.keys()))
    distill = caps["distill_summary"]
    for key in ("enabled", "timeout_ms", "max_tokens", "fallback"):
        assert distill[key]["source"] == "default"
    assert distill["enabled"]["value"] is False
    assert "description" in distill


def test_diagnose_llm_capabilities_file_overrides(tmp_path: Path) -> None:
    config = _bootstrap(
        tmp_path,
        raw={
            "allowed_roots": ["memory-bank"],
            "llm_defaults": {
                "timeout_ms": 12345,
                "capabilities": {
                    "distill_summary": {"enabled": True, "max_tokens": 2048},
                },
            },
        },
    )
    caps = config_diagnose(config)["llm_capabilities"]
    distill = caps["distill_summary"]
    assert distill["enabled"]["source"] == "file"
    assert distill["enabled"]["value"] is True
    assert distill["max_tokens"]["source"] == "file"
    assert distill["max_tokens"]["value"] == 2048
    # Source attribution must surface the file-level llm_defaults.timeout_ms
    # for capabilities that did not override timeout themselves; resolved
    # values may still come from per-capability defaults if the runner does
    # not honour the global, but the diagnose output should at minimum tell
    # operators where the value came from.
    summarize = caps["summarize_recall"]
    assert summarize["timeout_ms"]["source"] == "file"
