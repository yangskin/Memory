"""Compile cache and usage-stats helpers.

Extracted from `memory_compiler.py` (P1-B). All functions read/write JSON files
under `.ai-memory/` and do not touch source record markdown — they back the
"compiled output is rebuildable, sources are truth" invariant.

Files managed here:
- `.ai-memory/compile-cache/*.json` : compile cache entries
- `.ai-memory/usage-stats.json`     : per-record compile usage counters
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .memory_config import MemoryConfig
from .memory_corpus import CompilableRecord
from .memory_locks import file_lock
from .memory_record_io import _atomic_write_text


def load_compile_cache_entries(
    config: MemoryConfig,
    *,
    targets: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Load every cache entry under `.ai-memory/compile-cache/`.

    Optionally filter by `targets`. Each returned dict has a synthetic
    `cache_path` field with the repo-relative path of the JSON file.
    Malformed entries are silently skipped.
    """
    cache_dir = config.repo_root / ".ai-memory" / "compile-cache"
    if not cache_dir.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    for path in sorted(cache_dir.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        target = str(raw.get("target", ""))
        if targets is not None and target not in targets:
            continue
        raw["cache_path"] = str(path.relative_to(config.repo_root)).replace("\\", "/")
        entries.append(raw)
    return entries


def find_compile_cache_entry(config: MemoryConfig, compiled_path: str) -> dict[str, Any] | None:
    """Find the cache entry whose `path` matches `compiled_path` (slash-normalized)."""
    normalized = compiled_path.replace("\\", "/")
    for entry in load_compile_cache_entries(config):
        if str(entry.get("path", "")).replace("\\", "/") == normalized:
            return entry
    return None


def record_usage_stats(
    config: MemoryConfig,
    records: list[CompilableRecord],
    used_at: str,
    *,
    target: str,
) -> Path:
    """Persist compile usage stats to `.ai-memory/usage-stats.json`.

    The compiler must NOT mutate source record `.md` files (that would violate
    the truth-source invariant and pollute Git diffs / FTS index freshness).
    Usage stats live alongside the compile cache and can be deleted/rebuilt at
    any time.
    """
    stats_path = config.repo_root / ".ai-memory" / "usage-stats.json"
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    # Cross-process serialization: the read-modify-write below would lose
    # increments if two MCP server processes raced on the same record.
    with file_lock(config.repo_root, stats_path):
        data: dict[str, Any] = {}
        if stats_path.is_file():
            try:
                data = json.loads(stats_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            except (OSError, ValueError):
                data = {}
        for record in records:
            record_id = str(record.metadata.get("id", ""))
            if not record_id:
                continue
            entry = data.get(record_id) if isinstance(data.get(record_id), dict) else {}
            entry["last_used_at"] = used_at
            entry["path"] = record.path
            entry["compile_hit_count"] = int(entry.get("compile_hit_count", 0) or 0) + 1
            compile_targets = entry.get("compile_targets")
            if not isinstance(compile_targets, list):
                compile_targets = []
            normalized_targets = [str(item) for item in compile_targets if str(item).strip()]
            if target not in normalized_targets:
                normalized_targets.append(target)
            entry["compile_targets"] = normalized_targets
            data[record_id] = entry
        try:
            _atomic_write_text(
                stats_path,
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                fsync_strict=config.mcp_fsync_strict,
            )
        except OSError:
            pass
    return stats_path


def get_record_last_used_at(config: MemoryConfig, record_id: str) -> str | None:
    """Return the most recent compile-time usage timestamp for `record_id`."""
    stats_path = config.repo_root / ".ai-memory" / "usage-stats.json"
    if not stats_path.is_file():
        return None
    try:
        data = json.loads(stats_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    entry = data.get(record_id)
    if isinstance(entry, dict):
        value = entry.get("last_used_at")
        return str(value) if value is not None else None
    return None


__all__ = [
    "load_compile_cache_entries",
    "find_compile_cache_entry",
    "record_usage_stats",
    "get_record_last_used_at",
]
