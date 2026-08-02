"""P1-5: artifact path normalization (v0.6.0 OOTB hardening).

Bidirectional mapping for Unreal Engine artifact paths so two equivalent
inputs collapse to the same canonical form, e.g.:

    /Game/Foo/Bar           ->  /Game/Foo/Bar
    Content/Foo/Bar.uasset  ->  /Game/Foo/Bar
    Content/Foo/Bar         ->  /Game/Foo/Bar

A best-effort ``git_sha`` may be attached when the workspace is a git
checkout. Failures (no git, detached, unreadable) are silently ignored
because the linker is not allowed to fail on auxiliary metadata.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

_GAME_PREFIX = "/Game/"
_CONTENT_PREFIX_PATTERNS = (
    re.compile(r"^[Cc]ontent[\\/]+"),
)


def _strip_uasset(path: str) -> str:
    if path.lower().endswith(".uasset"):
        return path[: -len(".uasset")]
    if path.lower().endswith(".umap"):
        return path[: -len(".umap")]
    return path


def normalize_asset_path(raw: str) -> str:
    """Canonicalize an Unreal asset path to the ``/Game/...`` form.

    Idempotent. Returns the input unchanged when it doesn't look like a
    UE asset path.
    """
    if not isinstance(raw, str):
        return raw  # type: ignore[unreachable]
    s = raw.strip().replace("\\", "/")
    if not s:
        return s

    # Already canonical?
    if s.startswith(_GAME_PREFIX):
        return _strip_uasset(s)

    # Content/Foo/Bar(.uasset)? form
    for pat in _CONTENT_PREFIX_PATTERNS:
        if pat.match(s):
            stripped = pat.sub("", s)
            return _GAME_PREFIX + _strip_uasset(stripped).lstrip("/")

    # Absolute path containing /Content/...
    lower = s.lower()
    idx = lower.find("/content/")
    if idx >= 0:
        rest = s[idx + len("/content/"):]
        return _GAME_PREFIX + _strip_uasset(rest).lstrip("/")

    return s


def normalize_asset_paths(values: list[str] | None) -> list[str]:
    if not values:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        n = normalize_asset_path(v)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def get_git_sha(repo_root: Path) -> str | None:
    """Return short HEAD sha or ``None``; never raises."""
    git_dir = repo_root / ".git"
    if not git_dir.exists():
        return None
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    if completed.returncode != 0:
        return None
    sha = (completed.stdout or "").strip()
    return sha or None


def attach_git_sha(repo_root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Mutate-and-return: add ``git_sha`` if available."""
    sha = get_git_sha(repo_root)
    if sha:
        payload["git_sha"] = sha
    return payload


# Convenience: declare which artifact list fields participate in
# normalization. blueprint_paths conventionally use /Game/... too.
NORMALIZED_FIELDS: tuple[str, ...] = ("asset_paths", "blueprint_paths")
