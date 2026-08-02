"""User-id validation (P0-1, v0.6.0 OOTB hardening).

Goal: keep "out-of-the-box" promise — the only thing a teammate must
configure is their own user id; everything else is automatic.

Therefore:

- ``is_placeholder_user`` blocks the genuine fallback case (empty,
  whitespace, literal "unknown" variants) and any path-injection
  characters that would corrupt user-scoped paths.
- ``is_ambiguous_user`` flags common shared admin names (warning, not
  block) so CI / Windows machines using "Administrator" can still
  function but the user is nudged to override.
- ``validate_effective_user`` is the single entry point used by the
  write facades. Returns ``None`` when OK, otherwise a structured
  result with ``error="user_not_configured"`` and a ``setup_hint``.
"""

from __future__ import annotations

from typing import Any

from .memory_config import MemoryConfig
from .memory_events import get_current_user

# Hard-reject set: literal placeholder values produced by the fallback
# chain when neither ``user_config.local.json`` / ``.vscode/settings.json``
# nor env vars supply a user. Comparison is case-insensitive.
_PLACEHOLDER_NAMES: frozenset[str] = frozenset({"unknown"})

# Characters that must never appear in a user id because they would
# break user-scoped path resolution (``activeContext/{user}.md``).
_FORBIDDEN_CHARS: frozenset[str] = frozenset({"/", "\\", ":", "\n", "\r", "\0"})

# Soft-warning set: common shared / generic OS account names. These are
# accepted but emit a one-time ``user_ambiguous`` warning so multiple
# people on shared machines do not silently merge into the same id.
_AMBIGUOUS_NAMES: frozenset[str] = frozenset(
    {"administrator", "user", "admin", "root", "guest", "default"}
)


def is_placeholder_user(name: object) -> bool:
    """Return True if ``name`` is unsafe to use as a stable user id."""
    if not isinstance(name, str):
        return True
    stripped = name.strip()
    if not stripped:
        return True
    if stripped.lower() in _PLACEHOLDER_NAMES:
        return True
    return any(ch in stripped for ch in _FORBIDDEN_CHARS)


def is_ambiguous_user(name: object) -> bool:
    """Return True for common shared / generic OS account names."""
    if not isinstance(name, str):
        return False
    stripped = name.strip()
    if not stripped:
        return False
    return stripped.lower() in _AMBIGUOUS_NAMES


_SETUP_HINT = (
    "Set MEMORY_MCP_USER or create MCP/Memory/user_config.local.json with "
    '{"user_name":"<your-id>"}. Legacy fallback remains '
    "'.vscode/settings.json[\"memory-mcp.userName\"]'."
)


def validate_effective_user(config: MemoryConfig) -> dict[str, Any] | None:
    """Validate the user id derived from config / env / vscode settings.

    Returns ``None`` when the id is usable. When the id is a
    placeholder, returns a structured error dict suitable for direct
    return from a facade tool. Ambiguous (shared OS account) names
    return a non-blocking warning dict; callers may choose to surface
    or ignore it.

    The check is bypassed when ``mcp.allow_unknown_user=true`` is set
    in ``.ai-memory/config.json``.
    """
    if getattr(config, "mcp_allow_unknown_user", False):
        return None

    name = get_current_user(config.repo_root)
    if is_placeholder_user(name):
        return {
            "ok": False,
            "error": "user_not_configured",
            "message": (
                "Effective user id is empty/unknown. Multi-user safety requires a "
                "stable id; refusing to write to avoid silent collisions."
            ),
            "setup_hint": _SETUP_HINT,
        }
    if is_ambiguous_user(name):
        return {
            "ok": True,
            "warning": "user_ambiguous",
            "message": (
                f"User id '{name}' is a generic OS account name; consider setting "
                "MCP/Memory/user_config.local.json to a personal id."
            ),
            "setup_hint": _SETUP_HINT,
        }
    return None
