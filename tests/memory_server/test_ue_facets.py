"""P1-2: UE facet auto-introspection (v0.6.0 OOTB hardening)."""

from __future__ import annotations

import json
from pathlib import Path

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_ue_facets import (
    detect_ue_facets,
    known_components,
    load_facets,
    write_facets,
)


def _make_uproject(repo_root: Path, name: str = "Demo") -> None:
    (repo_root / f"{name}.uproject").write_text(
        json.dumps(
            {
                "FileVersion": 3,
                "EngineAssociation": "5.7",
                "Modules": [
                    {"Name": name, "Type": "Runtime", "LoadingPhase": "Default"},
                    {"Name": f"{name}Editor", "Type": "Editor"},
                ],
            }
        ),
        encoding="utf-8",
    )


def _make_buildcs(repo_root: Path, module: str, deps: list[str]) -> None:
    src = repo_root / "Source" / module
    src.mkdir(parents=True, exist_ok=True)
    deps_str = ", ".join(f'"{d}"' for d in deps)
    (src / f"{module}.Build.cs").write_text(
        f"""
using UnrealBuildTool;
public class {module} : ModuleRules
{{
    public {module}(ReadOnlyTargetRules Target) : base(Target)
    {{
        PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
        PublicDependencyModuleNames.AddRange(new string[] {{ {deps_str} }});
    }}
}}
""".strip(),
        encoding="utf-8",
    )


def _make_uplugin(repo_root: Path, name: str) -> None:
    plugin_dir = repo_root / "Plugins" / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / f"{name}.uplugin").write_text(
        json.dumps({"FileVersion": 3, "FriendlyName": name, "Modules": []}),
        encoding="utf-8",
    )


def test_detect_returns_blank_for_non_ue_repo(tmp_path: Path) -> None:
    facets = detect_ue_facets(tmp_path)
    assert facets.is_ue_project is False
    assert facets.project is None
    assert facets.modules == []


def test_detect_parses_uproject(tmp_path: Path) -> None:
    _make_uproject(tmp_path, "Demo")
    facets = detect_ue_facets(tmp_path)
    assert facets.is_ue_project is True
    assert facets.project == "Demo"
    assert facets.engine_association == "5.7"
    assert "Demo" in facets.modules
    assert "DemoEditor" in facets.modules


def test_detect_parses_buildcs_dependencies(tmp_path: Path) -> None:
    _make_uproject(tmp_path, "Demo")
    _make_buildcs(tmp_path, "Demo", ["Core", "CoreUObject", "Engine", "InputCore"])
    facets = detect_ue_facets(tmp_path)
    assert "Core" in facets.dependencies
    assert "InputCore" in facets.dependencies


def test_detect_parses_plugins(tmp_path: Path) -> None:
    _make_uproject(tmp_path, "Demo")
    _make_uplugin(tmp_path, "MyPlugin")
    facets = detect_ue_facets(tmp_path)
    assert "MyPlugin" in facets.plugins


def test_write_then_load_roundtrip(tmp_path: Path) -> None:
    (tmp_path / "memory-bank").mkdir()
    (tmp_path / ".ai-memory").mkdir()
    (tmp_path / ".ai-memory" / "config.json").write_text(
        json.dumps({"allowed_roots": ["memory-bank"]}), encoding="utf-8"
    )
    _make_uproject(tmp_path, "Demo")
    _make_buildcs(tmp_path, "Demo", ["Core", "Engine"])

    config = load_config(tmp_path)
    write_result = write_facets(config)
    assert write_result["ok"] is True
    assert (tmp_path / ".ai-memory" / "ue_facets.json").is_file()

    loaded = load_facets(config)
    assert loaded is not None
    assert loaded.project == "Demo"
    assert "Core" in loaded.dependencies


def test_known_components_union(tmp_path: Path) -> None:
    _make_uproject(tmp_path, "Demo")
    _make_buildcs(tmp_path, "Demo", ["Core"])
    _make_uplugin(tmp_path, "MyPlugin")

    facets = detect_ue_facets(tmp_path)
    known = known_components(facets)
    assert {"Demo", "DemoEditor", "Core", "MyPlugin"}.issubset(known)


def test_load_facets_returns_none_when_missing(tmp_path: Path) -> None:
    (tmp_path / "memory-bank").mkdir()
    (tmp_path / ".ai-memory").mkdir()
    (tmp_path / ".ai-memory" / "config.json").write_text(
        json.dumps({"allowed_roots": ["memory-bank"]}), encoding="utf-8"
    )
    config = load_config(tmp_path)
    assert load_facets(config) is None


def test_detect_tolerates_malformed_uproject(tmp_path: Path) -> None:
    (tmp_path / "Bad.uproject").write_text("{not json", encoding="utf-8")
    facets = detect_ue_facets(tmp_path)
    assert facets.is_ue_project is True
    # Project name still derived from file stem.
    assert facets.project is None
    assert facets.modules == []
