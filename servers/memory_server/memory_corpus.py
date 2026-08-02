"""Shared corpus projection used by both compiler and retrieval.

Lifts ``CompilableRecord``, ``_iter_records`` and the compact-body
extraction helpers out of ``memory_compiler`` so that other modules
(retrieval, future important_memories packagers, etc.) no longer have to
import private symbols from the compiler. The compiler keeps thin
re-exports for backward compatibility.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .memory_config import MemoryConfig
from .memory_compactor import _compact_warm_context
from .memory_record_io import iter_parsed_records
from .memory_request_id import content_sha


# ── Public constants ────────────────────────────────────────────────────

COMPACT_SECTION_PRIORITY = [
    "decision",
    "expected behavior",
    "acceptance checks",
    "next step",
    "next steps",
    "notes",
    "details",
]
COMPACT_BODY_CHAR_LIMIT = 600


@dataclass(frozen=True)
class CompilableRecord:
    """Compiler/retrieval-facing projection of a parsed Markdown record."""

    path: str
    metadata: dict[str, Any]
    body: str
    title: str


# ── Title / body helpers ────────────────────────────────────────────────


def first_heading(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return "Untitled Record"


def body_without_title(body: str, title: str) -> str:
    lines = body.strip().splitlines()
    if lines and lines[0].strip().startswith("#"):
        heading = lines[0].strip().lstrip("#").strip()
        if heading == title:
            return "\n".join(lines[1:]).strip()
    return body.strip()


def markdown_sections(body: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current_key: str | None = None
    current_lines: list[str] = []

    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if current_key is not None:
                sections[current_key] = current_lines
            current_key = stripped.lstrip("#").strip().lower()
            current_lines = [stripped]
        elif current_key is not None:
            current_lines.append(line)
    if current_key is not None:
        sections[current_key] = current_lines

    return {key: "\n".join(lines).strip() for key, lines in sections.items()}


def clip_text(text: str, limit: int = COMPACT_BODY_CHAR_LIMIT) -> str:
    normalized = text.strip()
    if len(normalized) <= limit:
        return normalized
    clipped = normalized[:limit].rstrip()
    if "\n" in clipped:
        clipped = clipped.rsplit("\n", 1)[0].rstrip() or clipped
    elif " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0].rstrip() or clipped
    return clipped + "\n\n..."


def compact_body(record: CompilableRecord) -> str:
    body = body_without_title(record.body, record.title)
    sections = markdown_sections(body)
    selected: list[str] = []

    for heading in COMPACT_SECTION_PRIORITY:
        if heading in sections:
            selected.append(sections[heading])
        if len("\n\n".join(selected)) >= COMPACT_BODY_CHAR_LIMIT:
            break

    if selected:
        return clip_text("\n\n".join(selected))

    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", body) if part.strip()]
    if paragraphs:
        return clip_text("\n\n".join(paragraphs[:2]))
    return "_No compact content extracted._"


# ── Corpus iteration ────────────────────────────────────────────────────


def iter_compilable_records(
    config: MemoryConfig,
    *,
    include_rel_paths: set[str] | None = None,
) -> tuple[list[CompilableRecord], dict[str, int]]:
    """Project every parsed record into ``CompilableRecord`` form."""
    parsed, stats = iter_parsed_records(config, include_rel_paths=include_rel_paths)
    records = [
        CompilableRecord(
            path=record.rel_path,
            metadata=record.metadata,
            body=record.body.strip(),
            title=first_heading(record.body),
        )
        for record in parsed
    ]
    archive_records = _iter_active_context_archives(config, include_rel_paths=include_rel_paths)
    if archive_records:
        records.extend(archive_records)
        stats["active_context_archives"] = len(archive_records)
    return records, stats


def _iter_active_context_archives(
    config: MemoryConfig,
    *,
    include_rel_paths: set[str] | None = None,
) -> list[CompilableRecord]:
    """Project archived user activeContext files as low-priority history.

    These archive files are plain Markdown, not raw records with Front
    Matter. They are still useful evidence for derived key documents, so we
    expose a deterministic, compact projection without making them a new
    truth source.
    """
    root = config.repo_root / "memory-bank" / "archive" / "activeContext"
    if not root.is_dir():
        return []

    out: list[CompilableRecord] = []
    try:
        candidates = sorted(path for path in root.glob("*/*.md") if path.is_file())
    except OSError:
        return []
    for path in candidates:
        try:
            rel_path = path.resolve().relative_to(config.repo_root).as_posix()
        except (OSError, ValueError):
            continue
        if include_rel_paths is not None and rel_path not in include_rel_paths:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            stat = path.stat()
        except OSError:
            continue
        compacted, _notes = _compact_warm_context(text)
        user = path.parent.name or "unknown"
        created = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
        archive_id = "archive_active_context_" + content_sha(rel_path + "\n" + text)[:16]
        metadata = {
            "schema_version": "2.0",
            "id": archive_id,
            "record_kind": "note",
            "scope": "personal",
            "status": "archived",
            "author": user,
            "created_at": created,
            "updated_at": created,
            "tags": ["archive_candidate"],
            "confidence": None,
            "source_refs": [rel_path],
            "task_id": None,
            "branch": None,
            "memory_tier": "cold",
            "cognitive_level": "shu",
            "system_area": "memory",
        }
        body = (
            f"# Archived Active Context ({user})\n\n"
            f"> source: `{rel_path}`\n\n"
            f"{body_without_title(compacted, first_heading(compacted))}"
        ).strip()
        out.append(
            CompilableRecord(
                path=rel_path,
                metadata=metadata,
                body=body,
                title=first_heading(body),
            )
        )
    return out
