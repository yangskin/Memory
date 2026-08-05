"""Rebuildable key documents (P4-C — 无感最高原则落地).

Doctrine (README §0 / DesignDoc §2.0 / DEVLOG 2026-04-27):

* ``activeContext`` is a per-user derived view written to
  ``memory-bank/activeContext/{user}.md``. ``teamContext`` plus
  ``progress``, ``techContext`` and ``systemPatterns`` are project-shared
  derived views over promoted/shared records.
* Their canonical body is reconstructed from the raw corpus on demand;
  humans never edit them directly. Manual edits, when found, are
  preserved by archival (see ``archive_manual_edit``) but the rebuilt
  view always overwrites the in-place file so the next read is
  consistent with the raw substrate.
* Renderers degrade in three tiers: LLM (preferred) → embedding-based
  template → deterministic. Only the deterministic tier is implemented
  in this first slice; the public surface accepts the renderer name
  ahead of time so callers can opt-in once LLM/embedding tiers land.
* Every generated body carries a ``<!-- generated_by=memory-mcp ... -->``
  header on the first line. ``is_generated`` and ``parse_generated_meta``
  let other tools (compactor, snapshot review, governance) tell raw
  text apart from rebuilt text without re-parsing the body.

Public API:
    KEY_DOCUMENTS, KEY_DOCUMENT_KEYS
    build_generated_header / is_generated / parse_generated_meta
    select_records_for / render_deterministic_document
    rebuild_key_documents

This module deliberately bypasses ``memory_writer.memory_write`` because
derived views must be overwritten atomically by the renderer rather than
going through append-only shared-file policy. Per-user ``activeContext``
uses an explicit ``{user}`` path template here instead of relying on the
file writer's legacy redirection.
Safety primitives (``file_lock``, ``backup_files``, ``_atomic_write_text``,
``append_event``) are still reused so the rebuild path stays crash-safe
and auditable.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .memory_backup import backup_files
from .memory_config import MemoryConfig
from .memory_corpus import CompilableRecord, iter_compilable_records
from .memory_events import append_event, get_current_user
from .memory_guard_optimizer import guard_budget_for_path, optimize_text_for_guard
from .memory_locks import LockTimeoutError, file_lock
from .memory_record_io import DiskFullError, _atomic_write_text
from .memory_request_id import new_request_id
from .memory_result import error_result
from .memory_vector_search import vector_search


# ── Document specifications ──────────────────────────────────────────────


KEY_DOCUMENTS: dict[str, dict[str, Any]] = {
    "activeContext": {
        "rel_path": "memory-bank/activeContext/{user}.md",
        "title": "Active Context",
        "role": "current user's sprint focus, recent decisions, in-progress items",
        "include_kinds": ["note", "decision", "observation", "incident", "handoff"],
        "preferred_tags": ["high_value", "handoff_ready", "needs_validation"],
        "max_items": 30,
        "visibility": "user",
    },
    "teamContext": {
        "rel_path": "memory-bank/teamContext.md",
        "title": "Team Context",
        "role": "team-wide current focus, shared decisions, cross-user coordination",
        "include_kinds": ["note", "decision", "observation", "incident", "handoff"],
        "preferred_tags": ["high_value", "handoff_ready", "needs_validation", "mcp"],
        "max_items": 40,
        "visibility": "shared",
    },
    "progress": {
        "rel_path": "memory-bank/progress.md",
        "title": "Progress",
        "role": "feature completion status, milestones, completed deliverables",
        "include_kinds": ["note", "decision", "observation", "validation_result"],
        "preferred_tags": ["high_value", "build", "asset_pipeline", "validation"],
        "max_items": 60,
        "visibility": "shared",
    },
    "techContext": {
        "rel_path": "memory-bank/techContext.md",
        "title": "Tech Context",
        "role": "tech stack, plugin matrix, architecture configuration",
        "include_kinds": [
            "decision",
            "claim_candidate",
            "rule_candidate",
            "system_rule",
            "note",
        ],
        "preferred_tags": ["build", "asset_pipeline", "mcp"],
        "max_items": 60,
        "visibility": "shared",
    },
    "systemPatterns": {
        "rel_path": "memory-bank/systemPatterns.md",
        "title": "System Patterns",
        "role": "architecture patterns, coding conventions, design decisions",
        "include_kinds": [
            "decision",
            "rule_candidate",
            "system_rule",
            "claim_candidate",
            "procedure",
        ],
        "preferred_tags": ["workflow", "validation", "mcp"],
        "max_items": 60,
        "visibility": "shared",
    },
}

KEY_DOCUMENT_KEYS: tuple[str, ...] = tuple(KEY_DOCUMENTS.keys())

ARCHIVE_DIR_RELPATH = "memory-bank/archive/manual-edits"

_GENERATED_MARKER = "generated_by=memory-mcp"
_HEADER_RE = re.compile(
    r"^<!--\s*generated_by=memory-mcp\s+(?P<body>.+?)\s*-->\s*$"
)


# ── Header utilities ─────────────────────────────────────────────────────


def build_generated_header(
    *,
    renderer: str,
) -> str:
    return (
        f"<!-- generated_by=memory-mcp"
        f" renderer={renderer}"
        f" -->"
    )


def is_generated(text: str) -> bool:
    if not text:
        return False
    first = text.lstrip().splitlines()[0] if text.lstrip().splitlines() else ""
    return _GENERATED_MARKER in first and first.strip().startswith("<!--")


def parse_generated_meta(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    lines = text.lstrip().splitlines()
    if not lines:
        return None
    match = _HEADER_RE.match(lines[0].strip())
    if not match:
        return None
    body = match.group("body")
    out: dict[str, Any] = {}
    # Tokens are space-separated `key=value` pairs.
    for token in _split_header_tokens(body):
        if "=" not in token:
            continue
        key, raw = token.split("=", 1)
        out[key] = raw
    return out


def _split_header_tokens(body: str) -> list[str]:
    tokens: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in body:
        if ch == "[":
            depth += 1
            current.append(ch)
        elif ch == "]":
            depth = max(0, depth - 1)
            current.append(ch)
        elif ch.isspace() and depth == 0:
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(ch)
    if current:
        tokens.append("".join(current))
    return tokens


def _strip_generated_header_line(text: str) -> str:
    stripped = text.lstrip()
    if not stripped:
        return ""
    lines = stripped.splitlines()
    if lines and lines[0].strip().startswith("<!--") and _GENERATED_MARKER in lines[0]:
        return "\n".join(lines[1:]).lstrip("\n")
    return stripped


def _normalize_for_compare(text: str) -> str:
    lines = [line.rstrip() for line in text.strip().splitlines()]
    return "\n".join(lines).strip()


def _extract_summary_body(text: str) -> str:
    lines = _strip_generated_header_line(text).splitlines()
    idx = 0
    if idx < len(lines) and lines[idx].startswith("# "):
        idx += 1
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx < len(lines) and lines[idx].lstrip().startswith("> _"):
        idx += 1
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    return "\n".join(lines[idx:]).strip()


def _llm_append_threshold_chars(config: MemoryConfig, rel_path: str) -> int:
    budget = guard_budget_for_path(config, rel_path)
    if budget is not None and budget.max_chars is not None:
        return max(1200, int(budget.max_chars * 0.8))
    return 8_000


def _build_incremental_llm_delta(existing_text: str, rendered_text: str) -> str:
    existing_body = _extract_summary_body(existing_text)
    rendered_body = _extract_summary_body(rendered_text)
    if not rendered_body:
        return ""
    if rendered_body in existing_body:
        return ""

    existing_lines = {line.strip() for line in existing_body.splitlines() if line.strip()}
    delta_lines: list[str] = []
    for line in rendered_body.splitlines():
        clean = line.strip()
        if not clean:
            continue
        if clean in existing_lines:
            continue
        delta_lines.append(line.rstrip())
        if len("\n".join(delta_lines)) >= 2000:
            break

    if not delta_lines:
        return ""
    return "\n".join(delta_lines).strip()


def _append_llm_delta(existing_text: str, delta: str, generated_at: str) -> str:
    day = generated_at[:10] if len(generated_at) >= 10 else generated_at
    block = [
        "",
        f"## Incremental Update ({day})",
        "",
        delta.strip(),
        "",
    ]
    return existing_text.rstrip() + "\n" + "\n".join(block)


# ── Record selection ────────────────────────────────────────────────────


def _record_created_at(record: CompilableRecord) -> str:
    return str(record.metadata.get("created_at") or "")


def _record_kind(record: CompilableRecord) -> str:
    return str(record.metadata.get("record_kind") or record.metadata.get("kind") or "")


def _record_id(record: CompilableRecord) -> str:
    return str(record.metadata.get("id") or "")


def _record_tags(record: CompilableRecord) -> list[str]:
    raw = record.metadata.get("tags") or []
    if isinstance(raw, list):
        return [str(t) for t in raw]
    return []


def _record_scope(record: CompilableRecord) -> str:
    return str(record.metadata.get("scope") or "")


def _record_status(record: CompilableRecord) -> str:
    return str(record.metadata.get("status") or "")


def _record_author(record: CompilableRecord) -> str:
    return str(record.metadata.get("author") or "")


def _clean_heading_text(value: Any, *, fallback: str = "Untitled Record") -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip()).strip()
    text = text.strip("# `*_")
    return text[:140].rstrip() or fallback


def _record_display_heading(record: CompilableRecord) -> str:
    raw_title = _clean_heading_text(record.title, fallback="Untitled Record")
    rid = _record_id(record)
    if raw_title == "Untitled Record":
        kind = _record_kind(record) or "record"
        system_area = str(record.metadata.get("system_area") or "").strip()
        raw_title = f"{kind}: {system_area}" if system_area else kind
        raw_title = _clean_heading_text(raw_title, fallback="record")
    return f"{raw_title} [{rid}]" if rid else raw_title


def _strip_leading_record_title(body: str, raw_title: str) -> str:
    lines = body.strip().splitlines()
    if not lines:
        return ""
    first = lines[0].strip()
    if first.startswith("#") and first.lstrip("#").strip() == raw_title.strip():
        return "\n".join(lines[1:]).strip()
    return body.strip()


def _demote_body_headings(body: str) -> str:
    lines: list[str] = []
    for line in body.splitlines():
        match = re.match(r"^(?P<indent>\s{0,3})(?P<marks>#{1,6})(?P<space>\s+)(?P<title>.*)$", line)
        if not match:
            lines.append(line)
            continue
        level = len(match.group("marks"))
        new_level = min(6, max(3, level + 1))
        lines.append(
            f"{match.group('indent')}{'#' * new_level}{match.group('space')}{match.group('title')}"
        )
    return "\n".join(lines).strip()


def _append_record_section(
    lines: list[str],
    record: CompilableRecord,
    *,
    extra_meta_bits: Iterable[str] = (),
) -> None:
    raw_title = record.title or "Untitled Record"
    title = _record_display_heading(record)
    kind = _record_kind(record) or "record"
    rid = _record_id(record)
    created = _record_created_at(record)
    tags = _record_tags(record)
    meta_bits = [f"kind=`{kind}`"]
    if rid:
        meta_bits.append(f"id=`{rid}`")
    if created:
        meta_bits.append(f"created=`{created}`")
    if tags:
        meta_bits.append("tags=" + ",".join(f"`{t}`" for t in tags))
    meta_bits.extend(str(bit) for bit in extra_meta_bits if str(bit).strip())

    lines.append(f"## {title}")
    lines.append("")
    lines.append("> " + " · ".join(meta_bits))
    lines.append("")
    body = _strip_leading_record_title(record.body.strip(), raw_title)
    body = _demote_body_headings(body)
    if body:
        lines.append(body)
        lines.append("")


def _is_shared_sediment_record(record: CompilableRecord) -> bool:
    """Return True for records allowed into team-level key documents."""
    scope = _record_scope(record)
    status = _record_status(record)
    if scope in {"shared", "project_shared", "org_shared"}:
        return True
    return status == "published" and scope not in {"personal", "user_private", "session", "task_or_branch", "local", "archive"}


def _rel_path_for_doc(config: MemoryConfig, doc_key: str, user: str | None) -> str:
    spec = KEY_DOCUMENTS[doc_key]
    rel_path = str(spec["rel_path"])
    if "{user}" not in rel_path:
        return rel_path
    resolved_user = str(user or get_current_user(config.repo_root) or "").strip()
    if not resolved_user or resolved_user == "unknown":
        raise ValueError(f"{doc_key} rebuild requires a stable user id")
    return rel_path.replace("{user}", resolved_user)


def select_records_for(
    config: MemoryConfig,
    *,
    doc_key: str,
    user: str | None,
) -> list[CompilableRecord]:
    spec = KEY_DOCUMENTS[doc_key]
    include_kinds = set(spec.get("include_kinds") or [])
    preferred_tags = set(spec.get("preferred_tags") or [])
    max_items = int(spec.get("max_items") or 60)

    records, _ = iter_compilable_records(config)
    selected: list[CompilableRecord] = []
    for rec in records:
        kind = _record_kind(rec)
        if include_kinds and kind not in include_kinds:
            continue
        visibility = str(spec.get("visibility") or "shared")
        if visibility == "user":
            if not user or _record_author(rec) != user:
                continue
            rec_user_match = True
        else:
            if not _is_shared_sediment_record(rec):
                continue
            rec_user_match = True

        score = 0
        tags = set(_record_tags(rec))
        if preferred_tags & tags:
            score += 10
        if rec_user_match:
            score += 5
        # newer first
        rec_sort = (-score, _record_created_at(rec))
        selected.append((rec_sort, rec))  # type: ignore[arg-type]

    selected.sort(key=lambda pair: pair[0], reverse=True)
    return [rec for _, rec in selected[:max_items]]


# ── Renderer ────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def render_deterministic_document(
    config: MemoryConfig,
    *,
    doc_key: str,
    user: str | None,
    generated_at: str | None = None,
) -> str:
    if doc_key not in KEY_DOCUMENTS:
        raise KeyError(doc_key)
    spec = KEY_DOCUMENTS[doc_key]
    records = select_records_for(config, doc_key=doc_key, user=user)
    header = build_generated_header(
        renderer="deterministic",
    )

    lines: list[str] = [header, "", f"# {spec['title']}", ""]
    role = spec.get("role")
    if role:
        lines.append(f"> _{role}_")
        lines.append("")

    if not records:
        lines.append("_No raw records currently match this view (corpus is empty)._")
        lines.append("")
        return "\n".join(lines) + "\n"

    for rec in records:
        _append_record_section(lines, rec)

    return "\n".join(lines).rstrip() + "\n"


# ── P5 Phase 2b — embedding-tier renderer (DesignDoc §15.4) ────────────#
# ⚠️  Strictly opt-in: requires `renderer="embedding"` AND
# `embeddings.enabled=True`.  The underlying vector tier is FROZEN at
# v0.11.1 (DesignDoc §15.5); see ``memory_vector_search`` for rationale.
# This renderer is kept wired so existing tests / opt-in callers still
# work, but it is not part of the default key_documents prefer order.

class _EmbeddingRendererError(RuntimeError):
    """Raised when the embedding tier cannot produce a useful ranking.

    The orchestrator catches this and falls through to the next renderer
    in ``key_documents_prefer_order``; embedding is *strictly best-effort*.
    """


def _embedding_query_for(spec: dict[str, Any]) -> str:
    """Build a stable natural-language query from the doc spec.

    Used only as the vector tier's input — the rendered body still comes
    from the same template as the deterministic renderer so output is
    deterministic given the (records, ordering) tuple.
    """

    parts = [str(spec.get("title") or ""), str(spec.get("role") or "")]
    tags = spec.get("preferred_tags") or []
    if isinstance(tags, list) and tags:
        parts.append(" ".join(str(t) for t in tags))
    kinds = spec.get("include_kinds") or []
    if isinstance(kinds, list) and kinds:
        parts.append(" ".join(str(k) for k in kinds))
    query = " \u00b7 ".join(p.strip() for p in parts if str(p).strip())
    return query.strip()


def _rerank_by_vector(
    config: MemoryConfig,
    *,
    records: list[CompilableRecord],
    query: str,
    top_k: int,
) -> tuple[list[CompilableRecord], dict[str, float]]:
    """Reorder ``records`` so semantically-strong matches come first.

    Records absent from the vector hit list keep their original relative
    order behind the boosted ones (stable partition).  Returns the new
    record list plus the ``{record_id: score}`` map for diagnostics.
    """

    if not query:
        return records, {}

    result = vector_search(config, query, top_k=max(top_k, len(records)))
    if not result.get("ok"):
        raise _EmbeddingRendererError(
            f"vector_search failed: {result.get('error')}"
        )

    candidate_ids = {_record_id(r): r for r in records if _record_id(r)}
    if not candidate_ids:
        return records, {}

    best: dict[str, float] = {}
    for hit in result.get("hits", []):
        rid = str(hit.get("record_id", ""))
        if rid not in candidate_ids:
            continue
        score = float(hit.get("score", 0.0))
        if score > best.get(rid, 0.0):
            best[rid] = score

    if not best:
        # No hit overlapped the candidate set — treat as a soft failure so
        # the orchestrator can fall through to the deterministic tier.
        raise _EmbeddingRendererError("no vector hits intersected the candidate set")

    promoted_ids = sorted(best, key=lambda rid: best[rid], reverse=True)
    promoted_ids = promoted_ids[:top_k]
    promoted_set = set(promoted_ids)
    promoted = [candidate_ids[rid] for rid in promoted_ids]
    remaining = [r for r in records if _record_id(r) not in promoted_set]
    return promoted + remaining, best


def render_embedding_document(
    config: MemoryConfig,
    *,
    doc_key: str,
    user: str | None,
    generated_at: str | None = None,
) -> str:
    """Embedding-tier rebuild — semantic re-ranking on top of the template.

    Falls back by raising ``_EmbeddingRendererError`` when the vector
    index is missing/disabled/empty.  The orchestrator catches the
    resulting ``render_failed`` error and tries the next tier (typically
    deterministic) so callers always get *some* output.
    """

    if doc_key not in KEY_DOCUMENTS:
        raise KeyError(doc_key)
    if not getattr(config, "embeddings_enabled", False):
        raise _EmbeddingRendererError("embeddings_enabled=false in config")

    spec = KEY_DOCUMENTS[doc_key]
    candidates = select_records_for(config, doc_key=doc_key, user=user)
    if not candidates:
        # Same empty-corpus fallback as the deterministic renderer — emit
        # a valid generated document instead of erroring out.
        return render_deterministic_document(
            config, doc_key=doc_key, user=user, generated_at=generated_at
        )

    query = _embedding_query_for(spec)
    max_items = int(spec.get("max_items") or 60)
    reordered, _ = _rerank_by_vector(
        config, records=candidates, query=query, top_k=max_items
    )

    header = build_generated_header(
        renderer="embedding",
    )

    lines: list[str] = [header, "", f"# {spec['title']}", ""]
    role = spec.get("role")
    if role:
        lines.append(f"> _{role}_")
        lines.append("")

    for rec in reordered:
        _append_record_section(lines, rec)

    return "\n".join(lines).rstrip() + "\n"


# ── Manual-edit archival + rebuild orchestrator ──────────────────────────


def _archive_manual_edit(
    config: MemoryConfig,
    rel_path: str,
    current_text: str,
    *,
    timestamp: str,
) -> str:
    archive_dir = config.repo_root / ARCHIVE_DIR_RELPATH
    archive_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(rel_path).stem
    safe_ts = timestamp.replace(":", "").replace("-", "").replace("+", "").replace(".", "")
    archive_path = archive_dir / f"{stem}-{safe_ts}.md"
    # ensure unique
    counter = 0
    while archive_path.exists():
        counter += 1
        archive_path = archive_dir / f"{stem}-{safe_ts}-{counter}.md"
    notice = (
        f"<!-- archived manual-edit of {rel_path} at {timestamp} by memory-mcp rebuild -->\n"
    )
    archive_path.write_text(notice + current_text, encoding="utf-8")
    return str(archive_path.relative_to(config.repo_root).as_posix())


def render_llm_document(
    config: MemoryConfig,
    *,
    doc_key: str,
    user: str | None,
    llm_client: Any,
    generated_at: str | None = None,
    existing_document: str | None = None,
) -> str:
    """LLM-backed renderer: produces the same scaffold as the deterministic
    tier (header → title → role) but the body is a faithful, concise summary
    drafted by the LLM over the selected raw records.

    Schema invariants are preserved by deterministic code:
    - The ``<!-- generated_by=memory-mcp ... -->`` header always comes from
      :func:`build_generated_header` (LLM never writes it).
    - Title and role lines are stamped by us, not the LLM.
    - The LLM only fills the body region after the role blockquote.

    Raises ``KeyError`` for unknown ``doc_key``. Any LLM-side failure
    (config / network / empty output) propagates as ``LLMError`` so the
    orchestrator can fall back to the deterministic tier.
    """
    if doc_key not in KEY_DOCUMENTS:
        raise KeyError(doc_key)
    spec = KEY_DOCUMENTS[doc_key]
    records = select_records_for(config, doc_key=doc_key, user=user)

    # Local imports keep memory_llm optional at module load time.
    from .memory_llm import (
        DEFAULT_DISTILL_SYSTEM_PROMPT,
        make_raw_record,
    )
    from .memory_llm_pipeline import map_reduce_distill

    raw_dicts: list[dict[str, Any]] = []
    for rec in records:
        rid = _record_id(rec) or f"anon::{len(raw_dicts)}"
        body = rec.body.strip() or rec.title or ""
        if not body:
            continue
        raw_dicts.append(
            make_raw_record(
                record_id=rid,
                content=body,
                source=rec.path or "memory_key_documents",
                captured_at=_record_created_at(rec) or _now_iso(),
                author=str(rec.metadata.get("author") or "system"),
                extra_meta={
                    "record_kind": _record_kind(rec),
                    "tags": _record_tags(rec),
                    "title": rec.title,
                },
            )
        )
    title = spec["title"]
    role = spec.get("role") or ""
    header = build_generated_header(
        renderer="llm",
    )

    if not raw_dicts:
        # No corpus → produce the same "empty" scaffold as deterministic
        # so the schema stays consistent and we don't fabricate content.
        lines = [header, "", f"# {title}", ""]
        if role:
            lines.append(f"> _{role}_")
            lines.append("")
        lines.append("_No raw records currently match this view (corpus is empty)._")
        return "\n".join(lines) + "\n"

    existing_body = _extract_summary_body(existing_document or "")

    sys_prompt = (
        f"{DEFAULT_DISTILL_SYSTEM_PROMPT}\n\n"
        f"Compose the body of the project's '{title}' document. "
        f"Role of this document: {role}. "
        "Output GitHub-flavored Markdown only. Do not write a top-level title "
        "(no `# {title}` line) — only sub-headings (## …) and prose. "
        "Stay strictly grounded in the raw records above; do not invent facts, "
        "deadlines, owners, file paths, or status. Prefer concise sectioned bullets. "
        "Preserve stable wording when information has not materially changed; avoid "
        "cosmetic rewrites."
    )
    user_instruction = (
        f"Produce the body of '{title}'. "
        "Group related raw records under short ## sub-headings. "
        "If a record contradicts another, surface both rather than picking one."
    )
    if existing_body:
        existing_excerpt = existing_body[:3000]
        user_instruction += (
            "\n\nCurrent document body (for stability reference):\n"
            f"{existing_excerpt}\n\n"
            "If no material update is required, keep structure and wording highly stable."
        )

    distilled = map_reduce_distill(
        llm_client,
        raw_dicts,
        record_id=f"key_documents::{doc_key}",
        distilled_at=generated_at or _now_iso(),
        system_prompt=sys_prompt,
        user_instruction=user_instruction,
        kind="summary",
    )
    body = str(distilled.get("content") or "").strip()
    if not body:
        from .memory_llm import LLMRequestError
        raise LLMRequestError(f"LLM returned empty body for key document {doc_key!r}")

    lines = [header, "", f"# {title}", ""]
    if role:
        lines.append(f"> _{role}_")
        lines.append("")
    lines.append(body)
    return "\n".join(lines).rstrip() + "\n"


# ----------------------------------------------------------------------
# Tier dispatch (D2 slim-down: unify the 3-tier renderer plumbing)
#
# Each tier-invoker has the same signature
#     (config, doc_key, user, generated_at, rel_path)
#         -> (text | None, error_dict | None)
# and exactly one of the two return slots is non-None. ``_rebuild_one``
# picks the invoker via ``_TIER_INVOKERS`` and then runs the shared
# backup / atomic-write / event path -- no per-tier branches downstream.
# ----------------------------------------------------------------------


def _invoke_deterministic_tier(
    config: MemoryConfig,
    doc_key: str,
    user: str | None,
    generated_at: str,
    rel_path: str,
) -> tuple[str | None, dict[str, Any] | None]:
    text = render_deterministic_document(
        config, doc_key=doc_key, user=user, generated_at=generated_at
    )
    return text, None


def _invoke_embedding_tier(
    config: MemoryConfig,
    doc_key: str,
    user: str | None,
    generated_at: str,
    rel_path: str,
) -> tuple[str | None, dict[str, Any] | None]:
    text = render_embedding_document(
        config, doc_key=doc_key, user=user, generated_at=generated_at
    )
    return text, None


def _invoke_llm_tier(
    config: MemoryConfig,
    doc_key: str,
    user: str | None,
    generated_at: str,
    rel_path: str,
) -> tuple[str | None, dict[str, Any] | None]:
    """LLM tier -- routed through the unified 7-status capability runner.

    DesignDoc 15.2-A: disabled / unavailable / timeout / budget / failed
    paths share the envelope used by distill / summarize / rewrite.
    ``force_enabled`` keeps the legacy contract that a user with an LLM
    client configured can rebuild even without flipping the capability
    flag in ``llm_defaults``.
    """
    from .memory_llm_runner import (
        STATUS_BUDGET,
        STATUS_DISABLED,
        STATUS_FAILED,
        STATUS_TIMEOUT,
        STATUS_UNAVAILABLE,
        run_llm_capability,
    )

    captured: dict[str, Any] = {}

    def _client_factory(_profile):
        # Honour ``_maybe_build_llm_client`` so monkey-patching tests keep working.
        client, err = _maybe_build_llm_client()
        if client is None:
            from .memory_llm import LLMConfigError

            raise LLMConfigError(
                (err or {}).get("message") or "LLM client unavailable"
            )
        return client

    existing_document: str | None = None
    target = (config.repo_root / rel_path).resolve()
    if target.exists() and target.is_file():
        try:
            existing_document = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            existing_document = None

    def _invoke(client, _profile):
        captured["text"] = render_llm_document(
            config,
            doc_key=doc_key,
            user=user,
            llm_client=client,
            generated_at=generated_at,
            existing_document=existing_document,
        )
        return captured["text"]

    envelope = run_llm_capability(
        config,
        "rebuild_key_document",
        _invoke,
        client_factory=_client_factory,
        force_enabled=True,
    )
    if not envelope.ok:
        status_to_code = {
            STATUS_DISABLED: "llm_disabled",
            STATUS_UNAVAILABLE: "llm_unavailable",
            STATUS_TIMEOUT: "llm_timeout",
            STATUS_BUDGET: "llm_budget_exceeded",
            STATUS_FAILED: "render_failed",
        }
        code = status_to_code.get(envelope.status, "render_failed")
        err = error_result(
            code,
            envelope.error or f"LLM tier failed for {doc_key!r}",
            path=rel_path,
            tier="llm",
        )
        err["envelope"] = envelope.to_dict()
        return None, err
    return captured.get("text") or str(envelope.value or ""), None


_TIER_INVOKERS: dict[str, Any] = {
    "deterministic": _invoke_deterministic_tier,
    "embedding": _invoke_embedding_tier,
    "llm": _invoke_llm_tier,
}


def _rebuild_one(
    config: MemoryConfig,
    *,
    doc_key: str,
    user: str | None,
    request_id: str,
    tier: str = "deterministic",
    guard_prefer_llm: bool = True,
) -> dict[str, Any]:
    spec = KEY_DOCUMENTS[doc_key]
    try:
        rel_path = _rel_path_for_doc(config, doc_key, user)
    except ValueError as exc:
        return error_result("user_not_configured", str(exc), doc_key=doc_key)
    target = (config.repo_root / rel_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    generated_at = _now_iso()
    invoker = _TIER_INVOKERS.get(tier, _invoke_deterministic_tier)
    try:
        rendered, dispatch_err = invoker(config, doc_key, user, generated_at, rel_path)
    except Exception as exc:
        return error_result(
            "render_failed",
            f"{tier} renderer failed for {doc_key!r}: {type(exc).__name__}: {exc}",
            path=rel_path,
            tier=tier,
        )
    if dispatch_err is not None:
        return dispatch_err
    if rendered is None:
        return error_result(
            "render_failed",
            f"{tier} renderer returned no content for {doc_key!r}",
            path=rel_path,
            tier=tier,
        )
    renderer_used = tier
    guard_optimization = {
        "optimized": False,
        "method": "none",
        "notes": [],
        "before": {},
        "after": {},
    }

    archived_to: str | None = None
    try:
        with file_lock(config.repo_root, target):
            existing: str | None = None
            if target.exists() and target.is_file():
                try:
                    existing = target.read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    return error_result(
                        "read_failed",
                        f"failed to read existing {rel_path}: {exc}",
                        path=rel_path,
                    )
                if existing is not None and is_generated(existing):
                    existing_cmp = _normalize_for_compare(_strip_generated_header_line(existing))
                    rendered_cmp = _normalize_for_compare(_strip_generated_header_line(rendered))
                    existing_header = existing.lstrip().splitlines()[0].strip()
                    rendered_header = rendered.lstrip().splitlines()[0].strip()
                    if existing_cmp == rendered_cmp and existing_header == rendered_header:
                        append_event(
                            config,
                            event_type="key_document_rebuild_skipped",
                            payload={
                                "doc_key": doc_key,
                                "path": rel_path,
                                "renderer": renderer_used,
                                "reason": "no_content_change",
                                "generated_at": generated_at,
                                "request_id": request_id,
                                "user": get_current_user(config.repo_root),
                                "for_user": user,
                            },
                        )
                        return {
                            "ok": True,
                            "doc_key": doc_key,
                            "path": rel_path,
                            "renderer": renderer_used,
                            "generated_at": generated_at,
                            "skipped": True,
                            "skip_reason": "no_content_change",
                            "archived_manual_edit_to": None,
                            "guard_optimization": guard_optimization,
                        }

                    if renderer_used == "llm" and len(existing) < _llm_append_threshold_chars(config, rel_path):
                        delta = _build_incremental_llm_delta(existing, rendered)
                        if delta:
                            rendered = _append_llm_delta(existing, delta, generated_at)

                if existing.strip() and not is_generated(existing):
                    archived_to = _archive_manual_edit(
                        config, rel_path, existing, timestamp=generated_at
                    )

                # routine pre-write backup so we can roll back the rebuild
                backup_files(
                    config,
                    [rel_path],
                    reason=f"key_documents.rebuild {doc_key} ({renderer_used})",
                    tag="pre_rebuild",
                    event_type="memory_backup",
                    write_event=True,
                )

            rendered, guard_optimization = optimize_text_for_guard(
                config,
                rel_path=rel_path,
                text=rendered,
                prefer_llm=guard_prefer_llm,
            )
            try:
                _atomic_write_text(
                    target, rendered, fsync_strict=config.mcp_fsync_strict
                )
            except DiskFullError as exc:
                return error_result(
                    "disk_full",
                    f"out of disk space writing {rel_path}: {exc}",
                    errno=exc.errno,
                    path=rel_path,
                )
            except OSError as exc:
                return error_result(
                    "write_failed",
                    f"failed to write {rel_path}: {exc}",
                    path=rel_path,
                )
    except LockTimeoutError as exc:
        return error_result(
            "lock_timeout",
            f"could not acquire write lock for {rel_path}: {exc}",
            path=rel_path,
        )

    append_event(
        config,
        event_type="key_document_rebuilt",
        payload={
            "doc_key": doc_key,
            "path": rel_path,
            "renderer": renderer_used,
            "generated_at": generated_at,
            "archived_manual_edit_to": archived_to,
            "request_id": request_id,
            "user": get_current_user(config.repo_root),
            "for_user": user,
            "guard_optimization": guard_optimization if guard_optimization.get("optimized") else None,
        },
    )

    return {
        "ok": True,
        "doc_key": doc_key,
        "path": rel_path,
        "renderer": renderer_used,
        "generated_at": generated_at,
        "archived_manual_edit_to": archived_to,
        "guard_optimization": guard_optimization,
    }


def rebuild_key_documents(
    config: MemoryConfig,
    *,
    targets: list[str] | None = None,
    user: str | None = None,
    renderer: str = "auto",
    request_id: str | None = None,
    guard_prefer_llm: bool = True,
) -> dict[str, Any]:
    """Rebuild one or more key documents from raw records.

    Args:
        config: Active MemoryConfig.
        targets: Subset of ``KEY_DOCUMENT_KEYS``. ``None`` = rebuild all.
        user: User id for per-user ``activeContext``. Shared documents ignore
            private/session records and only use promoted/shared records.
        renderer: Renderer selection.
            - ``"auto"`` (default): walk ``config.key_documents_prefer_order``
              and use the first tier that succeeds (typically ``llm`` →
              ``deterministic``). Per-document errors fall back to the next
              tier transparently.
            - ``"deterministic"``: force the no-LLM template renderer.
            - ``"llm"``: force the LLM renderer; if the LLM client is
              unavailable the call returns ``error="llm_unavailable"``.
            - ``"embedding"``: RAG-backed renderer (P5 Phase 2b). Requires
              ``embeddings.enabled=true`` and a built vector index; the
              renderer reranks per-record candidates by chunk vector
                            similarity.
              Returns ``error="embeddings_disabled"`` when the gate is off.
                guard_prefer_llm: Prefer the LLM guard compactor when a generated
                        key document exceeds its guard budget. CLI / explicit rebuilds
                        keep this enabled by default; MCP auto-rebuild can disable it so
                        checkpoint writes do not wait on an LLM call.

    Returns:
        ``{ok, written: {doc_key: per_doc_result}, errors: {…}, mode,
        renderer, request_id}``. When ``key_documents.mode`` is
        ``"manual"`` or ``"disabled"`` the call returns
        ``error="key_documents_manual_mode"`` without touching disk.
    """
    rid = request_id or new_request_id()

    mode = getattr(config, "key_documents_mode", "auto")
    if mode != "auto":
        return {
            "ok": False,
            "error": "key_documents_manual_mode",
            "message": (
                f"key_documents.mode={mode!r}: rebuild is disabled. "
                "Set key_documents.mode='auto' in .ai-memory/config.json to enable."
            ),
            "mode": mode,
            "request_id": rid,
        }

    if renderer == "embedding":
        if not getattr(config, "embeddings_enabled", False):
            return {
                "ok": False,
                "error": "embeddings_disabled",
                "message": (
                    "renderer='embedding' requires embeddings.enabled=true "
                    "in .ai-memory/config.json and a built vector index "
                    "(see DesignDoc §15.4)."
                ),
                "request_id": rid,
            }
    if renderer not in {"deterministic", "auto", "llm", "embedding"}:
        return error_result(
            "invalid_input",
            f"renderer must be one of: auto, deterministic, llm, embedding (got {renderer!r})",
            request_id=rid,
        )

    if targets is None:
        chosen = list(KEY_DOCUMENT_KEYS)
    else:
        if not isinstance(targets, list) or not targets:
            return error_result("invalid_input", "targets must be a non-empty list", request_id=rid)
        unknown = [t for t in targets if t not in KEY_DOCUMENTS]
        if unknown:
            return error_result(
                "invalid_input",
                f"unknown targets: {sorted(unknown)}; valid keys: {list(KEY_DOCUMENT_KEYS)}",
                request_id=rid,
            )
        chosen = list(targets)

    # Compose per-doc renderer order
    if renderer == "deterministic":
        per_doc_order: tuple[str, ...] = ("deterministic",)
    elif renderer == "llm":
        per_doc_order = ("llm",)
    elif renderer == "embedding":
        per_doc_order = ("embedding", "deterministic")
    else:  # auto
        per_doc_order = tuple(
            r for r in getattr(config, "key_documents_prefer_order", ("llm", "deterministic"))
            if r in {"llm", "deterministic", "embedding"}
        ) or ("deterministic",)
        if "deterministic" not in per_doc_order:
            per_doc_order = per_doc_order + ("deterministic",)

    # Lazily build LLM client only when needed.
    # \u00a715.2-A: client construction now happens inside the runner per-call
    # via ``_maybe_build_llm_client``; we just probe once here so explicit
    # ``renderer="llm"`` requests can fail fast with a useful error when the
    # user has no LLM configured.
    llm_unavailable_err: dict[str, Any] | None = None
    if "llm" in per_doc_order:
        probe_client, probe_err = _maybe_build_llm_client()
        if probe_client is None:
            llm_unavailable_err = probe_err or error_result(
                "llm_unavailable", "LLM client unavailable"
            )

    written: dict[str, dict[str, Any]] = {}
    errors: dict[str, dict[str, Any]] = {}
    for doc_key in chosen:
        last_error: dict[str, Any] | None = None
        for tier in per_doc_order:
            if tier == "llm" and llm_unavailable_err is not None:
                # explicit llm-only request \u2192 surface error; auto-mode falls through
                if renderer == "llm":
                    last_error = llm_unavailable_err
                    break
                continue
            outcome = _rebuild_one(
                config,
                doc_key=doc_key,
                user=user,
                request_id=rid,
                tier=tier,
                guard_prefer_llm=guard_prefer_llm,
            )
            if outcome.get("ok"):
                last_error = None
                written[doc_key] = outcome
                break
            last_error = outcome
            # tier failed; try next tier (if any)
        if doc_key not in written and last_error is not None:
            errors[doc_key] = last_error

    return {
        "ok": not errors,
        "written": written,
        "errors": errors,
        "mode": mode,
        "renderer": renderer,
        "renderer_order": list(per_doc_order),
        "request_id": rid,
    }


def _maybe_build_llm_client() -> tuple[Any, dict[str, Any] | None]:
    """Best-effort LLMClient construction; returns (client_or_None, err_or_None)."""
    try:
        from .memory_llm import LLMClient, LLMConfigError  # local import — optional dep
    except Exception as exc:  # pragma: no cover — defensive
        return None, error_result("llm_unavailable", f"memory_llm import failed: {exc}")
    try:
        return LLMClient(), None
    except LLMConfigError as exc:
        return None, error_result("llm_unavailable", str(exc))
    except Exception as exc:  # pragma: no cover — defensive
        return None, error_result("llm_unavailable", f"failed to build LLMClient: {exc}")
