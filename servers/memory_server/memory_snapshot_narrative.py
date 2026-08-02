"""LLM-generated executive summary for weekly / monthly snapshots (v0.10.0).

Snapshot bodies are still produced deterministically by
:func:`memory_compile_views.compile_snapshot_target`: that file IS the source
of truth.  This module adds a short *narrative* paragraph in front of the
deterministic body so a human (or an agent) can skim the snapshot in seconds
without giving up the auditable bullet list below.

Hard contracts (mirroring §12 / §15.3):

- **Additive only.**  We never modify, drop, or reorder the deterministic
  body — :func:`inject_narrative` just inserts a fenced section after the
  ``# Title`` line.  Re-rendering without the LLM tier produces an
  identical body to v0.9.x.
- **Read-only on records.**  The snapshot records themselves are not
  mutated; the narrative ends up in the compiled markdown only.
- **Fail-soft.**  When the LLM is unavailable / disabled, the snapshot is
  written deterministically with a ``narrative_status`` flag so callers
  can decide whether to retry later (e.g. via a scheduled cron).
- **Cacheable.**  Identical record set + model + window → identical cache
  key, so re-running a snapshot rebuild is free.

Designed to be invoked exclusively through
:func:`memory_llm_runner.run_llm_capability` with capability
``snapshot_narrative`` so the global timeout / budget / opt-in posture is
honoured uniformly.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Iterable

from .memory_llm import LLMClient, LLMConfigError, LLMRequestError, extract_text
from .memory_llm_pipeline import DistillCache

logger = logging.getLogger(__name__)


SNAPSHOT_NARRATIVE_SYSTEM_PROMPT = (
    "You produce a concise executive summary for a memory snapshot. The "
    "snapshot already lists every record below; your job is to surface the "
    "highest-signal themes for a busy reviewer. Rules: (1) write 3-6 short "
    "bullet points in the source language, (2) start with the most "
    "consequential change, (3) reference record titles when useful, (4) NEVER "
    "invent facts that are not present in the input, (5) NEVER include the "
    "raw record bodies verbatim, (6) NEVER speculate about anything outside "
    "the supplied window. Output the bullet list directly with no preamble "
    "and no trailing prose."
)


# Narrative section title — kept simple so renderers can detect / strip it.
NARRATIVE_HEADING = "## Narrative (LLM)"


@dataclass
class SnapshotNarrativeResult:
    """Outcome of :func:`generate_snapshot_narrative`.

    ``narrative`` is the bullet body the model produced (already
    sanitised); ``injected_section`` is the markdown block ready to be
    spliced into the deterministic snapshot via :func:`inject_narrative`.
    """

    ok: bool
    narrative: str
    model: str
    cache_hit: bool = False
    raw_response: str | None = None
    error: str | None = None
    record_count: int = 0
    target: str = ""

    @property
    def injected_section(self) -> str:
        if not self.narrative.strip():
            return ""
        body = self.narrative.strip()
        return f"{NARRATIVE_HEADING}\n\n{body}\n"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": bool(self.ok),
            "narrative": self.narrative,
            "model": self.model,
            "cache_hit": bool(self.cache_hit),
            "record_count": int(self.record_count),
            "target": self.target,
        }
        if self.error:
            out["error"] = self.error
        return out


def _format_records_for_narrative(
    records: Iterable[dict[str, Any]],
    *,
    max_records: int = 30,
    max_chars_per_record: int = 800,
) -> str:
    parts: list[str] = []
    count = 0
    for rec in records:
        if count >= max_records:
            break
        if not isinstance(rec, dict):
            continue
        rid = str(rec.get("id") or rec.get("record_id") or "?")
        title = str(rec.get("title") or "")
        kind = str(rec.get("record_kind") or rec.get("kind") or "")
        body = str(rec.get("content") or rec.get("body") or rec.get("body_excerpt") or "")
        if max_chars_per_record > 0 and len(body) > max_chars_per_record:
            body = body[:max_chars_per_record] + "…"
        head = f"--- record id={rid}"
        if title:
            head += f" title={title!r}"
        if kind:
            head += f" kind={kind}"
        head += " ---"
        parts.append(f"{head}\n{body}".strip())
        count += 1
    return "\n\n".join(parts)


def _compute_cache_key(
    *,
    target: str,
    label: str,
    model: str,
    serialized_records: str,
) -> str:
    payload = json.dumps(
        {
            "target": target,
            "label": label,
            "model": model,
            "records": serialized_records,
            "_v": "v0.10.0",
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    return "snap-narr::" + hashlib.sha256(payload).hexdigest()


def generate_snapshot_narrative(
    client: LLMClient,
    records: list[dict[str, Any]],
    *,
    target: str,
    label: str,
    model: str | None = None,
    max_tokens: int | None = None,
    cache: DistillCache | None = None,
    max_records: int = 30,
    max_chars_per_record: int = 800,
) -> SnapshotNarrativeResult:
    """Generate an executive summary for a (weekly|monthly) snapshot.

    The snapshot's deterministic body is *not* required to call this
    function; we accept the raw record dicts the caller already collected
    (compiler hands them in pre-rendering).  ``target`` is the snapshot
    target name (e.g. ``"weekly_snapshot"``) and ``label`` is the window
    label (e.g. ``"2026-W18"``).
    """

    if not isinstance(records, list):
        records = list(records or [])
    record_count = sum(1 for r in records if isinstance(r, dict))
    effective_model = model or client.config.model
    if record_count == 0:
        # Nothing to summarise — return a no-op result so the writer can
        # mark the snapshot's narrative_status appropriately without an
        # error envelope (this is a normal "quiet week" outcome).
        return SnapshotNarrativeResult(
            ok=True,
            narrative="",
            model=effective_model,
            target=target,
            record_count=0,
        )

    serialized = _format_records_for_narrative(
        records,
        max_records=max_records,
        max_chars_per_record=max_chars_per_record,
    )
    cache = cache if cache is not None else DistillCache()
    cache_key = _compute_cache_key(
        target=target,
        label=label,
        model=effective_model,
        serialized_records=serialized,
    )
    cached = cache.get(cache_key)
    if cached is not None:
        return SnapshotNarrativeResult(
            ok=True,
            narrative=cached.strip(),
            model=effective_model,
            cache_hit=True,
            raw_response=cached,
            record_count=record_count,
            target=target,
        )

    user_msg = (
        f"Snapshot target: {target}\n"
        f"Window label: {label}\n"
        f"Record count: {record_count}\n\n"
        f"Records (truncated for budget):\n\n{serialized}"
    )
    try:
        response = client.chat(
            [
                {"role": "system", "content": SNAPSHOT_NARRATIVE_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            model=model,
            max_tokens=max_tokens,
        )
    except LLMConfigError as exc:
        return SnapshotNarrativeResult(
            ok=False,
            narrative="",
            model=effective_model,
            error=f"config: {exc}",
            record_count=record_count,
            target=target,
        )
    except LLMRequestError as exc:
        return SnapshotNarrativeResult(
            ok=False,
            narrative="",
            model=effective_model,
            error=f"request: {exc}",
            record_count=record_count,
            target=target,
        )

    text = extract_text(response).strip()
    if not text:
        return SnapshotNarrativeResult(
            ok=True,
            narrative="",
            model=effective_model,
            raw_response="",
            record_count=record_count,
            target=target,
        )
    cache.put(cache_key, text)
    return SnapshotNarrativeResult(
        ok=True,
        narrative=text,
        model=effective_model,
        cache_hit=False,
        raw_response=text,
        record_count=record_count,
        target=target,
    )


def inject_narrative(snapshot_markdown: str, narrative_section: str) -> str:
    """Insert ``narrative_section`` immediately after the snapshot title.

    - If the deterministic body already contains a ``## Narrative (LLM)``
      block, it is replaced (so re-running a rebuild stays idempotent).
    - If ``narrative_section`` is empty, returns the body unchanged.
    - The deterministic body is never mutated beyond inserting/replacing
      this one block.
    """

    if not narrative_section.strip():
        return snapshot_markdown
    body = snapshot_markdown
    # Replace any existing narrative block (idempotent re-rebuild).
    if NARRATIVE_HEADING in body:
        lines = body.splitlines(keepends=True)
        out: list[str] = []
        skipping = False
        for line in lines:
            stripped = line.strip()
            if not skipping and stripped == NARRATIVE_HEADING:
                skipping = True
                continue
            if skipping:
                # Stop skipping when we hit the next H2 / EOF.
                if stripped.startswith("## ") and stripped != NARRATIVE_HEADING:
                    skipping = False
                else:
                    continue
            out.append(line)
        body = "".join(out)

    lines = body.splitlines()
    insert_at = 0
    for idx, line in enumerate(lines):
        if line.startswith("# "):
            insert_at = idx + 1
            # Skip the blank line directly under the title if present.
            if insert_at < len(lines) and lines[insert_at].strip() == "":
                insert_at += 1
            break
    section_lines = ["", *narrative_section.rstrip().splitlines(), ""]
    new_lines = lines[:insert_at] + section_lines + lines[insert_at:]
    return "\n".join(new_lines).rstrip() + "\n"


__all__ = [
    "NARRATIVE_HEADING",
    "SNAPSHOT_NARRATIVE_SYSTEM_PROMPT",
    "SnapshotNarrativeResult",
    "generate_snapshot_narrative",
    "inject_narrative",
]
