"""P1-2 integration: memory_write_record warns on unknown UE components."""

from __future__ import annotations

import json
from pathlib import Path

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_records import memory_write_record
from servers.memory_server.memory_ue_facets import write_facets, detect_ue_facets


def _make_repo(tmp_path: Path) -> object:
    (tmp_path / "memory-bank").mkdir()
    (tmp_path / ".ai-memory").mkdir()
    (tmp_path / ".ai-memory" / "config.json").write_text(
        json.dumps({"allowed_roots": ["memory-bank"]}), encoding="utf-8"
    )
    # Build a tiny UE shape
    (tmp_path / "Demo.uproject").write_text(
        json.dumps(
            {
                "EngineAssociation": "5.7",
                "Modules": [{"Name": "Demo", "Type": "Runtime"}],
            }
        ),
        encoding="utf-8",
    )
    src = tmp_path / "Source" / "Demo"
    src.mkdir(parents=True)
    (src / "Demo.Build.cs").write_text(
        'PublicDependencyModuleNames.AddRange(new string[] { "Core", "Engine" });',
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    write_facets(config, detect_ue_facets(tmp_path))
    return config


def test_write_record_warns_for_unknown_module(tmp_path: Path) -> None:
    config = _make_repo(tmp_path)
    result = memory_write_record(
        config,
        content_markdown="hello",
        record_kind="note",
        scope="personal",
        module_names=["Demo", "MysteryModule"],
    )
    assert result["ok"] is True
    assert "warnings" in result
    codes = [w["code"] for w in result["warnings"]]
    assert "ue_unknown_components" in codes
    assert "MysteryModule" in result["warnings"][0]["unknown"]
    assert "Demo" not in result["warnings"][0]["unknown"]


def test_write_record_no_warnings_when_all_known(tmp_path: Path) -> None:
    config = _make_repo(tmp_path)
    result = memory_write_record(
        config,
        content_markdown="hello",
        record_kind="note",
        scope="personal",
        module_names=["Demo"],
    )
    assert result["ok"] is True
    assert "warnings" not in result


def test_write_record_no_warnings_when_facets_missing(tmp_path: Path) -> None:
    (tmp_path / "memory-bank").mkdir()
    (tmp_path / ".ai-memory").mkdir()
    (tmp_path / ".ai-memory" / "config.json").write_text(
        json.dumps({"allowed_roots": ["memory-bank"]}), encoding="utf-8"
    )
    config = load_config(tmp_path)
    result = memory_write_record(
        config,
        content_markdown="hello",
        record_kind="note",
        scope="personal",
        module_names=["Anything"],
    )
    assert result["ok"] is True
    assert "warnings" not in result
