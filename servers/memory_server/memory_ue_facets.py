"""P1-2: UE facet auto-introspection (v0.6.0 OOTB hardening).

Scans the workspace for Unreal Engine project shape:

- ``*.uproject``   → project name, EngineAssociation, Modules
- ``Source/**/*.Build.cs`` → public/private dependency module names
- ``Plugins/**/*.uplugin`` → plugin names + descriptions

Result is cached at ``.ai-memory/ue_facets.json`` and consumed by
``memory_write_record`` to warn (non-blocking) when a record's
``components`` field references unknown UE module names.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .memory_config import MemoryConfig

_FACET_PATH_RELATIVE = Path(".ai-memory") / "ue_facets.json"

# Deps live in Build.cs as quoted strings inside *DependencyModuleNames.
_BUILDCS_DEPS_RE = re.compile(
    r'(?:Public|Private)?DependencyModuleNames\.(?:AddRange|Add)\s*\(\s*new\s+string\s*\[\s*\]\s*\{([^}]*)\}',
    re.DOTALL,
)
_QUOTED_RE = re.compile(r'"([^"]+)"')


@dataclass
class UEFacets:
    project: str | None = None
    engine_association: str | None = None
    modules: list[str] = field(default_factory=list)
    plugins: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    is_ue_project: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_uproject(path: Path) -> tuple[str | None, str | None, list[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None, []
    engine = data.get("EngineAssociation")
    modules: list[str] = []
    for mod in data.get("Modules", []) or []:
        if isinstance(mod, dict) and isinstance(mod.get("Name"), str):
            modules.append(mod["Name"])
    return path.stem, str(engine) if engine is not None else None, modules


def _parse_build_cs(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    deps: list[str] = []
    for block in _BUILDCS_DEPS_RE.findall(text):
        deps.extend(_QUOTED_RE.findall(block))
    return deps


def _parse_uplugin(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return path.stem
    name = data.get("FriendlyName") or data.get("Name") or path.stem
    return str(name)


def detect_ue_facets(repo_root: Path) -> UEFacets:
    """Scan ``repo_root`` for UE project shape; never raises."""
    facets = UEFacets()

    uprojects = list(repo_root.glob("*.uproject"))
    if uprojects:
        facets.is_ue_project = True
        # Pick the first; multi-uproject layouts are rare and out of scope.
        name, engine, modules = _parse_uproject(uprojects[0])
        facets.project = name
        facets.engine_association = engine
        facets.modules = modules

    source_dir = repo_root / "Source"
    if source_dir.is_dir():
        deps: set[str] = set()
        for build_cs in source_dir.rglob("*.Build.cs"):
            deps.update(_parse_build_cs(build_cs))
        facets.dependencies = sorted(deps)

    plugins_dir = repo_root / "Plugins"
    if plugins_dir.is_dir():
        plugins: list[str] = []
        for uplugin in plugins_dir.rglob("*.uplugin"):
            name = _parse_uplugin(uplugin)
            if name:
                plugins.append(name)
        facets.plugins = sorted(set(plugins))

    return facets


def write_facets(config: MemoryConfig, facets: UEFacets | None = None) -> dict[str, Any]:
    """Materialize facets to ``.ai-memory/ue_facets.json``."""
    facets = facets if facets is not None else detect_ue_facets(config.repo_root)
    out_path = config.repo_root / _FACET_PATH_RELATIVE
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = facets.as_dict()
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "path": str(out_path), **payload}


def load_facets(config: MemoryConfig) -> UEFacets | None:
    path = config.repo_root / _FACET_PATH_RELATIVE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return UEFacets(
        project=data.get("project"),
        engine_association=data.get("engine_association"),
        modules=list(data.get("modules") or []),
        plugins=list(data.get("plugins") or []),
        dependencies=list(data.get("dependencies") or []),
        is_ue_project=bool(data.get("is_ue_project", False)),
    )


def known_components(facets: UEFacets) -> set[str]:
    """Union of names treated as 'known' UE components for warning checks."""
    out: set[str] = set()
    out.update(facets.modules or [])
    out.update(facets.plugins or [])
    out.update(facets.dependencies or [])
    if facets.project:
        out.add(facets.project)
    return out
